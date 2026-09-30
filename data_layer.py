#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_layer.py — 数据层统一基建（2026-08-26 建立，2026-08-28 范围收敛）
=====================================================================
背景：stock_quant.py 数据源层曾存在三大架构问题（22 个模块依赖它）：
  1. stale 判定散落：fetch_main_flow / fetch_main_flow_sina / fetch_kline_sina
     各写一遍"数据日期 vs 今日"判定，且判定时机不一致（曾 09:30 vs 09:15 并存）
  2. 缓存机制分散：CACHE_TTL 文件缓存 + _MAIN_FLOW_CACHE 内存缓存 双轨并存，
     缓存键不统一、无版本、测试数据曾污染真实缓存（close=10.5 假数据）
  3. 降级链重复：fetch_main_flow 多源降级（push2his→新浪实时→新浪历史→push2）
     逻辑内联在各函数里，无法统一监控

本层提供（2026-08-28 范围收敛，只保留有生产消费方的组件）：
  CacheManager   统一缓存（文件 + 内存，TTL + 版本号 + 测试隔离）
                 —— stock_quant.cache_get/cache_set 委托实现
  validate_today 统一数据日期校验（"今日数据"判定，消除散落魔法时间）
                 —— fetch_main_flow / fetch_kline_push2his 消费

设计原则：
  - 独立可 import：本层不依赖 stock_quant，无循环依赖
  - 测试隔离：CacheManager 支持 env=TEST 时禁用文件落盘（防污染真实缓存）

已收敛移除（2026-08-28，零消费方死代码）：
  RateLimiter / DataSource / get_limiter —— 建立时设计为"限流+数据源抽象基类"，
  但限流最终落位在 stock_quant 手写机制（_random_ua/_jitter_delay/_em_cooldown_*），
  多源降级链最终收敛到 stock_quant._em_probe_all 统一轮询函数，本层基类从未被接上。
  半接线的抽象比没有更贵（见 2026-08-28 架构评估 P1-4）；若未来要做真正的
  数据源抽象，应以 _em_probe_all 的"多域名轮询+源级冷却"为底座重新设计。
"""
import hashlib
import json
import os
import pickle
import threading
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'data')

# 默认缓存 TTL（秒）：与 stock_quant 原 CACHE_TTL 对齐
CACHE_TTL = {'kline': 300, 'quote': 10, 'financial': 86400, 'sector': 300,
             'sector_close': 86400,  # P1-3 修复(2026-09-22体检)：此前未注册，fetch_sector_kline
             # 收盘后声明"86400长效缓存"实际走默认300s，5分钟即过期重拉，降限流目标落空
             'mainflow': 45, 'news': 600}


# ============================================================
# 统一缓存管理（文件 + 内存，TTL + 版本 + 测试隔离）
# ============================================================
class CacheManager:
    """统一缓存：文件缓存（跨进程）+ 内存缓存（进程内热数据）
    - key 规范化：md5(key + version) 作为文件名，避免非法字符
    - 测试隔离：env='TEST' 时禁用文件落盘（防测试 mock 数据污染真实缓存）
    - 版本号：数据结构变更时递增，自动失效旧缓存
    """

    def __init__(self, cache_dir=None, version=1, env=None):
        self.cache_dir = cache_dir or CACHE_DIR
        self.version = version
        self.env = env or os.environ.get('DATA_LAYER_ENV', 'prod')
        self._mem = {}          # key -> (value, ts)
        self._lock = threading.Lock()

    def _path(self, key):
        h = hashlib.md5(f'{key}:v{self.version}'.encode()).hexdigest()
        return os.path.join(self.cache_dir, f'{h}.cache')

    def get(self, key, category='kline', ttl=None):
        """读缓存：先内存，后文件；过期返回 None"""
        ttl = ttl or CACHE_TTL.get(category, 300)
        now = time.time()
        # 内存缓存
        with self._lock:
            hit = self._mem.get(key)
            if hit and now - hit[1] < ttl:
                return hit[0]
        # 文件缓存（仅 prod 环境）
        if self.env == 'TEST':
            return None
        path = self._path(key)
        try:
            if os.path.exists(path) and now - os.path.getmtime(path) < ttl:
                with open(path, 'rb') as f:
                    return pickle.load(f)
        except Exception:
            # P2-4 修复(2026-09-22体检)：坏文件（写一半/损坏 pickle）原样静默保留，
            # 每次读取都异常——读失败即删除，让下次 set 重建
            try:
                os.remove(path)
            except OSError:
                pass
        return None

    def set(self, key, value, category='kline'):
        """写缓存：内存 + 文件（TEST 环境仅内存，不落盘）"""
        with self._lock:
            self._mem[key] = (value, time.time())
        if self.env == 'TEST':
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self._path(key), 'wb') as f:
                pickle.dump(value, f)
        except Exception:
            pass

    def clear(self, key=None):
        """清缓存：key=None 清全部内存；指定 key 清内存+文件"""
        with self._lock:
            if key is None:
                self._mem.clear()
            else:
                self._mem.pop(key, None)
                try:
                    os.remove(self._path(key))
                except OSError:
                    pass

    def prune(self, max_age_days=7, max_files=5000):
        """P2-4 磁盘回收(2026-09-22体检)：.cache 文件只增不减（数千个混入 git 后已清理）。
        删除超过 max_age_days 的过期文件；若仍超 max_files 再按最旧先删。
        返回删除数量。daily_review --close 每日调用一次。"""
        import glob
        cutoff = time.time() - max_age_days * 86400
        try:
            files = glob.glob(os.path.join(self.cache_dir, '*.cache'))
        except OSError:
            return 0
        removed = 0
        # 按修改时间升序（最旧在前）
        entries = []
        for fp in files:
            try:
                entries.append((os.path.getmtime(fp), fp))
            except OSError:
                continue
        entries.sort()
        now = time.time()
        for mtime, fp in entries:
            if len(entries) - removed <= max_files and mtime >= cutoff:
                break  # 未过期且数量达标，停止
            try:
                os.remove(fp)
                removed += 1
            except OSError:
                continue
        return removed

    def file_count(self):
        """缓存文件数（健康监控用）"""
        try:
            return len([f for f in os.listdir(self.cache_dir) if f.endswith('.cache')])
        except OSError:
            return 0


# ============================================================
# 统一数据日期校验（"今日数据"判定，消除散落魔法时间）
# ============================================================
def validate_today(data_date, now=None, market_open='0915'):
    """统一数据日期校验：data_date 是否为"今日"数据
    规则（写死可证伪，2026-08-26 架构优化收敛）：
      - 竞价后（>= market_open，默认 09:15）要求 data_date == 今日
      - 盘前（< market_open）不强制（当日数据尚未生成属正常）
    返回 (is_today, is_stale, reason)
      is_today : 数据日期 == 今日
      is_stale : 竞价后拿到非今日数据 = stale（不可冒充当日）
      reason   : 说明文本
    """
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    hm = now.strftime('%H%M')
    is_today = bool(data_date and str(data_date)[:10] == today)
    if hm >= market_open:
        is_stale = not is_today
        reason = '当日' if is_today else f'stale: 数据日期{data_date}≠今日{today}（竞价后）'
    else:
        is_stale = False
        reason = f'盘前{hm}，不强制当日（数据日期{data_date}）'
    return is_today, is_stale, reason


# ============================================================
# 单例（进程内共享，避免重复初始化）
# ============================================================
_cache_manager = CacheManager()


def get_cache():
    return _cache_manager


def make_test_cache():
    """测试专用缓存（env=TEST，不落盘真实缓存）"""
    return CacheManager(env='TEST')


if __name__ == '__main__':
    # 自测
    cm = make_test_cache()
    cm.set('k1', {'a': 1}, 'quote')
    assert cm.get('k1', 'quote') == {'a': 1}, '内存缓存读写失败'
    cm.clear('k1')
    assert cm.get('k1', 'quote') is None, '清缓存失败'
    # 日期校验自测
    is_today, is_stale, reason = validate_today('2026-08-26',
                                                now=datetime(2026, 8, 26, 10, 0))
    assert is_today and not is_stale, f'当日判定失败: {reason}'
    is_today2, is_stale2, _ = validate_today('2026-08-25',
                                             now=datetime(2026, 8, 26, 10, 0))
    assert is_stale2, '竞价后旧数据应标 stale'
    is_today3, is_stale3, _ = validate_today('2026-08-25',
                                             now=datetime(2026, 8, 26, 8, 0))
    assert not is_stale3, '盘前旧数据不标 stale'
    print('✅ data_layer 自测通过（缓存/日期校验）')

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📊 A股十维量化分析系统 v2.0
功能：一键分析任意A股，覆盖 技术面+基本面+资金面+舆情+宏观+融资融券+龙虎榜+股东结构+主营构成+机构观点
用法：python3 stock_quant.py 600519
      python3 stock_quant.py 600036
      python3 stock_quant.py 贵州茅台
"""

import json, subprocess, sys, re, math, os, urllib.request, sqlite3, hashlib, time, pickle, concurrent.futures, logging, random
from datetime import datetime, timedelta
from collections import deque
from functools import lru_cache

# 架构优化（2026-08-26 P0）：统一数据源抽象层（缓存/限流/日期校验）
# data_layer 不依赖 stock_quant，无循环依赖；此处仅复用其统一缓存与日期校验
import data_layer as _dl
# 信号生命周期（2026-08-28 P0 闭环）：负期望信号自动停发/降权，record_signal 前置消费
try:
    import signal_lifecycle as _slc
except Exception:  # 生命周期层故障不阻断主流程，默认放行
    _slc = None

# 架构优化（2026-08-26 P0）：评分与圆桌分析层（机械拆分）
# calculate_quant_score / analyze_roundtable 已迁移至 scoring_layer.py，
# 此处 re-export 保持外部接口不变（22 个依赖方仍通过 stock_quant.xxx 调用）
from scoring_layer import calculate_quant_score, analyze_roundtable

# ============================================================
# 日志系统
# ============================================================
logging.basicConfig(level=logging.WARNING, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('stock_quant')
# 轮转文件备份（2026-08-25）：stderr 仍输出（兼容 cron `2>` 重定向到 *.err.log），
# 同时写 logs/stock_quant.log（10MB×3 轮转），避免 WARNING+ 日志无限增长。
# 注：*.err.log 的轮转建议在运行层（cron/logrotate）处理，代码层只保证结构化日志备份。
try:
    from logging.handlers import RotatingFileHandler
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(_log_dir, exist_ok=True)
    _fh = RotatingFileHandler(os.path.join(_log_dir, 'stock_quant.log'),
                              maxBytes=10 * 1024 * 1024, backupCount=3, encoding='utf-8')
    _fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    log.addHandler(_fh)
except Exception:
    pass

# ============================================================
# 全局配置
# ============================================================
CONFIG = {
    'CACHE_DIR': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'),
    'CACHE_TTL': {'kline': 300, 'quote': 10, 'financial': 86400, 'sector': 300},
    'TIMEOUT': 10,
    'MAX_WORKERS': 10,
    'ZHULONG': {
        'HG_THRESHOLD': 5,      # 强势信号HG阈值
        'ABC3_THRESHOLD': 5,    # 主力强度阈值
        'A1_EMA_FAST': 7,       # 多空线快EMA周期
        'A1_EMA_SLOW': 21,      # 多空线慢EMA周期
        'B1_ALPHA': 0.668,      # 平滑系数
    }
}

# ============================================================
# 本地数据缓存层
# ============================================================
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
CACHE_TTL = {'kline': 300, 'quote': 10, 'financial': 86400, 'sector': 300,
             'sector_close': 86400}  # 秒；sector_close=收盘后板块K线长效缓存（当日不再重拉）
_BPS_CACHE = {}  # BPS 内存缓存 {code: (value, timestamp)}，TTL 1天

# ============================================================
# HTTP 请求工具（带重试机制，应对东方财富限流）
# ============================================================
# ── 防限流：随机 UA 池（2026-08-10）──
# 东财限流是 IP 级行为识别，固定 UA('Mozilla/5.0') 是最易被识别的机器指纹。
# 维护真实浏览器 UA 池，每次请求随机取用，降低被风控判为爬虫的概率。
_UA_POOL = [
    # Chrome / Edge (Win / macOS / Linux)
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
    # Firefox
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0',
    'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0',
    # Safari
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
    # 国产浏览器 (360 / QQ / 搜狗，Chrome 内核)
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 360SE',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 360EE',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 QQBrowser/12.1',
    # 移动端
    'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
]

def _random_ua():
    """随机取一个真实浏览器 UA（防 IP 级 UA 指纹识别）"""
    return _UA_POOL[random.randrange(len(_UA_POOL))] if _UA_POOL else 'Mozilla/5.0'

def _jitter_delay(base):
    """请求间隔人形化：base 秒 ±50% 随机抖动
    行为分析识别的是固定间隔尖峰，随机化后变成噪声，触发阈值大幅降低
    """
    return base * (0.5 + random.random())


def http_get_json(url, headers=None, timeout=10, retries=3):
    """带重试的 JSON GET 请求（应对东方财富 Remote end closed connection 限流）
    重试策略：指数退避 1s → 2s → 4s（退避时间加 jitter 随机抖动，避免固定间隔被行为识别）
    """
    import urllib.request
    import urllib.error
    import time as _time
    h = {'User-Agent': _random_ua()}
    if headers:
        h.update(headers)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            resp = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(resp.read())
        except (urllib.error.URLError, ConnectionError, OSError):
            if attempt < retries - 1:
                _time.sleep(_jitter_delay(2 ** attempt))  # 指数退避 + jitter
                continue
            raise
        except Exception:
            if attempt < retries - 1:
                _time.sleep(_jitter_delay(2 ** attempt))
                continue
            raise
    return {}

def http_get_raw(url, headers=None, timeout=8, retries=3):
    """带重试的原始 GET 请求（返回字符串）"""
    import urllib.request
    import urllib.error
    import time as _time
    h = {'User-Agent': _random_ua()}
    if headers:
        h.update(headers)
    delay = 0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.read()
        except (urllib.error.URLError, ConnectionError, OSError):
            if attempt < retries - 1:
                _time.sleep(_jitter_delay(delay))
                delay += 1
                continue
            raise
    return b''

# ============================================================
# 信号追踪数据库（SQLite）
# ============================================================
SIGNAL_DB = os.path.join(CACHE_DIR, 'signals.db')

def _init_signal_db():
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(SIGNAL_DB)
    conn.execute('''CREATE TABLE IF NOT EXISTS signals
        (id INTEGER PRIMARY KEY AUTOINCREMENT,
         date TEXT, code TEXT, name TEXT, signal_type TEXT,
         price REAL, confidence REAL, status TEXT DEFAULT 'open',
         exit_price REAL, exit_date TEXT, return_pct REAL,
         strategy TEXT, hold_days INTEGER DEFAULT 5)''')
    # 兼容旧库：若无 hold_days 列则补上
    try:
        conn.execute("ALTER TABLE signals ADD COLUMN hold_days INTEGER DEFAULT 5")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def record_signal(code, name, signal_type, price, confidence=0, strategy='主升擒龙', hold_days=5):
    """记录信号到数据库（同日同代码同信号类型去重）
    生命周期闭环（2026-08-28）：record 前先过 signal_lifecycle.check——
    已裁决"停发"的负期望信号不再入库；"降权"信号置信度乘以乘数。
    """
    # 前置裁决：停发信号直接拦截（不入库即不参与后续胜率/回测统计）
    if _slc is not None:
        try:
            enabled, mult = _slc.check(strategy)
            if not enabled:
                log.warning(f"信号生命周期拦截: {strategy} 已停发（负期望信号），{code} {signal_type} 不入库")
                return
            if mult < 1.0 and confidence:
                confidence = round(confidence * mult, 2)
        except Exception as e:
            log.warning(f"生命周期 check 失败（默认放行）: {e}")
    try:
        _init_signal_db()
        today = datetime.now().strftime('%Y-%m-%d')
        conn = sqlite3.connect(SIGNAL_DB)
        # 去重：同一天同一只股票同一个信号类型不重复记录
        cur = conn.execute("SELECT id FROM signals WHERE date=? AND code=? AND signal_type=? AND status='open'",
                          (today, code, signal_type))
        if cur.fetchone():
            conn.close()
            return
        conn.execute("INSERT INTO signals (date, code, name, signal_type, price, confidence, strategy, hold_days) VALUES (?,?,?,?,?,?,?,?)",
                     (today, code, name, signal_type, price, confidence, strategy, hold_days))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"record_signal 失败: {e}")

def fetch_trading_calendar(force_refresh=False):
    """获取 A 股交易日历（缓存在 data/trading_days.json，每月刷新一次）
    数据源：上证指数日线 K 线日期（新浪/腾讯），覆盖 ~2 年，足够判断任意开仓日后的交易日数。
    返回：{'trading_days': ['YYYY-MM-DD', ...]（升序）, 'updated': 'YYYY-MM-DD'}
    """
    cal_path = os.path.join(CACHE_DIR, 'trading_days.json')
    # 30 天内已刷新过则直接读缓存
    if not force_refresh and os.path.exists(cal_path):
        try:
            mtime = os.path.getmtime(cal_path)
            if time.time() - mtime < 30 * 86400:
                with open(cal_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
    # 从上证指数日线拿交易日（覆盖最近 ~2 年）
    try:
        kline = fetch_kline_sina('sh000001', 240, 500)
        if kline and len(kline) > 60:
            # ⚠️ 字段是 'day'（fetch_kline_sina 返回 {day, open, ...}），不是 'date'
            days = sorted(str(d.get('day', ''))[:10] for d in kline if d.get('day'))
            days = [d for d in days if d]
            cal = {'trading_days': days, 'updated': datetime.now().strftime('%Y-%m-%d')}
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cal_path, 'w', encoding='utf-8') as f:
                json.dump(cal, f, ensure_ascii=False)
            return cal
    except Exception as e:
        log.warning(f"fetch_trading_calendar 失败: {e}")
    # 失败时返回空，调用方降级到 calendar_days * 5/7
    return {'trading_days': [], 'updated': ''}


def count_trading_days(start_date, end_date, calendar=None):
    """统计 [start_date, end_date) 区间内的交易日数
    calendar: fetch_trading_calendar() 的返回，None 则实时取
    返回：int（交易日数），失败返回 -1 触发降级
    """
    if calendar is None:
        calendar = fetch_trading_calendar()
    days = calendar.get('trading_days', [])
    if not days:
        return -1
    try:
        # 区间内（不含 end_date 当天）且 > start_date 的交易日数
        return sum(1 for d in days if start_date < d < end_date)
    except Exception:
        return -1


def close_expired_signals():
    """自动平仓：持有天数到期或止损止盈触发，计算实际收益率
    平仓规则：
    1. 持有 hold_days 个交易日后自动平仓
    2. 期间若跌幅 > 8% 提前止损平仓
    3. 期间若涨幅 > 15% 提前止盈平仓
    交易日数：优先用 fetch_trading_calendar() 的真实交易日历，失败降级到 calendar_days * 5/7
    """
    try:
        _init_signal_db()
        conn = sqlite3.connect(SIGNAL_DB)
        today = datetime.now().strftime('%Y-%m-%d')
        # 查所有未平仓信号
        cur = conn.execute("SELECT id, code, date, price, hold_days FROM signals WHERE status='open'")
        open_signals = cur.fetchall()

        # 一次性拉交易日历，所有信号共用
        cal = fetch_trading_calendar()

        closed_count = 0
        for sig_id, code, sig_date, entry_price, hold_days in open_signals:
            try:
                d1 = datetime.strptime(sig_date, '%Y-%m-%d')
                d2 = datetime.now()
                # 优先用真实交易日历
                td = count_trading_days(sig_date, today, cal)
                if td >= 0:
                    trading_days = td
                else:
                    # 降级：自然日 * 5/7
                    calendar_days = (d2 - d1).days
                    trading_days = int(calendar_days * 5 / 7)
                hold_days = hold_days or 5

                # 获取当前价格
                q = fetch_quote_tencent(code)
                if not q or q.get('price', 0) <= 0:
                    continue
                # ⚠️ 平仓价日期校验（2026-08-12 修复）：盘前/停牌时腾讯返回昨日收盘价
                # （price==pre_close、volume/open 为 0），若直接平仓会用昨日价冒充当日价，
                # 污染 return_pct 与校准结论。此时跳过，等收盘后重跑平仓。
                if q.get('inactive'):
                    continue
                cur_price = q['price']
                ret_pct = (cur_price - entry_price) / entry_price * 100

                # 平仓判定
                should_close = False
                exit_reason = ''
                if trading_days >= hold_days:
                    should_close = True
                    exit_reason = '到期平仓'
                elif ret_pct <= -8:
                    should_close = True
                    exit_reason = '止损平仓'
                elif ret_pct >= 15:
                    should_close = True
                    exit_reason = '止盈平仓'

                if should_close:
                    conn.execute("UPDATE signals SET status='closed', exit_price=?, exit_date=?, return_pct=? WHERE id=?",
                                (cur_price, today, round(ret_pct, 2), sig_id))
                    closed_count += 1
                    log.info(f"信号平仓 {code} {exit_reason} 收益{ret_pct:+.2f}%")
            except Exception as e:
                log.warning(f"平仓信号 {sig_id} 失败: {e}")
                continue

        conn.commit()
        conn.close()
        return closed_count
    except Exception as e:
        log.warning(f"close_expired_signals 失败: {e}")
        return 0

def get_signal_stats():
    """获取信号胜率统计（含成本口径，2026-08-25 接 sim_trade 统一）
    主字段(win_rate/avg_return/pl_ratio/odds_net)为含成本口径（佣金/印花税/滑点/过户费），
    price_* 前缀字段为原价格口径对比。避免与 signal_tracker.report / compute_performance 口径割裂。
    返回：总体胜率/平均收益/盈亏比 + 按策略分组的结构化统计。
    """
    try:
        _init_signal_db()
        conn = sqlite3.connect(SIGNAL_DB)
        rows = conn.execute("""SELECT code, COALESCE(name,''), COALESCE(strategy, signal_type),
                               price, exit_price, return_pct
                               FROM signals WHERE status='closed'
                               AND return_pct IS NOT NULL""").fetchall()
        open_count = conn.execute("SELECT COUNT(*) FROM signals WHERE status='open'").fetchone()[0]
        conn.close()
        _zero_overall = {'total': 0, 'avg_return': 0, 'win_rate': 0, 'pl_ratio': 0,
                         'odds_net': 0, 'illusion': '', 'price_win_rate': 0,
                         'price_avg_return': 0, 'price_pl_ratio': 0, 'price_odds_net': 0}
        if not rows:
            return {'stats': [], 'open_count': open_count, 'overall': dict(_zero_overall)}
        # 含成本口径：用 SimAccount 重算每条信号收益（买入entry→卖出exit，1000股T+0模拟）
        try:
            from sim_trade import SimAccount
        except Exception:
            SimAccount = None

        def _cost_ret(code, name, entry, exit_):
            """含成本收益；失败返回 None（调用方回退价格口径）"""
            if SimAccount is None or not entry or entry <= 0 or not exit_:
                return None
            try:
                acc = SimAccount(init_capital=100000, trade_cost={'t0_mode': True})
                acc.new_day('2026-01-01')
                b = acc.execute(code, 'buy', float(entry), 1000, date_str='2026-01-01')
                if not b['filled']:
                    return None
                s = acc.execute(code, 'sell', float(exit_), 1000, date_str='2026-01-01')
                if not s['filled']:
                    return None
                buy_total = b['actual_price'] * 1000 + b['trade_cost']
                sell_net = s['actual_price'] * 1000 - s['trade_cost']
                return (sell_net - buy_total) / buy_total * 100
            except Exception:
                return None

        from collections import defaultdict
        groups = defaultdict(list)
        for code, name, strategy, price, exit_price, pct in rows:
            cret = _cost_ret(code, name, price, exit_price)
            ret = cret if cret is not None else (pct or 0)  # 含成本失败回退价格口径
            groups[strategy or '未分类'].append((ret, pct or 0))

        def _agg(items, idx):
            rs = [it[idx] for it in items]
            total = len(rs)
            wins = sum(1 for r in rs if r > 0)
            profit = sum(r for r in rs if r > 0)
            loss = sum(-r for r in rs if r <= 0)
            return {
                'total': total, 'win_count': wins,
                'win_rate': round(wins / total * 100, 1) if total else 0,
                'avg_return': round(sum(rs) / total, 2) if total else 0,
                'max_return': round(max(rs), 2) if rs else 0,
                'min_return': round(min(rs), 2) if rs else 0,
                'pl_ratio': round(profit / loss, 2) if loss > 0 else (round(profit, 2) if profit > 0 else 0),
                'odds_net': round(sum(rs), 2),
            }
        all_items = [it for g in groups.values() for it in g]
        overall = _agg(all_items, 0)  # 含成本口径为主字段
        price_o = _agg(all_items, 1)  # 价格口径对比
        overall['price_win_rate'] = price_o['win_rate']
        overall['price_avg_return'] = price_o['avg_return']
        overall['price_pl_ratio'] = price_o['pl_ratio']
        overall['price_odds_net'] = price_o['odds_net']
        # 胜率幻觉检测（基于含成本口径）
        illusion = ''
        if overall['total'] >= 5 and overall['win_rate'] >= 50 and overall['odds_net'] <= 0:
            illusion = ('⚠️ 胜率幻觉：胜率看似及格，但赔率加权净值为负——'
                        '错误集中于高赔率日（拐点/主升段），按信号操作实际亏损，跑输躺平。'
                        '须用赔率加权记分，而非胜率')
        overall['illusion'] = illusion
        stats_list = [{'strategy': s, **_agg(items, 0)} for s, items in sorted(groups.items())]
        return {'stats': stats_list, 'open_count': open_count, 'overall': overall}
    except Exception as e:
        log.warning(f"get_signal_stats 失败: {e}")
        return {'stats': [], 'open_count': 0, 'overall': {'total': 0, 'avg_return': 0, 'win_rate': 0, 'pl_ratio': 0, 'odds_net': 0, 'illusion': '', 'price_win_rate': 0, 'price_avg_return': 0, 'price_pl_ratio': 0, 'price_odds_net': 0}}

def calibrate_weights():
    """评分权重回测校准：基于 signals.db 历史已平仓信号诊断权重有效性
    1. 置信度↔收益相关系数（评分是否有预测力）
    2. 高置信 vs 低置信分组胜率/均收益对比（权重方向是否合理）
    3. 输出校准建议（样本 <10 时提示继续积累数据）
    """
    print(f"{'='*72}")
    print("  ⚖️ 评分权重回测校准")
    print(f"{'='*72}")
    rows = []
    try:
        _init_signal_db()
        conn = sqlite3.connect(SIGNAL_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT date, code, name, signal_type, confidence, price,
                               exit_price, exit_date, return_pct, hold_days
                               FROM signals WHERE status='closed' AND return_pct IS NOT NULL
                               ORDER BY date""").fetchall()
        conn.close()
    except Exception as e:
        log.warning(f"校准数据读取失败: {e}")
    if len(rows) < 10:
        print(f"  ⚠️ 已平仓信号仅 {len(rows)} 个（样本不足），校准结论可信度低")
        print(f"     建议：继续运行分析积累数据，至少 30 个已平仓信号后再校准")
        if not rows:
            print(f"     当前无已平仓信号，先运行: python3 stock_quant.py <股票名> 积累信号")
        print(f"{'='*72}")
        return

    returns = [r['return_pct'] for r in rows]
    confs = [r['confidence'] for r in rows]
    n = len(confs)
    # 1. 置信度与收益的 Pearson 相关系数
    mean_c, mean_r = sum(confs) / n, sum(returns) / n
    cov = sum((c - mean_c) * (r - mean_r) for c, r in zip(confs, returns)) / n
    std_c = math.sqrt(sum((c - mean_c) ** 2 for c in confs) / n)
    std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / n)
    corr = round(cov / (std_c * std_r), 3) if std_c > 0 and std_r > 0 else 0.0

    # 2. 按置信度中位数分高/低两组
    median_c = sorted(confs)[n // 2]
    hi = [r['return_pct'] for r in rows if r['confidence'] >= median_c]
    lo = [r['return_pct'] for r in rows if r['confidence'] < median_c]
    # ⚠️ 空组保护（2026-08-12 修复）：置信度分布集中（如全部≥1）时低置信组可能为0，
    # 空组 avg=0 参与对比会得出误导性结论，必须检测并降级为"仅参考相关系数"
    group_valid = len(hi) >= 5 and len(lo) >= 5

    def _grp(rs):
        wins = sum(1 for r in rs if r > 0)
        return {'n': len(rs),
                'win_rate': round(wins / len(rs) * 100, 1) if rs else 0,
                'avg': round(sum(rs) / len(rs), 2) if rs else 0}

    m_hi, m_lo = _grp(hi), _grp(lo)
    overall_wr = round(sum(1 for r in returns if r > 0) / n * 100, 1)

    print(f"  样本: {n} 个已平仓信号（{rows[0]['date']} ~ {rows[-1]['date']}）")
    print(f"  整体胜率: {overall_wr}%  平均收益: {sum(returns)/n:+.2f}%")
    corr_verdict = ('🟢 正相关（评分有预测力）' if corr > 0.2 else
                    ('🟡 弱相关（评分参考价值有限）' if corr > 0 else '🔴 负相关/无相关（评分可能误导）'))
    print(f"  置信度↔收益相关系数: {corr:+.3f}  {corr_verdict}")
    print(f"  高置信组(≥{median_c}): 胜率{m_hi['win_rate']}% 均收益{m_hi['avg']:+.2f}%  ({m_hi['n']}个)")
    print(f"  低置信组(<{median_c}): 胜率{m_lo['win_rate']}% 均收益{m_lo['avg']:+.2f}%  ({m_lo['n']}个)")
    print()
    print("  【校准建议】")
    if not group_valid:
        print(f"  ⚠️ 置信度分组样本不足（高/低置信组需各≥5个，当前 {m_hi['n']}/{m_lo['n']}），"
              f"不做分组对比，仅看相关系数: {corr_verdict}")
        print("     → 继续积累信号后再校准（分组对比需置信度有分布差异）")
    elif corr <= 0 and m_hi['avg'] > m_lo['avg']:
        # 相关系数负但分组对比方向正常：自相矛盾，需提示而非直接采信
        print(f"  ⚠️ 结论冲突: 相关系数 {corr:+.3f}（负相关）但高置信组均收益更高——"
              f"样本小/置信度分布集中导致指标不一致，建议积累样本后重新校准")
        print(f"     → 当前不调整权重，观察后续信号")
    elif m_hi['avg'] > m_lo['avg'] and m_hi['win_rate'] > m_lo['win_rate']:
        print("  ✅ 当前权重方向正确：高置信信号确实表现更好，无需调整")
    elif m_hi['avg'] < m_lo['avg']:
        print("  ⚠️ 高置信组反而跑输低置信组 → 权重方向可能反了")
        print("     → 建议降低技术面/资金面权重，或提高舆情/估值分位权重")
    else:
        print("  ⚠️ 置信度分组差异不明显 → 当前权重区分度不足")
        print("     → 建议引入相对强弱(RS)/估值分位等新因子，并跟踪其与收益的关系")
    print(f"  ℹ️ 当前因子权重: 动量5/技术6/基本面4/量能3/风险2/舆情2/资金流2/Level2 2/相对强弱3")
    # ── 含成本撮合透视（2026-08-25 借鉴 khQuant sim_trade 新增）──
    # 对已平仓信号用模拟账户重算含成本收益（滑点/佣金/印花税/过户费/T+1/整百股），
    # 量化"无成本口径"的乐观偏差，避免校准结论基于偏乐观收益
    try:
        from sim_trade import SimAccount
        raw_rets, cost_rets, skipped = [], [], 0
        for r in rows:
            entry, exit_ = r['price'], r['exit_price']
            if not entry or entry <= 0 or not exit_ or exit_ <= 0:
                skipped += 1
                continue
            acc = SimAccount(init_capital=100000)
            acc.new_day(r['date'])
            vol = acc.max_buy_volume(r['code'], entry, cash_ratio=1.0)
            if vol <= 0:
                skipped += 1
                continue
            b = acc.execute(r['code'], 'buy', entry, vol, date_str=r['date'])
            if not b['filled']:
                skipped += 1
                continue
            s = acc.execute(r['code'], 'sell', exit_, b['position'],
                            date_str=r['exit_date'] or r['date'])
            if s['filled']:
                # 平仓后无持仓：总资产 = 现金
                ret_cost = (acc.cash - acc.init_capital) / acc.init_capital * 100
            else:
                # 卖出被拒（T+1/数据异常）：按持仓市值+现金估算
                ret_cost = (b['actual_price'] * b['position'] + acc.cash
                            - acc.init_capital) / acc.init_capital * 100
            raw_rets.append(r['return_pct'])
            cost_rets.append(ret_cost)
        if len(raw_rets) >= 5:
            n = len(raw_rets)
            avg_raw = sum(raw_rets) / n
            avg_cost = sum(cost_rets) / n
            wr_raw = sum(1 for x in raw_rets if x > 0) / n * 100
            wr_cost = sum(1 for x in cost_rets if x > 0) / n * 100
            print()
            print("  【含成本撮合透视】（sim_trade 模拟：滑点0.1%+佣金万3/最低5元+印花税+过户费）")
            print(f"  样本: {n}（{skipped} 条跳过：缺价/无法成交）")
            print(f"  原口径(无成本):  胜率{wr_raw:.1f}%  均收益{avg_raw:+.2f}%")
            print(f"  含成本口径:      胜率{wr_cost:.1f}%  均收益{avg_cost:+.2f}%")
            bias = avg_raw - avg_cost
            print(f"  乐观偏差: {bias:+.2f}% (原口径 - 含成本)")
            if bias >= 1.0:
                print("  ⚠️ 乐观偏差显著（≥1%）——回测结论须以含成本口径为准，权重校准同理")
            elif bias >= 0.3:
                print("  🟡 存在一定乐观偏差，建议关注交易成本对策略真实收益的侵蚀")
            else:
                print("  🟢 乐观偏差较小，无成本口径可作近似参考")
        elif len(raw_rets) > 0:
            print(f"\n  ℹ️ 含成本撮合透视样本不足（{len(raw_rets)}<5），跳过")
    except Exception as e:
        log.warning(f"含成本撮合透视失败: {e}")
    print(f"{'='*72}")


def get_open_signals():
    """获取所有未平仓信号（含当前浮盈/浮亏）"""
    try:
        _init_signal_db()
        conn = sqlite3.connect(SIGNAL_DB)
        cur = conn.execute("SELECT code, name, signal_type, date, price, id FROM signals WHERE status='open' ORDER BY date DESC")
        signals = cur.fetchall()
        conn.close()
        # 计算每个信号的当前浮盈
        result = []
        for code, name, sig_type, date, price, sig_id in signals:
            try:
                q = fetch_quote_tencent(code)
                cur_price = q.get('price', price) if q else price
                ret = (cur_price - price) / price * 100 if price > 0 else 0
                result.append({'code': code, 'name': name, 'signal_type': sig_type,
                              'date': date, 'entry_price': price, 'current_price': cur_price,
                              'return_pct': round(ret, 2), 'id': sig_id})
            except Exception:
                result.append({'code': code, 'name': name, 'signal_type': sig_type,
                              'date': date, 'entry_price': price, 'current_price': price,
                              'return_pct': 0, 'id': sig_id})
        return result
    except Exception as e:
        log.warning(f"get_open_signals 失败: {e}")
        return []

def _cache_path(key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    h = hashlib.md5(key.encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{h}.cache")

# 架构优化（2026-08-26 P0）：缓存机制统一委托 data_layer.CacheManager
# - 版本号 v1：数据结构变更时递增自动失效旧缓存
# - 测试隔离：DATA_LAYER_ENV=TEST 时 mock 数据不落盘（防污染真实缓存）
_DL_CACHE = _dl.CacheManager(cache_dir=CACHE_DIR, version=1)

def cache_get(key, category='kline'):
    """统一缓存读取（委托 data_layer.CacheManager，带版本号+测试隔离）"""
    return _DL_CACHE.get(f'{category}:{key}', category)

def cache_set(key, value, category='kline'):
    """统一缓存写入（委托 data_layer.CacheManager，原子写+测试隔离）"""
    _DL_CACHE.set(f'{category}:{key}', value, category)

# ============================================================
# 股票名称 ↔ 代码 映射表（常用A股，可自动扩展）
# ============================================================
STOCK_NAME_MAP = {
    # 内置常见名称（非池内标的走硬编码；池内标的由下方 _POOL_EXTRA 动态合并）
    # 常用大盘指数
    '上证指数': 'sh000001', '深证成指': 'sz399001', '创业板指': 'sz399006',
    '科创50': 'sh000688', '沪深300': 'sh000300',
    # 行业ETF
    '芯片etf': 'sh512760', '半导体etf': 'sh512480', '消费电子etf': 'sz159732',
    '新能源车etf': 'sh515700', '光伏etf': 'sh515790', '医药etf': 'sh512010',
    '证券etf': 'sh512880', '银行etf': 'sh512800',
}

# 股票池单源动态合并（2026-08-31 修复：resolve_stock 曾因硬编码池缺标的
# 而静默丢弃——盘前基准池只落部分标的。池内标的以 config 单源为准，
# 从此增删股票只改 data/pool.json，名称解析自动跟随）
try:
    from config import get_watch_pool as _get_watch_pool
    for _n, _c in _get_watch_pool():
        STOCK_NAME_MAP.setdefault(_n, _c)
except Exception:
    pass

# ── 最后成功载荷缓存（2026-09-30 P1-5：东财冷却期回退）──
# 背景：东财 IP 限流冷却期内，龙虎榜/股东/主营/机构研报/BPS 五维整块缺失
#（报告完整度曾跌至 5/10）。改为回退"最近一次成功载荷"（JSON 持久化+日期戳）：
# 滞后但可用，展示层带 T-N 口径标注，禁止冒充当日实时。
_LASTOK_DIR = os.path.join(CACHE_DIR, 'lastok')


def _lastok_save(category, code, payload):
    """成功抓取后保存载荷副本（带日期戳）；失败静默（缓存属增强非关键路径）"""
    try:
        os.makedirs(_LASTOK_DIR, exist_ok=True)
        payload = dict(payload or {})
        payload['_lastok_date'] = datetime.now().strftime('%Y-%m-%d')
        with open(os.path.join(_LASTOK_DIR, f'{category}_{code}.json'), 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
    except Exception:
        pass


def _lastok_load(category, code, max_age_days=7):
    """读取最近成功载荷；超龄/缺失返回 None（数据太旧宁可缺失也不误导）"""
    try:
        with open(os.path.join(_LASTOK_DIR, f'{category}_{code}.json'), encoding='utf-8') as f:
            payload = json.load(f)
        d = str(payload.get('_lastok_date', ''))
        if not d:
            return None
        age = (datetime.now() - datetime.strptime(d, '%Y-%m-%d')).days
        if age > max_age_days:
            return None
        return payload
    except Exception:
        return None


def _with_lastok(category, code, fetcher, *args, max_age_days=7):
    """冷却期回退包装：成功→存 lastok；失败/不可用→回退最近成功载荷（标滞后口径）
    消费方须知：返回 dict 含 _stale=True + stale_note 时，日期字段为真实数据日期，
    展示层日期自证，不得当作当日实时引用。"""
    try:
        res = fetcher(*args)
    except Exception:
        res = None
    if isinstance(res, dict):
        # 终态保护（2026-09-30）：available=False 但语义为"正常无数据"（如龙虎榜
        # "近期未上榜"是可复现的确定状态，非接口失败）——直接返回，不回退陈旧载荷
        if not res.get('available') and str(res.get('note', '')).startswith('近期未上榜'):
            return res
    if isinstance(res, dict) and res.get('available'):
        _lastok_save(category, code, res)
        return res
    stale = _lastok_load(category, code, max_age_days)
    if isinstance(stale, dict):
        stale = dict(stale)
        d = stale.pop('_lastok_date', '')
        if d:
            stale['available'] = True
            stale['_stale'] = True
            stale['stale_note'] = f'冷却期回退：最近成功抓取于 {d}（T-N滞后口径，非当日实时）'
            return stale
    # 契约保证（2026-09-30审查修复）：fetcher 抛异常/返回 None/失败 且无历史缓存时，
    # 必须返回 dict（消费方 dragon_tiger['available'] 直接下标访问，返回 None 会 TypeError）
    if not isinstance(res, dict):
        return {'available': False, 'note': f'{category} 获取失败且无最近成功缓存'}
    return res


# ── 在线名称补全（2026-09-30 P1-6）──
# 背景："天顺风能"未收录导致识别失败。名称→代码 走腾讯 smartbox 模糊查询，
# 命中持久化 name_map_extra.json（启动自动加载），下次零网络开销。
_NAME_EXTRA_F = os.path.join(CACHE_DIR, 'name_map_extra.json')


def _load_name_extra():
    """启动时加载在线补全的历史落库（STOCK_NAME_MAP 动态扩展）"""
    try:
        with open(_NAME_EXTRA_F, encoding='utf-8') as f:
            extra = json.load(f)
        for n, c in extra.items():
            STOCK_NAME_MAP.setdefault(n, c)
    except Exception:
        pass


def _resolve_name_online(name):
    """腾讯 smartbox 模糊查询：名称→(code, name)；命中即落库。失败返回 None"""
    try:
        import urllib.parse as _up
        url = f"https://smartbox.gtimg.cn/s3/?v=2&q={_up.quote(name.strip())}&t=all"
        raw = subprocess.run(f'curl -s --connect-timeout 5 "{url}"', shell=True,
                             capture_output=True, timeout=8).stdout.decode('gbk', errors='replace')
        # 实测返回5段: sz~002531~\u5929\u98ce\u80fd~tsfn~GP-A（名称后还有拼音段+类型段）
        # 多候选以 ^ 分隔（如"证券"返回 ZS/ETF/LOF 混排）——2026-09-30审查修复：
        # 扫描全部候选取首个 GP 类型（股票），而非只看第一候选（首个可能是指数/ETF）
        picked = None
        for m in re.finditer(r'(sz|sh)~(\d{6})~([^~"^]+)~[^~"^]*~([^~"^]*)', raw):
            sec_type = m.group(4)
            if sec_type.startswith('GP'):
                picked = (m.group(1), m.group(2), m.group(3), sec_type)
                break
        if not picked:
            return None
        prefix, pure, nm_esc, sec_type = picked
        # 类型过滤：仅收沪深普通股（GP-A/GP-B），排除指数(ZS)/基金(FB/FE)等
        if not sec_type.startswith('GP'):
            return None
        # smartbox 中文名为 \uXXXX 转义，解码
        try:
            nm = nm_esc.encode('latin-1').decode('unicode_escape')
        except Exception:
            nm = nm_esc
        if not nm:
            return None
        code = f"{prefix}{pure}"
        STOCK_NAME_MAP[name] = code
        if nm and nm != name:
            STOCK_NAME_MAP.setdefault(nm, code)
        try:
            extra = {}
            try:
                with open(_NAME_EXTRA_F, encoding='utf-8') as f:
                    extra = json.load(f)
            except Exception:
                pass
            extra[name] = code
            if nm and nm != name:
                extra.setdefault(nm, code)
            with open(_NAME_EXTRA_F, 'w', encoding='utf-8') as f:
                json.dump(extra, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return code, nm
    except Exception:
        return None


_load_name_extra()

# 带market前缀的格式转换
def to_tencent_code(code):
    if code.startswith('sh'): return f"sh{code[2:]}"
    if code.startswith('sz'): return f"sz{code[2:]}"
    return code

def to_sina_code(code):
    if code.startswith('sh'): return f"sh{code[2:]}"
    if code.startswith('sz'): return f"sz{code[2:]}"
    return code

def resolve_stock(query):
    """解析股票查询，返回(code, name)"""
    q = query.strip()
    # 直接代码匹配
    if re.match(r'^(sh|sz)\d{6}$', q):
        return q, q
    if re.match(r'^\d{6}$', q):
        # 先查股票池映射
        for name, code in STOCK_NAME_MAP.items():
            if code.endswith(q):
                return code, name
        # 6开头=沪市，0/3开头=深市（含创业板）
        prefix = 'sh' if q.startswith('6') else 'sz'
        return f"{prefix}{q}", q
    # 名称匹配
    q_lower = q.lower()
    for name, code in STOCK_NAME_MAP.items():
        if q in name or q_lower in name.lower():
            return code, name
    # 模糊匹配（整串包含，避免单字符误匹配）
    for name, code in STOCK_NAME_MAP.items():
        if q in name or q_lower in name.lower():
            return code, name
    # 在线补全（2026-09-30 P1-6）：腾讯 smartbox 模糊查询，命中即持久化落库
    # 背景："天顺风能"未收录导致识别失败，被迫改用代码查询
    hit = _resolve_name_online(q)
    if hit:
        code, nm = hit
        return code, nm
    return None, None


# ============================================================
# 模块一：实时行情（腾讯自选股数据源）
# ============================================================
def _fetch_quote_minimal(code):
    """最小行情请求（仅用于兜底估算的递归保护，不调用 fetch_main_flow 避免循环）
    返回 quote 字典，只含 outer/inner/amount/price/pre_close 等基础字段
    """
    tc = to_tencent_code(code)
    cmd = f'curl -s --connect-timeout 5 "https://web.sqt.gtimg.cn/q={tc}"'
    r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
    raw = r.stdout.decode('gbk', errors='replace')
    parts = raw.split('~')
    if len(parts) < 50:
        return None

    def safe_float(idx, default=0):
        return float(parts[idx]) if idx < len(parts) and parts[idx].strip() else default

    quote = {
        'name': parts[1],
        'code': parts[2],
        'price': safe_float(3),
        'pre_close': safe_float(4),
        'open': safe_float(5),
        'volume': safe_float(6),
        'outer': safe_float(7),
        'inner': safe_float(8),
        'high': safe_float(33),
        'low': safe_float(34),
        'amount': safe_float(37) / 1e4,
    }
    if quote['pre_close'] > 0:
        quote['pct'] = (quote['price'] - quote['pre_close']) / quote['pre_close'] * 100
    else:
        quote['pct'] = 0
    if quote['inner'] > 0:
        quote['outer_inner_ratio'] = round(quote['outer'] / quote['inner'], 2)
    else:
        quote['outer_inner_ratio'] = 1
    # 盘前/停牌标记
    quote['inactive'] = quote['volume'] == 0 and quote['open'] == 0
    return quote


def fetch_quote_tencent(code):
    """获取实时行情（含基本面数据）"""
    tc = to_tencent_code(code)
    cmd = f'curl -s --connect-timeout 5 "https://web.sqt.gtimg.cn/q={tc}"'
    r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
    raw = r.stdout.decode('gbk', errors='replace')
    parts = raw.split('~')
    if len(parts) < 50:
        return None

    def safe_float(idx, default=0):
        return float(parts[idx]) if idx < len(parts) and parts[idx].strip() else default

    quote = {
        'name': parts[1],
        'code': tc,  # 带前缀的腾讯代码（如 sh600519），便于下游 fetch_main_flow 等判断前缀
        'pure_code': parts[2],  # 纯6位数字代码
        'price': safe_float(3),
        'pre_close': safe_float(4),
        'open': safe_float(5),
        'volume': safe_float(6),  # 手
        'outer': safe_float(7),   # 外盘（手）
        'inner': safe_float(8),   # 内盘（手）
        'high': safe_float(33),   # 最高价
        'low': safe_float(34),    # 最低价
        'amount': safe_float(37) / 1e4,  # 字段37=万元，转亿
        'pe_dynamic': safe_float(39),
        'eps': safe_float(40),
        # PB 从东方财富 BPS 反推（腾讯 parts[41] 是最高价重复字段，非 PB）
        # 总股本：parts[72] 单位为"股"，转亿股
        'total_shares': safe_float(72) / 1e8,  # 亿股
        'market_cap': safe_float(44),  # 总市值（亿）
        'amplitude': safe_float(43),   # 振幅%
        # ⚠️ parts[47/48] 是涨停价/跌停价，不是主力流入/流出
        'limit_up': safe_float(47),    # 涨停价
        'limit_down': safe_float(48),  # 跌停价
        'pct_raw': safe_float(49),     # 涨跌幅%（腾讯原始字段）
        # 主力资金流从东方财富 f62 接口获取（腾讯基础行情无此字段）
        'main_in': 0,   # 待 fetch_main_flow 填充
        'main_out': 0,  # 待 fetch_main_flow 填充
        'main_net': 0,  # 待 fetch_main_flow 填充
    }
    # PB 反推：从东方财富获取每股净资产 BPS，PB = price / BPS
    # 冷却期回退（2026-09-30 P1-5）：BPS 为数值型，独立处理——
    # 成功存 lastok；失败回退最近 BPS（标滞后），否则 pb=0 导致基本面 PB 因子丢失
    bps = fetch_bps(quote['code'])
    bps_stale_note = ''
    if not bps or bps <= 0:
        stale_payload = _lastok_load('bps', quote['code'])
        if stale_payload and isinstance(stale_payload.get('bps'), (int, float)) and stale_payload['bps'] > 0:
            bps = stale_payload['bps']
            bps_stale_note = f"BPS为冷却期回退口径（抓取于 {stale_payload.get('_lastok_date','')}）"
    if bps and bps > 0:
        quote['pb'] = round(quote['price'] / bps, 2)
        quote['bps'] = bps
        if bps_stale_note:
            # 回退口径仅标注不重存——重存会以今日日期戳刷新陈旧载荷，冒充新鲜数据
            quote['bps_stale_note'] = bps_stale_note
        else:
            _lastok_save('bps', quote['code'], {'bps': bps, 'price_ref': quote['price']})
    else:
        quote['pb'] = 0
        quote['bps'] = 0
    # 主力资金流：从东方财富 f62 获取（腾讯基础行情无主力净额字段）
    main_flow = fetch_main_flow(quote['code'])
    if main_flow:
        quote['main_in'] = main_flow.get('main_in', 0)
        quote['main_out'] = main_flow.get('main_out', 0)
        quote['main_net'] = main_flow.get('main_net', 0)
        quote['main_data_source'] = main_flow.get('data_source', 'eastmoney')
        # 数据日期 + 时效标记（防止昨日数据冒充当日）
        quote['data_date'] = main_flow.get('data_date', main_flow.get('fflow_date', ''))
        quote['data_stale'] = main_flow.get('stale', False)
        # 数据合理性校验结果透传（脏数据/分档不自洽时报告提示）
        quote['main_sanity_fail'] = main_flow.get('sanity_fail', '')
        quote['main_sanity_note'] = main_flow.get('sanity_note', '')
        # 透传口径警告（估算兜底时存在，真实数据源时无此字段）
        if main_flow.get('caliber_warning'):
            quote['main_caliber_warning'] = main_flow['caliber_warning']
    else:
        quote['main_data_source'] = '限流(无数据)'
    # 涨跌幅
    if quote['pre_close'] > 0:
        quote['pct'] = (quote['price'] - quote['pre_close']) / quote['pre_close'] * 100
    else:
        quote['pct'] = 0
    # 外盘/内盘比
    quote['outer_inner_ratio'] = round(quote['outer'] / quote['inner'], 2) if quote['inner'] > 0 else 1
    # 非交易时段检测：成交量为0或今开为0，说明盘前/停牌/未开盘
    quote['inactive'] = (quote['volume'] == 0 or quote['open'] == 0)
    return quote


# ============================================================
# 模块二：技术面（多周期K线 + 指标计算）
# ============================================================
def fetch_kline_push2his(code, datalen=60, fq=1):
    """东财 push2his 个股日K（新浪K线限流时的备用源，多域名轮询）
    返回与新浪结构对齐: [{day, open, high, low, close, volume}, ...]
    ⚠️ 单位换算：push2his f56 成交量单位为「手」，新浪为「股」，×100 对齐
    防限流(2026-08-10)：东财源级冷却期内直接返回空（走其他兜底）
    复权口径(2026-08-25 借鉴 khQuant 统一)：fq=1 前复权 / 0 不复权 / 2 后复权
    """
    if _em_cooldown_check('eastmoney'):
        return []
    pure = code[2:] if code.startswith(('sh', 'sz')) else code
    if not pure.isdigit() or len(pure) != 6:
        return []
    market = '1' if code.startswith('sh') else '0'
    secid = f"{market}.{pure}"
    try:
        data = _em_probe_all(
            lambda s, h: (f"{s}://{h}/api/qt/stock/kline/get?"
                          f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6&"
                          f"fields2=f51,f52,f53,f54,f55,f56,f57,f58&"
                          f"klt=101&fqt={fq}&end=20500101&lmt={datalen}"),
            _EM_FFLOW_HOSTS, timeout=8,
            valid=lambda d: bool(d.get('data') and d['data'].get('klines')))
        if data:
            out = []
            for line in data['data']['klines']:
                p = line.split(',')
                if len(p) >= 6:
                    out.append({
                        'day': p[0],
                        'open': p[1], 'close': p[2],
                        'high': p[3], 'low': p[4],
                        'volume': float(p[5]) * 100,  # 手→股，与新浪对齐
                    })
            return out
    except Exception as e:
        log.warning(f"东财K线获取失败: {e}")
    return []


def fetch_kline_baostock(code, datalen=60, fq=1):
    """baostock 日K兜底源（2026-09-22 借鉴 Sequoia-X：免费无需注册无限流）
    定位：新浪+东财双限流时的第三道防线（东财IP级限流时唯一稳定K线源）。
    返回与新浪结构对齐: [{day, open, high, low, close, volume(股)}, ...]
    复权口径：fq=1 前复权(adjustflag=2) / 0 不复权(3) / 2 后复权(1)
    注意：baostock 当日数据约 17-19 点才更新，盘中拿到的最新一根可能是上一交易日
    （与新浪盘中滞后类似，调用方已有日期戳核验时会自动标 stale）
    P1-2 修复(2026-09-22体检)：baostock 底层是全局 socket 单连接，fetch_kline_batch
    多线程并发时 login/查询会串包竞态——整个临界区用 _BS_LOCK 串行化"""
    global _BS_SESSION
    try:
        import baostock as bs
    except ImportError:
        return []
    prefix, pure = code[:2], code[2:]
    if not pure.isdigit() or len(pure) != 6:
        return []
    bs_code = f"{prefix}.{pure}"  # baostock 格式：sh.600000（带点）
    adj = {'1': '2', '0': '3', '2': '1'}.get(str(fq), '2')
    with _BS_LOCK:
        if _BS_SESSION is None:
            lg = bs.login()
            if lg.error_code != '0':
                log.warning(f"baostock 登录失败: {lg.error_msg}")
                return []
            _BS_SESSION = bs
        try:
            import datetime as _dt  # 模块级 datetime 已被 from-import 遮蔽为类，独立别名
            end = _dt.date.today().strftime('%Y-%m-%d')
            start = (_dt.date.today() - _dt.timedelta(days=int(datalen * 2.2))).strftime('%Y-%m-%d')
            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,high,low,close,volume",
                start_date=start, end_date=end,
                frequency="d", adjustflag=adj)
            if rs.error_code != '0':
                log.warning(f"baostock 查询失败 {code}: {rs.error_msg}")
                return []
            out = []
            while rs.next():
                row = rs.get_row_data()
                try:
                    if not row[4] or float(row[5]) <= 0:
                        continue  # 跳过无收盘/零成交（停牌日）
                    out.append({
                        'day': row[0],
                        'open': row[1], 'close': row[4],
                        'high': row[2], 'low': row[3],
                        'volume': float(row[5]),  # baostock 单位=股，与新浪对齐
                    })
                except (ValueError, IndexError):
                    continue
            return out[-datalen:]
        except Exception as e:
            log.warning(f"fetch_kline_baostock 失败: {e}")
            _BS_SESSION = None  # 连接异常则下次重新登录
            return []


_BS_SESSION = None  # baostock 会话（模块级单例，避免每次重新login）
_BS_LOCK = __import__('threading').Lock()  # P1-2: 单连接串行化锁


def fetch_kline_sina(code, scale=240, datalen=60, fq=1):
    """获取K线数据（带缓存+内存缓存）
    新浪限流时自动回退东财 push2his 日K（防御性备用源）
    时效修复(2026-08-10)：新浪日K盘中滞后一日（最新一根为上一交易日），
    若东财 push2his 已有更新数据则自动切换，避免技术指标基于昨日K线计算
    复权口径(2026-08-25 借鉴 khQuant 统一)：fq=1 前复权 / 0 不复权 / 2 后复权
    （新浪接口不支持复权参数，fq 仅透传给东财备用源；回测类调用务必显式指定）
    """
    # P1-1 修复(2026-09-22体检)：新浪接口不支持复权参数，返回的是不复权数据，
    # 但旧 cache_key 含 fq —— fq=1 调用把不复权数据缓存在 fq=1 键下，与东财/baostock
    # 降级源（真前复权）同键混存，除权日后指标突变。新浪命中时强制以 fq=0 键写入；
    # 降级源命中时才用真实 fq 入缓存。缓存键与数据口径从此一一对应。
    cache_key = f"{code}_{scale}_{datalen}_{fq}"      # 降级源（真复权）用
    cache_key_sina = f"{code}_{scale}_{datalen}_0"    # 新浪源（恒为不复权）用
    cached = cache_get(cache_key, 'kline')
    if cached:
        return cached
    cached = cache_get(cache_key_sina, 'kline')
    if cached:
        return cached
    # 新浪源级冷却（2026-08-25 修复：批量场景反复限流时不再硬闯，直接走东财降级）
    # 复用 _em_cooldown_* 机制（key='sina'）；冷却期内日线直接取东财，非日线返回空
    if _em_cooldown_check('sina'):
        if scale == 240:
            alt = fetch_kline_push2his(code, datalen, fq=fq)
            if alt:
                cache_set(cache_key, alt, 'kline')
                return alt
            alt = fetch_kline_baostock(code, datalen, fq=fq)
            if alt:
                cache_set(cache_key, alt, 'kline')
                log.info(f"K线兜底源命中: {code} 走 baostock (新浪冷却+东财限流)")
                return alt
        return []
    prefix = 'sz' if code.startswith('sz') else 'sh'
    pure = code[2:]
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{pure}&scale={scale}&datalen={datalen}"
    cmd = f'curl -s --connect-timeout 6 "{url}" -H "Referer: https://finance.sina.com.cn"'
    r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
    try:
        result = json.loads(r.stdout.decode('gbk', errors='replace'))
        if result:
            # 日K时效检查：新浪盘中可能滞后一日，尝试东财源对比取更新者
            if scale == 240 and len(result) >= 1:
                sina_last_day = str(result[-1].get('day', ''))
                try:
                    alt = fetch_kline_push2his(code, datalen, fq=fq)
                    if alt and len(alt) >= 1:
                        em_last_day = str(alt[-1].get('day', ''))
                        # 字符串日期可直接比较（YYYY-MM-DD），东财更新则采用东财
                        if em_last_day > sina_last_day:
                            log.info(f"K线时效修复: {code} 新浪滞后({sina_last_day}) → 东财当日({em_last_day})")
                            cache_set(cache_key, alt, 'kline')
                            return alt
                except Exception as e:
                    log.warning(f"K线时效修复失败(东财): {e}")
            cache_set(cache_key_sina, result, 'kline')   # 新浪=不复权，必须落 fq=0 键
            return result
        # 空响应：限流或单个坏代码（新股/退市），计数器累积（≥3次才判真限流）
        _sina_fail_mark()
    except Exception as e:
        log.warning(f"fetch_kline_sina 失败: {e}")
        _sina_fail_mark()  # P2-9: 连续失败计数，避免单坏代码放大成全站冷却
    # 备用源：新浪限流时走东财 push2his 日K（仅日线）
    if scale == 240:
        alt = fetch_kline_push2his(code, datalen, fq=fq)
        if alt:
            cache_set(cache_key, alt, 'kline')
            log.info(f"K线备用源命中: {code} 走东财 push2his")
            return alt
        # 第三道防线：新浪+东财双限流时走 baostock（免费无限流）
        alt = fetch_kline_baostock(code, datalen, fq=fq)
        if alt:
            cache_set(cache_key, alt, 'kline')
            log.info(f"K线兜底源命中: {code} 走 baostock (新浪+东财双限流)")
            return alt
    return []


def fetch_kline_batch(codes, scale=240, datalen=60):
    """并行批量获取K线数据"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        fut = {executor.submit(fetch_kline_sina, c, scale, datalen): c for c in codes}
        results = {}
        for f in concurrent.futures.as_completed(fut):
            c = fut[f]
            try:
                results[c] = f.result()
            except Exception as e:
                log.warning(f"fetch_kline_batch {c} 失败: {e}")
                results[c] = []
        return results

def calc_ma(data, n):
    if len(data) < n: return None
    return sum(data[-n:]) / n

def calc_rsi(data, n=14):
    if len(data) < n+1: return None
    deltas = [data[i] - data[i-1] for i in range(-n, 0)]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_g = sum(gains) / n if gains else 0
    avg_l = sum(losses) / n if losses else 0.001
    return 100 - 100 / (1 + avg_g / avg_l)

def calc_ema(data, n):
    """指数移动平均（独立函数，供多指标复用）"""
    if len(data) < n: return None
    r = [data[0]]
    k = 2 / (n + 1)
    for i in range(1, len(data)):
        r.append(data[i] * k + r[-1] * (1 - k))
    return r  # 返回完整序列

def calc_dma(price, vol_ratio):
    """通达信 DMA 指标（递推式动态移动平均）
    DMA = 前DMA * (1 - ABC1) + C * ABC1，其中 ABC1 = 当日成交量 / 13日成交量之和
    vol_ratio 应为完整序列（每个元素代表该日 ABC1 权重）
    返回：最后一个交易日的 DMA 值（float），数据不足时返回 None
    """
    if not price or not vol_ratio or len(price) != len(vol_ratio) or len(price) < 2:
        return None
    dma = price[0]
    for i in range(1, len(price)):
        dma = dma * (1 - vol_ratio[i]) + price[i] * vol_ratio[i]
    return dma

def calc_macd(data, fast=12, slow=26, sig=9):
    if len(data) < slow+sig: return 0, 0, 0
    ef = calc_ema(data, fast); es = calc_ema(data, slow)
    if not ef or not es: return 0, 0, 0
    dif = [ef[i] - es[i] for i in range(len(data))]
    dea = calc_ema(dif, sig)
    if not dea: return 0, 0, 0
    return dif[-1], dea[-1], (dif[-1] - dea[-1]) * 2


# 多周期共振结果进程内缓存 {code: (result, ts)}，TTL 300s
# 避免同一次分析里 calculate_quant_score 和 generate_report 重复拉 500 根日线
_MTF_CACHE = {}
_MTF_TTL = 300


def calc_multi_timeframe(code, price, _use_cache=True, daily=None):
    """多周期共振分析（日线/周线/月线，日线聚合）
    _use_cache=False 时绕过进程内缓存（强制刷新）
    daily: 可选，复用调用方已拉取的日线（避免重复请求 500 根）
    """
    # 进程内缓存命中检查
    if _use_cache and code in _MTF_CACHE:
        result, ts = _MTF_CACHE[code]
        if time.time() - ts < _MTF_TTL:
            return result
    result = {}
    # 获取足够多的日线数据用于聚合（约2年 = 500个交易日）
    # ⚠️ 2026-08-12 修复：可传 daily 复用已拉取数据；否则拉 500 根（fetch_kline_sina 有文件缓存，
    #    同 key {code}_240_500 在 300s TTL 内命中，不会真实重复网络请求）
    if daily is None:
        daily = fetch_kline_sina(code, 240, 500)
    if not daily or len(daily) < 60:
        return {'weekly': {'available': False}, 'monthly': {'available': False}}
    
    # 提取日线数据
    # ⚠️ 2026-08-12 修复：字段是 'day'（fetch_kline_sina 返回 {day, open, ...}），不是 'date'
    #    （原代码用 d.get('date') 导致日期解析失败，周/月线聚合退化为 5天/21天切片，从未按真实自然周/月对齐）
    dates = [d.get('day', '') for d in daily]
    closes = [float(d['close']) for d in daily]
    highs = [float(d['high']) for d in daily]
    lows = [float(d['low']) for d in daily]
    opens = [float(d['open']) for d in daily]
    volumes = [float(d.get('volume', 0)) for d in daily]
    
    # ── 聚合周线（按自然周 ISO week 对齐）──
    weekly = {'close': [], 'high': [], 'low': [], 'open': [], 'volume': []}
    cur_week = None
    cur_o = cur_h = cur_l = cur_c = cur_v = None
    for i, d in enumerate(daily):
        ds = str(d.get('day', ''))  # ⚠️ 字段是 day 不是 date（2026-08-12 修复）
        # date 格式 "YYYY-MM-DD"，取 ISO 周序号
        try:
            dt = datetime.strptime(ds[:10], '%Y-%m-%d')
            wk = dt.isocalendar()[1] + dt.year * 100  # 年+周序号
        except Exception:
            wk = i // 5  # 解析失败时退化为按5天切片
        if cur_week is None:
            cur_week = wk
            cur_o = opens[i]; cur_h = highs[i]; cur_l = lows[i]; cur_c = closes[i]; cur_v = volumes[i]
        elif wk != cur_week:
            # 新周开始，提交上一周
            weekly['close'].append(cur_c)
            weekly['high'].append(cur_h)
            weekly['low'].append(cur_l)
            weekly['open'].append(cur_o)
            weekly['volume'].append(cur_v)
            cur_week = wk
            cur_o = opens[i]; cur_h = highs[i]; cur_l = lows[i]; cur_c = closes[i]; cur_v = volumes[i]
        else:
            cur_h = max(cur_h, highs[i]); cur_l = min(cur_l, lows[i])
            cur_c = closes[i]; cur_v += volumes[i]
    # 提交最后一周
    if cur_week is not None:
        weekly['close'].append(cur_c)
        weekly['high'].append(cur_h)
        weekly['low'].append(cur_l)
        weekly['open'].append(cur_o)
        weekly['volume'].append(cur_v)

    # ── 聚合月线（按自然月 YYYY-MM 对齐）──
    monthly = {'close': [], 'high': [], 'low': [], 'open': [], 'volume': []}
    cur_month = None
    cur_o = cur_h = cur_l = cur_c = cur_v = None
    for i, d in enumerate(daily):
        ds = str(d.get('day', ''))  # ⚠️ 字段是 day 不是 date（2026-08-12 修复）
        try:
            mo = int(ds[:4]) * 100 + int(ds[5:7])  # YYYYMM
        except Exception:
            mo = i // 21  # 解析失败时退化为按21天切片
        if cur_month is None:
            cur_month = mo
            cur_o = opens[i]; cur_h = highs[i]; cur_l = lows[i]; cur_c = closes[i]; cur_v = volumes[i]
        elif mo != cur_month:
            monthly['close'].append(cur_c)
            monthly['high'].append(cur_h)
            monthly['low'].append(cur_l)
            monthly['open'].append(cur_o)
            monthly['volume'].append(cur_v)
            cur_month = mo
            cur_o = opens[i]; cur_h = highs[i]; cur_l = lows[i]; cur_c = closes[i]; cur_v = volumes[i]
        else:
            cur_h = max(cur_h, highs[i]); cur_l = min(cur_l, lows[i])
            cur_c = closes[i]; cur_v += volumes[i]
    if cur_month is not None:
        monthly['close'].append(cur_c)
        monthly['high'].append(cur_h)
        monthly['low'].append(cur_l)
        monthly['open'].append(cur_o)
        monthly['volume'].append(cur_v)
    
    # ── 计算周线指标 ──
    for tf_name, tf_data in [('weekly', weekly), ('monthly', monthly)]:
        tf_closes = tf_data['close']
        if len(tf_closes) < 10:
            result[tf_name] = {'available': False}
            continue
        
        tf_ma5 = calc_ma(tf_closes, 5)
        tf_ma10 = calc_ma(tf_closes, 10)
        tf_ma20 = calc_ma(tf_closes, 20)
        tf_ma60 = calc_ma(tf_closes, 60)
        tf_dif, tf_dea, tf_macd = calc_macd(tf_closes)
        tf_rsi = calc_rsi(tf_closes, 14)
        
        tf_ema7 = calc_ema(tf_closes, 7)
        tf_ema21 = calc_ema(tf_closes, 21)
        tf_a1 = None; tf_b1 = None
        if tf_ema7 and tf_ema21 and len(tf_ema7) >= 3:
            a1_s = [tf_ema7[i] - tf_ema21[i] for i in range(len(tf_ema7))]
            tf_a1 = a1_s[-1]; tf_b1 = 0.668 * a1_s[-2] + 0.333 * a1_s[-1]
        
        tf_bull = sum(1 for ma in [tf_ma5, tf_ma10, tf_ma20, tf_ma60] if ma and price > ma)
        if tf_bull >= 3: tf_trend = '多头'
        elif tf_bull >= 1: tf_trend = '震荡'
        else: tf_trend = '空头'
        
        result[tf_name] = {
            'available': True, 'trend': tf_trend, 'bull_count': tf_bull,
            'ma5': tf_ma5, 'ma10': tf_ma10, 'ma20': tf_ma20, 'ma60': tf_ma60,
            'macd': tf_macd, 'dif': tf_dif, 'dea': tf_dea, 'rsi': tf_rsi,
            'a1': tf_a1, 'b1': tf_b1,
        }
    # 写入进程内缓存（即使部分不可用也缓存，避免重复拉 500 根日线）
    if _use_cache:
        _MTF_CACHE[code] = (result, time.time())
    return result

def calc_boll(data, n=20, k=2):
    if len(data) < n: return 0, 0, 0
    seg = data[-n:]; ma = sum(seg) / n
    var = sum((x - ma)**2 for x in seg) / n
    std = math.sqrt(var) if var > 0 else 0
    return ma + k*std, ma, ma - k*std


# ═══════════════════════════════════════════════════════════════
# 筹码分布分析（CYQ 风格，三角形分配 + 时间衰减）
# 2026-08-12 新增：结合 K线走势判断底部筹码锁定与主力成本区
# 原理：每根K线的成交量按当日价格区间 [low, high] 做三角形分配
#       （收盘价处权重最高，向 low/high 线性递减），逐日累加，
#       历史筹码按衰减系数稀释（越早越可能被换手）。
# 输出：获利盘比例 / 成本峰位 / 90%集中度 / 底部锁定度 / 平均成本
# ═══════════════════════════════════════════════════════════════
def analyze_chip_distribution(kline_data, price=None, decay=0.90, buckets=120, bottom_zone=0.20):
    """筹码分布分析（日线/分钟线通用）

    参数:
      kline_data: [{day, open, high, low, close, volume}, ...]（按时间升序）
      price:     当前价（缺省用最后一根 close）
      decay:     历史筹码日衰减系数（0-1，越小换手越快，默认 0.90）
      buckets:   价格分桶数（默认 120）
      bottom_zone: 底部锁定区定义（现价下方 X% 内算"底部筹码"，默认 20%）

    返回 dict:
      available:   是否成功
      winner_pct:  获利盘比例（现价下方筹码占比 %）
      cost_peak:   成本峰位（筹码最密集价格带）
      cost_avg:    平均成本（筹码加权均价）
      concentration: 90%集中度（90%筹码所处价格区间宽度 / 均价，越小越集中）
      bottom_lock: 底部锁定度（现价下方 bottom_zone 内筹码占比 %，越高底部越稳）
      chip_note:   一句话解读
      buckets:     [{'price': p, 'chip': c}, ...]（供绘图/展示）
    """
    import math as _m
    result = {'available': False, 'winner_pct': 0, 'cost_peak': 0, 'cost_avg': 0,
              'concentration': 0, 'bottom_lock': 0, 'chip_note': '', 'buckets': []}
    if not kline_data or len(kline_data) < 10:
        return result
    try:
        highs = [float(d['high']) for d in kline_data]
        lows = [float(d['low']) for d in kline_data]
        closes = [float(d['close']) for d in kline_data]
        vols = [float(d.get('volume', 0)) for d in kline_data]
    except Exception:
        return result
    if price is None:
        price = closes[-1]
    if price <= 0 or max(vols) <= 0:
        return result

    # ── 1. 价格分桶（覆盖全区间，留 5% 余量）──
    p_min = min(lows)
    p_max = max(highs)
    span = max(p_max - p_min, price * 0.01)
    lo, hi = p_min - span * 0.05, p_max + span * 0.05
    step = (hi - lo) / buckets
    chip = [0.0] * buckets
    price_at = [lo + step * (i + 0.5) for i in range(buckets)]

    # ── 2. 逐日三角形分配 + 衰减 ──
    for i in range(len(kline_data)):
        o = highs[i]; l = lows[i]; c = closes[i]; v = vols[i]
        if v <= 0 or c <= 0 or _m.isnan(v):
            continue
        # 三角形分布：收盘价 c 处权重最大（=2/(span_tri)），向 low/high 线性降到 0
        tri_lo, tri_hi = min(l, o), max(highs[i], o)  # 用当日真实波动区间
        if tri_hi - tri_lo < step:  # 极窄K线保护
            tri_lo, tri_hi = c - step, c + step
        tri_span = tri_hi - tri_lo
        if tri_span <= 0:
            continue
        # 该日成交量按三角形权重分配到各桶
        for j in range(buckets):
            p = price_at[j]
            if p < tri_lo or p > tri_hi:
                continue
            # 三角形高度：0（端点）→ 1（收盘价处）
            if p <= c:
                w = (p - tri_lo) / (c - tri_lo) if c > tri_lo else 1.0
            else:
                w = (tri_hi - p) / (tri_hi - c) if tri_hi > c else 1.0
            w = max(0.0, min(1.0, w))
            chip[j] += v * w
        # 历史筹码衰减（当日分配完成后再衰减，新筹码不衰减）
        if i < len(kline_data) - 1:
            chip = [x * decay for x in chip]

    # ── 3. 指标计算 ──
    total = sum(chip)
    if total <= 0:
        return result
    # 获利盘：现价下方筹码占比
    win = sum(chip[j] for j in range(buckets) if price_at[j] <= price)
    winner_pct = round(win / total * 100, 1)
    # 成本峰位：筹码最密集桶价格
    peak_j = max(range(buckets), key=lambda j: chip[j])
    cost_peak = round(price_at[peak_j], 2)
    # 平均成本：筹码加权均价
    cost_avg = round(sum(chip[j] * price_at[j] for j in range(buckets)) / total, 2)
    # 90%集中度：按筹码量排序取累计 90% 的价格区间
    order = sorted(range(buckets), key=lambda j: -chip[j])
    acc, p90_lo, p90_hi = 0.0, price_at[order[0]], price_at[order[0]]
    for j in order:
        acc += chip[j]
        p90_lo = min(p90_lo, price_at[j])
        p90_hi = max(p90_hi, price_at[j])
        if acc >= total * 0.90:
            break
    concentration = round((p90_hi - p90_lo) / cost_avg * 100, 1) if cost_avg > 0 else 0
    # 底部锁定度：现价下方 bottom_zone 内筹码占比
    bottom_floor = price * (1 - bottom_zone)
    bot = sum(chip[j] for j in range(buckets) if bottom_floor <= price_at[j] <= price)
    bottom_lock = round(bot / total * 100, 1)

    # ── 4. 解读 ──
    if winner_pct >= 75 and concentration <= 15:
        note = '🟢 高位获利盘多但筹码高度集中——主力控盘强，若价格站稳可看新高；警惕集中派发'
    elif winner_pct >= 75:
        note = '🟡 获利盘多（上方套牢盘少），上方压力轻，但需观察集中度是否收敛'
    elif winner_pct >= 50:
        note = '⚪ 获利盘过半，多空成本接近，方向待选择'
    elif bottom_lock >= 40 and winner_pct < 50:
        note = '🔵 底部筹码锁定（锁筹 X%），低位成本稳定——若放量突破则为深V/反转的筹码基础'
    else:
        note = '🔴 套牢盘较多且底部筹码不足，反弹压力大，需底部换手充分后再看'

    result.update({'available': True, 'winner_pct': winner_pct, 'cost_peak': cost_peak,
                   'cost_avg': cost_avg, 'concentration': concentration,
                   'bottom_lock': bottom_lock, 'chip_note': note,
                   'buckets': [{'price': round(price_at[j], 2), 'chip': round(chip[j], 0)}
                                for j in range(buckets)]})
    return result


def analyze_chip_60min(code, n=48, decay=0.95):
    """60分钟级别筹码集中度分析（复用日线核心函数，周期无关）

    60分钟K线反映短线资金博弈，筹码集中度比日线更敏感：
      - 集中度低（<15%）→ 短线成本高度一致，方向即将选择
      - 底部锁定度高 → 回调中低位筹码不松动，短线支撑强
    返回 analyze_chip_distribution 的 dict（含 available / winner_pct 等）。
    """
    kline = fetch_kline_sina(code, 60, n)
    if not kline or len(kline) < 10:
        return {'available': False, 'winner_pct': 0, 'cost_peak': 0, 'cost_avg': 0,
                'concentration': 0, 'bottom_lock': 0, 'chip_note': '', 'buckets': []}
    r = analyze_chip_distribution(kline, decay=decay)
    if r.get('available'):
        # 60分钟特有解读（追加）
        if r['concentration'] <= 15:
            r['chip_note'] += '｜60分钟筹码集中，短线成本一致，随时变盘'
        elif r['bottom_lock'] >= 40:
            r['chip_note'] += '｜60分钟底部筹码锁定，回调支撑强'
        else:
            r['chip_note'] += '｜60分钟筹码分散，短线方向未明'
    return r


def _chip_factor_scores(overhang, bottom_lock, dev):
    """筹码三因子分项得分（共享函数，analyze_chip_score 与 run_chip_score 复用）
    返回 (s_over, s_lock, s_dev, total)：
      套牢盘（0-30）：≤20% 最优；>60% 最差
      底部锁定（0-45）：≥50% 最优；<30% 最差（统计核心因子）
      成本偏离（0-25）：±5% 触发区最优；>+10% / <-10% 最差
    """
    s_over = 30 if overhang <= 20 else 22 if overhang <= 40 else 14 if overhang <= 60 else 6
    s_lock = 45 if bottom_lock >= 50 else 32 if bottom_lock >= 30 else 18
    s_dev = 25 if -5 <= dev <= 5 else 15 if -10 <= dev <= 10 else 8
    return s_over, s_lock, s_dev, s_over + s_lock + s_dev


def analyze_chip_score(code, name='', n=250, decay=0.90):
    """筹码三因子反弹评分（套牢盘 / 底部锁定 / 成本偏离）

    ⚠️ 2026-08-18 新增：三因子权重与档位基于当日大样本统计校准
    （41只股票×250交易日，3860个回调观测——T日收盘<MA20 场景）：
      1. 上方套牢盘 = 100 - 获利盘（现价上方套牢筹码占比）
         统计：回调场景下套牢盘大小本身区分度弱（样本中套牢>40%占98%），
         仅作辅助因子（权重30%，档位线性递减）
      2. 底部锁定度 = 现价下方20%内筹码占比 —— 核心决定因子（权重45%）
         同套牢>40%时：锁定中/高(30-50%/≥50%) T+10胜率58.9%/55.4%
         vs 锁定低(<30%)的51.3%，收益+2.60% vs +1.30%——差距近1倍
      3. 成本偏离 = (现价/成本均价-1)*100 —— 触发因子（权重25%）
         现价≈成本均价(±5%)为最佳反弹触发区；>+10%主力浮盈厚(兑现区)；
         <-10%主力深套(自救区，动力弱于±5%)
    返回 dict：available / factors / score / grade / prob_note / conclusion
    """
    result = {'available': False, 'factors': {}, 'score': 0, 'grade': '',
              'prob_note': '', 'conclusion': ''}
    try:
        kline = fetch_kline_sina(code, 240, n)
        if not kline or len(kline) < 30:
            result['note'] = 'K线数据不足'
            return result
        closes = [float(d['close']) for d in kline]
        price = closes[-1]
        ma20 = sum(closes[-20:]) / 20
        r = analyze_chip_distribution(kline, price=price, decay=decay)
        if not r.get('available'):
            result['note'] = '筹码分布计算失败'
            return result
        # ── 三因子原始值 ──
        overhang = round(100 - r['winner_pct'], 1)          # 上方套牢盘%
        bottom_lock = r['bottom_lock']                       # 底部锁定度%
        cost_avg = r['cost_avg']
        dev = round((price / cost_avg - 1) * 100, 1) if cost_avg > 0 else 0  # 成本偏离%
        # ── 分项得分（共享函数 _chip_factor_scores）──
        s_over, s_lock, s_dev, total = _chip_factor_scores(overhang, bottom_lock, dev)
        # ── 档位 ──
        if total >= 75:   grade = '🟢 强反弹结构'
        elif total >= 60: grade = '🟢 较优反弹结构'
        elif total >= 45: grade = '🟡 中等反弹结构'
        elif total >= 30: grade = '🟠 偏弱反弹结构'
        else:             grade = '🔴 弱反弹结构'
        # ── 概率参考（映射大样本统计档位）──
        if bottom_lock >= 30:
            if overhang <= 40:
                prob_note = '参考: 套牢中+锁高档 T+5胜率58.3% / T+10约54%'
            else:
                prob_note = '参考: 套牢多+锁定中高档 T+10胜率55-59% / 均收+0.8~2.6%'
        else:
            if overhang <= 40:
                prob_note = '参考: 套牢中+锁低档 样本少，反弹概率一般'
            else:
                prob_note = '参考: 套牢多+锁低档 T+10胜率51% / 均收+1.3%——反弹最弱档'
        # ── 一句话结论 ──
        parts = []
        parts.append(f'套牢盘{overhang:.0f}%({"轻" if overhang<=20 else "中" if overhang<=40 else "重"})')
        parts.append(f'底部锁定{bottom_lock:.0f}%({"高" if bottom_lock>=50 else "中" if bottom_lock>=30 else "低"})')
        parts.append(f'成本偏离{dev:+.1f}%({"触发区" if -5<=dev<=5 else "略高/略低" if -10<=dev<=10 else "透支/深套"})')
        if total >= 60:
            concl = f'✅ {name}筹码结构利于反弹：' + '、'.join(parts)
        elif total >= 45:
            concl = f'⚪ {name}筹码结构中性：' + '、'.join(parts) + '——需放量确认'
        else:
            concl = f'🔴 {name}筹码结构不利反弹：' + '、'.join(parts)
        result.update({
            'available': True, 'price': price, 'ma20': round(ma20, 2),
            'factors': {'overhang': overhang, 'bottom_lock': bottom_lock,
                        'cost_avg': cost_avg, 'cost_dev': dev,
                        'cost_peak': r['cost_peak'], 'concentration': r['concentration'],
                        'winner_pct': r['winner_pct']},
            'score': total, 'grade': grade, 'prob_note': prob_note,
            'conclusion': concl,
        })
        return result
    except Exception as e:
        log.warning(f"analyze_chip_score 失败: {e}")
        result['note'] = f'筹码评分异常: {e}'
        return result


def run_chip_score(code, name=''):
    """筹码三因子反弹评分：拉取数据→分析→格式化输出（--chip-score 入口）
    输出：三因子明细表 + 总分/档位 + 概率参考 + 确认开关
    """
    print(f"\n{'='*72}")
    print(f"  🧠 筹码三因子反弹评分 —— {name}({code})")
    print(f"{'='*72}")
    r = analyze_chip_score(code, name)
    if not r.get('available'):
        print(f"  ❌ 分析失败: {r.get('note', '未知错误')}")
        print(f"{'='*72}\n")
        return
    f = r['factors']
    price = r['price']
    ma20 = r['ma20']
    print(f"  现价 {price:.2f} | MA20 {ma20:.2f} ({price/ma20*100-100:+.1f}%)")
    print(f"  {'─'*60}")
    print(f"  {'因子':<12} {'数值':>10} {'得分':>6} {'判定'}")
    print(f"  {'─'*60}")
    # 分项得分回溯（共享函数 _chip_factor_scores，与 analyze_chip_score 完全一致）
    o = f['overhang']
    b = f['bottom_lock']
    d = f['cost_dev']
    s_over, s_lock, s_dev, _ = _chip_factor_scores(o, b, d)
    o_tag = '轻（上方真空）' if o <= 20 else '中' if o <= 40 else '重（解套抛压）'
    b_tag = '高（核心利多）' if b >= 50 else '中' if b >= 30 else '低（无锁仓）'
    d_tag = '触发区（最优）' if -5 <= d <= 5 else ('略高（兑现区边缘）' if d > 5 else '略低（自救区）')
    print(f"  {'上方套牢盘':<12} {o:>9.0f}% {s_over:>6d}  {o_tag}")
    print(f"  {'底部锁定度':<12} {b:>9.0f}% {s_lock:>6d}  {b_tag}")
    print(f"  {'成本偏离':<12} {d:>+9.1f}% {s_dev:>6d}  {d_tag}")
    print(f"  {'─'*60}")
    print(f"  📊 总分: {r['score']}/100  →  {r['grade']}")
    print(f"  📈 概率参考: {r['prob_note']}")
    print(f"  📌 结论: {r['conclusion']}")
    print(f"  {'─'*60}")
    print(f"  ⏱️  确认开关:")
    print(f"     🟢 反弹开启: 放量突破近期高点（成本峰 {f['cost_peak']:.2f} 上方）")
    print(f"     🔴 反弹失败: 放量跌破现价下方 5%（成本均价 {f['cost_avg']:.2f} 支撑失守）")
    print(f"  {'='*72}\n")


def calc_kdj(data, n=9):
    """标准 KDJ（递推式）
    RSV = (C - Ln) / (Hn - Ln) * 100
    K = 2/3 * 前K + 1/3 * RSV   （初值 50）
    D = 2/3 * 前D + 1/3 * K     （初值 50）
    J = 3*K - 2*D
    """
    if len(data) < n:
        return 50, 50, 50
    k = d = 50.0
    for i in range(n - 1, len(data)):
        window = data[i - n + 1: i + 1]
        hi = max(window)
        lo = min(window)
        rsv = (data[i] - lo) / (hi - lo) * 100 if hi != lo else 50
        k = 2/3 * k + 1/3 * rsv
        d = 2/3 * d + 1/3 * k
    j = 3 * k - 2 * d
    return k, d, j

def find_swing_points(highs, lows, volumes, closes, n=2, min_pct=3.0):
    """分形摆动点检测：识别高低点交替序列（swing high/low）
    - n: 左右确认K线数（2 = 5根K线确认一个分形点）
    - min_pct: 相邻摆动点最小变动幅度%（过滤锯齿噪声）
    返回: [{'type':'H'/'L', 'idx', 'price', 'vol', 'close'}, ...]（时间升序、H/L 交替）
    """
    pts = []
    for i in range(n, len(highs) - n):
        # 分形高点：high[i] 严格高于左右各 n 根
        h_win_l, h_win_r = highs[i - n:i], highs[i + 1:i + n + 1]
        if h_win_l and h_win_r and highs[i] > max(h_win_l) and highs[i] > max(h_win_r):
            pts.append({'type': 'H', 'idx': i, 'price': highs[i], 'vol': volumes[i], 'close': closes[i]})
        # 分形低点：low[i] 严格低于左右各 n 根
        l_win_l, l_win_r = lows[i - n:i], lows[i + 1:i + n + 1]
        if l_win_l and l_win_r and lows[i] < min(l_win_l) and lows[i] < min(l_win_r):
            pts.append({'type': 'L', 'idx': i, 'price': lows[i], 'vol': volumes[i], 'close': closes[i]})
    pts.sort(key=lambda p: p['idx'])

    # 合并相邻同类型点（保留更极端值）
    merged = []
    for p in pts:
        if merged and merged[-1]['type'] == p['type']:
            if (p['type'] == 'H' and p['price'] > merged[-1]['price']) or \
               (p['type'] == 'L' and p['price'] < merged[-1]['price']):
                merged[-1] = p
        else:
            merged.append(p)

    # 幅度过滤：相邻异类点变动 < min_pct → 视为噪声，丢弃当前点（保留前一个）
    filtered = []
    for p in merged:
        if filtered:
            prev = filtered[-1]
            if prev['type'] != p['type']:
                chg = abs(p['price'] - prev['price']) / prev['price'] * 100
                if chg < min_pct:
                    continue
        filtered.append(p)

    # 丢弃可能产生的相邻同类型点，再合并一次
    final = []
    for p in filtered:
        if final and final[-1]['type'] == p['type']:
            if (p['type'] == 'H' and p['price'] > final[-1]['price']) or \
               (p['type'] == 'L' and p['price'] < final[-1]['price']):
                final[-1] = p
        else:
            final.append(p)
    return final


_FLOAT_SHARES_CACHE = {}


def fetch_float_shares(code):
    """流通股本（股）——P1a 换手率口径数据源（2026-09-02 量价战法语档落地）
    数据源：东财 push2 stock/get f117(流通市值) / f43(最新价) 反推；
    主域名 push2 常被限流返回空包，走数字镜像(83.) HTTP（已验证 2026-09-02）。
    进程内缓存（流通股本短周期静态）。失败返回 None（调用方须降级处理）。
    """
    if code in _FLOAT_SHARES_CACHE:
        return _FLOAT_SHARES_CACHE[code]
    try:
        secid = (1 if code.startswith('sh') else 0)
        pure = code[2:] if len(code) == 8 else code
        url = (f"http://83.push2.eastmoney.com/api/qt/stock/get?"
               f"secid={secid}.{pure}&fields=f43,f117")
        r = subprocess.run(f'curl -s --connect-timeout 5 --max-time 8 '
                           f'-H "Referer: https://quote.eastmoney.com/" "{url}"',
                           shell=True, capture_output=True, timeout=12)
        j = json.loads(r.stdout.decode('utf-8', errors='replace'))
        d = j.get('data') or {}
        price = (d.get('f43') or 0) / 100
        float_mv = d.get('f117') or 0
        if price > 0 and float_mv > 0:
            fs = float_mv / price
            _FLOAT_SHARES_CACHE[code] = fs
            return fs
    except Exception:
        pass
    _FLOAT_SHARES_CACHE[code] = None
    return None


def _classify_surge_day(opens, highs, lows, closes, idx, surge_vol, base_vol):
    """P2a（2026-09-02 战法一"放量性质"）K线质量分型——只作输出字段，不并评分。
    战法一操作要点(4)：放量性质分 ①上攻启动 ②庄家进庄 ③试盘。量化口径：
    - 上攻启动：量≥2x + 强势阳线（收盘位置≥0.7）+ 突破前日高点
    - 疑似试盘：量 1.5-2x 或长上影（≥当日区间 35%）——试探抛压
    - 进庄型放量：阳线、量适中、收盘稳
    各分型后续 5 日表现差异待回测验证后再决定是否并入置信度（2026-09-02 标注）。
    """
    try:
        if idx < 1 or idx >= len(closes):
            return ''
        last_o, last_h, last_l, last_c = opens[idx], highs[idx], lows[idx], closes[idx]
        prior_h = highs[idx - 1]
        rng = max(last_h - last_l, 0.001)
        upper_shadow = (last_h - max(last_c, last_o)) / rng
        close_pos = (last_c - last_l) / rng
        vr = surge_vol / base_vol if base_vol > 0 else 0
        if vr >= 2.0 and close_pos >= 0.7 and last_c > prior_h:
            return '上攻启动'
        if vr < 2.0 or upper_shadow >= 0.35:
            return '疑似试盘'
        return '进庄型放量'
    except Exception:
        return ''


def analyze_wave_pattern(code):
    """波段结构与浪数识别（多波形态，不限于两波）
    基于分形摆动点把日K切分为"上涨浪/回调浪"交替序列，并统计：
    - 浪数：近60日识别出几波上涨、当前处于第几浪
    - 每浪量能特征：均量、回调浪量能衰减比（回调均量/前上涨浪均量）
    - 当前状态：上涨中 / 回调中 / 破位
    - 形态信号：缩量回调到位+放量启动 / 放量下跌出货风险 / 高位缩量背离
    P1a（2026-09-02 量价战法语档）：回调浪增加换手率绝对水平（turnover_avg），
    衰减比是相对口径（比值与量纲无关），真缩量须叠加换手率绝对确认——
    高换手股 0.5x 衰减仍在活跃换手，非文档所指"均匀缩量"。
    """
    result = {'available': False, 'wave_count': 0, 'current_wave': 0, 'state': '',
              'signal': '', 'detail': '', 'waves': []}
    try:
        kline = fetch_kline_sina(code, 240, 60)
        if not kline or len(kline) < 20:
            return result
        closes = [float(d['close']) for d in kline]
        highs = [float(d['high']) for d in kline]
        lows = [float(d['low']) for d in kline]
        opens = [float(d['open']) for d in kline]  # P2a（2026-09-02）K线质量分型需要开盘价
        volumes = [float(d.get('volume', 0)) for d in kline]

        pts = find_swing_points(highs, lows, volumes, closes, n=2, min_pct=3.0)
        if len(pts) < 3:
            return result

        # 构建波段：L→H 上涨浪，H→L 回调浪
        waves = []
        for i in range(1, len(pts)):
            a, b = pts[i - 1], pts[i]
            if a['type'] == b['type']:
                continue
            kind = 'up' if (a['type'] == 'L' and b['type'] == 'H') else 'down'
            seg_vols = volumes[a['idx']:b['idx'] + 1]
            avg_vol = sum(seg_vols) / len(seg_vols) if seg_vols else 0
            waves.append({
                'kind': kind, 'start_idx': a['idx'], 'end_idx': b['idx'],
                'start_price': a['price'], 'end_price': b['price'],
                'pct': (b['price'] - a['price']) / a['price'] * 100,
                'days': b['idx'] - a['idx'], 'avg_vol': round(avg_vol, 0),
            })

        # 量能衰减比：回调浪均量 / 前一个上涨浪均量（<0.5 缩量到位，>1.2 放量下跌）
        # P1a（2026-09-02）：换手率口径双轨——衰减比是相对量（量纲无关，保留原阈值），
        # 新增回调浪日均换手率（turnover_avg %）作绝对确认：高换手股 0.5x 衰减仍在
        # 活跃换手，文档"真缩量"要求换手率绝对水平同步走低（阈值 2%/4% 分档，
        # 待 P1a 回测校准后并入评分）
        float_shares = fetch_float_shares(code)
        for i, w in enumerate(waves):
            if w['kind'] == 'down' and i > 0 and waves[i - 1]['avg_vol'] > 0:
                w['vol_decay'] = round(w['avg_vol'] / waves[i - 1]['avg_vol'], 2)
                if float_shares:
                    w['turnover_avg'] = round(w['avg_vol'] / float_shares * 100, 2)
            else:
                w['vol_decay'] = None
                if float_shares:
                    w['turnover_avg'] = round(w['avg_vol'] / float_shares * 100, 2)

        # 浪数计数：已确认上涨浪数 + 当前进行中的浪
        up_waves = [w for w in waves if w['kind'] == 'up']
        wave_count = len(up_waves)
        last = pts[-1]
        cur_price = closes[-1]

        # 当前状态
        if last['type'] == 'L':
            state = '破位（跌破最近回调低点）' if cur_price < last['price'] else '上涨中'
        else:
            state = '回调中'

        # 当前处于第几浪（进行中的上涨浪 +1）
        current_wave = wave_count
        if last['type'] == 'L' and cur_price >= last['price']:
            current_wave = wave_count + 1

        # ── P1b（2026-09-02 量价战法语档）：距前高回撤深度 → 连续置信度变量 ──
        # 战法一操作要点(3)：低位放量区域与前期高点距离越远，空头能量消耗越充分，
        # 多头确认可靠性越强。用 240 日窗口最高价计算当前回撤深度，输出连续百分比；
        # 分档 30/40/50% 的单调性待回测验证（未验证前只作展示变量，不直接并评分）。
        high_240 = max(highs) if highs else cur_price
        retrace_from_high = round((high_240 - cur_price) / high_240 * 100, 1) if high_240 > 0 else 0

        # ── 市场环境过滤：参考指数 MA20/MA60 状态（与市场环境过滤器同口径）──
        # sz300/301→创业板指, sh688/689→科创50, 其余→上证指数
        market_status = 'unknown'
        market_ref = '大盘'
        try:
            if code.startswith('sz300') or code.startswith('sz301'):
                ref_code, market_ref = 'sz399006', '创业板指'
            elif code.startswith('sh688') or code.startswith('sh689'):
                ref_code, market_ref = 'sh000688', '科创50'
            else:
                ref_code, market_ref = 'sh000001', '上证指数'
            env_kline = fetch_kline_sina(ref_code, 240, 60)
            if env_kline and len(env_kline) > 20:
                env_closes = [float(d['close']) for d in env_kline]
                env_ma20 = calc_ma(env_closes, 20)
                env_ma60 = calc_ma(env_closes, 60)
                if env_ma60 and env_closes[-1] < env_ma60:
                    market_status = 'bear'
                elif env_ma20 and env_closes[-1] < env_ma20:
                    market_status = 'weak'
                else:
                    market_status = 'healthy'
        except Exception:
            pass

        # ── 形态信号（任务3）──
        signal = ''
        detail_parts = []
        cur_wave = waves[-1] if waves else None
        if state == '上涨中':
            # 回调低点后新上涨段（尚未确认成浪）：检查启动量能
            seg_vols = volumes[last['idx'] + 1:]
            start_vol = seg_vols[0] if seg_vols else 0
            prev_down = cur_wave if cur_wave and cur_wave['kind'] == 'down' else None
            base_vol = prev_down['avg_vol'] if prev_down else 0
            gain = (closes[-1] - last['price']) / last['price'] * 100 if last['price'] else 0
            if base_vol > 0 and start_vol / base_vol > 1.5:
                surge_day_quality = _classify_surge_day(opens, highs, lows, closes, last['idx'] + 1, start_vol, base_vol)
                # P0（2026-09-02 战法二）：浪位护栏文案——第2浪起标注防出货警示
                wave_tag = '·高位浪启动谨慎(防出货)' if current_wave >= 3 else ('·第2浪启动谨慎(防出货)' if current_wave == 2 else '')
                signal = (f"🟢 第{current_wave}浪放量启动（首日量{start_vol / base_vol:.1f}x，自低点{gain:+.1f}%）{wave_tag}"
                          + (f"·{surge_day_quality}" if surge_day_quality else ''))
            else:
                signal = f"🟢 第{current_wave}浪启动中（自低点{gain:+.1f}%）"
        elif cur_wave and cur_wave['kind'] == 'down' and cur_wave.get('vol_decay') is not None:
            decay = cur_wave['vol_decay']
            retrace = abs(cur_wave['pct'])
            prev_up = waves[-2] if len(waves) >= 2 else None
            prev_gain = prev_up['pct'] if prev_up and prev_up['kind'] == 'up' else 0
            detail_parts.append(f"回调量能衰减 {int(decay * 100)}%")
            # P1a（2026-09-02）：换手率绝对确认——衰减比合格但换手率仍高（≥4%）=
            # 活跃换手非真缩量，信号降级；换手率 <2% = 真缩量到位，确认加强
            to_avg = cur_wave.get('turnover_avg')
            to_tag = ''
            if to_avg is not None:
                detail_parts.append(f"回调日均换手 {to_avg:.2f}%")
                if to_avg < 2:
                    to_tag = '·换手率确认真缩量✅'
                elif to_avg >= 4:
                    to_tag = '·换手率仍活跃，缩量存疑'
            if decay < 0.5 and prev_gain > 0 and retrace < prev_gain * 0.6:
                if to_avg is not None and to_avg >= 4:
                    signal = (f"🟡 缩量回调到位（量能{int(decay * 100)}%）但换手率{to_avg:.1f}%仍活跃，"
                              f"第{current_wave + 1}浪确认存疑")
                else:
                    signal = f"🟢 缩量回调到位（量能{int(decay * 100)}%）{to_tag}，有望第{current_wave + 1}浪启动"
            elif decay > 1.2:
                signal = '⚠️ 放量下跌，警惕回调变反转（疑似出货）'
            else:
                signal = f"🟡 回调中（量能衰减{int(decay * 100)}%）{to_tag}，等待缩量到位"
        elif cur_wave and cur_wave['kind'] == 'up':
            # 上涨浪中：检查启动量能（本浪首日 vs 前回调浪均量）
            start_vol = volumes[cur_wave['start_idx']]
            prev_w = waves[-2] if len(waves) >= 2 else None
            prev_avg = prev_w['avg_vol'] if prev_w else 0
            if prev_avg > 0 and start_vol / prev_avg > 1.5:
                surge_day_quality = _classify_surge_day(opens, highs, lows, closes, cur_wave['start_idx'], start_vol, prev_avg)
                wave_tag = '·高位浪启动谨慎(防出货)' if current_wave >= 3 else ('·第2浪启动谨慎(防出货)' if current_wave == 2 else '')
                signal = (f"🟢 第{current_wave}浪放量启动（首日量{start_vol / prev_avg:.1f}x）{wave_tag}"
                          + (f"·{surge_day_quality}" if surge_day_quality else ''))
            else:
                signal = f"🟢 第{current_wave}浪上涨中（涨幅{cur_wave['pct']:+.1f}%）"

        # ── 资金面确认：当日主力净额标注，过滤游资一日游 ──
        main_net = None
        main_src = ''
        try:
            mf = fetch_main_flow(code)
            if mf:
                main_net = mf.get('main_net')
                main_src = mf.get('data_source', '')
        except Exception:
            pass
        if main_net is not None and signal:
            is_est = '估算' in main_src
            if is_est:
                signal += f"（主力{main_net:+.2f}亿·估算源）"
            elif main_net > 0:
                if '疑似出货' in signal:
                    signal = '🟡 ' + signal[2:] + f"，但主力净流入{main_net:+.2f}亿（流出存疑）"
                else:
                    signal += f"，主力净流入{main_net:+.2f}亿✅"
            else:
                if signal.startswith('🟢'):
                    signal = '🟡 ' + signal[2:] + f"，但主力净流出{abs(main_net):.2f}亿⚠️警惕一日游"
                elif '疑似出货' in signal:
                    signal = '🔴 ' + signal[2:] + f"，主力净流出{abs(main_net):.2f}亿确认出货"
                else:
                    signal += f"，主力净流出{abs(main_net):.2f}亿"

        # ── 市场环境过滤：熊市降级看多信号，过滤假突破 ──
        if market_status == 'bear':
            if signal.startswith('🟢'):
                signal = '🟡 ' + signal[2:] + f"，但{market_ref}在MA60下方（环境偏空，信号存疑）"
            else:
                signal += f"，{market_ref}偏空"
        elif market_status == 'healthy' and signal.startswith('🟢'):
            signal += f"，{market_ref}环境偏多✅"

        result.update({
            'available': True,
            'wave_count': wave_count, 'current_wave': current_wave, 'state': state,
            'signal': signal, 'waves': waves,
            'detail': ' | '.join(detail_parts),
            'main_net': main_net, 'main_flow_source': main_src,
            'market_status': market_status, 'market_ref': market_ref,
            # P1b（2026-09-02）：距240日前高回撤深度 %（连续置信度变量，展示为主）
            'retrace_from_high': retrace_from_high,
            'high_240': round(high_240, 2),
        })
    except Exception as e:
        log.warning(f"波段识别失败: {e}")
    return result


def analyze_relative_strength(code):
    """相对强弱 RS：个股 vs 所属板块/大盘指数的超额收益
    参考指数映射（与市场环境过滤器一致）：
      sz300/sz301 → 创业板指(399006), sh688/sh689 → 科创50(000688), 其余 → 上证指数(000001)
    RS = 个股区间涨幅 - 参考指数区间涨幅；>0 跑赢大盘，<0 跑输大盘
    """
    result = {'rs_5': None, 'rs_20': None, 'rs_today': None, 'ref_index': '', 'signal': ''}
    try:
        # 参考指数映射
        if code.startswith('sz300') or code.startswith('sz301'):
            ref_code, ref_name = 'sz399006', '创业板指'
        elif code.startswith('sh688') or code.startswith('sh689'):
            ref_code, ref_name = 'sh000688', '科创50'
        else:
            ref_code, ref_name = 'sh000001', '上证指数'
        result['ref_index'] = ref_name

        stock_k = fetch_kline_sina(code, 240, 60)
        ref_k = fetch_kline_sina(ref_code, 240, 60)
        if not stock_k or len(stock_k) < 21 or not ref_k or len(ref_k) < 21:
            return result

        stock_c = [float(d['close']) for d in stock_k]
        ref_c = [float(d['close']) for d in ref_k]

        def ret(arr, idx):
            """N日区间涨跌幅（含当日）"""
            if len(arr) < idx + 1:
                return None
            base = arr[-idx - 1]
            return (arr[-1] - base) / base * 100 if base else None

        st, sf5, sf20 = ret(stock_c, 1), ret(stock_c, 5), ret(stock_c, 20)
        rt, rf5, rf20 = ret(ref_c, 1), ret(ref_c, 5), ret(ref_c, 20)
        if st is not None and rt is not None:
            result['rs_today'] = round(st - rt, 2)
        if sf5 is not None and rf5 is not None:
            result['rs_5'] = round(sf5 - rf5, 2)
        if sf20 is not None and rf20 is not None:
            result['rs_20'] = round(sf20 - rf20, 2)

        # 信号：20日RS为主，5日RS为确认
        rs20, rs5 = result['rs_20'], result['rs_5']
        if rs20 is not None and rs5 is not None:
            if rs20 > 10 and rs5 > 0:
                result['signal'] = '🟢 强势（跑赢大盘）'
            elif rs20 < -10 and rs5 < 0:
                result['signal'] = '🔴 弱势（跑输大盘）'
            else:
                result['signal'] = '⚪ 与大盘同步'
    except Exception as e:
        log.warning(f"相对强弱分析失败: {e}")
    return result


def analyze_technical(code, price):
    """技术面多指标分析（含日线+分钟级+主升擒龙）"""
    kline = fetch_kline_sina(code, 240, 60)
    if not kline or len(kline) < 10:
        return {'error': 'K线数据不足'}
    
    closes = [float(d['close']) for d in kline]
    volumes = [float(d['volume']) for d in kline]
    highs = [float(d['high']) for d in kline]
    lows = [float(d['low']) for d in kline]
    opens = [float(d['open']) for d in kline]
    
    # ── 分钟级K线分析 ──
    min_data = {}
    for scale_name, scale_val in [('60min', 60), ('30min', 30)]:
        mk = fetch_kline_sina(code, scale_val, 48)
        if mk and len(mk) > 10:
            mc = [float(d['close']) for d in mk]
            mdif, mdea, mmacd = calc_macd(mc)
            mrsi = calc_rsi(mc, 14)
            mma5 = calc_ma(mc, 5)
            mma10 = calc_ma(mc, 10)
            mma20 = calc_ma(mc, 20)
            min_data[scale_name] = {
                'macd': mmacd, 'dif': mdif, 'dea': mdea, 'rsi': mrsi,
                'ma5': mma5, 'ma10': mma10, 'ma20': mma20,
                'price': mc[-1], 'pct': (mc[-1] - mc[-2]) / mc[-2] * 100 if len(mc) > 1 else 0
            }
    
    # ── 基础指标 ──
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    rsi = calc_rsi(closes, 14)
    if rsi is None:
        rsi = 50  # 数据不足时用中性值，但标记 rsi_available=False 供报告层提示
        rsi_available = False
    else:
        rsi_available = True
    # 极值反转层（Workbuddy 复盘报告 Layer 1）：RSI_2 < 10 时禁止看空
    rsi_2 = calc_rsi(closes, 2)
    reversal_watch = False
    if rsi_2 is not None and rsi_2 < 10:
        reversal_watch = True
    dif, dea, macd = calc_macd(closes)
    boll_up, boll_mid, boll_dn = calc_boll(closes)
    k, d, j = calc_kdj(closes)

    # 均线多头计数
    bull_count = sum(1 for ma in [ma5, ma10, ma20, ma60] if ma and price > ma)
    if bull_count >= 3: trend = '多头'
    elif bull_count >= 1: trend = '震荡'
    else: trend = '空头'

    # 量比
    vol_ratio = volumes[-1] / (sum(volumes[-6:-1]) / 5) if len(volumes) >= 6 else 1

    # 支撑/压力位
    support = min(ma5 or 0, ma10 or 0, ma20 or 0, boll_dn or 0)
    resistance = max(ma5 or 0, ma10 or 0, ma20 or 0, boll_up or 0)
    high_60d = max(highs)
    low_60d = min(lows)

    # ── 新增1：加权均价 JJ（主升擒龙） ──
    # JJ = (C*3 + H + L + O) / 6，比单用收盘价更抗噪
    jj = (closes[-1] * 3 + highs[-1] + lows[-1] + opens[-1]) / 6

    # ── 新增2：多空线 A1/B1（主升擒龙核心） ──
    # A1 = EMA(C,7) - EMA(C,21)  快慢EMA差，反映短期多空力度
    # B1 = EMA(0.668*REF(A1,1) + 0.333*A1, 1)  平滑后的多空基准
    ema7 = calc_ema(closes, 7)
    ema21 = calc_ema(closes, 21)
    a1 = None
    b1 = None
    if ema7 and ema21 and len(ema7) == len(ema21) and len(ema7) >= 3:
        a1_series = [ema7[i] - ema21[i] for i in range(len(ema7))]
        a1 = a1_series[-1]
        # B1 = EMA(0.668*REF(A1,1) + 0.333*A1, 1) 简化：取最近两期加权
        b1 = 0.668 * a1_series[-2] + 0.333 * a1_series[-1] if len(a1_series) >= 2 else a1

    # 多空方向判断
    duo_kong = '做多' if (a1 is not None and b1 is not None and a1 >= b1) else '做空' if (a1 is not None and b1 is not None) else '未知'

    # ── 新增3：量价动态均线 DMA + ABC3 主力强度（主升擒龙） ──
    # ABC1 = V / SUM(V,13)  日成交量占13日总成交量比例
    # ABC2 = DMA(C, ABC1)  以量比为权重的动态均价
    # ABC3 = (C-ABC2)/ABC2*40  价格偏离度→主力强度
    abc3 = 0
    if len(volumes) >= 13 and len(closes) >= 13:
        vol_sum_13 = sum(volumes[-13:])
        if vol_sum_13 > 0:
            # ABC1: 过去13天每天的成交量占比
            abc1_series = [v / vol_sum_13 for v in volumes[-13:]]
            # ABC2: DMA = Σ(P_i * W_i)
            abc2 = calc_dma(closes[-13:], abc1_series)
            if abc2 and abc2 > 0:
                abc3 = (closes[-1] - abc2) / abc2 * 40

    # ── 新增4：L2均价偏离度 HG（主升擒龙） ──
    # L2 = MA(AMOUNT/(100*V), 13)  13日均价
    # HG = (C-L2)/L2*100  价格偏离百分比
    hg = 0
    if len(volumes) >= 13 and len(closes) >= 13:
        # 检查kline是否有amount字段，有则用，无则用close*volume*100近似
        has_amount = 'amount' in kline[0] and kline[0]['amount']
        avg_prices = []
        for i in range(13):
            if has_amount:
                a = float(kline[-13 + i]['amount'])
            else:
                a = float(kline[-13 + i]['close']) * float(kline[-13 + i]['volume']) * 100
            v = volumes[-13 + i]
            avg_prices.append(a / (v * 100) if v > 0 else closes[-13 + i])
        l2 = sum(avg_prices) / 13
        if l2 > 0:
            hg = (closes[-1] - l2) / l2 * 100

    # ── 新增5：强势信号（主升擒龙综合共振） ──
    # 条件: HG>5 AND DIFF>DEA AND C>=EMA(C,5) AND A1>B1
    ema5 = calc_ma(closes, 5)  # 用简单MA近似EMA(C,5)
    qiang_shi = False
    if (hg > 5 and macd > 0 and a1 is not None and b1 is not None
            and a1 > b1 and ema5 and closes[-1] >= ema5):
        qiang_shi = True

    # ★强信号：强势且A1刚上穿B1（趋势由空转多）
    qiang_jin_qiang = False
    if (qiang_shi and a1 is not None and b1 is not None
            and len(ema7) >= 3 and len(ema21) >= 3):
        prev_a1 = (ema7[-2] - ema21[-2])
        prev_b1 = 0.668 * prev_a1 + 0.333 * (ema7[-3] - ema21[-3]) if len(ema7) >= 3 else prev_a1
        if prev_a1 <= prev_b1:  # 前一日为空头
            qiang_jin_qiang = True

    # ── 新增6：量价背离检测 ──
    # 顶背离: 价格走高但成交量走低 / MACD走低
    # 底背离: 价格走低但成交量走高 / MACD走高
    top_divergence = False
    bottom_divergence = False
    if len(closes) >= 20 and len(volumes) >= 20:
        # 检查过去20天是否有顶背离
        p20_max = max(closes[-20:])
        p20_max_idx = closes[-20:].index(p20_max)
        v20_at_max = volumes[-20 + p20_max_idx]
        v20_avg = sum(volumes[-20:]) / 20
        # 如果最近3天价格接近20日高点但成交量显著低于高点时的量
        if closes[-1] >= p20_max * 0.98 and volumes[-1] < v20_at_max * 0.7:
            top_divergence = True
        
        # 检查过去20天是否有底背离
        p20_min = min(closes[-20:])
        p20_min_idx = closes[-20:].index(p20_min)
        v20_at_min = volumes[-20 + p20_min_idx]
        # 如果最近3天价格接近20日低点但成交量显著高于低点时的量
        if closes[-1] <= p20_min * 1.02 and volumes[-1] > v20_at_min * 1.5:
            bottom_divergence = True
    
    # MACD顶背离/底背离（使用完整 dif 序列）
    macd_top_div = False
    macd_bottom_div = False
    if len(closes) >= 30:
        # 计算完整 dif 序列
        ef_full = calc_ema(closes, 12)
        es_full = calc_ema(closes, 26)
        if ef_full and es_full:
            min_len = min(len(ef_full), len(es_full))
            dif_series = [ef_full[i] - es_full[i] for i in range(min_len)]
            # 取最近 30 个数据点
            closes_30 = closes[-30:]
            dif_30 = dif_series[-30:] if len(dif_series) >= 30 else dif_series
            if len(dif_30) >= 20:
                # 找最近 20 天内的两个高点
                p_high1 = max(closes_30[:15])  # 前半段最高价
                p_high1_idx = closes_30[:15].index(p_high1)
                p_high2 = max(closes_30[15:])  # 后半段最高价
                p_high2_idx = 15 + closes_30[15:].index(p_high2)
                # 顶背离：价格创新高但 DIF 未创新高
                if p_high2 > p_high1 and dif_30[p_high2_idx] < dif_30[p_high1_idx]:
                    macd_top_div = True
                # 找最近 20 天内的两个低点
                p_low1 = min(closes_30[:15])
                p_low1_idx = closes_30[:15].index(p_low1)
                p_low2 = min(closes_30[15:])
                p_low2_idx = 15 + closes_30[15:].index(p_low2)
                # 底背离：价格创新低但 DIF 未创新低
                if p_low2 < p_low1 and dif_30[p_low2_idx] > dif_30[p_low1_idx]:
                    macd_bottom_div = True

    # ── P2b（2026-09-02 战法二"多头强势背离"）：区域性量价背离检测 ──
    # 战法二操作要点(1)：充分理解"区域性"量价背离——区别于单点比较，取
    # 10 日窗口的价格区域（均值±极值）vs 量能区域做对比，抗单根K线噪声。
    # 正背离（多头）：后窗价格中枢 ≥ 前窗 + 后窗量能 < 前窗 60% → 推高意愿坚决+抛压衰竭
    # 负背离（空头）：后窗价格中枢 ≤ 前窗 + 后窗量能 > 前窗 1.4x → 下跌放量，抛压仍在
    # 注意：本项只作展示字段，是否并入评分待回测验证（2026-09-02 标注）
    regional_div = None
    try:
        if len(closes) >= 25 and len(volumes) >= 25:
            w = 10
            prev_p = closes[-(2 * w):-w]
            cur_p = closes[-w:]
            prev_v = volumes[-(2 * w):-w]
            cur_v = volumes[-w:]
            pp_lo, pp_hi = min(prev_p), max(prev_p)
            cp_lo, cp_hi = min(cur_p), max(cur_p)
            pv_avg = sum(prev_v) / len(prev_v)
            cv_avg = sum(cur_v) / len(cur_v)
            if pv_avg > 0:
                ratio_v = cv_avg / pv_avg
                if cp_lo >= pp_hi * 0.99 and ratio_v < 0.6:
                    regional_div = ('正背离', round(ratio_v, 2),
                                    f"后10日价区{cp_lo:.2f}-{cp_hi:.2f}未回前区{pp_lo:.2f}-{pp_hi:.2f} 量能{int(ratio_v * 100)}%")
                elif cp_hi <= pp_lo * 1.01 and ratio_v > 1.4:
                    regional_div = ('负背离', round(ratio_v, 2),
                                    f"后10日价区{cp_lo:.2f}-{cp_hi:.2f}未离前区{pp_lo:.2f}-{pp_hi:.2f} 量能{int(ratio_v * 100)}%")
    except Exception:
        regional_div = None

    # 量价/MACD背离综合判定
    divergence = '无背离'
    if macd_top_div:
        divergence = '⚠️ MACD顶背离（价新高DIF未新高，警惕回调）'
    elif macd_bottom_div:
        divergence = '🟢 MACD底背离（价新低DIF未新低，关注反弹）'
    elif top_divergence:
        divergence = '⚠️ 顶背离（价升量缩，警惕回调）'
    elif bottom_divergence:
        divergence = '🟢 底背离（价跌量增，关注反弹）'
    # P2b：区域背离为附加维度（不与点式背离竞争主判定位），独立展示
    if regional_div and '无背离' != divergence:
        _rkind, _rv, _rdesc = regional_div
        divergence += f"｜区域{_rkind}（{_rdesc}，P2b待验证）"
    elif regional_div:
        _rkind, _rv, _rdesc = regional_div
        divergence = f"区域{_rkind}（{_rdesc}，P2b待验证）"

    # ── 新增7：市场环境过滤器（大盘 + 创业板/科创 双因子）──
    # 旧逻辑只看上证指数，对创业板/科创板个股适用性差
    # 新逻辑：同时看上证 + 个股所属板块指数，取更严格者
    market_signal = ''
    # 板块指数映射：根据代码前缀判断所属板块
    sector_index_code = None
    sector_index_name = ''
    if code.startswith('sz300') or code.startswith('sz301'):
        sector_index_code = 'sz399006'  # 创业板指
        sector_index_name = '创业板指'
    elif code.startswith('sh688') or code.startswith('sh689'):
        sector_index_code = 'sh000688'  # 科创50
        sector_index_name = '科创50'
    # 主板个股（sh600/601/603, sz000/002）主要看上证

    try:
        # 因子1：上证指数
        sh_kline = fetch_kline_sina('sh000001', 240, 60)
        sh_status = 'unknown'  # healthy / weak / bear
        if sh_kline and len(sh_kline) > 20:
            sh_closes = [float(d['close']) for d in sh_kline]
            sh_ma20 = calc_ma(sh_closes, 20)
            sh_ma60 = calc_ma(sh_closes, 60)
            sh_price = sh_closes[-1]
            # ⚠️ 2026-08-13 修复：站回 MA20 即解除熊市压制。
            # 原逻辑"price < MA60 即 bear"导致指数刚从深跌反弹站回 MA20 上方、
            # 但仍低于 MA60 时，被误判为熊市并机械压空（8/12、8/13 连续两天误判）。
            # 新逻辑：MA20 与 MA60 双下方才算真 bear；MA20 下方但 MA60 上方=weak；
            # 站回 MA20 上方=healthy（解除压制）。
            if sh_ma20 and sh_price < sh_ma20:
                if sh_ma60 and sh_price < sh_ma60:
                    sh_status = 'bear'
                else:
                    sh_status = 'weak'
            else:
                sh_status = 'healthy'

        # 因子2：板块指数（创业板/科创板个股才看）
        sector_status = 'unknown'
        if sector_index_code:
            sec_kline = fetch_kline_sina(sector_index_code, 240, 60)
            if sec_kline and len(sec_kline) > 20:
                sec_closes = [float(d['close']) for d in sec_kline]
                sec_ma20 = calc_ma(sec_closes, 20)
                sec_ma60 = calc_ma(sec_closes, 60)
                sec_price = sec_closes[-1]
                # ⚠️ 2026-08-13 修复：与上证一致，站回 MA20 即解除压制（见 sh_status 注释）
                if sec_ma20 and sec_price < sec_ma20:
                    if sec_ma60 and sec_price < sec_ma60:
                        sector_status = 'bear'
                    else:
                        sector_status = 'weak'
                else:
                    sector_status = 'healthy'

        # 择时原则：板块优先（创业板/科创板个股主要看板块指数），
        # 板块不可用时才用大盘。这样弱势市场中的强势板块不会被误伤。
        if sector_status != 'unknown':
            primary_status = sector_status
            primary_name = sector_index_name
        else:
            primary_status = sh_status
            primary_name = '大盘'

        if primary_status == 'bear':
            parts = [f'⚠️ {primary_name}在MA60下方，环境偏空']
            market_signal = '，'.join(parts)
        elif primary_status == 'weak':
            parts = [f'📉 {primary_name}在MA20下方，短期偏弱']
            market_signal = '，'.join(parts)
        elif primary_status == 'healthy':
            market_signal = f'📈 {primary_name}在MA20上方，环境偏多'
        # worst == 'unknown' 时 market_signal 保持空字符串

        # ⚠️ 2026-08-12 修复：板块/大盘状态用独立字段传递，不再拼进 market_signal
        # 原实现 market_signal += '|sector=...|sh=...' 会污染下游字符串匹配
        # （calculate_quant_score 用 'MA60' in ms / re.search('在MA', ms) 提取名称，会带出 '⚠️ 创业板指' 双emoji）
        # sector_status / sh_status 均在作用域内已定义（unknown 兜底）
    except Exception as e:
        log.warning(f"市场环境过滤失败: {e}")

    # 相对强弱 RS（个股 vs 参考指数超额收益，供评分与报告展示）
    rs = analyze_relative_strength(code)

    # 波段结构与浪数识别（多波形态，供评分与报告展示）
    wave = analyze_wave_pattern(code)

    # ── 筹码分布分析（2026-08-12 新增：结合 K线走势看底部筹码锁定与成本区）──
    chip = analyze_chip_distribution(kline)
    chip_60 = analyze_chip_60min(code) if code else {}

    # ── P0-2 顶部止盈系统化（专家团规则2，2026-08-14 沉淀 / 2026-08-20 修正）──
    # 规则2（修正版）：乖离中轨>15% 或 KDJ_J>100 → 强制减半信号（独立触发，不再要求站上BOLL上轨）
    # 修正背景（2026-08-20 实盘教训）：8/20 瑞丰乖离中轨25.4%>15% 但竞价18.66未站上上轨19.02，
    #   旧条件"价>上轨 且 乖离>15%"未触发→盘前仍给看多66.7→当日-9.62%暴跌。
    #   乖离>15% 本身就是超买风险信号，不应以"是否站上上轨"为前置。
    # 实盘验证：某标的 08-14 +16.09% 突破上轨15.97、乖离22.4%、KDJ_J>95 → 触发减半提示
    mid_dev = (closes[-1] - boll_mid) / boll_mid * 100 if boll_mid else 0
    top_overflow = False
    top_overflow_reason = ''
    if mid_dev > 15 or (j is not None and j > 100):
        top_overflow = True
        reasons = []
        if mid_dev > 15:
            reasons.append(f'乖离中轨{mid_dev:.1f}%>15%')
        if j is not None and j > 100:
            reasons.append(f'KDJ_J={j:.0f}>100')
        top_overflow_reason = '；'.join(reasons)

    # ── P0-1 反弹可靠性判定（专家团规则9，2026-08-14 沉淀）──
    # 规则9：自高位(>BOLL上轨)回落后出现的单日反弹，若融资余额同步下降且RSI未极端超卖
    # → 判定为"减仓反弹而非新升浪"，不上调评级
    # 实盘验证：某标的 08-13 +5.79% 反弹、融资4.27→4.15亿降、08-14 -9.98%跌停 → 减仓反弹
    bounce_suspect = False
    bounce_reason = ''
    if len(closes) >= 11 and len(highs) >= 11:
        recent_high = max(highs[-11:-1])  # 近10日高点（不含今日）
        drop_from_high = (closes[-1] - recent_high) / recent_high * 100
        # 曾触及/接近上轨（近10日高点>=上轨*0.98）且当前距高点回落>5%
        hit_upper = bool(boll_up) and recent_high >= boll_up * 0.98
        # 单日反弹：今日收涨
        today_up = closes[-1] > closes[-2]
        # RSI 未极端超卖（排除"超卖修复性反弹"）
        not_extreme = (rsi_2 is None or rsi_2 > 10) and (rsi is None or rsi > 30)
        if hit_upper and drop_from_high < -5 and today_up and not_extreme:
            bounce_suspect = True
            bounce_reason = (f"近10日高点{recent_high:.2f}触及上轨后回落{drop_from_high:.1f}%，"
                             f"今日反弹但RSI未极端超卖 → 疑似减仓反弹，需资金确认")

    return {
        # 原有指标
        'ma5': ma5, 'ma10': ma10, 'ma20': ma20, 'ma60': ma60,
        'bull_count': bull_count, 'trend': trend,
        'rsi': rsi, 'rsi_available': rsi_available, 'macd': macd, 'dif': dif, 'dea': dea,
        'rsi_2': rsi_2, 'reversal_watch': reversal_watch,
        'boll_up': boll_up, 'boll_mid': boll_mid, 'boll_dn': boll_dn,
        'kdj_k': k, 'kdj_d': d, 'kdj_j': j,
        'vol_ratio': vol_ratio,
        'support': support, 'resistance': resistance,
        'high_60d': high_60d, 'low_60d': low_60d,
        'pct_5d': (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0,
        'pct_20d': (closes[-1] - closes[-21]) / closes[-21] * 100 if len(closes) >= 21 else 0,
        # 新增：主升擒龙指标
        'jj': jj,                          # 加权均价
        'a1': a1, 'b1': b1,               # 多空线
        'duo_kong': duo_kong,              # 多空方向
        'abc3': abc3,                      # 主力强度（价格偏离度）
        'hg': hg,                          # 均价偏离度
        'qiang_shi': qiang_shi,            # 强势信号
        'qiang_jin_qiang': qiang_jin_qiang,# ★强信号
        'divergence': divergence,           # 量价背离
        'top_overflow': top_overflow,       # P0-2 顶部止盈信号（规则2：乖离>15% 或 KDJ_J>100）
        'top_overflow_reason': top_overflow_reason,  # 触发原因说明
        'bounce_suspect': bounce_suspect,   # P0-1 反弹可靠性预警（规则9：高位回落+单日反弹）
        'bounce_reason': bounce_reason,     # 反弹可靠性预警原因
        'market_signal': market_signal,     # 市场环境（纯文本，无 |sector 后缀污染）
        'sector_status': sector_status,     # 板块指数状态 healthy/weak/bear/unknown（独立字段）
        'sh_status': sh_status,             # 上证指数状态 healthy/weak/bear/unknown
        'min_data': min_data,               # 分钟级K线
        # 新增：相对强弱 RS
        'rs_5': rs.get('rs_5'), 'rs_20': rs.get('rs_20'), 'rs_today': rs.get('rs_today'),
        'rs_ref': rs.get('ref_index'), 'rs_signal': rs.get('signal'),
        # 新增：波段结构与浪数
        'wave_count': wave.get('wave_count'), 'current_wave': wave.get('current_wave'),
        'wave_state': wave.get('state'), 'wave_signal': wave.get('signal'),
        'wave_main_net': wave.get('main_net'), 'wave_market': wave.get('market_status'),
        'wave_retrace_high': wave.get('retrace_from_high'),  # P1b（2026-09-02）距前高回撤深度%
        # 新增：筹码分布（日线 + 60分钟）
        'chip_winner_pct': chip.get('winner_pct') if chip else None,
        'chip_cost_peak': chip.get('cost_peak') if chip else None,
        'chip_cost_avg': chip.get('cost_avg') if chip else None,
        'chip_concentration': chip.get('concentration') if chip else None,
        'chip_bottom_lock': chip.get('bottom_lock') if chip else None,
        'chip_note': chip.get('chip_note') if chip else '',
        'chip_buckets': chip.get('buckets', []) if chip else [],
        'chip_60_winner': chip_60.get('winner_pct') if chip_60 else None,
        'chip_60_peak': chip_60.get('cost_peak') if chip_60 else None,
        'chip_60_concentration': chip_60.get('concentration') if chip_60 else None,
        'chip_60_lock': chip_60.get('bottom_lock') if chip_60 else None,
        'chip_60_note': chip_60.get('chip_note') if chip_60 else '',
    }


# ============================================================
# 模块三：基本面分析
# ============================================================
def fetch_stock_industry(code):
    """获取股票所属行业分类（带缓存），失败返回 None"""
    pure = code[2:] if code.startswith(('sh', 'sz')) else code
    if not pure.isdigit() or len(pure) != 6:
        return None
    cache_key = f"ind_{code}"
    cached = _BPS_CACHE.get(cache_key)
    if cached and time.time() - cached[1] < 86400:
        return cached[0]
    try:
        url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
               f"reportName=RPT_F10_ORG_FN_BASIC&"
               f"columns=SECURITY_CODE,INDUSTRY_NAME&"
               f"filter=(SECURITY_CODE=%22{pure}%22)&pageNumber=1&pageSize=1")
        data = _em_http_json(url, headers={'Referer': 'https://emweb.eastmoney.com/'}, timeout=8)
        if data and data.get('success'):
            items = (data.get('result') or {}).get('data', [])
            if items and items[0].get('INDUSTRY_NAME'):
                ind = items[0]['INDUSTRY_NAME'].strip()
                _BPS_CACHE[cache_key] = (ind, time.time())
                return ind
    except Exception as e:
        log.debug(f"fetch_stock_industry 失败: {e}")
    return None


def fetch_valuation_percentile(code):
    """估值历史分位：PE(TTM)/PB(MRQ) 近6年分位
    数据源：东财 datacenter RPT_VALUEANALYSIS_DET（每日估值序列，pageSize=1500 ≈ 6年）
    分位 = 当前值在历史序列中的百分位（0-100）；≤20 历史低位，≥80 历史高位
    带内存缓存（TTL 1天），避免重复请求
    """
    cache_key = f"valpct_{code}"
    cached = _BPS_CACHE.get(cache_key)
    if cached and time.time() - cached[1] < 86400:
        return cached[0]
    result = {'available': False, 'pe_pct': None, 'pb_pct': None, 'pe_now': None, 'pb_now': None, 'days': 0}
    try:
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
               f"reportName=RPT_VALUEANALYSIS_DET&columns=SECURITY_CODE,TRADE_DATE,PE_TTM,PB_MRQ&"
               f"filter=(SECURITY_CODE%3D%22{pure}%22)&pageNumber=1&pageSize=1500&"
               f"sortColumns=TRADE_DATE&sortTypes=-1")
        data = _em_http_json(url, headers={'Referer': 'https://emweb.eastmoney.com/'}, timeout=12)
        if not data or not data.get('success'):
            return result
        items = (data.get('result') or {}).get('data', []) or []
        if not items:
            return result
        pes, pbs = [], []
        for it in items:
            try:
                pe = float(it.get('PE_TTM') or 0)
                pb = float(it.get('PB_MRQ') or 0)
            except (TypeError, ValueError):
                continue
            if pe > 0:
                pes.append(pe)
            if pb > 0:
                pbs.append(pb)
        if not pes or not pbs:
            return result
        # items 按日期倒序，第一条为最新
        pe_now, pb_now = pes[0], pbs[0]
        pe_pct = round(sum(1 for v in pes if v <= pe_now) / len(pes) * 100, 1)
        pb_pct = round(sum(1 for v in pbs if v <= pb_now) / len(pbs) * 100, 1)
        result.update({
            'available': True, 'pe_pct': pe_pct, 'pb_pct': pb_pct,
            'pe_now': round(pe_now, 2), 'pb_now': round(pb_now, 2), 'days': len(pes),
        })
        _BPS_CACHE[cache_key] = (result, time.time())
    except Exception as e:
        log.warning(f"估值分位获取失败: {e}")
    return result


def analyze_fundamental(quote, tech):
    """基本面分析（PE/PB/市值/ROE/毛利率/营收增速等）
    PE/PB 阈值区分行业：银行/券商/科技/消费/医药/周期/公用 各行业独立判断
    """
    result = {}
    pe = quote.get('pe_dynamic', 0)
    pb = quote.get('pb', 0)
    market_cap = quote.get('market_cap', 0)
    eps = quote.get('eps', 0)
    code = quote.get('code', '')
    
    # ── 获取行业分类，动态调整 PE/PB 阈值 ──
    industry = fetch_stock_industry(code)
    result['industry'] = industry or '未知'
    
    # 行业分桶阈值（pe_low=低估值上限, pe_high=高估值下限, pb_low, pb_high）
    industry_thresholds = {
        '银行':        {'pe_low':8,  'pe_high':15, 'pb_low':0.8, 'pb_high':1.5},
        '证券':        {'pe_low':15, 'pe_high':35, 'pb_low':1.0, 'pb_high':2.5},
        '保险':        {'pe_low':10, 'pe_high':25, 'pb_low':1.0, 'pb_high':2.0},
        '房地产':      {'pe_low':8,  'pe_high':20, 'pb_low':0.6, 'pb_high':1.5},
        '半导体':      {'pe_low':30, 'pe_high':80, 'pb_low':3.0, 'pb_high':8.0},
        '芯片':        {'pe_low':30, 'pe_high':80, 'pb_low':3.0, 'pb_high':8.0},
        '电子':        {'pe_low':20, 'pe_high':50, 'pb_low':2.0, 'pb_high':6.0},
        '计算机':      {'pe_low':25, 'pe_high':60, 'pb_low':2.5, 'pb_high':7.0},
        '软件':        {'pe_low':25, 'pe_high':60, 'pb_low':2.5, 'pb_high':7.0},
        '互联网':      {'pe_low':20, 'pe_high':50, 'pb_low':2.0, 'pb_high':6.0},
        '通信':        {'pe_low':15, 'pe_high':35, 'pb_low':1.5, 'pb_high':4.0},
        '医药':        {'pe_low':20, 'pe_high':50, 'pb_low':2.0, 'pb_high':6.0},
        '医疗':        {'pe_low':20, 'pe_high':50, 'pb_low':2.0, 'pb_high':6.0},
        '生物':        {'pe_low':25, 'pe_high':60, 'pb_low':3.0, 'pb_high':8.0},
        '食品':        {'pe_low':20, 'pe_high':45, 'pb_low':3.0, 'pb_high':8.0},
        '饮料':        {'pe_low':20, 'pe_high':45, 'pb_low':3.0, 'pb_high':8.0},
        '白酒':        {'pe_low':20, 'pe_high':45, 'pb_low':4.0, 'pb_high':10.0},
        '家电':        {'pe_low':10, 'pe_high':25, 'pb_low':1.5, 'pb_high':4.0},
        '汽车':        {'pe_low':10, 'pe_high':30, 'pb_low':1.0, 'pb_high':3.0},
        '新能源':      {'pe_low':20, 'pe_high':50, 'pb_low':2.0, 'pb_high':6.0},
        '光伏':        {'pe_low':15, 'pe_high':40, 'pb_low':1.5, 'pb_high':5.0},
        '军工':        {'pe_low':30, 'pe_high':70, 'pb_low':2.5, 'pb_high':7.0},
        '机械设备':    {'pe_low':12, 'pe_high':30, 'pb_low':1.5, 'pb_high':4.0},
        '化工':        {'pe_low':10, 'pe_high':25, 'pb_low':1.0, 'pb_high':3.0},
        '有色':        {'pe_low':10, 'pe_high':30, 'pb_low':1.5, 'pb_high':4.0},
        '煤炭':        {'pe_low':6,  'pe_high':15, 'pb_low':0.8, 'pb_high':2.0},
        '钢铁':        {'pe_low':5,  'pe_high':15, 'pb_low':0.6, 'pb_high':1.5},
        '建筑':        {'pe_low':6,  'pe_high':15, 'pb_low':0.6, 'pb_high':1.5},
        '交通运输':    {'pe_low':10, 'pe_high':25, 'pb_low':1.0, 'pb_high':2.5},
        '公用事业':    {'pe_low':10, 'pe_high':25, 'pb_low':1.0, 'pb_high':2.5},
        '电力':        {'pe_low':10, 'pe_high':25, 'pb_low':1.0, 'pb_high':2.5},
        '商贸':        {'pe_low':10, 'pe_high':25, 'pb_low':1.0, 'pb_high':3.0},
        '纺织':        {'pe_low':8,  'pe_high':20, 'pb_low':1.0, 'pb_high':2.5},
        '农林':        {'pe_low':10, 'pe_high':30, 'pb_low':1.5, 'pb_high':4.0},
        '传媒':        {'pe_low':15, 'pe_high':35, 'pb_low':1.5, 'pb_high':4.0},
    }
    
    # 匹配行业（支持模糊匹配）
    pe_low, pe_high = 15, 60  # 默认阈值
    pb_low, pb_high = 2, 5
    if industry:
        for key, th in industry_thresholds.items():
            if key in industry:
                pe_low, pe_high = th['pe_low'], th['pe_high']
                pb_low, pb_high = th['pb_low'], th['pb_high']
                break
    
    # PE估值区间判断（用行业阈值）
    if pe > 0:
        if pe < pe_low: result['pe_signal'] = f'🟢 低估值(<{pe_low})'
        elif pe < pe_high: result['pe_signal'] = '🟡 合理'
        else: result['pe_signal'] = f'🔴 高估值(>{pe_high})'
    else:
        result['pe_signal'] = '⚪ 亏损/无PE'
    
    # PB估值（用行业阈值）
    if pb > 0:
        if pb < pb_low: result['pb_signal'] = f'🟢 低PB(<{pb_low})'
        elif pb < pb_high: result['pb_signal'] = '🟡 合理PB'
        else: result['pb_signal'] = f'🔴 高PB(>{pb_high})'
    else:
        result['pb_signal'] = '⚪ 无PB'

    # 估值历史分位（近6年 PE/PB 分位，补充行业阈值判断的估值语境）
    vp = fetch_valuation_percentile(code)
    if vp and vp.get('available'):
        result['pe_pct'] = vp['pe_pct']
        result['pb_pct'] = vp['pb_pct']
        if vp['pe_pct'] is not None:
            pe_pct = vp['pe_pct']
            result['pe_pct_signal'] = '🟢 历史低位' if pe_pct <= 20 else ('🔴 历史高位' if pe_pct >= 80 else '🟡 历史中位')
        if vp['pb_pct'] is not None:
            pb_pct = vp['pb_pct']
            result['pb_pct_signal'] = '🟢 历史低位' if pb_pct <= 20 else ('🔴 历史高位' if pb_pct >= 80 else '🟡 历史中位')
    
    # 市值规模
    if market_cap > 10000: result['cap_level'] = '🏦 巨无霸(万亿+)'
    elif market_cap > 1000: result['cap_level'] = '🏢 大盘(千亿+)'
    elif market_cap > 100: result['cap_level'] = '🏬 中盘(百亿+)'
    else: result['cap_level'] = '🏪 小盘(百亿以下)'
    
    result['pe'] = pe
    result['pb'] = pb
    result['market_cap'] = market_cap
    result['eps'] = eps
    
    # 财务深度分析（从东方财富取最新财报数据）
    fin_depth = fetch_financial_depth(code)
    if fin_depth:
        result.update(fin_depth)
    
    # 阶段涨跌幅（从技术面取）
    if tech and 'pct_5d' in tech:
        result['pct_5d'] = tech['pct_5d']
        result['pct_20d'] = tech['pct_20d']
        result['high_60d'] = tech['high_60d']
        result['low_60d'] = tech['low_60d']

    # ── 盈利质量分档（Workbuddy 复盘报告 Layer 4：决定持有期与仓位，不决定方向）──
    # A 盈利超预期：净利同比 > +50% 且 PE>0 → 信号有效期 5-8 日，可持有
    # B 题材/亏损驱动：PE<0 或无盈利支撑 → 2-3 日强制移动止盈
    # C 低beta防御：公用事业/电力类 → 不参与趋势交易，用估值+股息框架
    pg = result.get('profit_growth')
    pe_val = result.get('pe', 0)
    ind = (result.get('industry') or '')
    if pg is not None and pg > 50 and pe_val > 0:
        result['quality_tier'] = ('🟢 A档 盈利超预期（净利同比>50%且PE为正）'
                                  '→ 信号有效期5-8日，可持有')
    elif pe_val <= 0:
        result['quality_tier'] = ('🟡 B档 题材/亏损驱动（PE为负或亏损）'
                                  '→ 仅2-3日，强制移动止盈')
    elif any(k in ind for k in ('电力', '公用事业', '煤炭')):
        result['quality_tier'] = ('🔵 C档 低beta防御（公用事业类）'
                                  '→ 不参与趋势交易，改用估值+股息框架')
    else:
        result['quality_tier'] = ''

    return result


def fetch_bps(code):
    """获取最新每股净资产 BPS（用于反推 PB = price / BPS）
    数据源：东方财富 F10 财务接口 RPT_F10_FINANCE_MAINFINADATA
    带内存缓存（TTL 1天），避免重复请求触发限流
    """
    # 指数代码（sh000xxx/sz399xxx）无 BPS，直接跳过
    pure = code[2:] if code.startswith(('sh', 'sz')) else code
    if not pure.isdigit() or len(pure) != 6:
        return 0
    # 指数代码过滤（2026-09-29体检修复）：只排除真实指数前缀——
    #   399xxx=深市指数、sh000xxx=沪市指数；sz000xxx 是深市主板个股
    #   （平安银行000001/万科000002）。原 `pure.startswith('000')` 把深市
    #   主板个股一并跳过 → 这批票 PB 恒为 0 → 基本面因子 PB 评分丢失。
    if pure.startswith('399') or (code.startswith('sh') and pure.startswith('000')):
        return 0

    # 内存缓存：BPS 不日内变化，缓存1天
    cache_key = f"bps_{code}"
    cached = _BPS_CACHE.get(cache_key)
    if cached and time.time() - cached[1] < 86400:  # 1天TTL
        return cached[0]

    url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
           f"reportName=RPT_F10_FINANCE_MAINFINADATA&"
           f"columns=SECURITY_CODE,BPS,EPSJB,REPORT_DATE&"
           f"filter=(SECURITY_CODE=%22{pure}%22)&pageNumber=1&pageSize=1&"
           f"sortTypes=-1&sortColumns=REPORT_DATE")
    try:
        data = _em_http_json(url, headers={'Referer': 'https://emweb.eastmoney.com/'}, timeout=8)
        if not data or not data.get('success'):
            return 0
        items = (data.get('result') or {}).get('data', []) or []
        if not items:
            return 0
        bps = items[0].get('BPS')
        bps_val = float(bps) if bps is not None else 0
        _BPS_CACHE[cache_key] = (bps_val, time.time())
        return bps_val
    except Exception as e:
        log.warning(f"fetch_bps 失败: {e}")
        return 0


# ============================================================
# 东方财富资金流多域名轮询（应对单域名限流/空包）
# 实测（2026-08-04）：push2his 主域名 HTTPS 常返回 200 空包（限流现场）；
# 数字镜像域名（92./83./48./13.）更稳定，push2his 走 HTTP 可返回真实四档资金流。
# ============================================================
_EM_FFLOW_HOSTS = [  # push2his 历史K线/资金流（kline/daykline）——数字镜像 HTTP 优先，主域 HTTPS 最后
    ('http', '92.push2his.eastmoney.com'),
    ('http', '83.push2his.eastmoney.com'),
    ('http', '48.push2his.eastmoney.com'),
    ('http', '13.push2his.eastmoney.com'),
    ('https', 'push2his.eastmoney.com'),
]
_EM_PUSH2_HOSTS = [  # push2 实时接口（行情/板块列表/成分股/市场宽度）——数字镜像 HTTP 优先
    ('http', '92.push2.eastmoney.com'),
    ('http', '83.push2.eastmoney.com'),
    ('http', '48.push2.eastmoney.com'),
    ('http', '13.push2.eastmoney.com'),
    ('http', '17.push2.eastmoney.com'),  # 2026-09-24 实测可用，补充镜像
    ('https', 'push2.eastmoney.com'),
]


def _em_probe(url, timeout=6):
    """东财单域名探测：单次请求不重试，返回解析后的 JSON 或 None
    东财限流典型表现是 HTTP 200 + 空 body，故按内容而非状态码判定
    防限流：随机 UA 池（降低 IP 级 UA 指纹识别概率）
    冷却下沉(2026-08-28 P0)：源级冷却期内直接返回 None——所有经此函数的
    东财入口（含原裸奔的 push2ex/datacenter 调用方）自动获得保护
    """
    if 'eastmoney.com' in url and _em_cooldown_check('eastmoney'):
        return None
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={'User-Agent': _random_ua()})
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        return json.loads(raw) if raw else None
    except Exception:
        return None


# ── 东财限流冷却标记（2026-08-10）──
# 东财限流是 IP 级行为识别：全部域名变体同时失败 = IP 被限流。
# 失败后记录冷却期，本轮分析内不再重复硬闯同源接口，直接走兜底源。
# key: 接口名 → value: 冷却截止时间戳（time.time）
_EM_COOLDOWN = {}
_EM_COOLDOWN_TTL = 60  # 秒：一次限流后冷却 60s，避免反复撞墙


def _em_cooldown_check(name):
    """检查某接口是否处于冷却期：是则返回 True（跳过请求）"""
    until = _EM_COOLDOWN.get(name, 0)
    if until > time.time():
        return True
    return False


def _em_cooldown_mark(name):
    """标记某接口进入冷却期（限流后调用）"""
    _EM_COOLDOWN[name] = time.time() + _EM_COOLDOWN_TTL


# P2-9 修复(2026-09-22体检)：新浪一次空响应/解析失败常是单个坏代码（新股/退市/无K线）
# 所致，旧逻辑直接标全站 60s 冷却——批量场景被一个坏代码放大成全站降级，反而加剧东财
# 压力。改为连续失败计数：同一分钟内连续 ≥3 次失败才判真限流、标全站冷却。
_SINA_FAIL_COUNT = {'n': 0, 'last': 0.0}


def _sina_fail_mark():
    now = time.time()
    if now - _SINA_FAIL_COUNT['last'] > 60:
        _SINA_FAIL_COUNT['n'] = 0
    _SINA_FAIL_COUNT['last'] = now
    _SINA_FAIL_COUNT['n'] += 1
    if _SINA_FAIL_COUNT['n'] >= 3:
        _em_cooldown_mark('sina')


def _em_probe_all(url_builder, hosts, timeout=6, valid=None, retries=2):
    """东财多域名统一轮询（2026-08-28 P0 收敛：冷却标记从各调用方上移到此处）
    url_builder(scheme, host) → url；逐个域名探测，返回首个通过 valid 校验的 JSON。
    valid(data) 默认要求 data 非空且含 data 键；全部域名失败 = IP 级限流，
    统一在此标记源级冷却（此前裸奔入口全挂不标记，批量场景反复撞墙）。
    冷却期内调用直接返回 None（_em_probe 已下沉拦截，此处再拦一次省轮询）。
    """
    if _em_cooldown_check('eastmoney'):
        return None
    if valid is None:
        valid = lambda d: bool(d)
    for attempt in range(max(1, retries)):
        for scheme, host in hosts:
            try:
                data = _em_probe(url_builder(scheme, host), timeout=timeout)
            except Exception:
                data = None
            if data is not None:
                # P2-8b(2026-09-22体检)：valid 闭包多假定 data 为 dict，接口偶返回
                # list 时 d.get 抛 AttributeError 且原无 try 包裹——异常直接炸穿
                # 整轮探测。降级为"校验失败"继续下一个域名。
                try:
                    ok = valid(data)
                except Exception:
                    ok = False
                if ok:
                    return data
        if attempt < retries - 1:
            time.sleep(_jitter_delay(3))
    # 全部域名失败 = IP 级限流，进入源级冷却
    _em_cooldown_mark('eastmoney')
    return None


def _em_http_json(url, headers=None, timeout=10, retries=3):
    """东财单域名 http_get_json 的冷却门版本（2026-08-28 P0）
    datacenter/reportapi 等无镜像可轮询的单域名入口：冷却期内直接抛异常
    （调用方原有的 except 分支透明标注限流），避免限流窗口内 3 次重试硬闯。
    非东财 URL 原样走 http_get_json（本函数只给东财入口用）。
    """
    if 'eastmoney.com' in url and _em_cooldown_check('eastmoney'):
        raise RuntimeError('eastmoney 源级冷却期，跳过请求')
    return http_get_json(url, headers=headers, timeout=timeout, retries=retries)


def _em_fflow_daykline(secid, fields2='f51,f52,f53,f54,f55,f56,f57,f58', lmt=1):
    """东财 push2his 历史资金流多域名轮询：返回首个含 klines 的 JSON 或 None
    klines 字段（逗号分隔）：f51日期,f52主力净,f53小单净,f54中单净,f55大单净,
    f56超大单净（单位:元）, f57主力净占比%, f58超大单净占比%
    防限流(2026-08-10)：jitter 退避 + 冷却标记（2026-08-28 收敛到 _em_probe_all）
    """
    return _em_probe_all(
        lambda s, h: (f"{s}://{h}/api/qt/stock/fflow/daykline/get?"
                      f"secid={secid}&fields1=f1,f2,f3,f7&fields2={fields2}&lmt={lmt}"),
        _EM_FFLOW_HOSTS, timeout=6,
        valid=lambda d: bool(d.get('data') and d['data'].get('klines')), retries=2)


def _em_push2_get(secid, fields):
    """东财 push2 实时接口多域名轮询：返回首个含非空 data 的 JSON 或 None
    防限流(2026-08-10)：jitter 退避 + 冷却标记（2026-08-28 收敛到 _em_probe_all）
    """
    return _em_probe_all(
        lambda s, h: f"{s}://{h}/api/qt/stock/get?secid={secid}&fields={fields}",
        _EM_PUSH2_HOSTS, timeout=6,
        valid=lambda d: bool(d.get('data')), retries=2)


def fetch_main_flow_sina_realtime(code):
    """新浪实时资金流（当日数据，东财限流时的当日兜底）
    接口：MoneyFlow.ssi_ssfx_flzjtj，返回【当日实时】分档资金流：
      r0=超大单, r1=大单, r2=中单, r3=小单 (in/out 单位:元)
      netamount=主力净流入(元) = (r0净 + r1净)
    注意：新浪口径与东财不同（分档阈值/计算逻辑），数值与东财APP可能不一致；
    核心价值：提供【当日】数据（trade/涨幅为当日快照），而非新浪历史接口的昨日数据
    """
    if not code.startswith(('sh', 'sz')):
        pure = code
        code = ('sh' if pure.startswith(('6', '9')) else 'sz') + pure
    url = (f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssi_ssfx_flzjtj?daima={code}")
    try:
        data = http_get_json(url, timeout=8)
        if not data:
            return None

        def _f(v):
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0.0

        r0_in, r0_out = _f(data.get('r0_in')), _f(data.get('r0_out'))
        r1_in, r1_out = _f(data.get('r1_in')), _f(data.get('r1_out'))
        r2_in, r2_out = _f(data.get('r2_in')), _f(data.get('r2_out'))
        r3_in, r3_out = _f(data.get('r3_in')), _f(data.get('r3_out'))
        net = _f(data.get('netamount'))
        # 盘前/无成交：四档均无数据
        if net == 0 and r0_in == 0 and r0_out == 0 and r1_in == 0:
            return None
        main_net = round(net / 1e8, 2)
        today = datetime.now().strftime('%Y-%m-%d')
        return {
            'main_net': main_net,
            'main_in': round((r0_in + r1_in) / 1e8, 2),
            'main_out': round((r0_out + r1_out) / 1e8, 2),
            'super_large_net': round((r0_in - r0_out) / 1e8, 2),
            'super_large_pct': 0,
            'large_net': round((r1_in - r1_out) / 1e8, 2),
            'large_pct': 0,
            'mid_net': round((r2_in - r2_out) / 1e8, 2),
            'small_net': round((r3_in - r3_out) / 1e8, 2),
            'main_total_net': main_net,
            'main_total_pct': 0,
            'data_source': 'sina_realtime',
            'fflow_date': today,
            'data_date': today,
            'stale': False,
        }
    except Exception as e:
        log.debug(f"新浪实时资金流失败: {e}")
        return None


def fetch_main_flow_sina(code):
    """新浪财经个股资金流（真实数据兜底，东财全挂时启用）
    MoneyFlow.ssl_qsfx_zjlrqs 返回每日资金流历史，最新一条 = 最近交易日：
      netamount=主力净额(元), r0_net=超大单净额(元), r0_ratio=超大单占比,
      ratioamount=主力净占比
    口径：新浪「主力」= 超大单+大单（与东财一致）；无中单/小单分档
    """
    if not code.startswith(('sh', 'sz')):
        pure = code
        code = ('sh' if pure.startswith(('6', '9')) else 'sz') + pure
    url = (f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=1&sort=opendate&asc=0&daima={code}")
    try:
        data = http_get_json(url, timeout=8)
        if not data or not isinstance(data, list) or not data:
            return None
        item = data[0]
        net = float(item.get('netamount', 0) or 0)
        super_large = float(item.get('r0_net', 0) or 0)
        main_net = round(net / 1e8, 2)
        super_large_net = round(super_large / 1e8, 2)
        # 大单 = 主力 - 超大单（新浪口径 主力=超大+大）
        large_net = round((net - super_large) / 1e8, 2)
        main_ratio = float(item.get('ratioamount', 0) or 0)
        super_ratio = float(item.get('r0_ratio', 0) or 0)
        fflow_date = item.get('opendate', '') or ''
        # ⚠️ 数据日期校验（2026-08-04 教训）：新浪当日资金流仅晚间更新，
        # 开盘(09:30)后若返回的仍不是当日日期 = 昨日数据，必须标注 stale，不得冒充当日
        # 架构优化(2026-08-26)：统一走 data_layer.validate_today（竞价后09:15起判定）
        _, is_stale, _ = _dl.validate_today(fflow_date)
        return {
            'main_net': main_net,
            'main_in': abs(main_net) if main_net > 0 else 0,
            'main_out': abs(main_net) if main_net < 0 else 0,
            'super_large_net': super_large_net,
            'super_large_pct': round(super_ratio * 100, 2),
            'large_net': large_net,
            'large_pct': round((main_ratio - super_ratio) * 100, 2),
            'mid_net': None,      # 新浪无中单分档
            'small_net': None,
            'main_total_net': main_net,
            'main_total_pct': round(main_ratio * 100, 2),
            'data_source': 'sina',
            'fflow_date': fflow_date,
            'data_date': fflow_date,
            'stale': is_stale,
        }
    except Exception as e:
        log.debug(f"新浪资金流失败: {e}")
        return None


# 主力资金流内存缓存（盘中实时变化，但单次分析内可复用）
_MAIN_FLOW_CACHE = {}


def _sanity_check_main_flow(result, amount=0):
    """数据合理性校验：主力净额量级 + 分档自洽
    - 量级：单日主力净额 > 300亿 基本为脏数据（A股单票单日成交超千亿的极少）
    - 自洽：主力净额 ≈ 超大单净 + 大单净（仅当两者均有真实值时校验）
    返回原 result，附加 sanity_fail / sanity_note 字段（供报告提示）
    """
    if not result:
        return result
    mn = result.get('main_net', 0) or 0
    sl = result.get('super_large_net')
    lg = result.get('large_net')
    if amount > 0 and abs(mn) > amount:
        result['sanity_fail'] = f'主力净额{mn}亿 > 成交额{amount}亿，数据异常'
        log.warning(result['sanity_fail'])
    elif abs(mn) > 300:
        result['sanity_fail'] = f'主力净额{mn}亿量级异常，疑似脏数据'
        log.warning(result['sanity_fail'])
    if sl is not None and lg is not None and abs(sl) > 0.001 and abs(mn) > 0.001:
        diff = abs((sl + lg) - mn)
        if diff > max(1.0, abs(mn) * 0.3):
            result['sanity_note'] = f"分档不自洽(主力{mn}亿 vs 超大{sl}+大{lg}={round(sl + lg, 2)}亿)"
    return result


CALIBRATION_FILE = os.path.join(CACHE_DIR, 'fundflow_calibration.json')


def record_fundflow_sample(code, name, em_net, sina_net, date):
    """记录 东财 vs 新浪实时 双源口径样本（用于长期校准偏差倍数）
    东财可用时由 fetch_main_flow 自动记录；样本积累后可用 fundflow_ratio_stats 查看偏差规律
    """
    try:
        data = {}
        if os.path.exists(CALIBRATION_FILE):
            with open(CALIBRATION_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        key = f"{date}_{code}"
        data[key] = {'code': code, 'name': name, 'date': date, 'em_net': em_net, 'sina_net': sina_net}
        with open(CALIBRATION_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log.debug(f"记录资金流校准样本失败: {e}")


def fundflow_ratio_stats():
    """东财/新浪实时 口径偏差统计：方向一致性 + 倍数中位数
    结论用于：看到新浪实时数值时估算东财口径（幅度折算）
    """
    result = {'samples': 0, 'direction_match': 0, 'median_ratio': None, 'note': ''}
    try:
        if not os.path.exists(CALIBRATION_FILE):
            return result
        with open(CALIBRATION_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        pairs = []
        for v in data.values():
            em, sina = v.get('em_net'), v.get('sina_net')
            if em is None or sina is None:
                continue
            pairs.append((em, sina))
            if (em >= 0) == (sina >= 0):
                result['direction_match'] += 1
        ratios = [abs(s / e) for e, s in pairs if e != 0 and s != 0]
        result['samples'] = len(pairs)
        if ratios:
            import statistics
            result['median_ratio'] = round(statistics.median(ratios), 2)
        result['note'] = '新浪实时系统性放大主力净流入（尤其超大单），方向可信、幅度需折算'
    except Exception as e:
        log.warning(f"口径偏差统计失败: {e}")
    return result


BASE_BUILD_FILE = os.path.join(CACHE_DIR, 'basebuilding_track.json')
# 筑基期资金追踪（历史一次性追踪器，08 月上旬已用完）：
# 基准口径：跟踪标的累计主力净流入 ≥ 日均基准×交易日数 且 ≥2只流入 → 筑基成立。
# 如需复用：把要跟踪的标的填回 _BASE_STOCKS（[(secid, 名称)]）与 _BASE_DAILY_BENCH（{名称: 日均亿}）。
_BASE_DAILY_BENCH = {}
_BASE_STOCKS = []


def track_basebuilding():
    """筑基期资金追踪：每日记录标的池主力净额 + 指定板块涨跌，累计总额，供窗口末评估
    用法：每日收盘后运行 python3 stock_quant.py --track-base
    """
    if not _BASE_STOCKS:
        print("  ⚪ 筑基追踪未配置标的（_BASE_STOCKS 为空）。"
              "在 stock_quant.py 中填入要跟踪的 [(secid, 名称)] 后启用。")
        return {}
    data = {}
    if os.path.exists(BASE_BUILD_FILE):
        try:
            with open(BASE_BUILD_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = {}
    today = datetime.now().strftime('%Y-%m-%d')
    if today not in data:
        rec = {'date': today}
        for code, name in _BASE_STOCKS:
            mf = fetch_main_flow(code)
            rec[name] = round(mf.get('main_net', 0), 2) if mf else None
            if rec[name] is None:
                log.warning(f"{name} 资金数据获取失败")
        kb = fetch_sector_kline('BK1037', 3)
        rec['消费电子板块'] = round(float(kb[-1].get('pct', 0)), 2) if kb else None
        data[today] = rec
        with open(BASE_BUILD_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print(f"  ✅ 已记录筑基第 {len(data)} 天（{today}）资金数据")

    # 汇总本周（筑基期窗口）
    print(f"\n  📊 筑基期资金追踪（窗口: 本周 {list(data.keys())[0]} ~ {today}）")
    print(f"  {'─'*52}")
    totals = {name: 0.0 for _, name in _BASE_STOCKS}
    inflow_days = {name: 0 for _, name in _BASE_STOCKS}
    sector_days = []
    for d in sorted(data.keys()):
        rec = data[d]
        parts = [f"{d[5:]}"]
        for _, name in _BASE_STOCKS:
            v = rec.get(name)
            if v is not None:
                totals[name] += v
                if v > 0:
                    inflow_days[name] += 1
                parts.append(f"{name}={v:+.2f}")
            else:
                parts.append(f"{name}=—")
        if rec.get('消费电子板块') is not None:
            sector_days.append(rec['消费电子板块'])
            parts.append(f"板块{rec['消费电子板块']:+.1f}%")
        print('  ' + ' | '.join(parts))
    print(f"  {'─'*52}")
    days = len(data)
    for _, name in _BASE_STOCKS:
        bench = _BASE_DAILY_BENCH.get(name, 0.1) * days
        status = '🟢 达标' if totals[name] >= bench else ('🟡 接近' if totals[name] > 0 else '🔴 未达标')
        print(f"  {name}: 累计 {totals[name]:+.2f}亿 / 基准{bench:.1f}亿（日均{_BASE_DAILY_BENCH.get(name)}）"
              f" | 流入 {inflow_days[name]}/{days} 天 {status}")
    print(f"  消费电子板块: 观察期日均涨跌 {sum(sector_days)/len(sector_days):+.2f}%" if sector_days else "")
    print(f"\n  📅 距周五评估还有 {max(0, 5 - (datetime.now().isoweekday() - 0)) if False else '见每日记录'} 天"
          if False else f"\n  📅 评估窗口: 本周五（{today[:8]}07）汇总判定筑基是否成立")
    print(f"  ✅ 成立标准: 首只标的累计≥{_BASE_DAILY_BENCH.get(_BASE_STOCKS[0][1], 0.1)*days:.1f}亿 且 ≥2只票净流入 ≥2天")
    print(f"  ❌ 证伪标准: ≥2只票累计净流出，或板块跌破筑基期低点")
    return data


def fetch_main_flow(code):
    """获取个股主力资金流
    数据源优先级：
    1. push2his 历史资金流接口（f52，单位: 元，返回真实主力净额）
    2. push2 实时接口（f62，⚠️ 返回占位值 1，不可用）
    3. 腾讯外内盘兜底估算

    返回: {'main_in': 亿, 'main_out': 亿, 'main_net': 亿, ...}
    """
    pure = code[2:] if code.startswith(('sh', 'sz')) else code
    # 指数代码无个股资金流
    if not pure.isdigit() or len(pure) != 6:
        return None
    if code in ('sh000001', 'sz399001', 'sz399006', 'sh000688', 'sh000300') or pure.startswith('000') or pure.startswith('399'):
        return None

    # 内存缓存：盘中资金流实时变化缓存45秒；盘后数据已定格缓存10分钟（减少请求量触发限流）
    _hm = datetime.now().strftime('%H%M')
    _ttl = 45 if '0930' <= _hm <= '1505' else 600
    cache_key = f"mainflow_{code}"
    cached = _MAIN_FLOW_CACHE.get(cache_key)
    if cached and time.time() - cached[1] < _ttl:
        return cached[0]

    market = '1' if code.startswith('sh') else '0'
    secid = f"{market}.{pure}"

    # ── 方案1：push2his 历史资金流（多域名轮询，最新一条 = 今日）──
    # klines 字段：f51日期,f52主力净,f53小单净,f54中单净,f55大单净,f56超大单净(元),
    #             f57主力净占比%, f58超大单净占比%
    his_data = _em_fflow_daykline(secid)
    if his_data:
        klines = his_data['data'].get('klines', [])
        if klines:
            parts = klines[-1].split(',')
            if len(parts) >= 8:
                def _yi(v):
                    """元转亿"""
                    try:
                        return round(float(v or 0) / 1e8, 2)
                    except (TypeError, ValueError):
                        return 0.0
                main_net = _yi(parts[1])          # 主力净
                small_net = _yi(parts[2])         # 小单净
                mid_net = _yi(parts[3])           # 中单净
                large_net = _yi(parts[4])         # 大单净
                super_large_net = _yi(parts[5])   # 超大单净
                # 架构优化(2026-08-26)：统一走 data_layer.validate_today（竞价后09:15起判定）
                # 原 P0-3 修复(2026-08-25)：竞价后(9:15起)若 push2his 返回非当日数据即 stale，
                # 原判定 09:30 后才标记，导致盘前竞价后(9:25-9:30)昨日资金流冒充当日
                # （某标的 8/25 误判主因：盘前用昨日 -0.32亿 支撑看空）
                _, is_stale, _ = _dl.validate_today(parts[0])
                result = {
                    'main_net': main_net,
                    'main_in': abs(main_net) if main_net > 0 else 0,
                    'main_out': abs(main_net) if main_net < 0 else 0,
                    'super_large_net': super_large_net,
                    'super_large_pct': round(float(parts[7] or 0), 2),  # 已是%
                    'large_net': large_net,
                    'large_pct': 0,  # push2his 无大单占比字段
                    'mid_net': mid_net,
                    'small_net': small_net,
                    'main_total_net': main_net,
                    'main_total_pct': round(float(parts[6] or 0), 2),   # 已是%
                    'data_source': 'eastmoney_push2his',
                    'fflow_date': parts[0],
                    'data_date': parts[0],
                    'stale': is_stale,
                }
                _sanity_check_main_flow(result)
                # ⚠️ 修复(2026-08-05)：东财 push2his daykline 盘中常返回【昨日】数据
                # (stale)，直接使用会误导当日资金面判断——先尝试当日数据源(新浪实时/
                # 新浪历史)，全部失败才退回 stale 数据(保留标记供报告层提示)
                if is_stale:
                    for _alt_fn in (fetch_main_flow_sina_realtime, fetch_main_flow_sina):
                        try:
                            _alt = _alt_fn(code)
                        except Exception:
                            _alt = None
                        if _alt and not _alt.get('stale'):
                            _sanity_check_main_flow(_alt)
                            _MAIN_FLOW_CACHE[cache_key] = (_alt, time.time())
                            return _alt
                _MAIN_FLOW_CACHE[cache_key] = (result, time.time())
                return result

    # ── 方案2：新浪实时资金流（【当日】数据，东财限流时的当日兜底）──
    sina_rt = fetch_main_flow_sina_realtime(code)
    if sina_rt:
        _sanity_check_main_flow(sina_rt)
        _MAIN_FLOW_CACHE[cache_key] = (sina_rt, time.time())
        return sina_rt

    # ── 方案3：新浪历史资金流（仅当日已更新时可用；否则跳过，绝不冒充当日）──
    sina_flow = fetch_main_flow_sina(code)
    if sina_flow and not sina_flow.get('stale'):
        _MAIN_FLOW_CACHE[cache_key] = (sina_flow, time.time())
        return sina_flow

    # ── 方案4：push2 实时接口（多域名轮询；⚠️ f62 返回占位值 1，仅用 f184/f175）──
    try:
        data = _em_push2_get(secid, 'f62,f184,f175,f165,f167,f168,f169,f170,f171,f172')
        d = data.get('data', {}) if data else {}
        if not d:
            return _estimate_main_flow_from_quote(code)

        def to_yi(v):
            """元转亿"""
            try:
                return round(float(v or 0) / 1e8, 2)
            except (TypeError, ValueError):
                return 0.0

        # ⚠️ f62 是占位值 1，不能用作主力净额；用 f184(超大单) 近似
        super_large_net = to_yi(d.get('f184', 0))
        large_net = to_yi(d.get('f175', 0))
        main_net = super_large_net + large_net  # 超大单+大单 ≈ 主力净额
        # 占比字段单位是「万分位 ‱」，÷10000 转%
        super_large_pct = round(float(d.get('f165', 0) or 0) / 10000, 2)
        large_pct = round(float(d.get('f167', 0) or 0) / 10000, 2)

        result = {
            'main_net': main_net,
            'main_in': abs(main_net) if main_net > 0 else 0,
            'main_out': abs(main_net) if main_net < 0 else 0,
            'super_large_net': super_large_net,
            'super_large_pct': super_large_pct,
            'large_net': large_net,
            'large_pct': large_pct,
            'main_total_net': main_net,
            'main_total_pct': round(float(d.get('f168', 0) or 0) / 10000, 2),
            'data_source': 'eastmoney_push2',
        }
        _MAIN_FLOW_CACHE[cache_key] = (result, time.time())
        return result
    except Exception as e:
        log.warning(f"fetch_main_flow 失败: {e}")
        return _estimate_main_flow_from_quote(code)


def _estimate_main_flow_from_quote(code):
    """东方财富限流时，用腾讯基础行情兜底估算主力资金流
    估算逻辑：
    - 外内盘比 = outer/inner，反映散户买卖盘强弱（>1 买盘占优，<1 卖盘占优）
    - 成交额 amount（亿）反映主力参与规模
    - 估算主力净额 ≈ amount × (outer-inner)/(outer+inner) × 0.3
      （0.3 为外内盘差对主力净额的解释权重，保守系数）
    - 标注 data_source='估算'，不伪造精度，报告层会透明提示
    递归保护：用 _ESTIMATING 标志避免 fetch_quote_tencent ↔ fetch_main_flow 循环
    """
    # 递归保护：如果正在估算中，直接用最小化行情请求（绕过 fetch_quote_tencent 的 fetch_main_flow 调用）
    if getattr(_estimate_main_flow_from_quote, '_ESTIMATING', False):
        q = _fetch_quote_minimal(code)
    else:
        _estimate_main_flow_from_quote._ESTIMATING = True
        try:
            q = fetch_quote_tencent(code)
        finally:
            _estimate_main_flow_from_quote._ESTIMATING = False
    if not q:
        return None
    outer = float(q.get('outer', 0) or 0)
    inner = float(q.get('inner', 0) or 0)
    amount = float(q.get('amount', 0) or 0)
    total = outer + inner
    if total <= 0 or amount <= 0:
        # 盘前/停牌无成交，返回0净额并标注
        return {
            'main_net': 0.0,
            'main_in': 0.0,
            'main_out': 0.0,
            'super_large_net': 0.0,
            'super_large_pct': 0.0,
            'large_net': 0.0,
            'large_pct': 0.0,
            'main_total_net': 0.0,
            'main_total_pct': 0.0,
            'data_source': '估算(无成交)',
            'outer': outer,
            'inner': inner,
            'outer_inner_ratio': round(outer / inner, 2) if inner > 0 else 0,
        }
    ratio_diff = (outer - inner) / total  # 范围 -1~1
    estimated_net = round(amount * ratio_diff * 0.3, 2)
    result = {
        'main_net': estimated_net,
        'main_in': abs(estimated_net) if estimated_net > 0 else 0,
        'main_out': abs(estimated_net) if estimated_net < 0 else 0,
        'super_large_net': 0.0,  # 无法估算分档，标注0
        'super_large_pct': 0.0,
        'large_net': 0.0,
        'large_pct': 0.0,
        'main_total_net': estimated_net,
        'main_total_pct': round(ratio_diff * 100, 2),
        'data_source': '估算(腾讯外内盘)',
        # ⚠️ 口径提示：腾讯外内盘只反映散户挂单，不含主力大单
        # 东方财富分档资金流(超大单+大单+中单)才反映真实主力动向
        # 估算结果与东方财富可能方向相反（如某标的 2026-07-27
        #   估算 -0.02亿流出 vs 东方财富真实 +0.34亿流入）
        # 报告层应以东方财富分档数据为准，估算仅作兜底参考
        'caliber_warning': '估算口径≠真实主力：腾讯外内盘仅含散户挂单，不含主力大单，东方财富分档资金流才反映真实主力动向',
        'outer': outer,
        'inner': inner,
        'outer_inner_ratio': round(outer / inner, 2) if inner > 0 else 0,
        'amount': amount,
    }
    # 估算结果也缓存，但 TTL 更短（5秒，盘中数据变化快）
    _MAIN_FLOW_CACHE[f"mainflow_{code}"] = (result, time.time())
    return result


def fetch_financial_depth(code):
    """获取财务深度数据（ROE/毛利率/营收增速/净利润增速）"""
    pure = code[2:] if code.startswith(('sh','sz')) else code
    # ⚠️ 东方财富 F10 接口真实字段名（非文档常见的下划线命名）：
    # ROEJQ=加权净资产收益率, XSMLL=销售毛利率, XSJLL=销售净利率
    # TOTALOPERATEREVETZ=营收同比, PARENTNETPROFITTZ=归母净利同比
    # EPSJB=基本每股收益, BPS=每股净资产
    url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
           f"reportName=RPT_F10_FINANCE_MAINFINADATA&"
           f"columns=SECURITY_CODE,ROEJQ,XSMLL,XSJLL,"
           f"TOTALOPERATEREVETZ,PARENTNETPROFITTZ,"
           f"EPSJB,BPS,REPORT_DATE&"
           f"filter=(SECURITY_CODE=%22{pure}%22)&pageNumber=1&pageSize=2&"
           f"sortTypes=-1&sortColumns=REPORT_DATE")
    try:
        data = _em_http_json(url, headers={'Referer': 'https://emweb.eastmoney.com/'}, timeout=10)
        if not data:
            return None
        result_obj = data.get('result') or {}
        items = result_obj.get('data', []) or []
        if not items:
            return None
        item = items[0]
        result = {}
        # ROE（加权净资产收益率）- 字段名 ROEJQ
        roe = item.get('ROEJQ')
        if roe is not None:
            result['roe'] = round(roe, 1)
            result['roe_signal'] = '🟢' if roe > 15 else ('🟡' if roe > 5 else '🔴')
        # 毛利率（销售毛利率）- 字段名 XSMLL
        gpm = item.get('XSMLL')
        if gpm is not None:
            result['gross_margin'] = round(gpm, 1)
            result['gross_margin_signal'] = '🟢' if gpm > 30 else ('🟡' if gpm > 15 else '🔴')
        # 净利润率（销售净利率）- 字段名 XSJLL
        npm = item.get('XSJLL')
        if npm is not None:
            result['net_margin'] = round(npm, 1)
        # 营收增速 - 字段名 TOTALOPERATEREVETZ
        rev_growth = item.get('TOTALOPERATEREVETZ')
        if rev_growth is not None:
            result['rev_growth'] = round(rev_growth, 1)
            result['rev_growth_signal'] = '🟢' if rev_growth > 20 else ('🟡' if rev_growth > 0 else '🔴')
        # 净利润增速 - 字段名 PARENTNETPROFITTZ
        profit_growth = item.get('PARENTNETPROFITTZ')
        if profit_growth is not None:
            result['profit_growth'] = round(profit_growth, 1)
            result['profit_growth_signal'] = '🟢' if profit_growth > 30 else ('🟡' if profit_growth > 0 else '🔴')
        # EPS - 字段名 EPSJB
        eps_item = item.get('EPSJB')
        if eps_item is not None:
            result['eps_detail'] = round(eps_item, 3)
        return result
    except Exception as e:
        log.warning(f"fetch_financial_depth 失败: {e}")
        return None

# ============================================================
# 模块四：资金面分析
# ============================================================
def fetch_sector_flow():
    """获取行业板块涨跌TOP5（东方财富板块接口，多域名轮询应对限流）
    东方财富板块接口：90.BK0475 形式，f3=涨跌幅 f14=板块名
    防限流(2026-08-10)：东财源级冷却期内直接返回不可用（走其他兜底）
    """
    if _em_cooldown_check('eastmoney'):
        return {'available': False, 'data_source': '限流(冷却期内)', 'note': '板块资金流数据暂不可用(东财冷却)'}
    try:
        # 东方财富行业板块列表接口（统一多域名轮询，全挂自动标记源级冷却）
        # f62=板块主力净额(元), f184=超大单净额(元), f175=大单净额(元)
        params = ("pn=1&pz=200&po=1&np=1&fltt=2&invt=2"
                  "&fields=f2,f3,f4,f12,f14,f62,f184,f175"
                  "&fs=m:90+t:2")  # m:90+t:2 = 行业板块
        data = _em_probe_all(
            lambda s, h: f"{s}://{h}/api/qt/clist/get?{params}",
            _EM_PUSH2_HOSTS, timeout=8,
            valid=lambda d: bool(d.get('data')))
        if not data or not data.get('data'):
            # 限流/失败：透明标注，不伪造板块数据
            return {'available': False, 'data_source': '限流(东方财富无响应)', 'note': '板块资金流数据暂不可用'}
        items = data.get('data', {}).get('diff', []) if data.get('data') else []
        sectors = []
        for it in items:
            name = it.get('f14', '')
            # 东财停牌/无数据时 f3/f2/f62 可能返回 '-'，安全转换（2026-08-17 修复）
            def _f(v):
                try:
                    return float(v) if v not in (None, '', '-') else 0.0
                except (TypeError, ValueError):
                    return 0.0
            pct = _f(it.get('f3'))
            price = _f(it.get('f2'))
            main_net = round(_f(it.get('f62')) / 1e8, 2)  # 元转亿
            # P2-8b(2026-09-22体检)：旧条件 pct!=0 把"平盘板块"误滤掉（f3='-'的停牌
            # 板块 _f 已转 0.0）——改用 price>0 判有效（'-'→0.0 被滤，平盘 price>0 保留）
            if name and price > 0:
                sectors.append({'name': name, 'pct': round(pct, 2), 'price': price, 'main_net': main_net})
        sectors.sort(key=lambda x: x['pct'], reverse=True)
        # 涨幅榜前5 + 跌幅榜前5（涨幅榜末位≠领跌，需单独取跌幅榜）
        leading = sectors[:5]
        lagging = sorted(sectors, key=lambda x: x['pct'])[:5]
        # 板块资金流排行：主力净流入TOP5 / 净流出TOP5（f62 口径，单位亿）
        fund_sorted = sorted([s for s in sectors if s['main_net'] != 0], key=lambda x: x['main_net'], reverse=True)
        fund_in = fund_sorted[:5]
        fund_out = fund_sorted[-5:] if len(fund_sorted) > 5 else []
        return {'available': True, 'data_source': 'eastmoney', 'sectors': leading, 'lagging_sectors': lagging,
                'fund_in': fund_in, 'fund_out': fund_out,
                'fund_in_count': sum(1 for s in sectors if s['main_net'] > 0)}
    except Exception as e:
        log.warning(f"fetch_sector_flow 失败: {e}")
        return {'available': False, 'data_source': '限流(东方财富异常)', 'note': f'板块资金流数据暂不可用: {e}'}


def analyze_sector_rotation():
    """板块轮动分析（东方财富行业板块 + 主力资金流排行）
    涨幅榜判断主线，主力净流入排行验证资金配合（涨幅大但资金流出 → 主线存疑）
    """
    result = {'leading': [], 'lagging': [], 'main_line': '', 'rotation_note': '',
              'fund_in_top': [], 'fund_out_top': []}
    try:
        sf = fetch_sector_flow()
        if not sf or not sf.get('available'):
            return result
        sectors = sf.get('sectors', [])
        fund_in = sf.get('fund_in', [])
        fund_out = sf.get('fund_out', [])
        fund_in_count = sf.get('fund_in_count', 0)
        result['fund_in_top'] = fund_in[:3]
        result['fund_out_top'] = fund_out[:3]
        if not sectors:
            return result
        result['leading'] = sectors[:3]
        top = sectors[0]
        # 主线判断：领涨板块 + 主力资金配合验证
        top_fund = top.get('main_net', 0)
        fund_tag = ''
        if top_fund > 1:
            fund_tag = f" 🟢资金配合({top_fund:+.1f}亿)"
        elif top_fund < -1:
            fund_tag = f" ⚠️资金背离({top_fund:+.1f}亿)"
        if top['pct'] > 5:
            result['main_line'] = f"🔥 今日主线: {top['name']} ({top['pct']:+.1f}%){fund_tag}"
        elif top['pct'] > 2:
            result['main_line'] = f"📈 强势板块: {top['name']} ({top['pct']:+.1f}%){fund_tag}"
        if len(sectors) >= 3:
            result['lagging'] = sf.get('lagging_sectors', sectors[-3:])[:3]
        # 连续性判断：涨幅 > 3% 的板块数量 + 主力净流入板块数量（f62>0）
        strong_count = sum(1 for s in sectors if s['pct'] > 3)
        if strong_count >= 5 and fund_in_count >= 5:
            result['rotation_note'] = f'🟢 多板块共振走强+{fund_in_count}个板块主力净流入，市场赚钱效应好'
        elif strong_count >= 2:
            result['rotation_note'] = '🟡 少数板块走强，结构性行情'
        elif fund_in_count >= 8:
            result['rotation_note'] = f'🟢 主力资金分散流入({fund_in_count}个板块)，市场情绪回暖'
        else:
            result['rotation_note'] = '🔴 板块普涨力度不足，警惕一日游'
        return result
    except Exception as e:
        log.warning(f"analyze_sector_rotation 失败: {e}")
        return result

_SECTOR_BK_MAP = {}  # 板块名称 → BK代码 缓存（行业板块列表稳定，避免重复请求触发限流）


def get_stock_sector_bk(code):
    """获取个股所属行业板块的 BK 代码
    步骤：1. 行业名称：push2 f127（如"消费电子"），限流时备用 datacenter F10 行业名
         2. 行业板块列表（clist m:90+t:2，push2his 镜像更稳）按名称匹配 BK 代码（带缓存）
    返回 {'bk_code': 'BK1037', 'name': '消费电子', 'industry': '消费电子'}
    """
    result = {'bk_code': '', 'name': '', 'industry': ''}
    try:
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        market = '1' if code.startswith('sh') else '0'
        # 1. 行业名称：push2 f127 优先，限流时用 datacenter F10 备用（不同服务器家族）
        industry = ''
        data = _em_push2_get(f"{market}.{pure}", 'f57,f58,f127')
        if data:
            industry = (data.get('data') or {}).get('f127', '') or ''
        if not industry:
            try:
                industry = fetch_stock_industry(code) or ''
            except Exception:
                pass
        result['industry'] = industry
        if not industry:
            return result
        # 2. 板块列表匹配 BK 代码（缓存命中则直接返回）
        if industry in _SECTOR_BK_MAP:
            result['bk_code'] = _SECTOR_BK_MAP[industry]
            result['name'] = industry
            return result
        params = ("pn=1&pz=200&po=1&np=1&fltt=2&invt=2"
                  "&fields=f12,f14&fs=m:90+t:2")
        if _em_cooldown_check('eastmoney'):
            return result  # 东财冷却期内跳过板块列表匹配
        data2 = _em_probe_all(
            lambda s, h: f"{s}://{h}/api/qt/clist/get?{params}",
            _EM_PUSH2_HOSTS, timeout=8,
            valid=lambda d: bool(d.get('data') and d['data'].get('diff')))
        if data2:
            diff = data2['data'].get('diff') or []
            hit = None
            # 精确匹配
            for it in diff:
                if it.get('f14', '') == industry:
                    hit = it
                    break
            # 模糊匹配（如"电子化学品Ⅱ"）
            if not hit:
                for it in diff:
                    nm = it.get('f14', '') or ''
                    if nm and (industry in nm or nm in industry):
                        hit = it
                        break
            if hit:
                result['bk_code'] = hit.get('f12', '')
                result['name'] = hit.get('f14', '')
                _SECTOR_BK_MAP[industry] = result['bk_code']
    except Exception as e:
        log.warning(f"行业板块映射失败: {e}")
    return result


def fetch_sector_kline(bk_code, datalen=60):
    """获取行业板块指数日K（东财 push2his，secid=90.BKxxxx）
    返回与 fetch_kline_sina 结构对齐的列表：date/close/high/low/volume/pct
    防限流(2026-08-10)：东财源级冷却期内直接返回空
    缓存(2026-08-15)：sector 类目 300s TTL（盘中）；收盘后(≥15:00)切换 sector_close
    长效缓存(86400s)——板块K线 T+1 才变，收盘后当日不再重拉，降低限流触发
    """
    if _em_cooldown_check('eastmoney'):
        return []
    if not bk_code:
        return []
    cache_key = f"{bk_code}_{datalen}"
    # 收盘后(≥15:00)用长效缓存：板块指数日K当日不再变化，无需反复请求
    after_market = datetime.now().hour >= 15
    cat = 'sector_close' if after_market else 'sector'
    cached = cache_get(cache_key, cat)
    if cached:
        return cached
    try:
        data = _em_probe_all(
            lambda s, h: (f"{s}://{h}/api/qt/stock/kline/get?"
                          f"secid=90.{bk_code}&fields1=f1,f2,f3,f4,f5,f6&"
                          f"fields2=f51,f52,f53,f54,f55,f56,f57,f58&"
                          f"klt=101&fqt=1&end=20500101&lmt={datalen}"),
            _EM_FFLOW_HOSTS, timeout=8,
            valid=lambda d: bool(d.get('data') and d['data'].get('klines')))
        if data:
            out = []
            for line in data['data']['klines']:
                p = line.split(',')
                if len(p) >= 6:
                    out.append({
                        'date': p[0],
                        'open': float(p[1]), 'close': float(p[2]),
                        'high': float(p[3]), 'low': float(p[4]),
                        'volume': float(p[5]),
                        'amount': float(p[6]) if len(p) > 6 else 0,
                        'pct': float(p[7]) if len(p) > 7 else 0,
                    })
            cache_set(cache_key, out, cat)
            return out
    except Exception as e:
        log.warning(f"板块K线获取失败: {e}")
    # P2-8b 透明标注(2026-09-22体检)：失败原样返回空列表，调用方拿到的 [] 无法区分
    # "无数据"与"限流降级"——冷却期内返回空时显式留痕，下游 analyze_sector_trend
    # 等 result['available']=False 的场景可据此注明数据源降级
    if _em_cooldown_check('eastmoney'):
        log.warning(f"fetch_sector_kline 返回空：eastmoney 限流冷却中，结果为降级空数据")
    return []


def analyze_sector_trend(code):
    """板块趋势分析：判断所属行业板块处于 筑底（反转）还是 反弹
    依据：
    1. 价格 vs MA20/MA60：双线站上=反转走强；MA20上/MA60下=反弹尝试；双线下=弱势
    2. 波段结构：摆动点"低点抬高"（筑底）vs "高点降低"（下降通道）
    3. 量能：板块指数当日量比
    返回: {'available', 'name', 'state', 'signal', 'detail'}
    """
    result = {'available': False, 'name': '', 'state': '', 'signal': '', 'detail': ''}
    try:
        sb = get_stock_sector_bk(code)
        if not sb.get('bk_code'):
            result['name'] = sb.get('industry', '')
            return result
        result['name'] = sb.get('name', sb.get('industry', ''))
        kline = fetch_sector_kline(sb['bk_code'], 60)
        if not kline or len(kline) < 25:
            return result
        closes = [float(d['close']) for d in kline]
        highs = [float(d['high']) for d in kline]
        lows = [float(d['low']) for d in kline]
        volumes = [float(d.get('volume', 0)) for d in kline]
        price = closes[-1]
        ma20 = calc_ma(closes, 20)
        ma60 = calc_ma(closes, 60)
        above20 = ma20 is not None and price > ma20
        above60 = ma60 is not None and price > ma60
        # 波段结构：最近两个低点/高点
        pts = find_swing_points(highs, lows, volumes, closes, n=2, min_pct=2.0)
        lows_pts = [p for p in pts if p['type'] == 'L']
        highs_pts = [p for p in pts if p['type'] == 'H']
        low_rising = len(lows_pts) >= 2 and lows_pts[-1]['price'] > lows_pts[-2]['price']
        high_rising = len(highs_pts) >= 2 and highs_pts[-1]['price'] > highs_pts[-2]['price']
        high_falling = len(highs_pts) >= 2 and highs_pts[-1]['price'] < highs_pts[-2]['price']
        # 量能：当日 vs 前20日均量
        avg_vol20 = sum(volumes[-25:-5]) / 20 if len(volumes) >= 25 else 0
        vol_ratio = round(volumes[-1] / avg_vol20, 2) if avg_vol20 > 0 else 1.0

        if above20 and above60:
            state = '走强（站上双均线，反转向上）'
            signal = f"🟢 板块已站上MA20/MA60，趋势反转向上（量比{vol_ratio}）"
        elif above20 and not above60:
            state = '反弹（MA20上方，MA60下方）'
            if low_rising:
                signal = f"🟡 反弹且低点抬高，若放量突破MA60({ma60:.1f})则确认筑底反转"
            else:
                signal = f"🟡 超跌反弹（MA20上方但MA60压制），需放量突破确认"
        else:
            state = '弱势（双均线下方）'
            if low_rising and not high_falling:
                signal = f"🔴 双均线下方但波段低点抬高，处筑底初段（量比{vol_ratio}）"
            else:
                signal = f"🔴 双均线下方且结构走低，属超跌反弹/下跌中继，追高需谨慎"
        result.update({
            'available': True, 'state': state, 'signal': signal,
            'detail': f"板块指数{price:.1f} MA20={ma20:.1f} MA60={ma60:.1f} 量比{vol_ratio}",
        })
    except Exception as e:
        log.warning(f"板块趋势分析失败: {e}")
    return result


def analyze_capital_flow(quote, tech):
    """资金面综合分析（含Level2逐笔资金流）"""
    result = {}
    code = quote.get('code', '')
    
    # 个股资金流
    outer = quote.get('outer', 0)
    inner = quote.get('inner', 0)
    ratio = quote.get('outer_inner_ratio', 1)
    main_net = quote.get('main_net', 0)
    amount = quote.get('amount', 0)
    main_data_source = quote.get('main_data_source', 'eastmoney')

    result['outer'] = outer
    result['inner'] = inner
    result['ratio'] = ratio
    result['main_net'] = main_net
    result['amount'] = amount
    result['main_data_source'] = main_data_source
    # 数据日期 + 时效标记透传（供报告显示，防止昨日数据冒充当日）
    result['data_date'] = quote.get('data_date', '')
    result['data_stale'] = quote.get('data_stale', False)
    # 数据合理性校验结果透传
    result['main_sanity_fail'] = quote.get('main_sanity_fail', '')
    result['main_sanity_note'] = quote.get('main_sanity_note', '')
    # 标记数据是否为估算/昨日（用于评分系统降权：昨日数据不得驱动今日评分）
    result['data_estimated'] = '估算' in (main_data_source or '') or bool(quote.get('data_stale'))
    # 透传口径警告（估算兜底时存在，报告层据此提示用户）
    if quote.get('main_caliber_warning'):
        result['main_caliber_warning'] = quote['main_caliber_warning']
    
    # 外/内盘判断
    if ratio > 1.3:
        result['flow_signal'] = '🟢🟢 买盘强势（外盘>内盘30%+）'
    elif ratio > 1.05:
        result['flow_signal'] = '🟢 买盘略强'
    elif ratio < 0.7:
        result['flow_signal'] = '🔴🔴 卖盘碾压（外盘<内盘30%+）'
    elif ratio < 0.95:
        result['flow_signal'] = '🔴 卖盘略强'
    else:
        result['flow_signal'] = '⚪ 买卖均衡'
    
    # 主力净流判断
    if main_net > 10:
        result['main_signal'] = '🟢🟢 主力大幅净流入'
    elif main_net > 3:
        result['main_signal'] = '🟢 主力净流入'
    elif main_net < -10:
        result['main_signal'] = '🔴🔴 主力大幅净流出'
    elif main_net < -3:
        result['main_signal'] = '🔴 主力净流出'
    else:
        result['main_signal'] = '⚪ 主力中性'
    
    # 量比（从技术面取）
    if tech:
        result['vol_ratio'] = tech.get('vol_ratio', 1)
        vr = result['vol_ratio']
        if vr > 2.0:
            result['vol_signal'] = '🔴🔴 巨量（警惕出货或突破）'
        elif vr > 1.5:
            result['vol_signal'] = '🟡 放量'
        elif vr < 0.5:
            result['vol_signal'] = '🔵 地量（变盘信号）'
        elif vr < 0.7:
            result['vol_signal'] = '🔵 缩量'
        else:
            result['vol_signal'] = '⚪ 平量'
    
    # Level2逐笔资金流（用于评分）
    level2 = fetch_level2_flow(code, amount)
    if level2 and level2.get('available'):
        result['level2_available'] = True
        result['level2_main_total'] = level2['main_total_net']
        result['level2_main_pct'] = level2.get('main_total_pct', 0)
        result['level2_super_large'] = level2['super_large_net']
        result['level2_super_pct'] = level2.get('super_large_pct', 0)
        result['level2_large'] = level2['large_net']
        result['level2_large_pct'] = level2.get('large_pct', 0)
        result['level2_mid'] = level2.get('mid_net', 0)
        result['level2_small'] = level2.get('small_net', 0)
        result['level2_data_source'] = level2.get('data_source', 'eastmoney')
        result['level2_detail_missing'] = level2.get('level2_detail_missing', False)
    else:
        result['level2_available'] = False
        result['level2_data_source'] = level2.get('data_source') if level2 else '限流'
    
    # 板块资金流
    sf = fetch_sector_flow()
    result['sectors'] = sf.get('sectors', []) if sf and sf.get('available') else []
    result['sectors_available'] = bool(sf and sf.get('available'))
    result['sectors_data_source'] = sf.get('data_source') if sf else None
    result['sectors_note'] = sf.get('note') if sf and not sf.get('available') else None
    # 板块资金排行（主力净流入/流出TOP，供报告展示与评分）
    result['sector_fund_in'] = sf.get('fund_in', []) if sf and sf.get('available') else []
    result['sector_fund_out'] = sf.get('fund_out', []) if sf and sf.get('available') else []
    result['sector_fund_in_count'] = sf.get('fund_in_count', 0) if sf else 0
    
    return result


# ============================================================
# 模块四-B：Level2逐笔资金流（大单/中单/小单分级）
# ============================================================
def fetch_level2_flow(code, amount=0):
    """获取Level2资金流（东方财富分档资金流）
    数据源优先级：
    1. push2his daykline 历史资金流（多域名轮询，四档真实数据+占比，主域名限流时走镜像）
    2. push2 实时接口（f62=占位值1不可用, f184/f175 单位为亿元且偶发脏数据）
    3. trends2 分时反推 → fetch_main_flow（新浪/估算）
    amount: 当日成交额（亿），用于主力净额合理性校验，防止接口脏数据
    """
    try:
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        market = '1' if code.startswith('sh') else '0'
        secid = f"{market}.{pure}"

        # ── 方案A0：复用 fetch_main_flow 刚取过的分档缓存（东财四档/新浪实时，消除重复请求减少限流触发）──
        _hm = datetime.now().strftime('%H%M')
        _ttl = 45 if '0930' <= _hm <= '1505' else 600
        _cached = _MAIN_FLOW_CACHE.get(f"mainflow_{code}")
        if _cached and time.time() - _cached[1] < _ttl:
            _mf = _cached[0]
            if _mf.get('data_source') in ('eastmoney_push2his', 'sina_realtime'):
                _mn = _mf.get('main_net', 0)
                if amount > 0 and abs(_mn) > amount:
                    return _level2_fallback(code)
                return {
                    'available': True,
                    'super_large_net': _mf.get('super_large_net', 0),
                    'super_large_pct': _mf.get('super_large_pct', 0),
                    'large_net': _mf.get('large_net', 0),
                    'large_pct': _mf.get('large_pct', 0),
                    'mid_net': _mf.get('mid_net'),
                    'small_net': _mf.get('small_net'),
                    'main_total_net': _mf.get('main_total_net', 0),
                    'main_total_pct': _mf.get('main_total_pct', 0),
                    'data_source': _mf.get('data_source', ''),
                    'fflow_date': _mf.get('fflow_date', ''),
                    'level2_detail_missing': _mf.get('mid_net') is None,
                }
        # ── 方案A：push2his daykline 四档真实数据（多域名轮询）──
        # klines 字段：f51日期,f52主力净,f53小单净,f54中单净,f55大单净,f56超大单净(元),
        #             f57主力净占比%, f58超大单净占比%
        his = _em_fflow_daykline(secid)
        if his:
            klines = his['data'].get('klines', [])
            if klines:
                parts = klines[-1].split(',')
                if len(parts) >= 8:
                    def _yi(v):
                        """元转亿"""
                        try:
                            return round(float(v or 0) / 1e8, 2)
                        except (TypeError, ValueError):
                            return 0.0
                    main_net = _yi(parts[1])          # 主力净
                    small_net = _yi(parts[2])         # 小单净
                    mid_net = _yi(parts[3])           # 中单净
                    large_net = _yi(parts[4])         # 大单净
                    super_large_net = _yi(parts[5])   # 超大单净
                    # 合理性校验：主力净额绝对值不可能超过当日成交额（单位均为亿）
                    if amount > 0 and abs(main_net) > amount:
                        log.warning(f"Level2 数据异常(主力净额 {main_net}亿 > 成交额 {amount}亿)，降级")
                        return _level2_fallback(code)
                    return {
                        'available': True,
                        'super_large_net': super_large_net,
                        'super_large_pct': round(float(parts[7] or 0), 2) if len(parts) > 7 else 0,
                        'large_net': large_net,
                        'large_pct': 0,  # daykline 无大单占比字段
                        'mid_net': mid_net,
                        'small_net': small_net,
                        'main_total_net': main_net,
                        'main_total_pct': round(float(parts[6] or 0), 2) if len(parts) > 6 else 0,
                        'data_source': 'eastmoney_push2his',
                        'fflow_date': parts[0],
                    }

        # ── 方案B：push2 实时接口（多域名轮询）──
        data = _em_push2_get(secid, 'f62,f184,f175,f164,f165,f166,'
                                   'f167,f168,f169,f170,f171,f172')
        d = data.get('data', {}) if data else {}
        if not d:
            # 东方财富限流时，用 fetch_main_flow 兜底（其内部会走新浪/腾讯估算）
            return _level2_fallback(code)

        # ⚠️ 单位说明（2026-07-27 验证）：
        # push2 实时接口的 f62 是占位值 1（不可用），f184/f175 单位是「亿元」不是元
        # 旧代码用 /1e8 转亿，导致 super_large_net 永远 = 0.0（bug）
        # 修复：push2 已是亿元单位，直接取值；占比 ÷10000 转%
        def to_yi(v):
            """push2 已是亿元单位，直接 float 化"""
            try:
                return round(float(v or 0), 2)
            except (TypeError, ValueError):
                return 0.0

        # ⚠️ f62 是占位值 1，不能用作主力净额；用 f184(超大单) 近似
        main_net = to_yi(d.get('f184', 0)) + to_yi(d.get('f175', 0))
        super_large_net = to_yi(d.get('f184', 0))
        large_net = to_yi(d.get('f175', 0))
        # 东方财富字段映射（通过多字段交叉获取四档）
        # f165=超大单净流入占比(‱), f167=大单净流入占比(‱)
        super_large_pct = round(float(d.get('f165', 0) or 0) / 10000, 2)
        large_pct = round(float(d.get('f167', 0) or 0) / 10000, 2)
        # 中单/小单通过主力净额反推
        # 中单 ≈ (总成交额 - 超大单 - 大单) * 0.4
        # 小单 ≈ (总成交额 - 超大单 - 大单) * 0.6
        mid_net = round(-(super_large_net + large_net) * 0.4, 2)
        small_net = round(-(super_large_net + large_net) * 0.6, 2)

        if super_large_net == 0 and large_net == 0 and main_net == 0:
            return _level2_fallback(code)

        # 合理性校验：主力净额绝对值不可能超过当日成交额（单位均为亿）
        # 防止接口返回脏数据（如 2026-07-31 某标的曾返回 大单+2764亿 > 成交额60亿）
        if amount > 0 and abs(main_net) > amount:
            log.warning(f"Level2 数据异常(主力净额 {main_net}亿 > 成交额 {amount}亿)，降级估算")
            return _level2_fallback(code)

        return {
            'available': True,
            'super_large_net': super_large_net,
            'super_large_pct': super_large_pct,
            'large_net': large_net,
            'large_pct': large_pct,
            'mid_net': mid_net,
            'small_net': small_net,
            'main_total_net': main_net,
            'main_total_pct': float(d.get('f168', 0) or 0),
            'data_source': 'eastmoney',
        }
    except Exception as e:
        log.warning(f"fetch_level2_flow 失败: {e}")
        return _level2_fallback(code)


def _estimate_level2_from_trends(code):
    """从东方财富分时成交明细（trends2）反推主力资金流
    策略：每5分钟一段，单段成交额 > 日均额×1.5 时标记"大单段"
    大单段中价格涨→大单买入，跌→大单卖出
    返回 Level2 格式数据（分档缺失，主力合计可用）
    """
    try:
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        market = '1' if code.startswith('sh') else '0'
        url = (f"https://push2.eastmoney.com/api/qt/stock/trends2/get?"
               f"secid={market}.{pure}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,"
               f"f10,f11,f12,f13&fields2=f51,f52,f53,f54,f55&lmt=256")
        data = _em_http_json(url, timeout=8)
        if not data or not data.get('data'):
            return None
        trends = data['data'].get('trends', [])
        if not trends or len(trends) < 10:
            return None

        # 解析分段数据：时间,价格,均价,成交量(手),成交额(元)
        segments = []
        for t in trends:
            parts = t.split(',')
            if len(parts) < 5:
                continue
            amt = float(parts[4] or 0)
            vol = float(parts[3] or 0)
            price = float(parts[1] or 0)
            if amt > 0 and vol > 0:
                segments.append({'amt': amt, 'vol': vol, 'price': price})

        if len(segments) < 10:
            return None

        total_amt = sum(s['amt'] for s in segments)
        avg_amt = total_amt / len(segments)
        threshold = avg_amt * 1.5  # 大单判定阈值

        # 分大单/小单
        large_buy = 0.0  # 大单段中价格上涨的成交额
        large_sell = 0.0  # 大单段中价格下跌的成交额
        large_mid = 0.0  # 大单段中价格平盘的成交额
        for i, s in enumerate(segments):
            if s['amt'] >= threshold:
                # 与上一段比较价格方向
                if i > 0:
                    prev_price = segments[i-1]['price']
                    if s['price'] > prev_price * 1.0005:  # 涨≥0.05%
                        large_buy += s['amt']
                    elif s['price'] < prev_price * 0.9995:  # 跌≥0.05%
                        large_sell += s['amt']
                    else:
                        large_mid += s['amt']
                else:
                    large_mid += s['amt']

        main_net = (large_buy - large_sell) / 1e8  # 元转亿
        main_total = (large_buy + large_sell) / 1e8
        main_pct = round((large_buy - large_sell) / (large_buy + large_sell) * 100, 2) if main_total > 0 else 0

        return {
            'available': True,
            'super_large_net': round(main_net * 0.5, 2),  # 保守拆分：超大单≈50%主力合计
            'super_large_pct': round(main_pct, 2),
            'large_net': round(main_net * 0.5, 2),
            'large_pct': round(main_pct, 2),
            'mid_net': None,
            'small_net': None,
            'main_total_net': round(main_net, 2),
            'main_total_pct': round(main_pct, 2),
            'data_source': 'trends2+反推',
            'level2_detail_missing': True,
        }
    except Exception as e:
        log.debug(f"_estimate_level2_from_trends 失败: {e}")
        return None


def _level2_fallback(code):
    """Level2 接口限流时的退化兜底：优先从分时成交反推，复用 fetch_main_flow 估算结果
    明确标注 data_source，不伪造分档数据（中单/小单返回 None）
    """
    # ★ 新增：先试 trends2 分时反推（精度高于外内盘估算）
    trends_est = _estimate_level2_from_trends(code)
    if trends_est and abs(trends_est.get('main_total_net', 0)) > 0.001:
        return trends_est

    # 原估算兜底逻辑不变
    mf = fetch_main_flow(code)
    if not mf:
        return {'available': False}
    # 透传 fetch_main_flow 结果中已有的分档字段（新浪/东财真实数据时非0，估算时为0）
    return {
        'available': True,
        'super_large_net': mf.get('super_large_net', 0),
        'super_large_pct': mf.get('super_large_pct', 0),
        'large_net': mf.get('large_net', 0),
        'large_pct': mf.get('large_pct', 0),
        'mid_net': mf.get('mid_net'),      # 新浪/估算无中单分档 → None
        'small_net': mf.get('small_net'),
        'main_total_net': mf.get('main_net', 0),
        'main_total_pct': mf.get('main_total_pct', 0),
        'data_source': mf.get('data_source', '估算'),  # 透传标注（eastmoney_push2his/sina/估算）
        'level2_detail_missing': mf.get('mid_net') is None,  # 无中单分档时提示用户
    }


# ============================================================
# 模块四-C：北向资金持股
# ============================================================
def fetch_north_flow(code):
    """获取北向资金持股数据（东方财富沪深港通持股接口）
    接口返回持股数量(股)、持股市值(元)、持股占已发行股本比例(%)
    """
    try:
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        # 东方财富沪深港通持股：沪市=1.xxx，深市=0.xxx
        market = '1' if code.startswith('sh') else '0'
        secid = f"{market}.{pure}"
        # f124=持股数量(股) f125=持股市值(元) f126=持股比例(%)
        url = (f"https://push2.eastmoney.com/api/qt/stock/get?"
               f"secid={secid}&fields=f124,f125,f126")
        data = _em_http_json(url, timeout=10)
        d = data.get('data', {}) if data else {}
        if not d:
            # 东方财富限流/失败：透明标注缺失，不伪造数据
            return {'available': False, 'data_source': '限流(东方财富无响应)', 'note': '北向持股数据暂不可用'}
        hold_shares = float(d.get('f124', 0) or 0)
        hold_value = float(d.get('f125', 0) or 0) / 1e8  # 转亿
        hold_pct = float(d.get('f126', 0) or 0)
        if hold_value > 0 or hold_pct > 0:
            return {
                'available': True,
                'hold_value': round(hold_value, 2),
                'pct': round(hold_pct, 2),
                'hold_shares': round(hold_shares, 0),
                'data_source': 'eastmoney',
            }
        # 数据正常但北向无持股（真实情况）
        return {'available': False, 'data_source': 'eastmoney', 'note': '北向暂无持股'}
    except Exception as e:
        log.warning(f"fetch_north_flow 失败: {e}")
        return {'available': False, 'data_source': '限流(东方财富异常)', 'note': f'北向持股数据暂不可用: {e}'}


# ============================================================
# 模块五：舆情/新闻分析
# ============================================================
def fetch_news_sina(code, stock_name):
    """获取个股新闻（含时效性过滤 + 加权情感 + 时间衰减 + 多源）"""
    news = []

    # ── 数据源 1：新浪个股页 ──
    try:
        cmd = f'curl -s --connect-timeout 6 "https://finance.sina.com.cn/realstock/company/{code}/nc.shtml" -H "Referer: https://finance.sina.com.cn"'
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
        html = r.stdout.decode('gbk', errors='replace')
        titles = re.findall(r'title=\"([^\"]{15,80})\"', html)
        for t in titles:
            news.append({'title': t.strip(), 'source': 'sina'})
    except Exception as e:
        log.warning(f"新浪新闻抓取失败: {e}")

    # ── 数据源 2：东方财富股吧标题 ──
    try:
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        # 东方财富股吧接口（中文关键词需 URL 编码）
        import urllib.parse as ulp
        kw_enc = ulp.quote(stock_name)
        guba_url = f"https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery&param=%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22{kw_enc}%22%2C%22type%22%3A%5B%22cmsArticleWebOld%22%5D%2C%22client%22%3A%22web%22%2C%22clientVersion%22%3A%22curr%22%2C%22param%22%3A%7B%22cmsArticleWebOld%22%3A%7B%22searchScope%22%3A%22default%22%2C%22sort%22%3A%22default%22%2C%22pageIndex%22%3A1%2C%22pageSize%22%3A10%2C%22preTag%22%3A%22%22%2C%22postTag%22%3A%22%22%7D%7D%7D"
        raw = http_get_raw(guba_url, headers={'Referer': 'https://so.eastmoney.com/'}, timeout=8).decode('utf-8', errors='replace')
        # 提取标题（jsonp 格式）
        em_titles = re.findall(r'"title":"([^"]{15,80})"', raw)
        for t in em_titles[:8]:
            news.append({'title': t.strip(), 'source': 'eastmoney'})
    except Exception as e:
        log.warning(f"东方财富股吧抓取失败: {e}")

    # ── 情感词典：优先从 data/sentiment_dict.json 加载，失败回退到内置词典 ──
    dict_path = os.path.join(CACHE_DIR, 'sentiment_dict.json')
    POSITIVE_WEIGHTED = {}
    NEGATIVE_WEIGHTED = {}
    NEGATIONS = ['不', '未', '没有', '无', '非']
    PUNCT_FOR_WINDOW = ['。', '！', '？', '，', '、', '；', '：', '(', ')', '《', '》']
    try:
        with open(dict_path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        POSITIVE_WEIGHTED = d.get('positive', {})
        NEGATIVE_WEIGHTED = d.get('negative', {})
        NEGATIONS = d.get('negations', NEGATIONS)
        PUNCT_FOR_WINDOW = d.get('punctuation_for_window', PUNCT_FOR_WINDOW)
    except Exception as e:
        log.warning(f"情感词典加载失败，回退内置: {e}")
        # 内置回退词典（与 data/sentiment_dict.json 保持一致的核心子集）
        POSITIVE_WEIGHTED = {
            '业绩预增': 3, '净利润增长': 3, '大幅增长': 3, '涨停': 2, '大涨': 2,
            '中标': 2, '签合同': 2, '大订单': 2, '突破': 2, '创新高': 3,
            '增持': 2, '回购': 2, '利好': 2, '获批': 2, '投产': 2, '量产': 2,
            '合作': 1, '扩产': 2, '买入': 2, '推荐': 2, '景气': 2, '超预期': 3,
            '高增长': 3, '订单激增': 3, '市占率提升': 2, '毛利率提升': 2,
        }
        NEGATIVE_WEIGHTED = {
            '减持': 2, '亏损': 3, '预亏': 3, '跌停': 3, '大跌': 2, '下调': 2,
            '利空': 2, '监管': 2, '问询': 2, '警示': 2, '调查': 3, '立案': 3,
            '处罚': 3, '退市': 3, '风险提示': 2, '违约': 3, '诉讼': 2,
            '计提': 2, '减值': 3, '暴雷': 3, '业绩下滑': 3, '不及预期': 3,
            '高管离职': 2, '停牌核查': 2, '被举报': 3,
        }

    def _negation_prefix(title, idx, max_window=8):
        """自适应否定窗口：取关键词前最近一个标点（或最多 max_window 字）"""
        # 从 idx 往前找最近一个标点
        start = max(0, idx - max_window)
        for j in range(idx - 1, start - 1, -1):
            if title[j] in PUNCT_FOR_WINDOW:
                start = j + 1
                break
        return title[start:idx]

    def analyze_sentiment(title):
        """加权情感分析，返回 (score, sentiment)
        否定窗口自适应：取关键词前"最近一个标点"到关键词之间的字符，最长 8 字。
        覆盖"连续两个季度未出现下滑"（窗口到逗号为止，命中"未"）等场景。
        新增：复合语义检测，处理"亏损收窄""下滑放缓"等缓和/反转句式。
        """
        pos_score = 0
        neg_score = 0

        # ★ 复合语义检测：先于单关键词匹配，检测整体语义方向
        # 缓和词：出现在负面关键词附近时，降低负面权重
        MITIGATION_WORDS = ['收窄', '放缓', '减少', '缩小', '缓解', '改善', '回升', '恢复', '反弹']
        # 反转词：出现在负面关键词附近时，将负面变为正面
        REVERSAL_PATTERNS = [
            ('亏损', ['扭亏', '由亏转盈', '亏损减少', '亏损收窄', '亏损改善', '亏损下降']),
            ('下滑', ['下滑放缓', '下滑收窄', '下滑改善', '下滑回升']),
            ('下降', ['下降收窄', '下降放缓', '降幅收窄']),
            ('减持', ['减持终止', '减持完成', '减持计划终止']),
        ]
        # 先检查反转模式
        for neg_word, reversal_list in REVERSAL_PATTERNS:
            if neg_word in title:
                for rev in reversal_list:
                    if rev in title:
                        # 反转：原先的负面词反而贡献正面
                        for w, weight in NEGATIVE_WEIGHTED.items():
                            if neg_word in w:
                                pos_score += weight * 0.5
                                break
                        # 跳过此负面词的常规匹配
                        break

        # 标准情感词典匹配
        for word, weight in POSITIVE_WEIGHTED.items():
            if word in title:
                idx = title.find(word)
                prefix = _negation_prefix(title, idx)
                if any(neg in prefix for neg in NEGATIONS):
                    neg_score += weight
                else:
                    pos_score += weight
        for word, weight in NEGATIVE_WEIGHTED.items():
            if word in title:
                # 跳过已被反转模式处理的词（已在上面处理）
                skip = False
                for neg_word, reversal_list in REVERSAL_PATTERNS:
                    if neg_word in word and any(rev in title for rev in reversal_list):
                        skip = True
                        break
                if skip:
                    continue
                idx = title.find(word)
                prefix = _negation_prefix(title, idx)
                if any(neg in prefix for neg in NEGATIONS):
                    pos_score += weight
                else:
                    # 检查词后是否有缓和词
                    mitigated = False
                    after_text = title[idx + len(word):idx + len(word) + 6]
                    for mw in MITIGATION_WORDS:
                        if mw in after_text:
                            neg_score += weight * 0.3  # 缓和：只记30%权重
                            mitigated = True
                            break
                    if not mitigated:
                        neg_score += weight
        # 兼容旧简单关键词（低权重）
        SIMPLE_POS = ['增长', '订单', '合同', '合作', '景气', '推荐']
        SIMPLE_NEG = ['下跌', '下滑', '回落', '疲软']
        for w in SIMPLE_POS:
            if w in title and not any(kw in title for kw in POSITIVE_WEIGHTED):
                pos_score += 0.5
        for w in SIMPLE_NEG:
            if w in title:
                neg_score += 0.5

        total_score = pos_score - neg_score
        if total_score > 1:
            return total_score, '利好'
        elif total_score < -1:
            return total_score, '利空'
        else:
            return total_score, '中性'

    current_year = datetime.now().year
    # 旧闻标记：标题含2年前及以上年份字样，或明确的历史事件关键词
    # 注意：通用规则已通过 title_year < current_year - 1 兜底，这里只保留少量确证的旧闻标记
    OLD_NEWS_KEYWORDS = ['20年业绩增长', '可降解塑料', 'PBAT']

    # ── 事件分类规则（2026-08-18 新增：标题规则化事件识别，区别于关键词情感打分）──
    # 每条规则：(关键词, 事件类型, 方向)。方向用于事件清单快览，事件本身的利好/利空
    # 以 analyze_sentiment 的结论为准（规则方向仅作预设参考，避免双重计数）。
    EVENT_RULES = [
        ('减持', '股东减持', '利空'), ('清仓', '股东清仓', '利空'),
        ('增持', '股东增持', '利好'), ('举牌', '举牌', '利好'),
        ('回购', '股份回购', '利好'),
        ('业绩预增', '业绩预增', '利好'), ('预增', '业绩预增', '利好'),
        ('业绩预亏', '业绩预亏', '利空'), ('预亏', '业绩预亏', '利空'),
        ('业绩快报', '业绩快报', '中性'), ('业绩预告', '业绩预告', '中性'),
        ('中标', '中标', '利好'), ('签订', '签约/订单', '利好'), ('大单', '签约/订单', '利好'),
        ('立案', '监管立案', '利空'), ('调查', '监管调查', '利空'),
        ('问询', '监管问询', '利空'), ('处罚', '监管处罚', '利空'),
        ('涨停', '涨停异动', '中性'), ('跌停', '跌停异动', '中性'),
        ('解禁', '限售解禁', '利空'), ('质押', '股权质押', '利空'),
        ('重组', '重组', '中性'), ('收购', '并购收购', '中性'), ('并购', '并购收购', '中性'),
        ('定增', '定增募资', '中性'), ('分红', '分红派息', '利好'), ('送转', '分红派息', '利好'),
        ('投产', '产能投产', '利好'), ('量产', '产能投产', '利好'),
        ('专利', '专利技术', '利好'), ('研发', '研发进展', '中性'),
        ('合作', '战略合作', '中性'), ('战略', '战略合作', '中性'),
        # 2026-08-31 新增事件触发词（借鉴收评"事件级催化"粒度：
        # 传媒线真正引爆点是《后西游记》定档上星，而非笼统的"AI内容"概念词）
        ('定档', '内容定档', '利好'), ('开播', '内容开播', '利好'),
        ('上线', '产品上线', '利好'), ('发布', '新品发布', '利好'),
        ('签约', '签约/订单', '利好'), ('订单', '签约/订单', '利好'),
        ('中标', '中标', '利好'), ('获批', '获批/核准', '利好'),
        ('注册生效', 'IPO/注册', '中性'), ('上市', 'IPO/上市', '中性'),
        ('回购', '股份回购', '利好'),
    ]

    def classify_event(title):
        """标题规则化事件分类：返回 (事件类型, 规则方向) 或 ('', '')"""
        for kw, ev_type, direction in EVENT_RULES:
            if kw in title:
                return ev_type, direction
        return '', ''

    processed_news = []
    seen_titles = set()  # 多源去重

    for item in news:
        t = item['title']
        src = item['source']
        if len(t) < 15:
            continue
        if any(k in t for k in ['随身自选', '免费股价', '融资融券', '模拟交易', '跟高手', '自选股']):
            continue
        # 去重
        t_key = t[:30]
        if t_key in seen_titles:
            continue
        seen_titles.add(t_key)

        # 提取年份
        year_match = re.search(r"(20\d{2})", t)
        year_match2 = re.search(r"(\d{2})年业绩", t)
        if year_match:
            title_year = int(year_match.group(1))
        elif year_match2:
            yr = int(year_match2.group(1))
            title_year = 2000 + yr if yr < 50 else 1900 + yr
        else:
            title_year = current_year

        source_type = '新闻'
        if any(k in t for k in ['研报', '点评', '评级']):
            source_type = '研报'
        elif any(k in t for k in ['公告', '通知', '提示']):
            source_type = '公告'

        is_known_old = any(k in t for k in OLD_NEWS_KEYWORDS)
        if is_known_old or title_year < current_year - 1:
            tag = '📜 旧闻'
        elif title_year < current_year:
            tag = '📅 去年'
        else:
            tag = '🆕 当前'

        sentiment_score, sentiment = analyze_sentiment(t)
        ev_type, ev_dir = classify_event(t)
        processed_news.append({
            'title': t[:60], 'sentiment': sentiment, 'sentiment_score': round(sentiment_score, 2),
            'tag': tag, 'source': source_type, 'data_source': src, 'year': title_year,
            'event': ev_type, 'event_dir': ev_dir,
            # 2026-08-31：事件驱动标记（命中事件触发词的新闻，供"事件驱动"汇总展示）
            'event_driven': bool(ev_type),
        })

    # ── 时间衰减加权评分 ──
    # 当年新闻权重 1.0，去年 0.5，旧闻 0.1
    valid_news = [n for n in processed_news if n['year'] >= current_year - 1]
    old_news = [n for n in processed_news if n['year'] < current_year - 1]

    total_weight = 0
    pos_count = 0
    neg_count = 0
    for n in valid_news:
        weight = 1.0 if n['year'] == current_year else 0.5
        total_weight += weight
        if n['sentiment'] == '利好':
            pos_count += weight
        elif n['sentiment'] == '利空':
            neg_count += weight

    # 情绪评分：正负面加权差 / 总权重 * 100，范围 [-100, +100]
    if total_weight > 0:
        score = round((pos_count - neg_count) / total_weight * 100, 1)
    else:
        score = 0
    pos_count = int(pos_count)
    neg_count = int(neg_count)

    has_old_news = len(old_news) > 0

    # 事件清单：当前年份内、命中事件规则的新闻（按事件类型去重合并，供快览）
    event_map = {}  # (事件类型) → {'titles': [...], 'direction': 预设方向}
    for n in processed_news:
        if n.get('event') and n['year'] >= current_year - 1:
            key = n['event']
            if key not in event_map:
                event_map[key] = {'direction': n.get('event_dir', '中性'), 'count': 0, 'titles': []}
            event_map[key]['count'] += 1
            if len(event_map[key]['titles']) < 2:
                event_map[key]['titles'].append(n['title'][:40])
    events = [{'type': k, 'direction': v['direction'], 'count': v['count'], 'titles': v['titles']}
              for k, v in event_map.items()]

    return {
        'news': processed_news[:10],
        'score': score,
        'positive': pos_count,
        'negative': neg_count,
        'total': len(valid_news),
        'old_news_count': len(old_news),
        'has_old_news': has_old_news,
        'sources': len(seen_titles),
        # 事件清单（标题规则化分类，2026-08-18 新增）
        'events': events,
        # 数据完整度标记：0 条新闻时降级，避免被默认中性评分误导
        'data_available': len(processed_news) > 0,
    }


# ============================================================
# 模块五-B：财联社电报（全市场 7×24 快讯层）
# 数据源：本地自建 RSSHub 实例（/cls/telegraph 路由）
# 用途（2026-08-10 用户调整）：【只获取加红电报，全量、不按个股过滤】
#   → 加红 = 财联社标注的重要/异动快讯，用于"市场板块异动归因"——
#     捕捉导致板块异动的重要政策、事件、盘面快讯（如规划印发、涨停分析、收评）
# 注意：RSSHub 的 category 是【路径参数】/cls/telegraph/red（非 query 参数），
#       RSSHub 由社区持续跟进财联社改版（零维护）。
# ============================================================
_CLS_RSSHUB_URL = os.environ.get('CLS_RSSHUB_URL', 'http://localhost:1200/cls/telegraph')
# 架构修复(2026-09-24)：本地 RSSHub 实例已停且本机无 docker——补充两个公共实例兜底，
# 按"本地优先（快）→ 公共回退"顺序探测；全部失败才判不可用
_CLS_RSSHUB_FALLBACKS = [
    'https://rsshub.rssforever.com/cls/telegraph',
    'https://rsshub.ktachibana.party/cls/telegraph',
]
# 防静默失效(2026-09-24教训)：RSSHub 实例停掉后函数静默返回 available=False，
# 报告只是少一段、无人发现——连续失败 ≥3 次升级为 ERROR 级日志
_CLS_FAIL_STATE = {'n': 0}


def fetch_cls_telegraph(limit=20, category='red'):
    """获取财联社电报（全市场 7×24 快讯，本地 RSSHub 优先、公共实例回退）
    category: 'red'=加红(默认) / 'watch'=看盘 / 'announcement'=公司 / 'remind'=提醒
              'fund'=基金 / 'hk_us'=港美股 / ''=全部
    返回 dict：
      items:  [{title, content, pub_date, link}, ...]
      available: 是否获取成功
      data_source: 数据源标注（含命中的实例）
    """
    result = {'items': [], 'available': False, 'data_source': '', 'note': ''}
    import urllib.request
    import xml.etree.ElementTree as ET
    import http.client

    def _parse(raw, src_label):
        root = ET.fromstring(raw)
        items = []
        for item in root.iter('item'):
            title = (item.findtext('title') or '').strip()
            desc = (item.findtext('description') or '').strip()
            link = (item.findtext('link') or '').strip()
            pub = (item.findtext('pubDate') or '').strip()
            if title:
                items.append({
                    'title': title[:120],
                    'content': desc[:300],
                    'pub_date': pub,
                    'link': link,
                })
            if len(items) >= limit:
                break
        return items, src_label

    # 本地优先，公共实例回退
    base = _CLS_RSSHUB_URL.rstrip('/')
    sources = [base] + [u.rstrip('/').replace('/cls/telegraph', '') for u in _CLS_RSSHUB_FALLBACKS]
    last_err = None
    for i, host in enumerate(sources):
        try:
            url = f"{host}/cls/telegraph/{category}" if category else f"{host}/cls/telegraph"
            req = urllib.request.Request(url, headers={'User-Agent': _random_ua()})
            raw = urllib.request.urlopen(req, timeout=12).read()
            items, src_label = _parse(raw, host)
            result['items'] = items
            result['available'] = len(items) > 0
            result['data_source'] = f'cls_rsshub_{category}@{host.split("//")[1].split("/")[0]}'
            if not items:
                result['note'] = 'RSSHub 返回空（财联社接口异常或未更新）'
            if _CLS_FAIL_STATE['n'] > 0:
                log.info(f"财联社电报恢复（连续失败{_CLS_FAIL_STATE['n']}次后）via {result['data_source']}")
                _CLS_FAIL_STATE['n'] = 0
            return result
        except Exception as e:
            last_err = e
            continue
    # 全部实例失败：连续失败计数，≥3 次 ERROR 级告警（防静默失效）
    _CLS_FAIL_STATE['n'] += 1
    msg = f"财联社电报获取失败（连续第{_CLS_FAIL_STATE['n']}次）: {last_err}"
    if _CLS_FAIL_STATE['n'] >= 3:
        log.error(msg + " —— 请检查 RSSHub 实例与网络！")
    else:
        log.warning(msg)
    result['note'] = f'财联社电报不可用: {last_err}'
    return result


def summarize_cls_telegraph(limit=10, category='red'):
    """财联社电报摘要：只保留标题行（电报标题已含关键信息），供宏观/快讯展示
    默认获取加红电报（category='red'），用于板块异动归因
    返回: [{'title', 'time'}, ...]
    """
    data = fetch_cls_telegraph(limit, category)
    out = []
    for it in data.get('items', [])[:limit]:
        # 电报标题形如"财联社8月10日电，XXXX"
        title = it['title']
        time_str = ''
        m = re.search(r'(\d{1,2})月(\d{1,2})日', title)
        if m:
            time_str = f"{m.group(1)}月{m.group(2)}日"
        out.append({'title': title, 'time': time_str})
    return out, data.get('available', False)


# ============================================================
# 模块六：宏观分析
# ============================================================
def fetch_market_breadth():
    """市场宽度：全市场涨跌家数 + 涨停/跌停/连板统计
    数据源：
    1. 东财 ulist.np 大盘指标（f136=上涨家数 f137=下跌家数 f138=平盘家数），多域名轮询
    2. 东财 push2ex 涨停池/跌停池（盘中有 pool 明细与 zttj 统计，盘前返回空）
    """
    result = {'up_count': 0, 'down_count': 0, 'flat_count': 0, 'ratio': 0,
              'zt_count': 0, 'dt_count': 0, 'zbc_count': 0, 'lb_count': 0,
              'available': False, 'data_source': '', 'note': ''}

    # 1. 涨跌家数（ulist.np，统一多域名轮询应对限流，全挂自动标记源级冷却）
    up_total = down_total = flat_total = 0
    if not _em_cooldown_check('eastmoney'):
        data = _em_probe_all(
            lambda s, h: (f"{s}://{h}/api/qt/ulist.np/get?"
                          "fltt=2&fields=f1,f2,f3,f4,f6,f12,f13,f136,f137,f138,f104,f105"
                          "&secids=1.000001,0.399001"),
            _EM_PUSH2_HOSTS, timeout=8,
            valid=lambda d: bool(d.get('data') and d['data'].get('diff')))
        if data:
            for it in data['data'].get('diff', []):
                up_total += int(it.get('f136', 0) or 0)
                down_total += int(it.get('f137', 0) or 0)
                flat_total += int(it.get('f138', 0) or 0)

    # 2. 涨停/跌停/连板/炸板（push2ex 涨停池，盘中有数据）
    zt_count = dt_count = zbc_count = lb_count = 0
    try:
        today = datetime.now().strftime('%Y%m%d')
        # 涨停池：data.pool 为涨停明细，data.zttj[0] 含 DT/ZBC/LB 计数
        zt_url = ("https://push2ex.eastmoney.com/getTopicZTPool?"
                  "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
                  f"&Pageindex=0&pagesize=100&sort=fbt%3Aasc&date={today}")
        zt_data = _em_probe(zt_url, timeout=8)
        if zt_data and zt_data.get('data'):
            zt = zt_data['data']
            pool = zt.get('pool') or []
            zt_count = len(pool)
            zttj = zt.get('zttj') or []
            if zttj:
                tj = zttj[0]
                dt_count = int(tj.get('DT_count', 0) or 0)
                zbc_count = int(tj.get('ZBC_count', 0) or 0)
                lb_count = int(tj.get('LB_count', 0) or 0)
            else:
                # 2026-09-04 修复：zttj 常为 None，连板改用池明细 lbc 推导；炸板无来源
                lb_count = sum(1 for it in pool if (it.get('lbc') or 1) >= 2)
                result['note'] = '炸板统计缺失(zttj为空)'
            if not pool and not zttj:
                result['note'] = '盘前/开盘初期，涨停池尚未形成'
        # 跌停池
        dt_url = ("https://push2ex.eastmoney.com/getTopicDTPool?"
                  "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
                  f"&Pageindex=0&pagesize=100&sort=fund%3Aasc&date={today}")
        dt_data = _em_probe(dt_url, timeout=8)
        if dt_data and dt_data.get('data'):
            dt_count = max(dt_count, len(dt_data['data'].get('pool') or []))
    except Exception as e:
        log.warning(f"涨停/跌停池统计失败: {e}")

    result.update({
        'up_count': up_total, 'down_count': down_total, 'flat_count': flat_total,
        'ratio': round(up_total / down_total, 2) if down_total > 0 else 0,
        'zt_count': zt_count, 'dt_count': dt_count, 'zbc_count': zbc_count, 'lb_count': lb_count,
        'available': up_total + down_total > 0,
        'data_source': 'eastmoney',
    })
    return result


def analyze_market_breadth(breadth=None):
    """市场宽度信号：涨跌比 + 涨停效应 → 市场情绪判断"""
    result = {'available': False, 'signal': '', 'note': ''}
    if breadth is None:
        breadth = fetch_market_breadth()
    if not breadth or not breadth.get('available'):
        return result
    result['available'] = True
    result['note'] = breadth.get('note', '')
    up, down = breadth['up_count'], breadth['down_count']
    zt, dt = breadth['zt_count'], breadth['dt_count']
    total = up + down
    if total == 0:
        return result
    ratio = up / down if down > 0 else 99
    if ratio >= 2.5 and zt >= 50:
        result['signal'] = f'🟢🟢 市场极强（涨跌比 {ratio:.1f}，涨停 {zt} 家）'
    elif ratio >= 1.5:
        result['signal'] = f'🟢 市场偏强（涨跌比 {ratio:.1f}，涨停 {zt} 家）'
    elif ratio >= 0.8:
        result['signal'] = f'⚪ 市场分化（涨跌比 {ratio:.1f}，涨停 {zt} 家）'
    elif ratio >= 0.5:
        result['signal'] = f'🔴 市场偏弱（涨跌比 {ratio:.1f}，涨停 {zt} 家）'
    else:
        result['signal'] = f'🔴🔴 市场极弱（涨跌比 {ratio:.1f}，涨停 {zt} 家）'
    return result


def analyze_market_sentiment(breadth=None):
    """市场情绪温度计：涨停/连板/炸板率综合评分（0-100）
    输入 fetch_market_breadth 结果，分四档计分：
    - 涨停家数（0-40分）：核心情绪
    - 连板高度（0-20分）：赚钱效应
    - 炸板率（0-20分）：追高情绪，炸板率高 = 情绪退潮
    - 涨停/跌停比（0-20分）：多空极端
    """
    result = {'available': False, 'score': 0, 'temp': '中性', 'detail': ''}
    if breadth is None:
        breadth = fetch_market_breadth()
    if not breadth:
        return result
    zt = breadth.get('zt_count', 0)
    dt = breadth.get('dt_count', 0)
    zbc = breadth.get('zbc_count', 0)
    lb = breadth.get('lb_count', 0)
    # 盘前/涨停池未形成时无情绪数据
    if zt + dt + zbc + lb == 0:
        return result

    score = 0
    # 1. 涨停家数（0-40分）
    if zt >= 80: score += 40
    elif zt >= 50: score += 30
    elif zt >= 30: score += 20
    elif zt >= 10: score += 10
    elif zt > 0: score += 5
    # 2. 连板高度（0-20分）
    if lb >= 7: score += 20
    elif lb >= 5: score += 15
    elif lb >= 3: score += 10
    elif lb >= 2: score += 5
    # 3. 炸板率（0-20分，低炸板率高分）
    total_zt = zt + zbc
    zbc_rate = zbc / total_zt if total_zt > 0 else 0
    if zbc_rate <= 0.15: score += 20
    elif zbc_rate <= 0.3: score += 13
    elif zbc_rate <= 0.45: score += 6
    # 4. 涨停/跌停比（0-20分）
    if dt > 0:
        zt_dt_ratio = zt / dt
        if zt_dt_ratio >= 5: score += 20
        elif zt_dt_ratio >= 2: score += 13
        elif zt_dt_ratio >= 1: score += 7
        elif zt_dt_ratio >= 0.5: score += 3
    else:
        score += 20 if zt > 0 else 10  # 无跌停 = 情绪极强

    # 温度分档
    if score >= 70: temp = '🔥 过热（追高风险）'
    elif score >= 50: temp = '🟢 亢奋（赚钱效应强）'
    elif score >= 30: temp = '🟡 温和（结构性行情）'
    elif score >= 15: temp = '🔵 低迷（观望为主）'
    else: temp = '🧊 冰点（恐慌尾声）'

    result.update({
        'available': True, 'score': score, 'temp': temp,
        'detail': (f'涨停{zt}家 跌停{dt}家 炸板{zbc}家({zbc_rate*100:.0f}%) '
                   f'最高{lb}板 温度{score}分'),
    })
    return result


def analyze_market_volume():
    """两市量能趋势：今日两市成交额（亿）+ 量能倍数（vs 近20日均量）
    数据源：腾讯实时行情(成交额) + 新浪指数日K(成交量序列，稳定无限流)
    量能倍数 = 今日沪+深成交量 / 近20日均成交量；>1.3 放量，<0.7 缩量
    """
    result = {'available': False, 'today_amount': 0.0, 'vol_ratio': 0.0, 'signal': '', 'note': ''}
    try:
        # 今日两市成交额（亿）：上证 + 深证（腾讯实时，盘前为前收值）
        sh_q = fetch_quote_tencent('sh000001')
        sz_q = fetch_quote_tencent('sz399001')
        today_amount = round((sh_q.get('amount', 0) if sh_q else 0) + (sz_q.get('amount', 0) if sz_q else 0), 0)

        # 量能倍数：上证+深证成交量 vs 近20日均量（新浪日K，最后一条为当日累计）
        sh_k = fetch_kline_sina('sh000001', 240, 30)
        sz_k = fetch_kline_sina('sz399001', 240, 30)
        if sh_k and sz_k and len(sh_k) >= 21 and len(sz_k) >= 21:
            def _vols(kl):
                out = []
                for d in kl:
                    try:
                        out.append(float(d.get('volume', 0) or 0))
                    except (TypeError, ValueError):
                        out.append(0.0)
                return out
            sh_v, sz_v = _vols(sh_k), _vols(sz_k)
            today_v = sh_v[-1] + sz_v[-1]
            avg20 = (sum(sh_v[-21:-1]) + sum(sz_v[-21:-1])) / 20
            if avg20 > 0 and today_v > 0:
                vol_ratio = round(today_v / avg20, 2)
                result['vol_ratio'] = vol_ratio
                result['today_amount'] = today_amount
                if vol_ratio >= 1.3:
                    result['signal'] = f'🟢 两市放量（量能{vol_ratio:.2f}x，成交额{today_amount:.0f}亿）'
                elif vol_ratio <= 0.7:
                    result['signal'] = f'🔴 两市缩量（量能{vol_ratio:.2f}x，成交额{today_amount:.0f}亿）'
                else:
                    result['signal'] = f'⚪ 量能平稳（{vol_ratio:.2f}x，成交额{today_amount:.0f}亿）'
                result['available'] = True
    except Exception as e:
        log.warning(f"两市量能分析失败: {e}")
    return result


def fetch_event_calendar(code):
    """事件日历：未来90天限售解禁 + 最新业绩预告
    数据源：东财 datacenter（RPT_LIFT_STAGE 解禁 + RPT_PUBLIC_OP_NEWPREDICT 业绩预告）
    ⚠️ 股东减持：东财 datacenter 报表名无法解析（多候选均 404），暂不提供
    """
    result = {'available': False, 'events': [], 'data_source': ''}
    pure = code[2:] if code.startswith(('sh', 'sz')) else code
    today = datetime.now().strftime('%Y-%m-%d')
    deadline = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%d')
    events = []

    # 1. 限售解禁（未来90天内）
    try:
        url = (f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
               f"reportName=RPT_LIFT_STAGE&columns=SECURITY_CODE,SECURITY_NAME_ABBR,"
               f"FREE_DATE,FREE_SHARES,FREE_RATIO,LIFT_MARKET_CAP&"
               f"filter=(SECURITY_CODE%3D%22{pure}%22)&pageNumber=1&pageSize=100&"
               f"sortColumns=FREE_DATE&sortTypes=1")
        data = _em_http_json(url, headers={'Referer': 'https://data.eastmoney.com/'}, timeout=10)
        if data and data.get('success'):
            items = (data.get('result') or {}).get('data', []) or []
            for it in items:
                free_date = (it.get('FREE_DATE') or '')[:10]
                if free_date and today <= free_date <= deadline:
                    free_cap = round(float(it.get('LIFT_MARKET_CAP', 0) or 0) / 10000, 2)  # 万元→亿
                    free_ratio = round(float(it.get('FREE_RATIO', 0) or 0) * 100, 2)
                    events.append({
                        'type': '解禁', 'date': free_date,
                        'desc': f"解禁市值{free_cap}亿，占总股本{free_ratio}%",
                    })
    except Exception as e:
        log.warning(f"解禁数据获取失败: {e}")

    # 2. 业绩预告（最新一期）
    try:
        url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
               f"reportName=RPT_PUBLIC_OP_NEWPREDICT&columns=SECURITY_CODE,SECURITY_NAME_ABBR,"
               f"PREDICT_FINANCE_CODE,PREDICT_AMT_LOWER,PREDICT_AMT_UPPER,PREDICT_TYPE,"
               f"PREDICT_CONTENT,NOTICE_DATE&"
               f"filter=(SECURITY_CODE%3D%22{pure}%22)&pageNumber=1&pageSize=5&"
               f"sortColumns=NOTICE_DATE&sortTypes=-1")
        data = _em_http_json(url, headers={'Referer': 'https://data.eastmoney.com/'}, timeout=10)
        if data and data.get('success'):
            items = (data.get('result') or {}).get('data', []) or []
            if items:
                it = items[0]
                pred_type = it.get('PREDICT_TYPE', '') or ''
                amt_low = round(float(it.get('PREDICT_AMT_LOWER', 0) or 0) / 1e8, 1)
                amt_high = round(float(it.get('PREDICT_AMT_UPPER', 0) or 0) / 1e8, 1)
                content = (it.get('PREDICT_CONTENT') or '')[:60]
                events.append({
                    'type': '业绩预告', 'date': (it.get('NOTICE_DATE') or '')[:10],
                    'desc': f"{pred_type}净利{amt_low}~{amt_high}亿 | {content}",
                })
    except Exception as e:
        log.warning(f"业绩预告获取失败: {e}")

    if events:
        result['available'] = True
        result['events'] = events
        result['data_source'] = 'eastmoney_datacenter'
    result['tz_note'] = '本日历全部为北京时间口径；海外事件（如美股财报）必须经 annotate_overseas_event() 双时区换算后再引用'
    return result


def hk_market_session(now=None):
    """港股交易时段判定（2026-09-30 P0-7：港股行情引用必须带时段口径标注）
    背景: 竞价阶段(9:30前)引用 hkHSI 实为上一交易日收盘，曾被误当"今晨领先信号"。
    港股时段(北京时间): 竞价09:00-09:30 / 上午09:30-12:00 / 午休12:00-13:00 / 下午13:00-16:00
    返回 dict: {'status': ..., 'note': ...}
    ⚠️ 简化实现未含港股假期表（周末判定+时段判定），法定假期须人工核对——
    且港股交易日历与A股不同（如国庆A股休市港股开市），跨市引用先核对日历。
    """
    now = now or datetime.now()
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return {'status': '休市日', 'note': '今日为周末，港股未开盘——显示为上一交易日收盘数据'}
    if hm < 570:
        return {'status': '未开盘', 'note': '港股未开盘（北京时间09:30前）——当前报价为上一交易日收盘，不得作为今日领先信号'}
    if hm < 720:
        return {'status': '盘中', 'note': '港股上午盘进行中，报价为实时口径'}
    if hm < 780:
        return {'status': '午间休市', 'note': '港股午间休市（12:00-13:00）——报价为上午收盘口径'}
    if hm < 960:
        return {'status': '盘中', 'note': '港股下午盘进行中，报价为实时口径'}
    return {'status': '已收盘', 'note': f'港股已收盘（16:00后）——报价为{now:%Y-%m-%d}收盘数据'}


def annotate_overseas_event(name, et_dt_str, kind=''):
    """海外事件→北京时间换算 + A股可交易性标注（2026-09-30 P0-4）
    背景: 美光财报曾被误判"周二盘后"（实为美东周三盘后=北京时间周四凌晨）——
    差一天导致节前博弈结构判断全错。海外事件一律双时区呈现。
    参数: name 事件名; et_dt_str 美东时间 'YYYY-MM-DD HH:MM'（24h制）;
          kind 'AMC'美股盘后 / 'BMO'美股盘前 / 其他原文
    返回 dict: {'name','et','beijing','trade_note'}
    换算: 夏令时(3月第2个周日~11月第1个周日)ET=UTC-4→北京+12h；其余EST=UTC-5→+13h
    ⚠️ 未含A股法定假日校验——若事件落地日恰逢A股休市（如国庆），反应顺延至节后首个交易日
    """
    try:
        et = datetime.strptime(et_dt_str.strip(), '%Y-%m-%d %H:%M')
    except Exception as e:
        return {'name': name, 'et': et_dt_str, 'beijing': '', 'trade_note': f'时间解析失败: {e}'}

    def _nth_sunday(year, month, n):
        d = datetime(year, month, 1)
        return d + timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))

    is_dst = _nth_sunday(et.year, 3, 2) <= et < _nth_sunday(et.year, 11, 1)
    beijing = et + timedelta(hours=12 if is_dst else 13)
    kind_note = {'AMC': '美股盘后', 'BMO': '美股盘前'}.get(kind, kind)
    bj_hm = beijing.hour * 60 + beijing.minute
    bj_fmt = beijing.strftime('%m-%d %H:%M')
    if beijing.weekday() >= 5:
        trade_note = f'事件落地于北京周末（{bj_fmt}），A股下个交易日开盘才可反应'
    elif bj_hm < 570:
        trade_note = f'事件落地于北京时间 {bj_fmt}（A股竞价前）——当日开盘即可反应'
    elif 690 <= bj_hm < 780:
        trade_note = f'事件落地于北京时间 {bj_fmt}（A股午休）——13:00午后盘反应'
    elif bj_hm >= 900:
        trade_note = f'事件落地于北京时间 {bj_fmt}（A股已收盘）——次日开盘反应'
    else:
        trade_note = f'事件落地于北京时间 {bj_fmt}（A股盘中）——盘中即时反应'
    prefix = f'[{kind_note}] ' if kind_note else ''
    bj_date = beijing.strftime('%m-%d')
    # A股法定假日常用窗口（2026版，每年年初人工更新；覆盖春节/国庆两大长假期）
    _A_SHARE_HOLIDAYS = {'01-01': ('01-01', '01-03'),
                         '02-16': ('02-16', '02-22'),   # 2026春节(示例窗口，按国务院通知校准)
                         '10-01': ('10-01', '10-08')}   # 2026国庆中秋连休
    holiday_note = ''
    for _anchor, (_s, _e) in _A_SHARE_HOLIDAYS.items():
        if _s <= bj_date <= _e:
            holiday_note = '；⚠️ 事件落地于A股法定假期（休市），反应顺延至节后首个交易日'
            break
    return {'name': name, 'et': et_dt_str, 'beijing': beijing.strftime('%Y-%m-%d %H:%M'),
            'trade_note': f'{prefix}北京时间 {bj_fmt}；{trade_note}{holiday_note}'}


def fetch_macro():
    """获取宏观数据（大盘指数+板块资金流+市场广度）"""
    result = {}
    
    # 大盘指数
    indices_codes = ['sh000001', 'sz399001', 'sz399006', 'sh000688', 'sh000300']
    names = {'sh000001':'上证指数','sz399001':'深证成指','sz399006':'创业板指','sh000688':'科创50','sh000300':'沪深300'}
    indices = []
    for code in indices_codes:
        q = fetch_quote_tencent(code)
        if q:
            indices.append({'name': names[code], 'price': q['price'], 'pct': q['pct']})
    result['indices'] = indices
    
    # 板块资金流
    sf = fetch_sector_flow()
    result['sectors'] = sf.get('sectors', []) if sf and sf.get('available') else []
    result['sectors_available'] = bool(sf and sf.get('available'))
    result['sectors_data_source'] = sf.get('data_source') if sf else None
    
    # 市场宽度：全市场涨跌家数 + 涨停/跌停/连板（多域名轮询 + push2ex 涨停池）
    breadth = fetch_market_breadth()
    result['breadth'] = breadth
    result['breadth_signal'] = analyze_market_breadth(breadth).get('signal', '')

    # 市场情绪温度计：涨停/连板/炸板率综合评分
    result['sentiment'] = analyze_market_sentiment(breadth)

    # 两市量能趋势：今日成交额 + 量能倍数（vs 20日均量）
    result['volume_trend'] = analyze_market_volume()

    return result


# ============================================================
# 模块六-B：强势股池推荐（大盘向好时拉走势向好股）
# ============================================================
def fetch_hot_stocks(top_n=10, watchlist_pool=None):
    """获取强势股池推荐（大盘向好时拉走势向好股）
    数据源优先级（应对东方财富限流，逐级降级兜底）：
    1. 东方财富 clist 涨幅榜（push2 接口，f3=涨跌幅排序，盘中限流高）
    2. 东方财富板块榜→领涨板块成分股反推（push2 板块+成分股两步）
    3. 关注股池兜底 + 技术面评分（腾讯单股行情稳定可用）

    参数：
    - top_n: 返回股票数量上限
    - watchlist_pool: 兜底股池（默认用 STOCK_MAP 中的关注股池）

    返回：{'available': bool, 'stocks': [{'code','name','price','pct','reason'}], 'data_source'}
    """
    import urllib.parse as _ulp
    result = {'available': False, 'stocks': [], 'data_source': '限流(全部数据源失败)'}

    # ── 方案1：东方财富 clist 涨幅榜（f3 降序）──
    try:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?"
               "fid=f3&po=1&pz={top_n}&pn=1&np=1&fltt=2&invt=2"
               "&fields=f2,f3,f12,f14,f15,f16,f17"
               "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23").format(top_n=top_n)
        data = _em_http_json(url, timeout=8)
        items = data.get('data', {}).get('diff', []) if data and data.get('data') else []
        if items:
            stocks = []
            for it in items[:top_n]:
                code = it.get('f12', '')
                # 东财 clist 返回纯6位代码，加市场前缀便于下游统一处理
                mkt = 'sh' if code.startswith(('60','68')) else 'sz'
                stocks.append({
                    'code': f"{mkt}{code}",
                    'name': it.get('f14', ''),
                    'price': float(it.get('f2', 0) or 0),
                    'pct': float(it.get('f3', 0) or 0),
                    'reason': f"涨幅榜 {it.get('f3', 0)}%",
                })
            result['available'] = True
            result['stocks'] = stocks
            result['data_source'] = 'eastmoney_clist'
            return result
    except Exception as e:
        log.warning(f"fetch_hot_stocks 方案1(涨幅榜)失败: {e}")

    # ── 方案2：板块榜→领涨板块成分股反推 ──
    try:
        # 步骤2a: 拉板块涨幅榜（fetch_sector_flow 已带重试）
        sf = fetch_sector_flow()
        if sf and sf.get('available') and sf.get('sectors'):
            top_sector = sf['sectors'][0]  # 领涨板块
            # 步骤2b: 拉该板块成分股（板块代码格式 BK0xxx → b:BK0xxx）
            sec_code = top_sector.get('code', '')
            url = ("https://push2.eastmoney.com/api/qt/clist/get?"
                   "fid=f3&po=1&pz={n}&pn=1&np=1&fltt=2&invt=2"
                   "&fields=f2,f3,f12,f14,f15,f16,f17"
                   "&fs=b:{sec}").format(n=top_n, sec=sec_code)
            data = _em_http_json(url, timeout=8)
            items = data.get('data', {}).get('diff', []) if data and data.get('data') else []
            if items:
                stocks = []
                for it in items[:top_n]:
                    code = it.get('f12', '')
                    mkt = 'sh' if code.startswith(('60','68')) else 'sz'
                    stocks.append({
                        'code': f"{mkt}{code}",
                        'name': it.get('f14', ''),
                        'price': float(it.get('f2', 0) or 0),
                        'pct': float(it.get('f3', 0) or 0),
                        'reason': f"领涨板块[{top_sector['name']}]成分股 {it.get('f3', 0)}%",
                    })
                result['available'] = True
                result['stocks'] = stocks
                result['data_source'] = 'eastmoney_sector_peer'
                return result
    except Exception as e:
        log.warning(f"fetch_hot_stocks 方案2(板块反推)失败: {e}")

    # ── 方案3：关注股池兜底 + 技术面评分筛选走势向好股 ──
    if not watchlist_pool:
        # 默认用 STOCK_NAME_MAP 中的关注股池（不含指数/ETF）
        watchlist_pool = [
            v for v in STOCK_NAME_MAP.values()
            if v.startswith(('sh60','sz30','sz00')) and not v.startswith(('sh000','sz399'))
        ]
    stocks = []
    for code in watchlist_pool:
        try:
            # 用最小行情请求（绕过 fetch_main_flow 避免限流）
            q = _fetch_quote_minimal(code)
            if not q or q.get('price', 0) <= 0:
                continue
            # 拉技术面评分（多周期趋势 + RSI + MACD）
            tech = analyze_technical(code, q['price'])
            if not tech:
                continue
            # 走势向好筛选：趋势多头/中身 + RSI 40-70 + MACD 不空
            trend = tech.get('trend', '')
            rsi = tech.get('rsi', 50)
            # tech['macd'] 是 float（柱状值），tech['duo_kong'] 是多空方向字符串
            macd_bar = tech.get('macd', 0)  # float: 柱状值，>0 多头 <0 空头
            duo_kong = tech.get('duo_kong', '')  # 做多/做空
            score = 0
            reasons = []
            if '多头' in trend:
                score += 2
                reasons.append(f"趋势{trend}")
            elif '中身' in trend or '震荡' in trend:
                score += 1
                reasons.append(f"趋势{trend}")
            if 40 <= rsi <= 70:
                score += 1
                reasons.append(f"RSI{rsi:.0f}")
            # MACD 柱状值>0 视为多头
            if isinstance(macd_bar, (int, float)) and macd_bar > 0:
                score += 1
                reasons.append("MACD多")
            elif '做多' in duo_kong:
                score += 1
                reasons.append("多空做多")
            if score >= 2:  # 至少2分才算走势向好
                stocks.append({
                    'code': code,
                    'name': q.get('name', code),
                    'price': q['price'],
                    'pct': q.get('pct', 0),
                    'reason': '关注池+' + '+'.join(reasons) + f' (评分{score})',
                    'score': score,
                })
        except Exception as e:
            log.warning(f"fetch_hot_stocks 方案3({code})失败: {e}")
    if stocks:
        # 按评分降序，取 top_n
        stocks.sort(key=lambda x: x.get('score', 0), reverse=True)
        result['available'] = True
        result['stocks'] = stocks[:top_n]
        result['data_source'] = 'watchlist+tech_score(兜底)'
    return result


# ============================================================
# 模块七：融资融券分析
# ============================================================
def fetch_margin_data(code, quote=None):
    """获取个股资金流向数据（从腾讯自选股提取）"""
    result = {'available': False, 'net_flow': 0, 'main_in': 0, 'main_out': 0}
    if quote:
        # 从腾讯自选股行情数据中提取资金流向
        main_net = quote.get('main_net', 0)
        if main_net != 0:
            result['available'] = True
            result['net_flow'] = main_net
            result['main_in'] = quote.get('main_in', 0)
            result['main_out'] = quote.get('main_out', 0)
    return result


# ============================================================
# 模块八：龙虎榜数据
# ============================================================
def fetch_dragon_tiger(code):
    """获取龙虎榜数据（上榜原因/净买额/机构买入数/上榜后表现）
    ⚠️ 2026-08-04 修复：正确报表为 RPT_DAILYBILLBOARD_DETAILS（旧名
    RPT_DAILYBILLBOARD_BILLBOARD_DETAILS 不存在，导致一直无数据）
    ⚠️ 2026-08-18 修复：同一交易日可能因多个原因同时上榜（如"日换手率30%"
    与"连续3日涨幅偏离30%"），原代码只取 items[0] 会漏报上榜原因且低估
    机构买入家数。现合并最新交易日全部记录：机构数取各原因最大值，
    买卖/净额取净额绝对值最大的一条（不同原因口径不同，累加会重复计算）。
    区分三种状态：有上榜记录 / 近期未上榜（正常）/ 接口异常
    """
    result = {'available': False, 'reason': '', 'reasons': [], 'bought': 0, 'sold': 0, 'net': 0,
              'seats': [], 'trade_date': '', 'note': '', 'inst_count': 0,
              'd1': None, 'd5': None, 'd10': None, 'record_count': 0,
              # 席位类型统计（2026-08-20 新增：机构/游资 买卖金额）
              'inst_buy': 0, 'inst_sell': 0, 'inst_net': 0,
              'hot_buy': 0, 'hot_sell': 0, 'hot_net': 0}
    pure = code[2:] if code.startswith(('sh', 'sz')) else code
    try:
        url = (f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
               f"reportName=RPT_DAILYBILLBOARD_DETAILS&"
               f"columns=SECURITY_CODE,TRADE_DATE,EXPLANATION,EXPLAIN,"
               f"BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,BILLBOARD_NET_AMT,"
               f"D1_CLOSE_ADJCHRATE,D5_CLOSE_ADJCHRATE,D10_CLOSE_ADJCHRATE&"
               f"filter=(SECURITY_CODE=%22{pure}%22)&pageNumber=1&pageSize=10&"
               f"sortTypes=-1&sortColumns=TRADE_DATE")
        data = _em_http_json(url, headers={'Referer': 'https://data.eastmoney.com/'}, timeout=10)
        if data is None:
            result['note'] = '龙虎榜接口无响应'
            return result
        result_obj = data.get('result') or {}
        items = result_obj.get('data', []) or []
        if not items:
            result['note'] = '近期未上榜（龙虎榜仅上榜个股有数据，属正常）'
            return result
        # 仅取最新交易日的全部记录（同日可能因多原因重复上榜）
        latest_date = (items[0].get('TRADE_DATE') or '')[:10]
        day_items = [it for it in items if (it.get('TRADE_DATE') or '')[:10] == latest_date]
        # 主记录：净额绝对值最大的一条（买卖/净额展示口径）
        main_item = max(day_items, key=lambda x: abs(float(x.get('BILLBOARD_NET_AMT', 0) or 0)))
        result['available'] = True
        result['trade_date'] = latest_date
        result['record_count'] = len(day_items)
        # 合并全部上榜原因（去重保序）
        reasons = []
        for it in day_items:
            r = (it.get('EXPLANATION', '') or '').strip()[:50]
            if r and r not in reasons:
                reasons.append(r)
        result['reasons'] = reasons
        result['reason'] = '；'.join(reasons)
        result['bought'] = round(float(main_item.get('BILLBOARD_BUY_AMT', 0) or 0) / 1e8, 2)
        result['sold'] = round(float(main_item.get('BILLBOARD_SELL_AMT', 0) or 0) / 1e8, 2)
        result['net'] = round(float(main_item.get('BILLBOARD_NET_AMT', 0) or 0) / 1e8, 2)
        # 机构买入统计（EXPLAIN 形如 "1家机构买入，成功率55.22%"）——取同日各原因最大值
        inst_count = 0
        for it in day_items:
            m = re.search(r'(\d+)家机构买入', (it.get('EXPLAIN', '') or ''))
            if m:
                inst_count = max(inst_count, int(m.group(1)))
        result['inst_count'] = inst_count
        # 上榜后 1/5/10 日表现（后复权涨跌幅%，取主记录）
        result['d1'] = round(float(main_item.get('D1_CLOSE_ADJCHRATE', 0) or 0), 2)
        result['d5'] = round(float(main_item.get('D5_CLOSE_ADJCHRATE', 0) or 0), 2)
        result['d10'] = round(float(main_item.get('D10_CLOSE_ADJCHRATE', 0) or 0), 2)
        # ── 席位类型统计（2026-08-20 新增：机构/游资）────────────────
        # 游资识别：营业部名不含"机构专用"即视为游资/其他（开源西安太华路等为知名游资）
        try:
            seat_rows = []
            for seat_rn in ('RPT_BILLBOARD_DAILYDETAILSBUY', 'RPT_BILLBOARD_DAILYDETAILSSELL'):
                surl = (f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
                        f"reportName={seat_rn}&columns=ALL&pageNumber=1&pageSize=20&"
                        f"filter=(TRADE_DATE%3D%27{latest_date}%27)(SECURITY_CODE%3D%22{pure}%22)")
                sdata = http_get_json(surl, headers={'Referer': 'https://data.eastmoney.com/'}, timeout=10)
                sitems = ((sdata or {}).get('result') or {}).get('data') or []
                for s in sitems:
                    name = (s.get('OPERATEDEPT_NAME') or '').strip()
                    if not name:
                        continue
                    buy = float(s.get('BUY', 0) or 0)
                    sell = float(s.get('SELL', 0) or 0)
                    seat_rows.append({'name': name, 'buy': buy, 'sell': sell})
            inst_buy = sum(s['buy'] for s in seat_rows if '机构专用' in s['name'])
            inst_sell = sum(s['sell'] for s in seat_rows if '机构专用' in s['name'])
            hot_buy = sum(s['buy'] for s in seat_rows if '机构专用' not in s['name'])
            hot_sell = sum(s['sell'] for s in seat_rows if '机构专用' not in s['name'])
            result['inst_buy'] = round(inst_buy / 1e8, 2)
            result['inst_sell'] = round(inst_sell / 1e8, 2)
            result['inst_net'] = round((inst_buy - inst_sell) / 1e8, 2)
            result['hot_buy'] = round(hot_buy / 1e8, 2)
            result['hot_sell'] = round(hot_sell / 1e8, 2)
            result['hot_net'] = round((hot_buy - hot_sell) / 1e8, 2)
            # 席位明细（按金额排序，取买卖各前3）
            result['seats'] = sorted(seat_rows, key=lambda x: -(x['buy'] + x['sell']))[:6]
        except Exception:
            pass
        return result
    except Exception as e:
        log.warning(f"fetch_dragon_tiger 失败: {e}")
        result['note'] = f'龙虎榜接口异常: {e}'
        return result


# ============================================================
# 模块九：股东结构分析 + 行业对比
# ============================================================
def fetch_shareholders(code):
    """获取股东结构数据（东方财富 F10 十大流通股东接口）
    ⚠️ 2026-08-04 修复：真实字段为 HOLDER_RANK/HOLD_NUM/HOLD_NUM_RATIO/HOLD_NUM_CHANGE
    （旧代码用 RANK/HOLD_NUM 字段名错误导致一直无数据）
    """
    result = {'available': False, 'holders': [], 'total_holders': 0, 'change_pct': 0}
    try:
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get?"
               "reportName=RPT_F10_EH_HOLDERS&"
               "columns=SECURITY_CODE,HOLDER_NAME,HOLD_NUM,HOLD_NUM_RATIO,HOLDER_RANK,HOLD_NUM_CHANGE,END_DATE&"
               f"filter=(SECURITY_CODE=%22{pure}%22)&pageNumber=1&pageSize=10&"
               "sortTypes=-1&sortColumns=END_DATE")
        data = _em_http_json(url, headers={'Referer': 'https://emweb.eastmoney.com/'}, timeout=10)
        items = data.get('result', {}).get('data', []) if data.get('result') else []
        if items:
            result['available'] = True
            # 取最新报告期（接口未按日期排序，需自行取 max END_DATE）
            latest = max((it.get('END_DATE', '') or '')[:10] for it in items)
            result['report_period'] = latest
            for it in items:
                if (it.get('END_DATE') or '')[:10] != latest:
                    continue
                result['holders'].append({
                    'name': it.get('HOLDER_NAME', '')[:20],
                    'hold': round(float(it.get('HOLD_NUM', 0) or 0) / 1e8, 2),      # 亿股
                    'ratio': round(float(it.get('HOLD_NUM_RATIO', 0) or 0), 2),     # %
                    'change': it.get('HOLD_NUM_CHANGE', '') or '',                  # 不变/增持/减持
                })
        return result
    except Exception as e:
        log.warning(f"fetch_shareholders 失败: {e}")
        return result


def fetch_industry_peers(code, max_peers=5):
    """获取同行业股票对比（东方财富行业分类接口自动化）
    步骤：1. 用 get_stock_sector_bk 拿正确 BK 代码（f127 是行业名非板块代码，勿直接用）
         2. 取该板块成分股 3. 获取实时行情
    防限流(2026-08-24)：改用 push2 数字镜像多域名轮询（原主域名 HTTPS 常被限流）
    """
    result = {'peers': [], 'industry': '', 'industry_code': ''}
    try:
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        market = '1' if code.startswith('sh') else '0'
        # 行业板块 BK 代码：走 get_stock_sector_bk（f127=行业名，需在板块列表匹配 BK）
        sbk = get_stock_sector_bk(code)
        industry_code = sbk.get('bk_code', '')
        industry_name = sbk.get('name', '') or sbk.get('industry', '')
        if not industry_code:
            return result
        result['industry'] = industry_name
        result['industry_code'] = industry_code
        # 取该行业板块成分股（按涨跌幅排序，统一多域名轮询）
        items = []
        data2 = _em_probe_all(
            lambda s, h: (f"{s}://{h}/api/qt/clist/get?"
                          f"pn=1&pz={max_peers + 5}&po=1&np=1&fltt=2&invt=2"
                          f"&fields=f2,f3,f12,f13,f14&fs=b:{industry_code}"),
            _EM_PUSH2_HOSTS, timeout=8,
            valid=lambda d: bool((d.get('data') or {}).get('diff')))
        if data2:
            items = (data2.get('data') or {}).get('diff', []) or []
        for it in items[:max_peers]:
            peer_code = it.get('f12', '')
            peer_market = 'sh' if str(it.get('f13', 0)) == '1' else 'sz'
            # 排除自身
            if peer_code == pure:
                continue
            result['peers'].append({
                'name': it.get('f14', ''),
                'code': f"{peer_market}{peer_code}",
                'price': float(it.get('f2', 0) or 0),
                'pct': float(it.get('f3', 0) or 0),
            })
    except Exception as e:
        log.warning(f"fetch_industry_peers 失败: {e}")
    return result


# ── 行业定价锚对标（2026-08-24 新增）────────────────────────────
# 教训来源：拿"上游材料(PE 85)"与"下游组装(PE 26)"比估值属跨环节误判——
# 估值定价锚必须按同行业/同环节对比，而非组合内部横向比或绝对 PE。
def _parse_tencent_line(line):
    """解析腾讯批量行情单行 v_sh600519="..."，返回 (代码, dict) 或 None"""
    line = line.strip()
    if not line.startswith('v_'):
        return None
    try:
        key = line[2:line.index('=')]
        parts = line.split('"')[1].split('~')
        if len(parts) < 40:
            return None
        def sf(idx, default=0):
            return float(parts[idx]) if idx < len(parts) and parts[idx].strip() else default
        return key, {'name': parts[1], 'price': sf(3), 'pe': sf(39),
                     'pct': sf(32)}
    except Exception:
        return None


def fetch_peer_valuation_benchmark(code, max_peers=8):
    """行业定价锚对标：同行业/同环节成分股 PE(动) 对比 + 本股分位
    数据源：腾讯批量行情（web.sqt.gtimg.cn，一次拉多只，字段39=PE动）
    分位：本股 PE 在同业有效 PE（>0）中的百分位（0=同业最便宜，100=同业最贵）
    返回：{'available', 'industry', 'self_pe', 'peers':[{name,code,price,pe,pct}],
          'median_pe', 'percentile', 'signal'}
    """
    result = {'available': False, 'industry': '', 'self_pe': 0, 'peers': [],
              'median_pe': 0, 'percentile': None, 'signal': ''}
    try:
        ind = fetch_industry_peers(code, max_peers=max_peers)
        if not ind.get('peers'):
            return result
        result['industry'] = ind.get('industry', '')
        # 本股 + 同业代码（去重），本股也纳入以算分位
        codes = [code] + [p['code'] for p in ind['peers'] if p.get('code')]
        uniq, seen = [], set()
        for c in codes:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        tc_str = ','.join(to_tencent_code(c) for c in uniq)
        cmd = f'curl -s --connect-timeout 5 "https://web.sqt.gtimg.cn/q={tc_str}"'
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
        raw = r.stdout.decode('gbk', errors='replace')
        quotes = {}
        for line in raw.split(';'):
            parsed = _parse_tencent_line(line)
            if parsed:
                quotes[parsed[0]] = parsed[1]
        # 组装 peers（含本股，标注 is_self）
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        peers = []
        for c in uniq:
            q = quotes.get(to_tencent_code(c))
            if not q:
                continue
            peers.append({'name': q['name'], 'code': c, 'price': q['price'],
                          'pe': q['pe'], 'pct': q['pct'],
                          'is_self': (c[2:] if len(c) > 2 else c) == pure})
        if not peers:
            return result
        result['peers'] = peers
        self_q = next((p for p in peers if p['is_self']), None)
        if self_q:
            result['self_pe'] = self_q['pe']
        # 有效 PE（>0，剔除亏损/无PE）计算中位与分位
        valid = sorted([p['pe'] for p in peers if p['pe'] and p['pe'] > 0])
        if valid:
            n = len(valid)
            result['median_pe'] = valid[n // 2] if n % 2 else (valid[n // 2 - 1] + valid[n // 2]) / 2
            self_pe = result['self_pe']
            if self_pe and self_pe > 0:
                # 分位 = 同业中 PE 低于本股的比例（含本股自身的位次）
                lower = sum(1 for v in valid if v < self_pe)
                result['percentile'] = round(lower / n * 100, 1)
                pct = result['percentile']
                if pct <= 30:
                    result['signal'] = f'🟢 估值低于同业多数（分位{pct:.0f}%）'
                elif pct <= 70:
                    result['signal'] = f'🟡 同业中位附近（分位{pct:.0f}%）'
                else:
                    result['signal'] = f'🔴 高于同业多数（分位{pct:.0f}%）'
            else:
                result['signal'] = '⚪ 本股亏损/无PE，无法分位'
        result['available'] = True
    except Exception as e:
        log.warning(f"fetch_peer_valuation_benchmark 失败: {e}")
    return result


# ============================================================
# 模块十：主营业务构成 + 涨停原因 + 业绩预告
# ============================================================
def fetch_business_structure(code):
    """获取主营业务构成（东方财富 F10 主营业务接口）
    ⚠️ 2026-08-04 修复：真实字段为 ITEM_NAME/MAIN_BUSINESS_INCOME/MBI_RATIO/REPORT_DATE
    （旧代码用 MAIN_BUSC_INCOME/MAIN_BUSC_INCOME_RATIO 字段名错误导致一直无数据）
    """
    result = {'available': False, 'main_business': [], 'report_period': ''}
    try:
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get?"
               "reportName=RPT_F10_FN_MAINOP&"
               "columns=SECURITY_CODE,REPORT_DATE,ITEM_NAME,MAIN_BUSINESS_INCOME,MBI_RATIO&"
               f"filter=(SECURITY_CODE=%22{pure}%22)&pageNumber=1&pageSize=30")
        data = _em_http_json(url, headers={'Referer': 'https://emweb.eastmoney.com/'}, timeout=10)
        items = data.get('result', {}).get('data', []) if data.get('result') else []
        if items:
            # 取最新报告期的主营构成（接口未排序，需自行取最新 REPORT_DATE）
            latest = max((it.get('REPORT_DATE', '') or '')[:10] for it in items)
            result['report_period'] = latest
            for it in items:
                if (it.get('REPORT_DATE') or '')[:10] != latest:
                    continue
                result['main_business'].append({
                    'name': it.get('ITEM_NAME', '')[:15],
                    'ratio': round(float(it.get('MBI_RATIO', 0) or 0) * 100, 1),  # 占比%
                })
            result['main_business'].sort(key=lambda x: x['ratio'], reverse=True)
            result['available'] = True
        return result
    except Exception as e:
        log.warning(f"fetch_business_structure 失败: {e}")
        return result


def fetch_limit_reason(code):
    """获取涨停/跌停原因（东方财富涨停板池接口）"""
    result = {'available': False, 'reason': '', 'type': ''}
    try:
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        url = ("https://push2ex.eastmoney.com/getTopicZTPool?"
               "ut=7eea3edcaed734bea9c&dpt=wz.ztzt&Pageindex=0&pagesize=20&date="
               + datetime.now().strftime('%Y%m%d'))
        data = _em_http_json(url, timeout=10)
        pool = data.get('data', {}).get('pool', []) if data.get('data') else []
        for it in pool:
            if it.get('c') == pure:
                result['available'] = True
                result['type'] = '涨停'
                result['reason'] = (it.get('hybk') or '')[:50]
                return result
        return result
    except Exception as e:
        log.warning(f"fetch_limit_reason 失败: {e}")
        return result


def fetch_performance_forecast(code):
    """获取业绩预告/快报数据"""
    result = {'available': False, 'type': '', 'period': '', 'profit_growth': 0}
    pure = code[2:] if code.startswith(('sh','sz')) else code
    try:
        url = (f"https://datacenter.eastmoney.com/securities/api/data/v1/get?"
               f"reportName=RPT_F10_PROFIT_FORECAST&"
               f"columns=SECURITY_CODE,REPORT_DATE,FORECAST_TYPE,"
               f"PROFIT_GROWTH_RATE,CHANGE_REASON&"
               f"filter=(SECURITY_CODE=%22{pure}%22)&pageNumber=1&pageSize=3&"
               f"sortTypes=-1&sortColumns=REPORT_DATE")
        data = _em_http_json(url, headers={'Referer': 'https://emweb.eastmoney.com/'}, timeout=10)
        if not data:
            return result
        result_obj = data.get('result') or {}
        items = result_obj.get('data', []) or []
        if items:
            item = items[0]
            result['available'] = True
            result['type'] = item.get('FORECAST_TYPE', '')
            result['period'] = item.get('REPORT_DATE', '')[:10]
            result['profit_growth'] = (item.get('PROFIT_GROWTH_RATE', 0) or 0)
            result['reason'] = (item.get('CHANGE_REASON', '') or '')[:100]
        return result
    except Exception as e:
        log.warning(f"fetch_performance_forecast 失败: {e}")
        return result


# ============================================================
# 模块十一：机构观点聚合
# ============================================================
def fetch_institution_views(code, stock_name):
    """获取机构观点（从新浪财经提取研报评级信息）"""
    result = {'available': False, 'ratings': [], 'buy_count': 0, 'total_count': 0}
    
    cmd = f'curl -s --connect-timeout 6 "https://finance.sina.com.cn/realstock/company/{code}/nc.shtml" -H "Referer: https://finance.sina.com.cn"'
    r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
    html = r.stdout.decode('gbk', errors='replace')
    titles = re.findall(r'title=\"([^\"]{15,80})\"', html)
    
    rating_keywords = ['买入', '增持', '推荐', '优于大市', '跑赢', '看好', '目标价', '研报', '评级']
    ratings = []
    for t in titles:
        t = t.strip()
        if len(t) < 15: continue
        if any(k in t for k in rating_keywords):
            is_buy = any(k in t for k in ['买入', '增持', '推荐', '看好'])
            ratings.append({'title': t[:60], 'is_buy': is_buy})
    
    if ratings:
        result['available'] = True
        result['ratings'] = ratings[:5]
        result['buy_count'] = sum(1 for r in ratings if r['is_buy'])
        result['total_count'] = len(ratings)
    
    return result


def fetch_institution_views_em(code, stock_name, months=6):
    """获取机构评级 + 一致预期（东财 reportapi 个股研报，2026-08-25 新增）
    数据源：reportapi.eastmoney.com/report/list（与 akshare stock_research_report_em 同源，
    东财数据中心"个股研报"页 API），返回近 N 月真实研报：
      - 机构（orgSName）、评级（emRatingName/Value：买入/增持/中性/减持/卖出）
      - 评级变动（ratingChange：调高/调低/维持/首次）
      - 目标价区间（indvAimPriceT/L）
      - 一致预期 EPS/PE（predictThisYearEps/Pe、predictNextYearEps/Pe、predictNextTwoYearEps/Pe）
    结构兼容 fetch_institution_views：{'available', 'ratings':[{title,is_buy,org,rating,
    aim_price,eps_now,pe_now}], 'buy_count','total_count','consensus'}
    """
    result = {'available': False, 'ratings': [], 'buy_count': 0, 'total_count': 0, 'consensus': {}}
    try:
        pure = ''.join(ch for ch in code if ch.isdigit())
        if not pure or len(pure) != 6:
            return result
        begin = (datetime.now() - timedelta(days=30 * max(months, 1))).strftime('%Y-%m-%d')
        end = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        url = (f"https://reportapi.eastmoney.com/report/list?"
               f"industryCode=*&pageSize=20&industry=*&rating=*&ratingChange=*"
               f"&beginTime={begin}&endTime={end}&pageNo=1&fields=&qType=0"
               f"&orgCode=&code={pure}&rcode=&p=1&pageNum=1&pageNumber=1")
        data = _em_http_json(url, headers={'Referer': 'https://data.eastmoney.com/'}, timeout=12)
        if not data or not data.get('data'):
            return result
        items = data['data']
        ratings = []
        for it in items:
            rating_name = (it.get('emRatingName') or '').strip()
            if not rating_name:
                continue
            is_buy = rating_name in ('买入', '增持', '推荐', '强烈推荐', '优于大市')
            aim_t = it.get('indvAimPriceT') or it.get('indvAimPriceL') or ''
            try:
                aim_price = round(float(aim_t), 2) if aim_t else None
            except (TypeError, ValueError):
                aim_price = None
            ratings.append({
                'title': (it.get('title') or '')[:60],
                'is_buy': is_buy,
                'org': it.get('orgSName') or it.get('orgName') or '',
                'rating': rating_name,
                'rating_change': it.get('ratingChange', 0),
                'date': (it.get('publishDate') or '')[:10],
                'aim_price': aim_price,
                'eps_now': it.get('predictThisYearEps'),
                'pe_now': it.get('predictThisYearPe'),
                'author': (it.get('researcher') or '')[:20],
            })
        if not ratings:
            return result
        result['available'] = True
        result['ratings'] = ratings[:6]
        result['buy_count'] = sum(1 for r in ratings if r['is_buy'])
        result['total_count'] = len(ratings)
        # 一致预期（取最新一篇的预测值）
        first = items[0]
        result['consensus'] = {
            'eps_this': first.get('predictThisYearEps'),
            'pe_this': first.get('predictThisYearPe'),
            'eps_next': first.get('predictNextYearEps'),
            'pe_next': first.get('predictNextYearPe'),
            'eps_next2': first.get('predictNextTwoYearEps'),
            'pe_next2': first.get('predictNextTwoYearPe'),
        }
        result['data_source'] = 'eastmoney_reportapi'
    except Exception as e:
        log.warning(f"fetch_institution_views_em 失败: {e}")
    return result


# ============================================================
# 模块十二：K线形态识别
# ============================================================
def recognize_candlestick_patterns(closes, highs, lows, opens=None, volumes=None):
    """识别K线形态（本地计算，带趋势前提过滤+量价确认）
    改进：
    1. 实体中点用 (close+open)/2 而非 (high+low)/2
    2. 反转形态需要前 N 日有相反趋势作为前提
    3. 形态出现时量比 > 1.2 才算有效（放量确认）
    """
    patterns = []
    if len(closes) < 5:
        return patterns

    # 量比（今日量 / 前5日均量）
    vol_ratio = 1.0
    if volumes and len(volumes) >= 6:
        avg5 = sum(volumes[-6:-1]) / 5
        vol_ratio = volumes[-1] / avg5 if avg5 > 0 else 1.0

    # 趋势前提：前 10 日涨跌幅
    trend_up = trend_down = False
    if len(closes) >= 10:
        ret_10d = (closes[-1] - closes[-10]) / closes[-10] * 100
        # 反转形态前提：前 10 日下跌 > 5% 才算"有下跌趋势"
        trend_down = ret_10d < -5
        trend_up = ret_10d > 5

    i = -1
    o = opens[i] if opens else closes[i]
    c = closes[i]
    h = highs[i]
    l = lows[i]
    # 实体中点用 (open+close)/2，实体大小 = |close - open|
    mid = (o + c) / 2
    body = abs(c - o) or 0.01
    lower = min(o, c) - l  # 下影线
    upper = h - max(o, c)   # 上影线

    # 量价确认：放量时形态有效
    vol_confirmed = vol_ratio > 1.2
    vol_tag = '✅放量' if vol_confirmed else '⚪缩量'

    # 锤子线（需要前 10 日下跌趋势作为前提）
    if lower > body * 2 and upper < body * 0.5 and trend_down:
        patterns.append(('🔨 锤子线', '底部反转', '看涨', f'{vol_tag} 量比{vol_ratio:.1f}'))
    # 射击之星（需要前 10 日上涨趋势作为前提）
    if upper > body * 2 and lower < body * 0.5 and trend_up:
        patterns.append(('🏹 射击之星', '顶部反转', '看跌', f'{vol_tag} 量比{vol_ratio:.1f}'))
    # 十字星（需要趋势前提，且实体极小）
    if body < (h - l) * 0.1 and (trend_up or trend_down):
        direction = '顶部变盘' if trend_up else '底部变盘'
        patterns.append(('✝️ 十字星', direction, '中性', f'{vol_tag} 量比{vol_ratio:.1f}'))

    # 阳包阴/阴包阳（用实体中点 (open+close)/2）
    if len(closes) >= 2 and opens is not None:
        o2 = opens[-2]
        c2 = closes[-2]
        mid2 = (o2 + c2) / 2
        body2 = abs(c2 - o2) or 0.01
        # 阳包阴：前日阴线(c2<o2)，今日阳线(c>o)，且今日实体覆盖前日实体
        if c > o and c2 < o2 and c > c2 and o < o2:
            patterns.append(('🟢 阳包阴', '强烈看涨', '看涨', f'{vol_tag} 量比{vol_ratio:.1f}'))
        # 阴包阳：前日阳线，今日阴线，且今日实体覆盖前日实体
        if c < o and c2 > o2 and c < c2 and o > o2:
            patterns.append(('🔴 阴包阳', '强烈看跌', '看跌', f'{vol_tag} 量比{vol_ratio:.1f}'))

    # 三只乌鸦 / 红三兵（检查实体大小，连续 3 根实体递减/递增）
    if len(closes) >= 3 and opens is not None:
        bodies = [abs(closes[-j] - opens[-j]) for j in range(1, 4)]
        # 红三兵：连续 3 根阳线，实体递增，且无长上影
        is_red_3 = all(closes[-j] > opens[-j] for j in range(1, 4))
        body_increasing = bodies[0] < bodies[1] < bodies[2]
        no_long_upper = all(highs[-j] - max(closes[-j], opens[-j]) < bodies[j-1] for j in range(1, 4))
        if is_red_3 and body_increasing and no_long_upper:
            patterns.append(('🟢🟢🟢 红三兵', '持续看涨', '看涨', f'{vol_tag} 量比{vol_ratio:.1f}'))
        # 三只乌鸦：连续 3 根阴线，实体递增
        is_black_3 = all(closes[-j] < opens[-j] for j in range(1, 4))
        if is_black_3 and body_increasing:
            patterns.append(('🐦‍⬛ 三只乌鸦', '持续看跌', '看跌', f'{vol_tag} 量比{vol_ratio:.1f}'))

    # 黄昏之星/早晨之星（用实体中点 (open+close)/2）
    if len(closes) >= 3 and opens is not None:
        o3 = opens[-3]
        c3 = closes[-3]
        mid3 = (o3 + c3) / 2
        body3 = abs(c3 - o3) or 0.01
        # 早晨之星：前日大阴线 + 昨日十字星 + 今日大阳线
        if c3 < o3 and body3 > abs(c3 - mid3) and c > o and c > mid3 and trend_down:
            patterns.append(('🌅 早晨之星', '底部反转', '看涨', f'{vol_tag} 量比{vol_ratio:.1f}'))
        # 黄昏之星：前日大阳线 + 昨日十字星 + 今日大阴线
        if c3 > o3 and body3 > abs(c3 - mid3) and c < o and c < mid3 and trend_up:
            patterns.append(('🌆 黄昏之星', '顶部反转', '看跌', f'{vol_tag} 量比{vol_ratio:.1f}'))

    return patterns


def analyze_candlestick(tech, kline_data):
    """K线形态综合分析（传入 opens/volumes 供量价确认）"""
    if tech and 'error' not in tech:
        kline = kline_data
        if kline and len(kline) >= 5:
            closes = [float(d['close']) for d in kline]
            highs = [float(d['high']) for d in kline]
            lows = [float(d['low']) for d in kline]
            opens = [float(d['open']) for d in kline]
            volumes = [float(d.get('volume', 0)) for d in kline]
            return recognize_candlestick_patterns(closes, highs, lows, opens, volumes)
    return []


# ============================================================
# 模块十四：趋势通道分析（基于通达信趋势通道指标原理）
# ============================================================
def analyze_trend_channel(kline_data, price):
    """
    趋势通道分析 — 基于通达信趋势通道指标原理
    核心算法：线性回归通道 + 偏移量
    输出：上轨/中轨/下轨/通道宽度/斜率/价格位置/信号
    """
    result = {'available': False, 'upper': 0, 'mid': 0, 'lower': 0,
              'width': 0, 'width_pct': 0, 'slope': 0, 'position': '', 'signal': ''}

    if not kline_data or len(kline_data) < 20:
        return result

    closes = [float(d['close']) for d in kline_data]
    highs = [float(d['high']) for d in kline_data]
    lows = [float(d['low']) for d in kline_data]
    n = min(20, len(closes))

    recent_c = closes[-n:]
    recent_h = highs[-n:]
    recent_l = lows[-n:]

    # 线性回归计算中轨（趋势线）
    x = list(range(n))
    y = recent_c
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(x[i] * y[i] for i in range(n))
    sum_xx = sum(x[i] * x[i] for i in range(n))

    denom = n * sum_xx - sum_x * sum_x
    slope = (n * sum_xy - sum_x * sum_y) / denom if denom != 0 else 0
    intercept = (sum_y - slope * sum_x) / n

    mid = slope * (n - 1) + intercept

    # 计算通道宽度（最大偏差的1.5倍）
    deviations = []
    for i in range(n):
        reg_val = slope * i + intercept
        dev = max(abs(recent_h[i] - reg_val), abs(recent_l[i] - reg_val), abs(recent_c[i] - reg_val))
        deviations.append(dev)

    avg_dev = sum(deviations) / n
    channel_width = avg_dev * 1.5

    upper = mid + channel_width
    lower = mid - channel_width
    width_pct = (channel_width / mid * 100) if mid != 0 else 0

    # 价格在通道中的位置 (0~100)
    pos_pct = (price - lower) / (upper - lower) * 100 if upper != lower else 50

    # 位置判断
    if pos_pct > 95:
        position = '🔴 上轨外（超买）'
    elif pos_pct > 80:
        position = '🟡 上轨附近（偏高）'
    elif pos_pct < 5:
        position = '🟢 下轨外（超卖）'
    elif pos_pct < 20:
        position = '🟢 下轨附近（偏低）'
    else:
        position = '⚪ 通道中轨区域'

    # 信号判断
    signals = []
    if price > upper:
        signals.append('🔴 突破上轨（超买/加速）')
    elif price < lower:
        signals.append('🟢 跌破下轨（超卖/反弹）')
    if slope > 0:
        signals.append(f'↗️ 通道向上（斜率{slope:.4f}）')
    elif slope < 0:
        signals.append(f'↘️ 通道向下（斜率{slope:.4f}）')
    else:
        signals.append('➡️ 通道走平')
    if width_pct > 15:
        signals.append('📊 通道宽（高波动）')
    elif width_pct < 5:
        signals.append('📊 通道窄（低波动/变盘前兆）')

    return {
        'available': True, 'upper': upper, 'mid': mid, 'lower': lower,
        'width': channel_width, 'width_pct': width_pct, 'slope': slope,
        'position': position, 'pos_pct': pos_pct,
        'signal': ' | '.join(signals),
    }


# ============================================================
# 模块十五：组合管理与风控（标准化ATR + 仓位建议 + 相关性）
# ============================================================
def calc_atr(kline_data, n=14):
    """标准化 ATR（True Range 的 N 日 EMA）
    TR = max(H-L, |H-前C|, |L-前C|)
    ATR = EMA(TR, n)
    """
    if not kline_data or len(kline_data) < n + 1:
        return 0
    trs = []
    for i in range(1, len(kline_data)):
        h = float(kline_data[i]['high'])
        l = float(kline_data[i]['low'])
        pc = float(kline_data[i - 1]['close'])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < n:
        return sum(trs) / len(trs) if trs else 0
    # EMA 计算
    k = 2 / (n + 1)
    atr = trs[0]
    for tr in trs[1:]:
        atr = tr * k + atr * (1 - k)
    return round(atr, 3)


def calc_position_size(price, atr, account_size=100000, risk_pct=0.02):
    """基于风险预算的仓位建议（凯利公式简化版）
    每笔交易最多亏损账户的 risk_pct（默认2%）
    仓位 = (账户 * risk_pct) / (ATR * 2)  -- 止损用 2 倍 ATR
    返回：建议股数、建议仓位比例、止损价、止盈价
    """
    if price <= 0 or atr <= 0:
        return {'shares': 0, 'position_pct': 0, 'stop_loss': 0, 'take_profit': 0}
    risk_amount = account_size * risk_pct
    stop_distance = atr * 2  # 止损距离 = 2 * ATR（每股止损亏损额，单位：元/股）
    # 风险预算：股数 = 可承受亏损额 / 每股止损距离（再按 100 股取整）。
    # 修复(2026-09-13)：原式误乘 /price *account_size 使股数放大 1000 倍，
    # 导致 2% 风险预算恒被单票20%上限覆盖、atr/risk_pct 实际失效（恒返回20%仓位）。
    max_shares = int(risk_amount / stop_distance / 100) * 100  # 按 100 股取整
    # 单只股票最多用 20% 仓位
    max_position_value = account_size * 0.2
    max_shares_by_value = int(max_position_value / price / 100) * 100
    shares = min(max_shares, max_shares_by_value)
    if shares <= 0:
        shares = 100  # 最少 1 手
    position_pct = round(shares * price / account_size * 100, 1)
    stop_loss = round(price - atr * 2, 2)
    take_profit = round(price + atr * 3, 2)
    return {
        'shares': shares,
        'position_pct': position_pct,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'stop_pct': round((stop_loss - price) / price * 100, 1),
        'profit_pct': round((take_profit - price) / price * 100, 1),
    }


def calc_correlation(codes, datalen=60):
    """计算多只股票收益率的相关性矩阵
    用于组合管理：相关性高的标的不宜同时持有
    返回：{code1: {code2: corr, ...}, ...}
    """
    try:
        import numpy as np
    except ImportError:
        return {}
    returns = {}
    for code in codes:
        kline = fetch_kline_sina(code, 240, datalen)
        if not kline or len(kline) < 10:
            continue
        closes = np.array([float(d['close']) for d in kline])
        ret = np.diff(closes) / closes[:-1] * 100  # 日收益率
        if len(ret) >= 10:
            returns[code] = ret
    if len(returns) < 2:
        return {}
    # 计算两两相关性
    corr_matrix = {}
    codes_with_data = list(returns.keys())
    for i, c1 in enumerate(codes_with_data):
        corr_matrix[c1] = {}
        for j, c2 in enumerate(codes_with_data):
            if i == j:
                corr_matrix[c1][c2] = 1.0
            elif j > i:
                # 对齐长度
                min_len = min(len(returns[c1]), len(returns[c2]))
                if min_len >= 10:
                    r1 = returns[c1][-min_len:]
                    r2 = returns[c2][-min_len:]
                    corr = float(np.corrcoef(r1, r2)[0, 1])
                    corr_matrix[c1][c2] = round(corr, 2)
                else:
                    corr_matrix[c1][c2] = 0
    # 补全对称矩阵
    for c1 in codes_with_data:
        for c2 in codes_with_data:
            if c2 not in corr_matrix.get(c1, {}):
                corr_matrix[c1][c2] = corr_matrix.get(c2, {}).get(c1, 0)
    return corr_matrix


def analyze_portfolio(codes, names=None):
    """组合分析：相关性矩阵 + 分散化建议 + 风险敞口"""
    names = names or {}
    result = {'correlation': {}, 'groups': [], 'advice': ''}
    corr = calc_correlation(codes)
    if not corr:
        return result
    result['correlation'] = corr

    # 按相关性分组：相关性 > 0.7 的标的归为同一组
    visited = set()
    groups = []
    for c1 in codes:
        if c1 in visited:
            continue
        group = [c1]
        visited.add(c1)
        for c2 in codes:
            if c2 in visited:
                continue
            corr_val = corr.get(c1, {}).get(c2, 0)
            if corr_val > 0.7:
                group.append(c2)
                visited.add(c2)
        groups.append(group)
    result['groups'] = groups

    # 分散化建议
    if len(groups) == 1 and len(codes) >= 3:
        result['advice'] = '⚠️ 组合内标的正相关性过高（>0.7），实际分散效果差，建议增加不同行业/风格的标的'
    elif len(groups) < len(codes) * 0.5:
        result['advice'] = '🟡 组合内存在部分高相关性标的，分散化一般'
    else:
        result['advice'] = '🟢 组合分散化良好，各标的相关性较低'
    return result


# ============================================================
# 相似K线匹配（DTW算法）
# ============================================================
def find_similar_kline_patterns(code, pattern_len=30, top_n=5, peers_pool=None):
    """用DTW算法找历史相似K线形态（numpy向量化加速）
    peers_pool: list[code] 同行业股票池，传入时只在该池内扫描（避免全市场扫，提速+降噪）
                 None 时只在个股自身历史K线上找相似（原逻辑）
    """
    try:
        import numpy as np
    except ImportError:
        return {'matches': [], 'avg_future_return': 0, 'up_probability': 0, 'sample_count': 0, 'low_sample': True}
    result = {'matches': [], 'avg_future_return': 0, 'up_probability': 0, 'sample_count': 0, 'low_sample': True}
    kline = fetch_kline_sina(code, 240, 500)
    if not kline or len(kline) < pattern_len + 20:
        return result
    closes = np.array([float(d['close']) for d in kline])

    # 当前模式（最近pattern_len天），用收益率序列归一化（对价格水平不敏感）
    pattern = closes[-pattern_len:]
    pattern_ret = np.diff(pattern) / pattern[:-1] * 100  # 日收益率%
    # 标准化（z-score），消除波动率差异
    p_std = pattern_ret.std()
    pattern_norm = (pattern_ret - pattern_ret.mean()) / (p_std if p_std > 1e-9 else 1.0)

    def dtw_distance(s1, s2):
        """numpy向量化DTW（限制带宽w=5加速，且防止过度匹配）
        内层循环用 np.minimum.reduce 向量化，比逐元素赋值快 3-5x
        """
        n, m = len(s1), len(s2)
        w = max(5, abs(n - m))  # Sakoe-Chiba带宽
        INF = float('inf')
        d = np.full((n + 1, m + 1), INF)
        d[0, 0] = 0
        s1_arr = np.asarray(s1)
        s2_arr = np.asarray(s2)
        for i in range(1, n + 1):
            j_start = max(1, i - w)
            j_end = min(m, i + w) + 1
            # 向量化：一次算出整行 cost + 三方向最小值
            j_idx = np.arange(j_start, j_end)
            cost = np.abs(s1_arr[i - 1] - s2_arr[j_idx - 1])
            # d[i-1, j]、d[i, j-1]、d[i-1, j-1] 三个方向的值
            up = d[i - 1, j_idx]
            left = d[i, j_idx - 1]
            diag = d[i - 1, j_idx - 1]
            best_prev = np.minimum(np.minimum(up, left), diag)
            d[i, j_start:j_end] = cost + best_prev
        return d[n, m] / min(n, m)

    matches = []
    # 默认：在个股自身历史K线上找相似
    scan_codes = [code]
    # peers_pool 模式：在同行业股票池内扫描，避免全市场扫（提速+降噪）
    if peers_pool:
        # 去重并排除自身
        scan_codes = [c for c in dict.fromkeys(peers_pool) if c != code]
        # 最多扫 30 只（避免过多网络请求），同行业通常 < 30
        scan_codes = scan_codes[:30]

    for scan_code in scan_codes:
        # 同行业股票池：每只拉自己的 K 线，模式匹配在各自的收益率序列上
        if scan_code != code:
            peer_kline = fetch_kline_sina(scan_code, 240, 500)
            if not peer_kline or len(peer_kline) < pattern_len + 20:
                continue
            peer_closes = np.array([float(d['close']) for d in peer_kline])
            peer_label = scan_code  # 用于 matches 记录来源
        else:
            peer_closes = closes
            peer_label = None
        for i in range(0, len(peer_closes) - pattern_len - 20):
            window = peer_closes[i:i + pattern_len]
            window_ret = np.diff(window) / window[:-1] * 100
            w_std = window_ret.std()
            window_norm = (window_ret - window_ret.mean()) / (w_std if w_std > 1e-9 else 1.0)
            dist = dtw_distance(pattern_norm, window_norm)
            if dist < 2.0:  # 标准化后阈值更严格
                future_idx = i + pattern_len + 5
                # 越界的匹配跳过，不计入统计（避免 future_ret=0 污染概率）
                if future_idx >= len(peer_closes):
                    continue
                future_ret = (peer_closes[future_idx] - peer_closes[i + pattern_len - 1]) / peer_closes[i + pattern_len - 1] * 100
                m = {'dist': round(dist, 4), 'future_ret': round(float(future_ret), 2), 'pos': i}
                if peer_label:
                    m['source'] = peer_label
                matches.append(m)

    matches.sort(key=lambda x: x['dist'])
    best = matches[:top_n]
    if best:
        result['matches'] = [{'dist': m['dist'], 'future_ret': m['future_ret']} for m in best]
        avg_ret = sum(m['future_ret'] for m in best) / len(best)
        up_prob = sum(1 for m in best if m['future_ret'] > 0) / len(best) * 100
        result['avg_future_return'] = round(avg_ret, 2)
        result['up_probability'] = round(up_prob, 1)
    # P1-8(2026-09-30)：小样本噪声拦截——样本<10 的概率/收益统计不构成信号，仅展示参考
    result['sample_count'] = len(best)
    result['low_sample'] = len(best) < 10
    return result
# ============================================================
def export_kline_chart(code, stock_name, kline_data, tech):
    """生成K线图+主升擒龙信号标注"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import FancyBboxPatch
    from datetime import datetime
    
    if not kline_data or len(kline_data) < 10:
        return
    
    dates = []
    opens, closes, highs, lows = [], [], [], []
    for d in kline_data[-60:]:
        try:
            dt_str = str(d.get('date', '')).strip()
            if not dt_str:
                continue  # �过空日期字段，不刷屏日志
            dt = datetime.strptime(dt_str, '%Y-%m-%d')
            dates.append(dt)
            opens.append(float(d['open']))
            closes.append(float(d['close']))
            highs.append(float(d['high']))
            lows.append(float(d['low']))
        except Exception as e:
            log.debug(f"export_kline_chart 跳过一条记录: {e}")
            continue
    
    if len(dates) < 10:
        return
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={'height_ratios': [3, 1]})
    fig.suptitle(f'{stock_name} ({code}) - K线 + 主升擒龙信号', fontsize=14, fontweight='bold')
    
    # 绘制K线
    for i in range(len(dates)):
        color = 'red' if closes[i] >= opens[i] else 'green'
        ax1.vlines(dates[i], lows[i], highs[i], color=color, linewidth=0.8)
        rect = plt.Rectangle((mdates.date2num(dates[i])-0.3, min(opens[i], closes[i])),
                             0.6, abs(closes[i]-opens[i]), facecolor=color, alpha=0.8)
        ax1.add_patch(rect)
    
    # 均线
    closes_f = [float(d['close']) for d in kline_data]
    if len(closes_f) >= 5:
        ma5 = [sum(closes_f[max(0,i-4):i+1])/min(5,i+1) for i in range(-len(dates), 0)]
        ax1.plot(dates, ma5, 'b-', label='MA5', linewidth=0.8)
    if len(closes_f) >= 10:
        ma10 = [sum(closes_f[max(0,i-9):i+1])/min(10,i+1) for i in range(-len(dates), 0)]
        ax1.plot(dates, ma10, 'orange', label='MA10', linewidth=0.8)
    if len(closes_f) >= 20:
        ma20 = [sum(closes_f[max(0,i-19):i+1])/min(20,i+1) for i in range(-len(dates), 0)]
        ax1.plot(dates, ma20, 'purple', label='MA20', linewidth=0.8)
    
    # 主升擒龙信号标注
    if tech and 'error' not in tech:
        if tech.get('qiang_jin_qiang'):
            ax1.annotate('★强', xy=(dates[-1], closes[-1]), xytext=(10, 10),
                        textcoords='offset points', fontsize=14, color='red',
                        bbox=dict(boxstyle='round,pad=0.3', facecolor='yellow', alpha=0.8))
        elif tech.get('qiang_shi'):
            ax1.annotate('强势', xy=(dates[-1], closes[-1]), xytext=(10, -15),
                        textcoords='offset points', fontsize=12, color='red',
                        bbox=dict(boxstyle='round', facecolor='pink', alpha=0.7))
    
    ax1.set_ylabel('价格')
    ax1.legend(loc='upper left', fontsize=8)
    ax1.grid(True, alpha=0.3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    
    # 成交量
    vols = [float(d.get('volume', 0)) for d in kline_data[-60:]][-len(dates):]
    colors = ['red' if closes[i] >= opens[i] else 'green' for i in range(len(dates))]
    ax2.bar(dates, vols, color=colors, alpha=0.6, width=0.6)
    ax2.set_ylabel('成交量')
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    
    plt.tight_layout()
    out_path = os.path.join(CACHE_DIR, f'{stock_name}_{code[-6:]}_kline.png')
    plt.savefig(out_path, dpi=120, bbox_inches='tight')
    plt.close()
    print(f"  🖼️ K线图已保存: {out_path}")


def export_csv_report(code, stock_name, quote, tech, fund, cap_flow, sentiment, quant_score):
    """导出CSV格式分析报告"""
    import csv
    out_path = os.path.join(CACHE_DIR, f'{stock_name}_{code[-6:]}_report.csv')
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['指标', '数值'])
        w.writerow(['股票', stock_name])
        w.writerow(['代码', code])
        w.writerow(['现价', quote.get('price', 0)])
        w.writerow(['涨跌幅', f"{quote.get('pct', 0):+.2f}%"])
        if tech and 'error' not in tech:
            w.writerow(['趋势', tech.get('trend', '')])
            w.writerow(['多空方向', tech.get('duo_kong', '')])
            w.writerow(['A1', tech.get('a1', 0)])
            w.writerow(['B1', tech.get('b1', 0)])
            w.writerow(['ABC3', tech.get('abc3', 0)])
            w.writerow(['HG', f"{tech.get('hg', 0):+.1f}%"])
            w.writerow(['强势信号', tech.get('qiang_shi', False)])
            w.writerow(['★强信号', tech.get('qiang_jin_qiang', False)])
            w.writerow(['RSI', tech.get('rsi', 0)])
            w.writerow(['MACD', tech.get('macd', 0)])
        w.writerow(['PE', fund.get('pe', 0)])
        w.writerow(['PB', fund.get('pb', 0)])
        w.writerow(['市值(亿)', fund.get('market_cap', 0)])
        if cap_flow.get('level2_available'):
            w.writerow(['Level2主力净额(亿)', cap_flow.get('level2_main_total', 0)])
        w.writerow(['量化评分', quant_score.get('pct', 0)])
        w.writerow(['评分等级', quant_score.get('level', '')])
    print(f"  📋 CSV已导出: {out_path}")


def export_html_report(code, stock_name, quote, tech, fund, cap_flow, sentiment, quant_score, macro=None, completeness=100, roundtable=None):
    """导出 HTML 可视化报告（含评分雷达图 + 新闻卡片 + K线图引用）"""
    import html as _html
    esc = _html.escape

    # 圆桌多视角增强 HTML（Workbuddy 方法论互补）
    roundtable_html = ''
    if roundtable and roundtable.get('views'):
        rt_views = ' · '.join(
            f'<span style="color:{("#27ae60" if d=="多" else "#e74c3c" if d=="空" else "#95a5a6")};font-weight:bold">{v} {d}</span>'
            for v, d in roundtable['views'].items())
        bp = roundtable.get('bull_pct', 50)
        evidence = (f'多头 <b style="color:#27ae60">{roundtable.get("bull_evidence", 0):+.1f}</b> '
                    f'({bp}%) vs 空头 <b style="color:#e74c3c">{roundtable.get("bear_evidence", 0):.1f}</b> ({100-bp}%)')
        parts = [f'<div class="rt-item"><b>共识度</b>：{esc(roundtable.get("consensus", ""))}</div>',
                 f'<div class="rt-item"><b>视角分布</b>：{rt_views}</div>',
                 f'<div class="rt-item"><b>多空证据</b>：{evidence}</div>']
        # 多空辩论权重梯度（Workbuddy 多头/空头研究员镜像梯度）
        bg = roundtable.get('bull_gradient', [])
        rg = roundtable.get('bear_gradient', [])
        if bg or rg:
            bull_str = ' &gt; '.join(f'{esc(k)}+{v}' for k, v in bg[:3]) if bg else '无'
            bear_str = ' &gt; '.join(f'{esc(k)}-{v}' for k, v in rg[:3]) if rg else '无'
            parts.append(f'<div class="rt-item"><b>辩论梯度</b>：'
                         f'多头论据[{bull_str}] | 空头论据[{bear_str}]</div>')
            if roundtable.get('debate'):
                parts.append(f'<div class="rt-item" style="font-weight:bold">{esc(roundtable["debate"])}</div>')
        # 时效加权裁决（截面降权0.4×/实时升权1.5×）
        if 'timing_net' in roundtable:
            tbp = roundtable.get('timing_bull_pct', 50)
            parts.append(f'<div class="rt-item"><b>时效加权裁决</b>：'
                         f'净分 <b>{roundtable.get("timing_net", 0):+.1f}</b>（实时多头 {tbp}%）</div>')
            if roundtable.get('timing_note'):
                parts.append(f'<div class="rt-item" style="color:#7c3aed">{esc(roundtable["timing_note"])}</div>')
        if roundtable.get('trap'):
            trap_color = '#e67e22' if '陷阱' in roundtable['trap'] else '#2980b9'
            parts.append(f'<div class="rt-item" style="color:{trap_color}">{esc(roundtable["trap"])}</div>')
        path = roundtable.get('path')
        if path:
            _low_n = path.get('low_sample') or (path.get('n') or 0) < 10
            _warn = (' <span style="color:#e67e22">⚠️ 小样本（&lt;10），仅展示参考不构成信号</span>'
                     if _low_n else '')
            parts.append(f'<div class="rt-item"><b>历史相似路径概率</b>：'
                         f'上涨 <b style="color:#27ae60">{path["up"]}%</b> / 震荡 {path["flat"]}% / '
                         f'下跌 <b style="color:#e74c3c">{path["down"]}%</b>'
                         f'（{path["n"]}样本，均收益{path["avg_ret"]:+.2f}%）{_warn}</div>')
        obs = roundtable.get('observation', [])
        if obs:
            obs_rows = ''.join(
                f'<tr><td>{esc(v)}</td><td style="font-weight:bold">{esc(cur)}</td><td style="color:#555">{esc(act)}</td></tr>'
                for v, cur, act in obs)
            parts.append(f'<div class="rt-item"><b>关键变量观察台</b>：'
                         f'<table style="width:100%;border-collapse:collapse;margin-top:6px">'
                         f'<tr style="background:#f0f0f0"><th style="padding:4px;text-align:left">变量</th>'
                         f'<th style="padding:4px;text-align:left">当前值</th>'
                         f'<th style="padding:4px;text-align:left">触发线 → 动作</th></tr>{obs_rows}</table></div>')
        inv = roundtable.get('invalidations', [])
        if inv:
            inv_items = ''.join(f'<li>{esc(x)}</li>' for x in inv)
            parts.append(f'<div class="rt-item"><b>结论失效条件</b>（证伪框架）：'
                         f'<ul style="margin:6px 0 0 18px;color:#7c2d12">{inv_items}</ul></div>')
        roundtable_html = f'''
  <div class="section" style="background:#fff8f0;border:1px solid #f5d9a8">
    <h2>🏛️ 圆桌多视角增强</h2>
    {''.join(parts)}
  </div>
'''

    # 评分雷达图数据（6维）
    scores = quant_score.get('scores', {})
    radar_labels = ['动量', '技术', '基本面', '量能', '风险', '舆情', '资金流', 'Level2']
    radar_keys = ['动量', '技术', '基本面', '量能', '风险', '舆情', '资金流', 'Level2']
    radar_data = [scores.get(k, 0) for k in radar_keys]

    # 新闻列表
    news_html = ''
    if sentiment and sentiment.get('news'):
        news_items = []
        for n in sentiment['news'][:8]:
            tag = esc(n.get('tag', ''))
            title = esc(n.get('title', ''))
            sent = n.get('sentiment', '中性')
            sent_color = '#e74c3c' if sent == '利空' else '#27ae60' if sent == '利好' else '#95a5a6'
            news_items.append(
                f'<div class="news-item"><span class="tag">{tag}</span>'
                f'<span class="title">{title}</span>'
                f'<span class="sentiment" style="color:{sent_color}">{sent}</span></div>'
            )
        news_html = ''.join(news_items)

    # 评分详情行
    score_rows = ''
    for k, v in scores.items():
        color = '#27ae60' if v > 0 else '#e74c3c' if v < 0 else '#95a5a6'
        score_rows += f'<tr><td>{k}</td><td style="color:{color};font-weight:bold">{v:+.0f}</td></tr>'

    # 关键指标卡
    price = quote.get('price', 0) if quote else 0
    pct = quote.get('pct', 0) if quote else 0
    pct_color = '#e74c3c' if pct > 0 else '#27ae60' if pct < 0 else '#333'
    # A股涨红跌绿，这里 pct>0 涨用红，跌用绿
    pct_color = '#e74c3c' if pct >= 0 else '#27ae60'

    trend = tech.get('trend', '') if tech else ''
    duo_kong = tech.get('duo_kong', '') if tech else ''
    rsi = tech.get('rsi', 0) if tech else 0
    abc3 = tech.get('abc3', 0) if tech else 0
    hg = tech.get('hg', 0) if tech else 0
    pe = fund.get('pe', 0) if fund else 0
    pb = fund.get('pb', 0) if fund else 0
    roe = fund.get('roe', '-') if fund else '-'
    rev_g = fund.get('rev_growth', '-') if fund else '-'
    profit_g = fund.get('profit_growth', '-') if fund else '-'

    # Level2 / 资金流
    l2_net = cap_flow.get('level2_main_total', 0) if cap_flow and cap_flow.get('level2_available') else 0
    l2_color = '#e74c3c' if l2_net > 0 else '#27ae60' if l2_net < 0 else '#333'

    # 评分等级
    pct_score = quant_score.get('pct', 0)
    level = quant_score.get('level', '')
    level_color = '#27ae60' if pct_score >= 70 else '#f39c12' if pct_score >= 50 else '#e74c3c'

    # 择时过滤
    timing = quant_score.get('timing_filter', '')
    timing_html = f'<div class="warning">⛔ {esc(timing)}</div>' if timing and '⛔' in timing else (
        f'<div class="warning">⚠️ {esc(timing)}</div>' if timing else ''
    )

    # 雷达图 SVG（极坐标）
    import math
    n_axes = len(radar_labels)
    max_val = 6  # 各维度上限差异不大，统一用 6
    center_x, center_y = 150, 150
    radius = 110
    # 计算各点坐标
    points = []
    for i, val in enumerate(radar_data):
        angle = -math.pi / 2 + 2 * math.pi * i / n_axes
        # 负值映射到 0
        r = max(0, min(max_val, val)) / max_val * radius
        x = center_x + r * math.cos(angle)
        y = center_y + r * math.sin(angle)
        points.append((x, y))
    # 网格圆（4层）
    grid_circles = ''
    for layer in range(1, 5):
        r = radius * layer / 4
        grid_circles += f'<circle cx="{center_x}" cy="{center_y}" r="{r}" fill="none" stroke="#eee" stroke-width="1"/>'
    # 轴线 + 标签
    axis_lines = ''
    label_svg = ''
    for i, label in enumerate(radar_labels):
        angle = -math.pi / 2 + 2 * math.pi * i / n_axes
        x_end = center_x + radius * math.cos(angle)
        y_end = center_y + radius * math.sin(angle)
        axis_lines += f'<line x1="{center_x}" y1="{center_y}" x2="{x_end}" y2="{y_end}" stroke="#eee" stroke-width="1"/>'
        # 标签位置稍外推
        lx = center_x + (radius + 18) * math.cos(angle)
        ly = center_y + (radius + 18) * math.sin(angle)
        label_svg += f'<text x="{lx}" y="{ly}" text-anchor="middle" dominant-baseline="middle" font-size="11" fill="#666">{label}</text>'
    # 数据多边形
    polygon_pts = ' '.join(f'{x},{y}' for x, y in points)
    radar_svg = (
        f'<svg width="300" height="300" viewBox="0 0 300 300">'
        f'{grid_circles}{axis_lines}'
        f'<polygon points="{polygon_pts}" fill="rgba(52,152,219,0.3)" stroke="#3498db" stroke-width="2"/>'
        f'{label_svg}'
        f'</svg>'
    )

    # K线图引用（export_kline_chart 生成的 png）
    kline_img = ''
    kline_path = os.path.join(CACHE_DIR, f'{stock_name}_{code[-6:]}_kline.png')
    if os.path.exists(kline_path):
        import base64
        with open(kline_path, 'rb') as f:
            img_b64 = base64.b64encode(f.read()).decode()
        kline_img = f'<div class="section"><h2>📈 K线图</h2><img src="data:image/png;base64,{img_b64}" style="max-width:100%;border:1px solid #eee;border-radius:4px"/></div>'

    # ── 走势解读区块（复用 analyze_technical 已算字段，补 HTML 缺失的叙述层）──
    trend_html = ''
    if tech:
        tparts = []
        # 波段结构与浪数
        if tech.get('wave_count') is not None:
            env_tag = f"（环境[{tech.get('wave_market','')}]）" if tech.get('wave_market') else ''
            tparts.append(f'<div class="rt-item"><b>波段结构</b>：近60日识别 <b>{tech["wave_count"]}</b> 波上涨，'
                          f'当前第 <b>{tech.get("current_wave", 0)}</b> 浪 [{tech.get("wave_state", "")}]{env_tag}</div>')
            if tech.get('wave_signal'):
                tparts.append(f'<div class="rt-item" style="color:#7c3aed">{esc(tech["wave_signal"])}</div>')
        # 量价背离
        if tech.get('divergence') and tech['divergence'] != '无背离':
            tparts.append(f'<div class="rt-item" style="color:#e67e22">{esc(tech["divergence"])}</div>')
        # 相对强弱
        if tech.get('rs_20') is not None:
            rs_ico = '🟢' if tech['rs_20'] > 5 else ('🔴' if tech['rs_20'] < -5 else '⚪')
            tparts.append(f'<div class="rt-item"><b>相对强弱</b>：{rs_ico} vs {esc(tech.get("rs_ref") or "大盘")} '
                          f'当日{tech.get("rs_today", 0):+.1f}% 5日{tech.get("rs_5", 0):+.1f}% '
                          f'20日<b>{tech["rs_20"]:+.1f}%</b> {esc(tech.get("rs_signal") or "")}</div>')
        # 多空线
        if tech.get('duo_kong'):
            dk_icon = '🟢' if tech['duo_kong'] == '做多' else ('🔴' if tech['duo_kong'] == '做空' else '⚪')
            a1s = f'{tech["a1"]:.3f}' if tech.get('a1') is not None else '-'
            b1s = f'{tech["b1"]:.3f}' if tech.get('b1') is not None else '-'
            tparts.append(f'<div class="rt-item"><b>多空线</b>：{dk_icon} {esc(tech["duo_kong"])}（A1={a1s} B1={b1s}）</div>')
        # 关键价位 + 阶段表现
        if tech.get('support') or tech.get('resistance'):
            sup = f'{tech["support"]:.2f}' if tech.get('support') else '-'
            res = f'{tech["resistance"]:.2f}' if tech.get('resistance') else '-'
            tparts.append(f'<div class="rt-item"><b>关键价位</b>：支撑 <b>{sup}</b> ｜ 压力 <b>{res}</b> ｜ '
                          f'近5日{tech.get("pct_5d", 0):+.2f}% / 近20日{tech.get("pct_20d", 0):+.2f}%</div>')
        # 筹码分布（日线 + 60分钟）
        if tech.get('chip_winner_pct') is not None:
            wp = tech['chip_winner_pct']
            cp = tech.get('chip_cost_peak', 0) or 0
            ca = tech.get('chip_cost_avg', 0) or 0
            cc = tech.get('chip_concentration', 0) or 0
            bl = tech.get('chip_bottom_lock', 0) or 0
            tparts.append(f'<div class="rt-item"><b>筹码分布</b>：获利盘 <b>{wp}%</b> ｜ '
                          f'成本峰 <b>{cp:.2f}</b> ｜ 平均成本 {ca:.2f} ｜ '
                          f'集中度 {cc}% ｜ 底部锁定 {bl}%</div>')
            if tech.get('chip_note'):
                tparts.append(f'<div class="rt-item" style="color:#7c3aed">{esc(tech["chip_note"])}</div>')
            if tech.get('chip_60_note'):
                tparts.append(f'<div class="rt-item" style="color:#2980b9">⏱ {esc(tech["chip_60_note"])}</div>')
        if tparts:
            trend_html = ('<div class="section"><h2>📉 走势解读</h2>' + ''.join(tparts) + '</div>')

    html_content = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(stock_name)} ({esc(code)}) 量化分析报告</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       background: #f5f6fa; color: #2c3e50; line-height: 1.6; padding: 20px; }}
.container {{ max-width: 1000px; margin: 0 auto; background: white;
             border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); overflow: hidden; }}
.header {{ background: linear-gradient(135deg, #667eea, #764ba2); color: white;
          padding: 30px; text-align: center; }}
.header h1 {{ font-size: 28px; margin-bottom: 8px; }}
.header .meta {{ font-size: 14px; opacity: 0.9; }}
.section {{ padding: 24px 30px; border-bottom: 1px solid #eee; }}
.section h2 {{ font-size: 18px; margin-bottom: 16px; color: #2c3e50;
              border-left: 4px solid #667eea; padding-left: 10px; }}
.metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }}
.metric-card {{ background: #f8f9fa; padding: 14px; border-radius: 8px; text-align: center; }}
.metric-card .label {{ font-size: 12px; color: #7f8c8d; margin-bottom: 4px; }}
.metric-card .value {{ font-size: 20px; font-weight: bold; }}
.score-box {{ text-align: center; padding: 20px; background: linear-gradient(135deg, #f8f9fa, #e8eaf6);
             border-radius: 10px; margin: 16px 0; }}
.score-box .pct {{ font-size: 48px; font-weight: bold; color: {level_color}; }}
.score-box .level {{ font-size: 18px; margin-top: 8px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #eee; font-size: 14px; }}
th {{ background: #f8f9fa; font-weight: 600; }}
.news-item {{ display: flex; align-items: center; gap: 10px; padding: 10px 0;
             border-bottom: 1px solid #f0f0f0; }}
.news-item .tag {{ font-size: 11px; color: #999; min-width: 60px; }}
.news-item .title {{ flex: 1; font-size: 14px; }}
.news-item .sentiment {{ font-size: 12px; font-weight: bold; min-width: 40px; text-align: right; }}
.warning {{ background: #fff3cd; color: #856404; padding: 12px; border-radius: 6px; margin: 12px 0; }}
.radar-container {{ display: flex; align-items: center; justify-content: center; gap: 30px; flex-wrap: wrap; }}
.footer {{ padding: 20px 30px; text-align: center; font-size: 12px; color: #95a5a6; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>{esc(stock_name)} <span style="font-size:18px;opacity:0.8">({esc(code)})</span></h1>
    <div class="meta">量化分析报告 · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
  </div>

  <div class="section">
    <h2>💰 行情概览</h2>
    <div class="metrics-grid">
      <div class="metric-card"><div class="label">现价</div><div class="value">¥{price:.2f}</div></div>
      <div class="metric-card"><div class="label">涨跌幅</div><div class="value" style="color:{pct_color}">{pct:+.2f}%</div></div>
      <div class="metric-card"><div class="label">趋势</div><div class="value" style="font-size:16px">{esc(trend)}</div></div>
      <div class="metric-card"><div class="label">多空方向</div><div class="value" style="font-size:16px">{esc(duo_kong)}</div></div>
    </div>
  </div>

  <div class="section">
    <h2>📊 量化评分</h2>
    <div class="score-box">
      <div class="pct">{pct_score:.0f}<span style="font-size:24px">/100</span></div>
      <div class="level">{esc(level)}</div>
    </div>
    {timing_html}
    <table>
      <tr><th>评分维度</th><th>得分</th></tr>
      {score_rows}
    </table>
  </div>

  <div class="section">
    <h2>📐 六维评分雷达图</h2>
    <div class="radar-container">
      {radar_svg}
      <div style="font-size:13px;color:#666;max-width:300px">
        <p>雷达图展示 8 个评分维度的得分（0 = 中性，正值偏多，负值偏空）。</p>
        <p>数据完整度: <strong>{completeness:.0f}%</strong></p>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>📋 技术面详情</h2>
    <div class="metrics-grid">
      <div class="metric-card"><div class="label">RSI</div><div class="value">{rsi:.0f}</div></div>
      <div class="metric-card"><div class="label">主力强度(ABC3)</div><div class="value">{abc3:+.1f}</div></div>
      <div class="metric-card"><div class="label">均价偏离(HG)</div><div class="value">{hg:+.1f}%</div></div>
      <div class="metric-card"><div class="label">PE(动)</div><div class="value">{pe:.1f}</div></div>
      <div class="metric-card"><div class="label">PB</div><div class="value">{pb:.2f}</div></div>
      <div class="metric-card"><div class="label">ROE</div><div class="value">{roe}%</div></div>
    </div>
  </div>

  {trend_html}

  {roundtable_html}

  <div class="section">
    <h2>📈 基本面</h2>
    <table>
      <tr><th>指标</th><th>数值</th></tr>
      <tr><td>营收增速</td><td>{rev_g}%</td></tr>
      <tr><td>净利润增速</td><td>{profit_g}%</td></tr>
      <tr><td>ROE</td><td>{roe}%</td></tr>
    </table>
  </div>

  <div class="section">
    <h2>💸 资金面</h2>
    <table>
      <tr><th>指标</th><th>数值</th></tr>
      <tr><td>Level2 主力净额</td><td style="color:{l2_color};font-weight:bold">{l2_net:+.2f} 亿</td></tr>
    </table>
  </div>

  <div class="section">
    <h2>📰 舆情新闻</h2>
    {news_html if news_html else '<p style="color:#999;text-align:center;padding:20px">暂无新闻数据</p>'}
  </div>

  {kline_img}

  <div class="footer">
    ⚠️ 以上分析基于公开数据，不构成投资建议 · Powered by stock-quant
  </div>
</div>
</body>
</html>'''

    out_path = os.path.join(CACHE_DIR, f'{stock_name}_{code[-6:]}_report.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    print(f"  🌐 HTML报告已导出: {out_path}")
    return out_path


# ============================================================
def final_advice_text(pct):
    """最终建议文案（0-100 评分 → 建议文本）
    2026-08-28 抽出：主流程 print 与 Workbuddy 契约 JSON 共用，避免双份分支漂移
    """
    if pct >= 70:
        return '🟢🟢 强烈看多，五维共振向上，可积极关注'
    elif pct >= 50:
        return '🟢 偏多，多数维度向好，可逢低布局'
    elif pct < 30:
        return '🔴🔴 强烈看空，五维共振向下，建议回避'
    elif pct < 50:
        return '🔴 偏空，多数维度偏弱，建议观望'
    else:
        return '🟡 多空分歧，控制仓位，等待方向明确'


def name_candle_pattern(quote):
    """当日 K 线形态命名（2026-08-31 新增，借鉴收评"光头阳线"形态定性）
    基于 OHLC 关系判断，纯规则无新数据源：
      光头阳线（收=高，开≈低）/ 光脚阳线（收=高，开≈高，低远离）/
      长上影阳线 / 长下影阳线 / 十字星 / 大阳线 / 大阴线 /
      长上影阴线 / 长下影阴线 / 十字星(阴) / 普通阳线 / 普通阴线
    返回 (形态名, 说明)；数据不足返回 (None, None)
    """
    try:
        o = float(quote.get('open') or 0)
        h = float(quote.get('high') or 0)
        l = float(quote.get('low') or 0)
        c = float(quote.get('price') or 0)
        prev = float(quote.get('pre_close') or 0)
    except (TypeError, ValueError):
        return None, None
    if min(o, h, l, c, prev) <= 0:
        return None, None
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    upper = h - max(o, c)   # 上影
    lower = min(o, c) - l   # 下影
    is_yang = c >= o
    # 阈值：影线 ≥ 振幅35% 且 ≥ 实体 才算"长"（实体大时不再要求2倍，
    # 2026-08-31 修订：长下影阴线 开10.5/高10.6/低9.4/收10.0 原被 body*2 漏判）
    long_upper = upper >= rng * 0.35 and upper >= max(body, 1e-9)
    long_lower = lower >= rng * 0.35 and lower >= max(body, 1e-9)
    doji = body <= max(rng * 0.1, 1e-9)
    no_upper = upper <= max(rng * 0.02, 1e-9)
    no_lower = lower <= max(rng * 0.02, 1e-9)
    big = body >= rng * 0.6 and abs(c - prev) / prev * 100 >= 3
    if doji:
        return '十字星', '多空均衡，变盘窗口（收≈开）'
    if is_yang:
        # 2026-08-31 修订：纯光头蜡烛（开≈低且收=高，上下影都≈0）原分支漏判，
        # no_upper 优先级提到最前（今日上证 3926.53开/3926.50低/3986.30收=高 即此形态）
        if no_upper:
            if big:
                return '光头大阳线', '收在全天最高且涨幅大，多方尾盘完全控盘'
            desc = '收在全天最高（无上影），尾盘多方主导'
            if no_lower:
                desc += '，纯光头蜡烛（开≈低、收=高，无实体外影线）'
            return '光头阳线', desc
        if no_lower:
            return '光脚阳线', '开在全天最低（无下影），开盘即买盘承接'
        if big:
            return '大阳线', '实体占振幅60%+，强趋势日'
        if long_upper:
            return '长上影阳线', '冲高回落，上方抛压明显'
        if long_lower:
            return '长下影阳线', '下探收回，下方承接有力'
        return '普通阳线', '常规收涨'
    else:
        if no_lower:
            if big:
                return '光脚大阴线', '收在全天最低且跌幅大，空方尾盘完全控盘'
            return '光脚阴线', '收在全天最低（无下影），尾盘空方主导'
        if no_upper:
            return '光头阴线', '开在全天最高（无上影），开盘即抛压'
        if big:
            return '大阴线', '实体占振幅60%+，强趋势日'
        if long_upper:
            return '长上影阴线', '反弹无力即回落，抛压沉重'
        if long_lower:
            return '长下影阴线', '下杀后收回，下方有抄底资金'
        return '普通阴线', '常规收跌'


def generate_report(code, stock_name, contract_json=None):
    """生成五维综合分析报告
    contract_json: 非空时，报告末尾输出单行 JSON 结构化契约（供
    prepare_workbuddy_input 机器消费，替代 emoji 正则文本契约；2026-08-28）
    """
    print(f"\n{'='*72}")
    print(f"  📊 A股十维量化分析系统 v2.0")
    print(f"  {'='*60}")
    print(f"  标的: {stock_name} ({code})")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*72}")
    
    # Step 1: 获取实时行情
    print(f"\n📡 正在获取数据...")
    quote = fetch_quote_tencent(code)
    if not quote or quote['price'] == 0:
        print(f"  ❌ 无法获取 {stock_name} 的行情数据，请检查股票名称或代码")
        return
    
    price = quote['price']
    pct = quote['pct']
    arrow = '🟢' if pct >= 0 else '🔴'
    print(f"  ✅ 行情获取成功: {price:.2f} ({pct:+.2f}%)")

    # 非交易时段/停牌降级处理：当成交量=0或今开=0时，资金面/量价类指标无效
    inactive = quote.get('inactive', False)
    if inactive:
        now_hm = datetime.now().strftime('%H:%M')
        print(f"  ⚠️ 当前为非交易时段（{now_hm}），实时资金面/量价类指标将显示为0或无效值，建议在交易时间（9:30-15:00）内运行")
    
    # Step 2: 技术面分析
    tech = analyze_technical(code, price)
    tech_ok = tech and 'error' not in tech
    print(f"  {'✅' if tech_ok else '❌'} 技术面分析完成")
    
    # Step 3: 基本面分析
    fund = analyze_fundamental(quote, tech)
    print(f"  ✅ 基本面分析完成")
    
    # Step 4: 资金面分析
    cap_flow = analyze_capital_flow(quote, tech)
    print(f"  ✅ 资金面分析完成")
    
    # Step 5: 舆情分析
    sentiment = fetch_news_sina(code, stock_name)
    if sentiment.get('data_available'):
        print(f"  ✅ 舆情分析完成（{sentiment['total']}条新闻）")
    else:
        print(f"  ⚠️ 舆情数据获取失败（0条新闻），本维度评分按中性处理")
    
    # Step 6: 宏观分析
    macro = fetch_macro()
    print(f"  ✅ 宏观分析完成")
    
    # Step 7: 融资融券
    margin_data = fetch_margin_data(code, quote)
    print(f"  {'✅' if margin_data['available'] else '⚪'} 融资融券分析完成")
    
    # Step 8: 龙虎榜（2026-09-30 P1-5：冷却期回退最近成功载荷，标滞后口径）
    dragon_tiger = _with_lastok('dragon_tiger', code, fetch_dragon_tiger, code)
    print(f"  {'✅' if dragon_tiger['available'] else '⚪'} 龙虎榜分析完成"
          + ('（冷却期回退T-N口径）' if dragon_tiger.get('_stale') else ''))

    # Step 9: 股东结构
    shareholders = _with_lastok('shareholders', code, fetch_shareholders, code)
    print(f"  {'✅' if shareholders['available'] else '⚪'} 股东结构分析完成"
          + ('（冷却期回退T-N口径）' if shareholders.get('_stale') else ''))

    # Step 10: 主营业务
    biz_structure = _with_lastok('business', code, fetch_business_structure, code)
    print(f"  {'✅' if biz_structure['available'] else '⚪'} 主营业务分析完成"
          + ('（冷却期回退T-N口径）' if biz_structure.get('_stale') else ''))

    # Step 11: 机构观点（东财 reportapi 优先，含一致预期；失败回退新浪；冷却期回退 lastok）
    inst_views = _with_lastok('inst_views', code, fetch_institution_views_em, code, stock_name)
    if not inst_views.get('available'):
        inst_views = fetch_institution_views(code, stock_name)
    print(f"  {'✅' if inst_views['available'] else '⚪'} 机构观点分析完成"
          + ('（冷却期回退T-N口径）' if inst_views.get('_stale') else ''))
    
    # Step 12: K线形态识别
    kline_data = fetch_kline_sina(code, 240, 60)
    patterns = analyze_candlestick(tech, kline_data) if tech_ok else []
    print(f"  {'✅' if patterns else '⚪'} K线形态识别完成（{len(patterns)}种形态）")
    

    # Step 13: 趋势通道分析
    trend_ch = analyze_trend_channel(kline_data, price) if tech_ok else {"available": False}
    print(f"  {'✅' if trend_ch.get('available', False) else '⚪'} 趋势通道分析完成")
    # Step 13: 多因子量化评分
    quant_score = calculate_quant_score(tech, fund, cap_flow, sentiment, margin_data, inst_views, patterns, code)
    print(f"  ✅ 多因子量化评分完成（{quant_score['level']} {quant_score['pct']}分）")

    # Step 13.5: 圆桌多视角增强（Workbuddy 方法论互补：共识度/多空证据/背离陷阱/概率路径/失效条件）
    roundtable = None
    sim = None  # 预初始化，供后续"相似K线匹配"复用，避免重复 DTW 计算
    try:
        sim = find_similar_kline_patterns(code)
        roundtable = analyze_roundtable(quant_score, tech, cap_flow, price, sim)
        print(f"  ✅ 圆桌多视角增强完成（{roundtable.get('consensus', '')}）")
    except Exception as e:
        log.warning(f"圆桌多视角增强失败: {e}")
    
    # ================================================================
    # 评分卡摘要（一行浓缩关键指标）
    # ================================================================
    print(f"\n{'='*72}")
    score_tech = tech.get('trend', '') if tech_ok else ''
    score_dk = tech.get('duo_kong', '') if tech_ok else ''
    score_qs = '★强' if tech.get('qiang_jin_qiang') else ('强势' if tech.get('qiang_shi') else ('做多' if tech.get('duo_kong')=='做多' else '做空')) if tech_ok else ''
    score_abc3 = tech.get('abc3', 0) if tech_ok else 0
    score_hg = tech.get('hg', 0) if tech_ok else 0
    score_pe = fund.get('pe', 0)
    # 2026-08-21 修复：评分卡 L2 字段改用 main_net（push2his 主力净额，与 westock data_fund_flow 口径一致），
    # 不再用 level2_main_total（L2逐笔口径，新浪兜底时显著低估——工富实测 3.62亿 vs push2his 6.27亿）
    score_l2 = cap_flow.get('main_net', 0) if cap_flow else 0
    score_sent = sentiment.get('score', 0) if sentiment else 0
    score_rsi = tech.get('rsi', 50) if tech_ok else 50
    # MACD"多"口径坑修复（2026-08-25 专家研判复现：鼎龙/温州 预填"多" vs 连接器"空微"）：
    # 柱>0（红柱）≠ 真多头，仅 DIF 零轴上才算真多头。评分卡显式标注 方向(柱值/DIF值)，
    # 让专家一眼看出口径（如 红柱(假多)(柱+0.083/DIF-0.766)），不再标"待核验"。
    if tech_ok:
        _macd_bar = tech.get('macd', 0) or 0
        _macd_dif = tech.get('dif', 0) or 0
        if _macd_dif > 0:
            score_macd = '真多'
        elif _macd_bar > 0:
            score_macd = '红柱(假多)'
        else:
            score_macd = '空'
        score_macd_detail = f'{score_macd}(柱{_macd_bar:+.3f}/DIF{_macd_dif:+.3f})'
    else:
        score_macd = ''
        score_macd_detail = ''
    score_pct5 = tech.get('pct_5d', 0) if tech_ok else 0
    score_pct20 = tech.get('pct_20d', 0) if tech_ok else 0
    score_quant = quant_score.get('pct', 0)
    
    print(f"  📊 评分卡 | {arrow} {price:.2f} ({pct:+.2f}%) | 趋势{score_tech} | 多空{score_dk} | 信号{score_qs} | "
          f"RSI(14)={score_rsi:.0f} | MACD{score_macd_detail} | ABC3={score_abc3:+.1f} | HG={score_hg:+.1f}% | "
          f"PE={score_pe:.0f} | L2={score_l2:+.1f}亿 | 情绪{score_sent:+.0f} | 量化{score_quant:.0f}分")
    print(f"  {'='*72}")
    
    # ================================================================
    # 输出报告
    # ================================================================
    print(f"\n{'='*72}")
    print(f"  📈 十维分析报告 — {stock_name} ({code})")
    print(f"{'='*72}")
    
    # 一、基础行情
    print(f"\n{'─'*60}")
    print(f"  📋 一、基础行情")
    print(f"  {'─'*60}")
    print(f"  {arrow} 现价: {price:.2f}  涨跌: {pct:+.2f}%  昨收: {quote['pre_close']:.2f}")
    print(f"  今开: {quote['open']:.2f}  最高: {quote['high']:.2f}  最低: {quote['low']:.2f}")
    print(f"  振幅: {quote['amplitude']:.2f}%  成交额: {quote['amount']:.2f}亿")
    # 当日 K 线形态命名（2026-08-31 新增：光头阳线/长上影/十字星等，OHLC 关系判断）
    _pat, _pat_desc = name_candle_pattern(quote)
    if _pat:
        print(f"  【K线形态】{arrow.replace('🟢', '').replace('🔴', '').replace('🟡', '')} {_pat}: {_pat_desc}")
    # 事件日历（未来90天解禁 + 业绩预告）
    events = fetch_event_calendar(code)
    if events and events.get('available'):
        print(f"  【事件日历】")
        for ev in events['events'][:3]:
            print(f"  📅 {ev['date']} {ev['type']}: {ev['desc']}")
    
    # 二、技术面
    print(f"\n{'─'*60}")
    print(f"  📐 二、技术面分析")
    print(f"  {'─'*60}")
    if tech_ok:
        print(f"  【均线系统】")
        print(f"  MA5={tech['ma5']:.2f}{'✅' if price > tech['ma5'] else '❌'}  "
              f"MA10={tech['ma10']:.2f}{'✅' if price > tech['ma10'] else '❌'}  "
              f"MA20={tech['ma20']:.2f}{'✅' if price > tech['ma20'] else '❌'}  "
              f"MA60={tech['ma60']:.2f}{'✅' if price > tech['ma60'] else '❌'}")
        print(f"  趋势: 【{tech['trend']}】 站上均线: {tech['bull_count']}/4")
        print(f"  【技术指标】")
        print(f"  RSI(14)={tech['rsi']:.1f}  {'⚠️深度超卖' if tech['rsi']<25 else '⚠️超卖' if tech['rsi']<35 else '⚠️超买' if tech['rsi']>75 else '⚠️接近超买' if tech['rsi']>65 else '中性'}")
        # 极值反转层（Workbuddy 复盘报告 Layer 1）：RSI_2 < 10 禁止看空
        if tech.get('reversal_watch'):
            r2 = tech.get('rsi_2')
            print(f"  ⚡ 极值反转观察: RSI(2)={r2:.1f} < 10，短期超卖极值 → 禁止发出新的看空信号，强制切换为反转观察")
        macd_sig = '🟢多头' if tech['macd'] > 0 else '🔴空头'
        print(f"  MACD: DIF={tech['dif']:.3f}  DEA={tech['dea']:.3f}  柱={tech['macd']:.3f} ({macd_sig})")
        print(f"  KDJ: K={tech['kdj_k']:.1f}  D={tech['kdj_d']:.1f}  J={tech['kdj_j']:.1f}  "
              f"{'⚠️超卖' if tech['kdj_j']<20 else '⚠️超买' if tech['kdj_j']>80 else '中性'}")
        print(f"  BOLL: 上轨={tech['boll_up']:.2f}  中轨={tech['boll_mid']:.2f}  下轨={tech['boll_dn']:.2f}")
        # P0-2 顶部止盈信号（规则2：乖离>15% 或 KDJ_J>100 强制减半）
        if tech.get('top_overflow'):
            print(f"  🚨 顶部止盈信号(规则2): {tech.get('top_overflow_reason', '')} → 强制减半/不追高")
        # P0-1 反弹可靠性预警（规则9：高位回落+单日反弹，需资金确认）
        if tech.get('bounce_suspect'):
            print(f"  ⚠️ 反弹可靠性预警(规则9): {tech.get('bounce_reason', '')} → 不上调评级，结合融资/资金确认")
        print(f"  【关键价位】")
        print(f"  支撑: {tech['support']:.2f}  |  压力: {tech['resistance']:.2f}  |  "
              f"60日高: {tech['high_60d']:.2f}  |  60日低: {tech['low_60d']:.2f}")
        print(f"  【阶段表现】近5日: {tech['pct_5d']:+.2f}%  |  近20日: {tech['pct_20d']:+.2f}%")
        # 主升擒龙新增指标
        jj = tech.get('jj', 0)
        duo_kong = tech.get('duo_kong', '未知')
        a1 = tech.get('a1')
        b1 = tech.get('b1')
        abc3 = tech.get('abc3', 0)
        hg = tech.get('hg', 0)
        qs = tech.get('qiang_shi', False)
        qjq = tech.get('qiang_jin_qiang', False)
        dk_icon = '🟢' if duo_kong == '做多' else ('🔴' if duo_kong == '做空' else '⚪')
        print(f"  【主升擒龙（新增）】")
        print(f"  加权均价(JJ)={jj:.2f}  多空方向: {dk_icon} {duo_kong}  "
              f"A1={a1:.3f}" if a1 is not None else "A1=N/A", end="")
        if b1 is not None:
            print(f"  B1={b1:.3f}")
        else:
            print()
        print(f"  主力强度(ABC3)={abc3:+.2f}  {'🟢主力介入' if abc3 > 5 else '🔴主力流出' if abc3 < -5 else '⚪中性'}")
        print(f"  均价偏离(HG)={hg:+.2f}%  {'🟢偏离健康' if 5 < hg < 20 else '🔴偏离过大' if hg > 20 else '⚪正常' if hg > 0 else '🔴低于均价'}")
        qs_icon = '🟢🟢' if qjq else ('🟢' if qs else '⚪')
        qs_label = '★强信号' if qjq else ('强势' if qs else '无信号')
        print(f"  综合信号: {qs_icon} {qs_label}")
        # 多周期共振（周线/月线）
        mtf = calc_multi_timeframe(code, price)
        weekly = mtf.get('weekly', {})
        monthly = mtf.get('monthly', {})
        if weekly.get('available') or monthly.get('available'):
            print(f"  【多周期共振】")
            if weekly.get('available'):
                w_trend = weekly['trend']
                w_icon = '🟢' if w_trend == '多头' else ('🔴' if w_trend == '空头' else '⚪')
                w_bull = weekly['bull_count']
                w_macd = '🟢' if weekly.get('macd', 0) > 0 else '🔴'
                w_a1 = weekly.get('a1')
                w_b1 = weekly.get('b1')
                w_dk = '🟢做多' if (w_a1 and w_b1 and w_a1 >= w_b1) else '🔴做空'
                print(f"  周线: {w_icon}趋势{w_trend} 站上均线{w_bull}/4 MACD{w_macd} {w_dk}")
            if monthly.get('available'):
                m_trend = monthly['trend']
                m_icon = '🟢' if m_trend == '多头' else ('🔴' if m_trend == '空头' else '⚪')
                m_bull = monthly['bull_count']
                m_macd = '🟢' if monthly.get('macd', 0) > 0 else '🔴'
                m_a1 = monthly.get('a1')
                m_b1 = monthly.get('b1')
                m_dk = '🟢做多' if (m_a1 and m_b1 and m_a1 >= m_b1) else '🔴做空'
                print(f"  月线: {m_icon}趋势{m_trend} 站上均线{m_bull}/4 MACD{m_macd} {m_dk}")
        # 分钟级K线
        min_data = tech.get('min_data', {}) if tech_ok else {}
        for tf in ['60min', '30min']:
            md = min_data.get(tf)
            if md:
                mm = '🟢' if md.get('macd', 0) > 0 else '🔴'
                mr = md.get('rsi', 50)
                mp = md.get('pct', 0)
                print(f"  {tf}: MACD{mm} RSI={mr:.0f} 最新涨跌{mp:+.2f}%")
        # 量价背离
        if tech_ok and tech.get('divergence') and tech['divergence'] != '无背离':
            print(f"  【量价关系】{tech['divergence']}")
        # 相对强弱 RS（个股 vs 参考指数超额收益）
        if tech_ok and tech.get('rs_20') is not None:
            rs_ico = '🟢' if tech['rs_20'] > 5 else ('🔴' if tech['rs_20'] < -5 else '⚪')
            print(f"  【相对强弱】{rs_ico} vs {tech.get('rs_ref','大盘')}  "
                  f"当日{tech.get('rs_today',0):+.1f}%  5日{tech.get('rs_5',0):+.1f}%  "
                  f"20日{tech.get('rs_20',0):+.1f}%  {tech.get('rs_signal','')}")
        # 波段结构与浪数（多波形态识别）
        if tech_ok and tech.get('wave_count') is not None:
            env_tag = f" 环境[{tech.get('wave_market','')}]" if tech.get('wave_market') else ''
            print(f"  【波段结构】近60日识别{tech['wave_count']}波上涨，当前第{tech.get('current_wave',0)}浪 [{tech.get('wave_state','')}]{env_tag}")
            if tech.get('wave_signal'):
                print(f"  {tech['wave_signal']}")
        # 筹码分布（日线 + 60分钟）
        if tech_ok and tech.get('chip_winner_pct') is not None:
            print(f"  【筹码分布】获利盘{tech['chip_winner_pct']}% 成本峰{tech.get('chip_cost_peak',0)}  "
                  f"平均成本{tech.get('chip_cost_avg',0)} 集中度{tech.get('chip_concentration',0)}%  "
                  f"底部锁定{tech.get('chip_bottom_lock',0)}%")
            if tech.get('chip_note'):
                print(f"  {tech['chip_note']}")
            if tech.get('chip_60_note'):
                print(f"  ⏱ {tech['chip_60_note']}")
    
    # 三、基本面
    print(f"\n{'─'*60}")
    print(f"  📋 三、基本面分析")
    print(f"  {'─'*60}")
    print(f"  PE(动): {fund['pe']:.2f}  {fund.get('pe_signal','')}")
    print(f"  PB: {fund['pb']:.2f}  {fund.get('pb_signal','')}")
    if fund.get('pe_pct') is not None:
        print(f"  PE历史分位: {fund['pe_pct']:.0f}% {fund.get('pe_pct_signal','')}  |  "
              f"PB历史分位: {fund['pb_pct']:.0f}% {fund.get('pb_pct_signal','')}")
    print(f"  市值: {fund['market_cap']:.2f}亿  {fund.get('cap_level','')}")
    if fund.get('eps'):
        print(f"  EPS: {fund['eps']:.3f}")
    if fund.get('roe') is not None:
        print(f"  ROE: {fund['roe']}% {fund.get('roe_signal','')}  "
              f"毛利率: {fund.get('gross_margin','N/A')}% {fund.get('gross_margin_signal','')}")
    if fund.get('rev_growth') is not None:
        print(f"  营收增速: {fund['rev_growth']:+.1f}% {fund.get('rev_growth_signal','')}  "
              f"净利增速: {fund['profit_growth']:+.1f}% {fund.get('profit_growth_signal','')}")
    if fund.get('quality_tier'):
        print(f"  {fund['quality_tier']}")
    if fund.get('high_60d'):
        print(f"  60日区间: {fund['low_60d']:.2f} ~ {fund['high_60d']:.2f}")
    
    # 行业对比 + 定价锚对标（2026-08-24 新增：估值按同行业/同环节对比，而非绝对PE或跨环节）
    peers = fetch_industry_peers(code)
    if peers.get('peers'):
        print(f"\n{'─'*60}")
        print(f"  🏭 行业对比（{peers.get('industry','')}）")
        print(f"  {'─'*60}")
        print(f"  {'名称':10s} {'现价':>8s} {'涨跌':>8s}")
        for p in peers['peers']:
            p_icon = '🟢' if p.get('pct', 0) >= 0 else '🔴'
            print(f"  {p['name']:10s} {p['price']:>8.2f} {p_icon} {p.get('pct',0):>+7.2f}%")
    # 定价锚对标：本股 PE 在同行业/同环节中的分位（教训 2026-08-24：勿拿材料商与组装厂比 PE）
    anchor = fetch_peer_valuation_benchmark(code)
    if anchor.get('available'):
        print(f"\n  ⚓ 定价锚对标（{anchor.get('industry','')} · 同业中位PE {anchor.get('median_pe',0):.1f}）")
        print(f"  {'名称':10s} {'PE(动)':>8s} {'现价':>8s} {'涨跌':>8s}")
        for p in anchor.get('peers', []):
            self_tag = ' ← 本股' if p.get('is_self') else ''
            pe_txt = f"{p['pe']:.1f}" if p.get('pe') and p['pe'] > 0 else '亏损/无'
            print(f"  {p['name']:10s} {pe_txt:>8s} {p['price']:>8.2f} {p['pct']:>+7.2f}%{self_tag}")
        print(f"  → {anchor.get('signal','')}")
    
    # 四、资金面
    print(f"\n{'─'*60}")
    print(f"  💰 四、资金面分析")
    print(f"  {'─'*60}")
    # 盘中数据时效性标注
    now = datetime.now()
    now_hm = now.strftime('%H:%M')
    is_trading = (now.weekday() < 5 and
                  ((now.hour == 9 and now.minute >= 30) or 10 <= now.hour <= 14 or
                   (now.hour == 15 and now.minute <= 0)))
    if is_trading:
        # 判断盘中进度：9:30开盘，15:00收盘，共330分钟
        open_min = 9 * 60 + 30
        close_min = 15 * 60
        cur_min = now.hour * 60 + now.minute
        progress = max(0, min(100, (cur_min - open_min) / (close_min - open_min) * 100))
        print(f"  ⏰ 盘中数据（{now_hm}，交易日进度 {progress:.0f}%）—— 外内盘比/量比/成交额均为瞬时值，不代表全天")
    elif quote.get('inactive'):
        print(f"  ⚠️ 非交易时段（{now_hm}），资金面数据为上一交易日收盘值或无效")
    print(f"  【个股资金流】")
    # ETF 单位是"份"，个股是"手"（100股）——根据成交量数量级自动判断
    # 个股 volume 通常 < 100万手，ETF volume 常在千万级以上
    vol_unit = '份' if cap_flow['outer'] > 1e6 or cap_flow['inner'] > 1e6 else '手'
    print(f"  外盘: {cap_flow['outer']:.0f}{vol_unit}  |  内盘: {cap_flow['inner']:.0f}{vol_unit}  |  "
          f"比={cap_flow['ratio']}  {cap_flow.get('flow_signal','')}")
    mds_raw = str(cap_flow.get('main_data_source', 'eastmoney'))
    mds_tag = '东财' if 'eastmoney' in mds_raw else ('新浪' if 'sina' in mds_raw else ('估算' if '估算' in mds_raw else mds_raw))
    # 数据日期 + 时效标注（防止昨日数据冒充当日）
    data_date = str(cap_flow.get('data_date', ''))
    date_tag = f"  [数据日期: {data_date[5:]}]" if len(data_date) >= 10 else ''
    if cap_flow.get('data_stale'):
        date_tag += ' ⚠️昨日数据'
    print(f"  主力净额: {cap_flow['main_net']:+.2f}  {cap_flow.get('main_signal','')}  [数据源: {mds_tag}]{date_tag}")
    if cap_flow.get('data_stale'):
        print(f"  ⚠️ 无当日资金数据：当前仅有 {data_date} 的参考值，单日判断不可用（资金面评分已降权）")
    if cap_flow.get('main_sanity_fail'):
        print(f"  ⚠️ 数据异常: {cap_flow['main_sanity_fail']}")
    if cap_flow.get('main_sanity_note'):
        print(f"  ⚠️ {cap_flow['main_sanity_note']}")
    if 'sina' in mds_raw or '估算' in mds_raw:
        print(f"  ⚠️ 东财资金流不可用，本值为{mds_tag}口径，与东财APP/同花顺口径可能不同")
    # 估算口径警告：腾讯外内盘估算 ≠ 真实主力，提示以东方财富分档为准
    if cap_flow.get('main_caliber_warning'):
        print(f"  ⚠️ {cap_flow['main_caliber_warning']}")
    if 'vol_ratio' in cap_flow:
        print(f"  量比: {cap_flow['vol_ratio']:.2f}  {cap_flow.get('vol_signal','')}")
    print(f"  成交额: {cap_flow['amount']:.2f}亿")
    # Level2逐笔资金流
    level2 = fetch_level2_flow(code, cap_flow.get('amount', 0))
    if level2 and level2.get('available'):
        print(f"  【Level2逐笔资金流】")
        l2_src = level2.get('data_source', 'eastmoney')
        sl = level2['super_large_net']
        lg = level2['large_net']
        md = level2['mid_net']
        sm = level2['small_net']
        def _icon(v):
            """资金流图标，None 时返回数据缺失标记"""
            if v is None:
                return '⚠️'
            return '🟢' if v > 0 else ('🔴' if v < 0 else '⚪')
        def _fmt(v):
            """资金流格式化，None 时返回'数据缺失'"""
            return f'{v:+.2f}亿' if v is not None else '数据缺失'
        sl_icon = _icon(sl); lg_icon = _icon(lg); md_icon = _icon(md); sm_icon = _icon(sm)
        if level2.get('level2_detail_missing'):
            # 中单/小单缺失，但超大单/大单可能有真实数据（如新浪源）
            has_partial = (sl is not None and abs(sl) > 0.001) or (lg is not None and abs(lg) > 0.001)
            if has_partial:
                print(f"  ⚠️ 中单/小单分档缺失（数据源: {l2_src}），超大单/大单如下")
                print(f"  {sl_icon} 超大单: {_fmt(sl)} ({level2['super_large_pct']:+.1f}%)  "
                      f"{lg_icon} 大单: {_fmt(lg)} ({level2['large_pct']:+.1f}%)")
            else:
                print(f"  ⚠️ Level2 分档数据暂不可用（数据源: {l2_src}），仅展示主力合计")
        else:
            print(f"  {sl_icon} 超大单: {_fmt(sl)} ({level2['super_large_pct']:+.1f}%)  "
                  f"{lg_icon} 大单: {_fmt(lg)} ({level2['large_pct']:+.1f}%)")
            print(f"  {md_icon} 中单: {_fmt(md)}  "
                  f"{sm_icon} 小单: {_fmt(sm)}")
        mt = level2['main_total_net']
        mt_icon = '🟢🟢' if mt > 5 else ('🟢' if mt > 1 else ('🔴🔴' if mt < -5 else ('🔴' if mt < -1 else '⚪')))
        print(f"  主力合计(超大+大单): {mt_icon} {mt:+.2f}亿 ({level2['main_total_pct']:+.1f}%)  [数据源: {l2_src}]")
        # 口径一致性提示：主力净额与 Level2 来源不同时说明（数据源降级切换所致）
        main_ds_raw = str(cap_flow.get('main_data_source', ''))
        if l2_src and main_ds_raw and l2_src != main_ds_raw and '限流' not in l2_src and '估算' not in main_ds_raw:
            # 自动识别哪个是东财口径（与东财APP一致）
            if 'eastmoney' in l2_src:
                ref = f"Level2[{l2_src}] 为东财口径，与东财APP一致"
            elif 'eastmoney' in main_ds_raw:
                ref = f"主力净额[{main_ds_raw}] 为东财口径，与东财APP一致"
            else:
                ref = '两者均非东财口径，对照东财APP需谨慎'
            print(f"  ⚠️ 口径提示: 主力净额[来源: {main_ds_raw}] 与 Level2[来源: {l2_src}] 非同一口径，{ref}")
    # 北向资金持股
    north = fetch_north_flow(code)
    if north and north.get('available'):
        print(f"  【北向资金持股】持股: {north['hold_value']:.2f}亿 ({north['pct']:.2f}%)")
    print(f"  【板块资金流】")
    if cap_flow['sectors']:
        print(f"  · 涨幅榜: " + " | ".join(f"{s['name']}({s['pct']:+.1f}%)" for s in cap_flow['sectors'][:5]))
        fin = cap_flow.get('sector_fund_in', [])
        fout = cap_flow.get('sector_fund_out', [])
        if fin:
            print(f"  · 主力流入TOP: " + " | ".join(f"{s['name']}({s.get('main_net',0):+.1f}亿)" for s in fin[:5]))
        if fout:
            print(f"  · 主力流出TOP: " + " | ".join(f"{s['name']}({s.get('main_net',0):+.1f}亿)" for s in fout[:5]))
    else:
        # 板块数据不可用：明确标注原因，而非误导性"数据获取中"
        if cap_flow.get('sectors_note'):
            print(f"  ⚠️ 板块资金流数据暂不可用: {cap_flow['sectors_note']}")
        else:
            print(f"  ⚠️ 板块资金流数据暂不可用（接口限流或未返回）")
    
    # 五、舆情分析
    # 板块轮动
    rotation = analyze_sector_rotation()
    if rotation.get('main_line'):
        print(f"  {rotation['main_line']}")
    if rotation.get('leading'):
        lead_strs = [f"{s['name']}({s['pct']:+.1f}%)" for s in rotation['leading'][:3]]
        print(f"  领涨: {' | '.join(lead_strs)}")
    if rotation.get('lagging'):
        lag_strs = [f"{s['name']}({s['pct']:+.1f}%)" for s in rotation['lagging'][:3]]
        print(f"  领跌: {' | '.join(lag_strs)}")
    
    print(f"\n{'─'*60}")
    print(f"  📰 五、舆情分析")
    print(f"  {'─'*60}")
    s = sentiment
    score_str = f"{s['score']:+.0f}"
    if s['score'] > 30: score_icon = '🟢🟢 强烈利好'
    elif s['score'] > 10: score_icon = '🟢 偏利好'
    elif s['score'] < -30: score_icon = '🔴🔴 强烈利空'
    elif s['score'] < -10: score_icon = '🔴 偏利空'
    else: score_icon = '⚪ 中性'
    print(f"  情绪评分: {score_str}  {score_icon}")
    print(f"  利好: {s['positive']}条  利空: {s['negative']}条  中性: {s['total']-s['positive']-s['negative']}条")
    if s.get('has_old_news'):
        print(f"  ⚠️ 含{s['old_news_count']}条历史旧闻（已排除出情绪评分）")
    # 事件清单（标题规则化分类，2026-08-18 新增）
    ev_list = s.get('events') or []
    if ev_list:
        print(f"  📋 事件清单（标题分类）:")
        for ev in ev_list[:6]:
            ev_icon = {'利好': '🟢', '利空': '🔴'}.get(ev.get('direction', '中性'), '⚪')
            t1 = ev.get('titles', [])
            title_snip = f"：{t1[0]}" if t1 else ''
            multi = f"（{ev['count']}条）" if ev['count'] > 1 else ''
            print(f"    {ev_icon} {ev['type']}{multi}{title_snip}")
    # 事件驱动标注（2026-08-31 新增：命中事件触发词的新闻汇总，
    # 借鉴收评"事件级催化"——盘前研判需区分"事件驱动"与"概念跟随"）
    ev_driven = [n for n in (s.get('news') or []) if n.get('event_driven')]
    if ev_driven:
        types = {}
        for n in ev_driven:
            types.setdefault(n['event'], 0)
            types[n['event']] += 1
        type_str = '、'.join(f"{t}×{c}" if c > 1 else t for t, c in types.items())
        print(f"  🎯 事件驱动: {len(ev_driven)}条命中事件触发词（{type_str}）—— 题材定价日，量化评分对消息催化不敏感，盘前需人工核事件催化")
    for n in s['news'][:5]:
        ico = '🟢' if n['sentiment'] == '利好' else ('🔴' if n['sentiment'] == '利空' else '⚪')
        tag = n.get('tag', '')
        src = n.get('source', '')
        ds = n.get('data_source', '')
        yr = n.get('year', '')
        # 财联社电报来源标记（区别于新浪/东财个股新闻）
        cls_tag = '📰' if ds == 'cls_telegraph' else ''
        if tag == '📜 旧闻':
            print(f"  {ico} {cls_tag}{tag} {n['title']} ({yr}年)")
        else:
            print(f"  {ico} {cls_tag} {n['title']}")
    
    # 补充维度：融资融券/龙虎榜/股东结构/主营构成/机构观点（十维框架完整反馈）
    print(f"\n{'─'*60}")
    print(f"  📑 补充维度（十维框架）")
    print(f"  {'─'*60}")
    # 冷却期回退汇总标注（2026-09-30 P1-5）：滞后维度显式声明，禁止冒充当日实时
    _stale_dims = [nm for nm, _d in (('龙虎榜', dragon_tiger), ('股东结构', shareholders),
                                     ('主营构成', biz_structure), ('机构观点', inst_views))
                   if isinstance(_d, dict) and _d.get('_stale')]
    if _stale_dims:
        print(f"  ⚠️ 滞后口径声明: {'、'.join(_stale_dims)}为冷却期回退的最近成功数据（非当日实时），日期以各维度自身标注为准")
    # 融资融券（当前为腾讯行情主力净额口径，独立两融余额接口待接入）
    if margin_data and margin_data.get('available'):
        print(f"  【融资融券】主力净额 {margin_data.get('net_flow', 0):+.2f}亿（近似口径）")
    else:
        print(f"  【融资融券】数据暂不可用")
    # 龙虎榜（含 未上榜/接口异常 标注）
    if dragon_tiger and dragon_tiger.get('available'):
        dt_net = dragon_tiger.get('net', 0)
        dt_icon = '🟢' if dt_net > 0 else ('🔴' if dt_net < 0 else '⚪')
        multi = f"（{dragon_tiger.get('record_count', 1)}条上榜原因）" if dragon_tiger.get('record_count', 1) > 1 else ''
        print(f"  【龙虎榜】{dragon_tiger.get('trade_date','')} 上榜 {dt_icon} 净买{dt_net:+.2f}亿 "
              f"(买{dragon_tiger.get('bought',0):.1f}/卖{dragon_tiger.get('sold',0):.1f}亿) 机构买入{dragon_tiger.get('inst_count',0)}家{multi}")
        # 席位类型统计（2026-08-20 新增：机构/游资 识别，游资含知名营业部）
        if dragon_tiger.get('inst_buy') or dragon_tiger.get('hot_buy') or dragon_tiger.get('inst_sell') or dragon_tiger.get('hot_sell'):
            inst_net = dragon_tiger.get('inst_net', 0)
            hot_net = dragon_tiger.get('hot_net', 0)
            inst_icon = '🟢' if inst_net > 0 else ('🔴' if inst_net < 0 else '⚪')
            hot_icon = '🟢' if hot_net > 0 else ('🔴' if hot_net < 0 else '⚪')
            print(f"    席位类型: 机构{inst_icon}买{dragon_tiger.get('inst_buy',0):.2f}亿/卖{dragon_tiger.get('inst_sell',0):.2f}亿 净{inst_net:+.2f}亿 | "
                  f"游资{hot_icon}买{dragon_tiger.get('hot_buy',0):.2f}亿/卖{dragon_tiger.get('hot_sell',0):.2f}亿 净{hot_net:+.2f}亿")
        # 席位明细：同名"机构专用"合并显示（多家机构），游资/其他按营业部展示
        seat_map = {}
        for seat in dragon_tiger.get('seats', []):
            sname = seat.get('name', '')
            seat_map.setdefault(sname, {'buy': 0, 'sell': 0, 'cnt': 0})
            seat_map[sname]['buy'] += seat.get('buy', 0)
            seat_map[sname]['sell'] += seat.get('sell', 0)
            seat_map[sname]['cnt'] += 1
        for sname, agg in list(seat_map.items())[:4]:
            stype = '🏛️机构' if '机构专用' in sname else '🐉游资'
            snet = (agg['buy'] - agg['sell']) / 1e8
            sicon = '🟢' if snet > 0 else ('🔴' if snet < 0 else '⚪')
            cnt = f'×{agg["cnt"]}' if agg['cnt'] > 1 else ''
            disp = '机构专用(多家)' if '机构专用' in sname and agg['cnt'] > 1 else sname[:22]
            print(f"      {stype} {disp:22s}{cnt} 买{agg['buy']/1e8:.2f}亿 卖{agg['sell']/1e8:.2f}亿 净{snet:+.2f}亿")
        print(f"    上榜后表现: 1日{dragon_tiger.get('d1',0):+.2f}%  5日{dragon_tiger.get('d5',0):+.2f}%  10日{dragon_tiger.get('d10',0):+.2f}%")
        reasons = dragon_tiger.get('reasons') or ([dragon_tiger['reason']] if dragon_tiger.get('reason') else [])
        for i, r in enumerate(reasons, 1):
            print(f"    上榜原因{i}: {r}")
    else:
        print(f"  【龙虎榜】{dragon_tiger.get('note', '近期未上榜') if dragon_tiger else '近期未上榜'}")
    # 股东结构（十大流通股东 + 增减持）
    if shareholders and shareholders.get('available'):
        print(f"  【股东结构】{shareholders.get('report_period','')} 十大流通股东:")
        for h in shareholders.get('holders', [])[:3]:
            chg = h.get('change', '') or ''
            chg_icon = {'增持': '🟢', '减持': '🔴', '新进': '🟢'}.get(chg, '')
            print(f"    {h['name'][:16]:16s} {h['hold']:.2f}亿股 ({h['ratio']:.1f}%) {chg_icon}{chg}")
    else:
        print(f"  【股东结构】数据暂不可用")
    # 主营业务构成
    if biz_structure and biz_structure.get('available'):
        mb = biz_structure.get('main_business', [])[:3]
        mb_str = ' | '.join(f"{b['name']} {b['ratio']:.1f}%" for b in mb)
        print(f"  【主营构成】{biz_structure.get('report_period','')}: {mb_str}")
    else:
        print(f"  【主营构成】数据暂不可用")
    # 机构观点（研报评级 + 一致预期；2026-08-25 东财 reportapi 接入）
    if inst_views and inst_views.get('available'):
        iv_buy = inst_views.get('buy_count', 0)
        iv_tot = inst_views.get('total_count', 0)
        iv_icon = '🟢' if iv_tot > 0 and iv_buy > iv_tot * 0.5 else '⚪'
        src = inst_views.get('data_source', 'sina')
        print(f"  【机构观点】{iv_icon} 买入/看好 {iv_buy}/{iv_tot} 条研报评级（源: {src}）")
        for rt in inst_views.get('ratings', [])[:3]:
            org = rt.get('org', '')
            rating = rt.get('rating', '')
            aim = f" 目标{rt['aim_price']:.2f}" if rt.get('aim_price') else ''
            date = rt.get('date', '')
            print(f"    {'🟢' if rt.get('is_buy') else '⚪'} {org} [{rating}]{aim} {date}｜{rt.get('title','')[:38]}")
        # 一致预期（东财源才有）
        cons = inst_views.get('consensus', {}) or {}
        if cons and any(cons.values()):
            eps = cons.get('eps_this')
            pe = cons.get('pe_this')
            eps_n = cons.get('eps_next')
            pe_n = cons.get('pe_next')
            cons_txt = f"EPS预测 今年{eps}" if eps is not None else ''
            if pe is not None:
                cons_txt += f"/PE{pe}" if cons_txt else f"PE预测今年{pe}"
            if eps_n is not None:
                cons_txt += f"，明年EPS{eps_n}"
            if pe_n is not None:
                cons_txt += f"/PE{pe_n}" if cons_txt else f"PE预测明年{pe_n}"
            if cons_txt:
                print(f"    📊 一致预期: {cons_txt}")
    else:
        print(f"  【机构观点】数据暂不可用")
    
    # 六、宏观环境
    print(f"\n{'─'*60}")
    print(f"  🌍 六、宏观环境")
    print(f"  {'─'*60}")
    for idx in macro['indices']:
        ic = '🟢' if idx['pct'] >= 0 else '🔴'
        print(f"  {ic} {idx['name']:6s}  {idx['price']:.2f}  {idx['pct']:+.2f}%")
    # Beta 闸门（Workbuddy 复盘报告 Layer 0）：大盘异动 |涨跌|>3% 时信号挂起
    # 依据：07-28 创业板 -7.35% 当天，净利+348% 的某标的暴跌 -10.65%，利好被 beta 碾平
    beta_gate = ''
    for idx in macro.get('indices', []):
        if idx['name'] in ('创业板指', '沪深300') and abs(idx['pct']) > 3:
            gate_icon = '🔴' if idx['pct'] < 0 else '🟢'
            beta_gate = (f"{gate_icon} Beta闸门触发：{idx['name']} 当日 {idx['pct']:+.2f}%"
                         f"（|涨跌|>3%），当日所有个股信号挂起、不新开仓；"
                         f"已触发信号顺延，至 |大盘涨跌|<2% 的交易日重新确认")
            break
    if beta_gate:
        print(f"  ⛔ {beta_gate}")
    # 市场宽度：涨跌家数 + 涨停/跌停/连板
    breadth = macro.get('breadth', {})
    if breadth and breadth.get('available'):
        print(f"  【市场宽度】涨 {breadth.get('up_count',0)} 家 / 跌 {breadth.get('down_count',0)} 家 / "
              f"平 {breadth.get('flat_count',0)} 家（涨跌比 {breadth.get('ratio',0):.2f}）")
        print(f"  涨停 {breadth.get('zt_count',0)} 家 | 跌停 {breadth.get('dt_count',0)} 家 | "
              f"炸板 {breadth.get('zbc_count',0)} 家 | 最高连板 {breadth.get('lb_count',0)} 板")
        if macro.get('breadth_signal'):
            print(f"  {macro['breadth_signal']}")
    elif breadth and breadth.get('note'):
        print(f"  【市场宽度】{breadth['note']}")
    # 两市量能趋势 + 市场情绪温度计
    vt = macro.get('volume_trend', {})
    if vt and vt.get('signal'):
        print(f"  {vt['signal']}")
    st = macro.get('sentiment', {})
    if st and st.get('available'):
        print(f"  【市场情绪】{st['detail']}  {st['temp']}")
    # 板块走势（筑底/反弹判断：所属行业板块指数结构）
    sect = analyze_sector_trend(code)
    if sect and sect.get('available'):
        print(f"  【板块走势】{sect['name']}: {sect['state']}")
        if sect.get('signal'):
            print(f"  {sect['signal']}")
    elif sect and sect.get('name'):
        print(f"  【板块走势】{sect['name']}: 数据暂不可用（限流/无K线）")

    # 财联社加红电报（全市场重要/异动快讯，走本地 RSSHub，板块异动归因）
    try:
        _cls, _cls_ok = summarize_cls_telegraph(8, category='red')
        if _cls_ok and _cls:
            print(f"\n  📰 财联社加红电报（板块异动归因）")
            for _c in _cls[:8]:
                print(f"  · {_c['title'][:80]}")
        elif not _cls_ok:
            print(f"\n  📰 财联社加红电报: 暂不可用（RSSHub 未运行？）")
    except Exception as _e:
        log.warning(f"财联社电报展示失败: {_e}")

    # 七、综合评分与操作建议
    print(f"\n{'─'*60}")

    # 八、趋势通道分析
    if trend_ch and trend_ch.get("available"):
        sep = "─" * 60
        print(f'\n{sep}')
        print(f"  📈 八、趋势通道分析")
        print(f"  {sep}")
        print(f"  上轨: {trend_ch.get('upper', 0):.2f}  |  中轨: {trend_ch.get('mid', 0):.2f}  |  下轨: {trend_ch.get('lower', 0):.2f}")
        print(f"  通道宽度: {trend_ch.get('width', 0):.2f} ({trend_ch.get('width_pct', 0):.1f}%)")
        print(f"  价格位置: {trend_ch.get('position', 0)}（{trend_ch.get('pos_pct', 0):.0f}%）")
        print(f"  通道信号: {trend_ch.get('signal', 0)}")
        # 趋势通道对综合评分的修正
        if trend_ch["pos_pct"] > 95:
            print(f"  ⚠️ 价格已突破上轨，短期超买，注意回调风险")
        elif trend_ch["pos_pct"] < 5:
            print(f"  ⚠️ 价格已跌破下轨，短期超卖，关注反弹机会")
        if trend_ch["slope"] > 0:
            print(f"  ↗️ 通道向上倾斜，中期趋势偏多")
        elif trend_ch["slope"] < 0:
            print(f"  ↘️ 通道向下倾斜，中期趋势偏空")
    print(f"  🎯 七、综合研判")
    print(f"  {'─'*60}")

    # 统一使用 calculate_quant_score 的 7 因子评分（删除手写重复评分）
    scores = quant_score.get('scores', {})
    score_tech = scores.get('技术', 0) + scores.get('动量', 0)
    score_fund = scores.get('基本面', 0)
    score_cap = scores.get('量能', 0) + scores.get('资金流', 0) + scores.get('Level2', 0)
    score_news = scores.get('舆情', 0)
    score_risk = scores.get('风险', 0)
    score_rs = scores.get('相对强弱', 0)

    print(f"  技术面评分: {score_tech:+.0f}  |  基本面评分: {score_fund:+.0f}")
    print(f"  资金面评分: {score_cap:+.0f}  |  舆情评分: {score_news:+.0f}")
    print(f"  风险因子: {score_risk:+.0f}  |  相对强弱: {score_rs:+.0f}  |  量化总分: {quant_score['total']:+.0f}/{quant_score['pct']:.0f}%")
    print(f"  {'─'*40}")

    # ── 因子贡献拆解表 ──
    print(f"\n  📊 因子贡献拆解")
    print(f"  {'─'*40}")
    factor_labels = {
        '动量': '动量(趋势+5日)', '技术': '技术(主升擒龙)', '基本面': '基本面(PE+PB)',
        '量能': '量能(外内盘+主力)', '风险': '风险(K线形态)', '舆情': '舆情(新闻情绪)',
        '资金流': '资金流(融资+机构)', 'Level2': 'Level2(超大单占比)',
        '相对强弱': '相对强弱(跑赢/输大盘)'
    }
    factor_scores = quant_score.get('scores', {})
    # 按贡献从高到低排序
    sorted_factors = sorted(factor_scores.items(), key=lambda x: x[1], reverse=True)
    for fname, fscore in sorted_factors:
        label = factor_labels.get(fname, fname)
        icon = '🟢' if fscore > 0 else ('🔴' if fscore < 0 else '⚪')
        print(f"  {icon} {label}: {fscore:+.0f}")

    # ── 瓶颈因子提示 ──
    bottleneck = sorted_factors[-1] if sorted_factors else None
    if bottleneck and bottleneck[1] < 0:
        bname = bottleneck[0]
        blabel = factor_labels.get(bname, bname)
        print(f"\n  🔍 瓶颈因子: {blabel}({bottleneck[1]:+.0f}) → 建议关注{'估值水平' if bname=='基本面' else '资金流向' if bname in ('量能','Level2') else '趋势方向' if bname=='动量' else '市场情绪' if bname=='舆情' else '风险控制'}")

    # ── 置信度展示 ──
    conf = quant_score.get('confidence', 0)
    conf_label = quant_score.get('conf_label', '⚪ 无信号')
    print(f"\n  🎯 信号置信度: {conf_label} (置信度={conf}/7)")

    # ── 圆桌多视角增强（Workbuddy 方法论互补）──
    if roundtable and roundtable.get('views'):
        print(f"\n  🏛️ 圆桌多视角增强")
        print(f"  {'─'*40}")
        # 视角方向 + 共识度
        views_str = ' | '.join(f'{v}:{"🟢" if d=="多" else "🔴" if d=="空" else "⚪"}{d}' for v, d in roundtable['views'].items())
        print(f"  视角分布: {views_str}")
        print(f"  共识度: {roundtable.get('consensus', '')}")
        # 多空证据权重分离
        bp = roundtable.get('bull_pct', 50)
        print(f"  多空证据: 多头 {roundtable.get('bull_evidence', 0):+.1f} ({bp}%) vs 空头 {roundtable.get('bear_evidence', 0):.1f} ({100-bp}%)"
              f"  {'🟢 一边倒偏多' if bp >= 70 else '🔴 一边倒偏空' if bp <= 30 else '⚪ 多空拉锯'}")
        # 多空辩论权重梯度（Workbuddy 多头/空头研究员镜像梯度）
        bg = roundtable.get('bull_gradient', [])
        rg = roundtable.get('bear_gradient', [])
        if bg or rg:
            bull_str = ' > '.join(f'{k}+{v}' for k, v in bg[:3]) if bg else '无'
            bear_str = ' > '.join(f'{k}-{v}' for k, v in rg[:3]) if rg else '无'
            print(f"  ⚔️ 辩论梯度: 多头论据[{bull_str}] | 空头论据[{bear_str}]")
            if roundtable.get('debate'):
                print(f"  {roundtable['debate']}")
        # 时效加权裁决（截面降权0.4×/实时升权1.5×）
        if 'timing_net' in roundtable:
            tbp = roundtable.get('timing_bull_pct', 50)
            print(f"  ⏱️ 时效加权裁决: 净分 {roundtable.get('timing_net', 0):+.1f}（实时多头 {tbp}%）")
            if roundtable.get('timing_note'):
                print(f"  {roundtable['timing_note']}")
        # 资金-价格背离陷阱
        if roundtable.get('trap'):
            print(f"  {roundtable['trap']}")
        # 概率化路径
        path = roundtable.get('path')
        if path:
            _low_n = path.get('low_sample') or (path.get('n') or 0) < 10
            _warn = ' ⚠️ 小样本（<10），仅展示参考不构成信号' if _low_n else ''
            print(f"  🎲 历史相似路径概率: 上涨{path['up']}% / 震荡{path['flat']}% / 下跌{path['down']}%"
                  f"（{path['n']}样本，均收益{path['avg_ret']:+.2f}%）{_warn}")
        # 关键变量观察台
        obs = roundtable.get('observation', [])
        if obs:
            print(f"\n  🔭 关键变量观察台")
            for var, cur, act in obs:
                print(f"    · {var} {cur}: {act}")
        # 结论失效条件
        inv = roundtable.get('invalidations', [])
        if inv:
            print(f"\n  ⛔ 结论失效条件（证伪框架）")
            for i, x in enumerate(inv, 1):
                print(f"    {i}. {x}")

    # ── 历史胜率反馈（回测校准闭环）──
    # 用 signals.db 中已平仓的"波段启动"信号胜率，反馈当前形态信号的可信度
    try:
        hist = get_signal_stats()
        hw = None
        for st in hist.get('stats', []):
            if st.get('strategy') == '波段启动':
                hw = st
                break
        if hw and hw.get('total', 0) >= 3:
            wr = hw.get('win_rate', 0)
            wr_icon = '🟢' if wr >= 50 else ('🟡' if wr >= 30 else '🔴')
            print(f"\n  📈 历史波段启动信号: {hw['total']}单 胜率{wr}% 均收益{hw.get('avg_return', 0):+.2f}% {wr_icon}")
            wave_sig = tech.get('wave_signal', '') or ''
            if '放量启动' in wave_sig or '缩量回调到位' in wave_sig:
                if wr < 30:
                    print(f"     ⚠️ 历史胜率偏低，当前形态信号建议谨慎对待（先小仓验证）")
                elif wr >= 50:
                    print(f"     ✅ 历史胜率支持该形态，当前信号可信度较高")
        elif hw and hw.get('total', 0) > 0:
            print(f"\n  📈 历史波段启动信号仅 {hw['total']} 单，样本不足，胜率参考价值有限")
    except Exception:
        pass

    # ── 择时过滤提示 ──
    tf = quant_score.get('timing_filter', '')
    if tf:
        print(f"  ⏰ 择时过滤: {tf}")
        orig_pct = quant_score.get('original_pct', pct)
        if orig_pct != pct:
            print(f"  ⚠️ 原始评分{orig_pct:.0f}%被择时过滤降为{pct:.0f}%")

    # 最终建议基于 quant_score 的 pct（0-100）
    pct = quant_score['pct']
    if pct >= 70:
        print(f"\n  ✅ 最终建议: 🟢🟢 强烈看多，五维共振向上，可积极关注")
    elif pct >= 50:
        print(f"\n  ✅ 最终建议: 🟢 偏多，多数维度向好，可逢低布局")
    elif pct < 30:
        print(f"\n  ❌ 最终建议: 🔴🔴 强烈看空，五维共振向下，建议回避")
    elif pct < 50:
        print(f"\n  ❌ 最终建议: 🔴 偏空，多数维度偏弱，建议观望")
    else:
        print(f"\n  ⚠️ 最终建议: 🟡 多空分歧，控制仓位，等待方向明确")
    
    # 标准化 ATR + 仓位建议（凯利公式简化版）
    if tech_ok and price > 0:
        atr = calc_atr(kline_data, 14)
        if atr <= 0:
            atr = price * 0.02  # 退化：用 2% 作为 ATR
        pos = calc_position_size(price, atr, account_size=100000, risk_pct=0.02)
        stop_loss = pos['stop_loss']
        take_profit = pos['take_profit']
        print(f"\n  📐 风控参考（标准化ATR + 仓位建议）")
        print(f"  ATR(14日): {atr:.2f}  |  止损: {stop_loss:.2f} ({pos['stop_pct']:+.1f}%)  |  止盈: {take_profit:.2f} ({pos['profit_pct']:+.1f}%)")
        print(f"  建议仓位: {pos['shares']}股 ({pos['position_pct']:.1f}% 账户)  |  风险预算: 2%账户/笔")
    
    # 相似K线匹配
    try:
        sim = find_similar_kline_patterns(code)
        if sim.get('matches'):
            print(f"\n  🔮 相似K线匹配（DTW）")
            _low_sim = sim.get('low_sample', sim.get('sample_count', 0) < 10)
            _sim_warn = ' ⚠️ 小样本（<10），仅展示参考不构成信号' if _low_sim else ''
            print(f"  历史上相似形态后5日上涨概率: {sim['up_probability']}%  平均收益: {sim['avg_future_return']:+.2f}%{_sim_warn}")
    except Exception as e:
        log.warning(f"相似K线匹配失败: {e}")
    
    # 记录信号到数据库
    if tech_ok:
        signal_type = '★强' if tech.get('qiang_jin_qiang') else ('强势' if tech.get('qiang_shi') else ('做多' if tech.get('duo_kong')=='做多' else ''))
        if signal_type:
            record_signal(code, stock_name, signal_type, price, quant_score.get('confidence', 0))
        # 波段启动信号入库（供 --calibrate 回测验证量价形态胜率）
        # 置信度编码信号强度：🟢 完整确认=3.0，🟡 降级（主力流出/环境偏空）=1.0
        wave_sig = tech.get('wave_signal', '') or ''
        if ('放量启动' in wave_sig or '缩量回调到位' in wave_sig) and '破位' not in wave_sig:
            # 置信度编码信号强度：🟢 完整确认=3.0，🟡 降级（主力流出/环境偏空）=1.0
            # P0+P1b（2026-09-02 量价战法语档）：连续置信度编码（--calibrate 将验证单调性）
            # P0 浪位降档：current_wave≥3（高位浪）×0.6 / =2（第2浪）×0.8——
            #   战法二操作要点(4)"谨防庄家利用第二浪出货"，降档幅度待回测校准
            # P1b 回撤深度加分：距240日前高回撤≥40% ×1.1——
            #   战法一操作要点(3)"距前高远=空头消耗充分=可靠性强"，加分幅度待回测校准
            wave_conf = 3.0 if wave_sig.startswith('🟢') else 1.0
            _cw = tech.get('current_wave') or 1
            if _cw >= 3:
                wave_conf *= 0.6
            elif _cw == 2:
                wave_conf *= 0.8
            _rh = tech.get('wave_retrace_high')
            if _rh is not None and _rh >= 40:
                wave_conf *= 1.1
            record_signal(code, stock_name, '波段启动', price, round(wave_conf, 2))

    # 信号回测闭环：先自动平仓到期信号，再统计胜率
    closed_now = close_expired_signals()
    # 生命周期闭环（2026-08-28）：平仓后刷新各策略裁决缓存——
    # 新平仓样本即时影响"保留/降权/停发"，停发裁决在下次 record_signal 生效
    if closed_now > 0 and _slc is not None:
        try:
            _slc.report(apply=True)
            log.info("信号生命周期裁决已随本次平仓刷新")
        except Exception as e:
            log.warning(f"生命周期裁决刷新失败（不影响主流程）: {e}")
    sig_stats = get_signal_stats()
    open_sigs = get_open_signals()
    overall = sig_stats.get('overall', {})
    has_tracking = sig_stats['stats'] or open_sigs or overall.get('total', 0) > 0
    if has_tracking:
        print(f"\n  📊 信号追踪（回测闭环）")
        # 总体胜率/盈亏比
        total_closed = overall.get('total', 0)
        if total_closed > 0:
            avg_ret = overall.get('avg_return', 0)
            win_rate = overall.get('win_rate', 0)
            pl_ratio = overall.get('pl_ratio', 0)
            odds_net = overall.get('odds_net', 0)
            print(f"  总体: 已平仓{total_closed}单 胜率{win_rate}% 平均收益{avg_ret:+.2f}% 盈亏比{pl_ratio} 赔率加权净值{odds_net:+.2f}%")
            illusion = overall.get('illusion', '')
            if illusion:
                print(f"  {illusion}")
        # 按策略统计
        if sig_stats['stats']:
            for st in sig_stats['stats']:
                print(f"  · {st['strategy']}: {st['total']}次 胜率{st['win_rate']}% 均收益{st['avg_return']:+.2f}% "
                      f"盈亏比{st['pl_ratio']} 赔率净值{st.get('odds_net', 0):+.2f}% "
                      f"(最佳{st['max_return']:+.2f}% 最差{st['min_return']:+.2f}%)")
        # 当前持仓（含浮盈浮亏）
        if open_sigs:
            print(f"  当前持仓: {len(open_sigs)}个信号")
            for s in open_sigs[:5]:
                ret = s.get('return_pct', 0)
                ret_icon = '🟢' if ret >= 0 else '🔴'
                print(f"    {ret_icon} {s['name']}({s['code']}): {s['signal_type']} 入场{s['date']}@{s['entry_price']:.2f} 现{s['current_price']:.2f} ({ret:+.2f}%)")
        if closed_now > 0:
            print(f"  ⚡ 本次自动平仓 {closed_now} 个信号")
        # 生命周期裁决状态（2026-08-28 闭环可见化：停发/降权一眼可见）
        if _slc is not None:
            try:
                _lc_cache = _slc.load_cache()
                _lc_notes = []
                for _st, _ent in _lc_cache.items():
                    if _ent.get('verdict') in ('停发', '降权', '降权观察'):
                        if not _ent.get('enabled', True):
                            _lc_notes.append(f"{_st}:{_ent['verdict']}(停发中)")
                        else:
                            _lc_notes.append(f"{_st}:{_ent['verdict']}(x{_ent.get('confidence_multiplier', 1.0)})")
                if _lc_notes:
                    print(f"  ⚖️ 生命周期裁决: {' / '.join(_lc_notes)}")
            except Exception:
                pass
    
    # ── 数据完整度汇总（十维框架全覆盖）──
    # 检查 10 个维度是否都产生了反馈（龙虎榜"未上榜"算已分析，"接口异常"算缺失）
    lhb_note = dragon_tiger.get('note', '') if dragon_tiger else ''
    lhb_covered = bool(dragon_tiger and (dragon_tiger.get('available') or
                    (lhb_note and '异常' not in lhb_note and '无响应' not in lhb_note)))
    dim_checks = [
        ('技术面', bool(tech_ok and 'error' not in tech)),
        ('基本面', bool(fund and fund.get('roe') is not None)),
        ('资金面', bool(cap_flow and cap_flow.get('level2_available'))),
        ('舆情', bool(sentiment and sentiment.get('data_available'))),
        ('宏观', bool(macro and macro.get('indices'))),
        ('融资融券', bool(margin_data and margin_data.get('available'))),
        ('龙虎榜', lhb_covered),
        ('股东结构', bool(shareholders and shareholders.get('available'))),
        ('主营构成', bool(biz_structure and biz_structure.get('available'))),
        ('机构观点', bool(inst_views and inst_views.get('available'))),
    ]
    avail_dims = [d[0] for d in dim_checks if d[1]]
    missing_dims = [d[0] for d in dim_checks if not d[1]]
    completeness = round(len(avail_dims) / len(dim_checks) * 100, 0)

    print(f"\n{'─'*72}")
    print(f"  📊 数据完整度: {completeness:.0f}% ({len(avail_dims)}/{len(dim_checks)} 维度齐全)")
    if missing_dims:
        print(f"  ⚠️ 数据缺失维度: {', '.join(missing_dims)}")
        print(f"     → 评分可能因数据缺失而偏低，建议结合手动复盘")
    else:
        print(f"  ✅ 全维度数据齐全，评分可信度高")
    # 数据盲区标注（区别于接口故障）：盲区 = 该股本无此数据/未上榜，非采集失败
    blind_notes = []
    # 融资融券盲区：非两融标的（如小盘/次新股无杠杆数据）
    if margin_data is not None and not margin_data.get('available'):
        blind_notes.append('融资融券（可能非两融标的，属数据盲区）')
    # 北向盲区：真实无持仓 ≠ 接口异常（接口异常时 note 含"限流/异常"）
    if north is not None and not north.get('available'):
        n_note = north.get('note', '')
        if n_note and '暂无持股' in str(n_note):
            blind_notes.append('北向持股（该股无北向持仓，属数据盲区）')
    # 龙虎榜盲区：未上榜 ≠ 数据缺失（note 含"异常/无响应"才是接口故障）
    if dragon_tiger is not None and not dragon_tiger.get('available'):
        d_note = dragon_tiger.get('note', '')
        if d_note and '异常' not in str(d_note) and '无响应' not in str(d_note) and '失败' not in str(d_note):
            blind_notes.append('龙虎榜（近期未上榜，非数据缺失）')
    if blind_notes:
        print(f"  🕳️ 数据盲区（非接口故障）: {'；'.join(blind_notes)}")
        print(f"     → 盲区是标的本身缺乏该维度数据，不影响评分可信度，但引用该维度时需知悉缺口")
    # Level2 / 北向 / 板块的限流降级单独提示
    l2_ds = cap_flow.get('level2_data_source') if cap_flow else None
    if l2_ds and '限流' in str(l2_ds):
        print(f"  ⚡ Level2 资金流限流，已用估算值兜底")
    if cap_flow and cap_flow.get('level2_detail_missing'):
        print(f"  ⚡ Level2 分档明细缺失，仅用总量估算")
    print(f"{'─'*72}")

    print(f"\n{'='*72}")
    print(f"  ⚠️ 以上分析基于公开数据，不构成投资建议")
    print(f"{'='*72}")
    
    # 导出K线图
    try:
        export_kline_chart(code, stock_name, kline_data, tech)
    except Exception as e:
        log.warning(f"export_kline_chart 失败: {e}")
    # 导出CSV
    try:
        if args.export == 'csv':
            export_csv_report(code, stock_name, quote, tech, fund, cap_flow, sentiment, quant_score)
    except Exception as e:
        log.warning(f"export_csv_report 失败: {e}")
    # 导出HTML（含雷达图 + 新闻卡片 + K线图引用）
    try:
        if getattr(args, 'html', False) or getattr(args, 'export', None) == 'html':
            export_html_report(code, stock_name, quote, tech, fund, cap_flow, sentiment,
                              quant_score, macro=macro, completeness=completeness,
                              roundtable=roundtable)
    except Exception as e:
        log.warning(f"export_html_report 失败: {e}")

    # ── Workbuddy 结构化契约（2026-08-28：替代 emoji 正则文本契约）──
    # 单行 JSON 以 WORKBUDDY_CONTRACT: 前缀输出，prepare_workbuddy_input 优先读它；
    # 解析失败时 prepare 回退到旧的文本块提取（双轨过渡，防契约漂移静默断供）
    if contract_json:
        try:
            digits = ''.join(ch for ch in code if ch.isdigit())
            if digits[:3] in ('300', '301', '688'):
                _beta = '高β'
            elif digits[:3] in ('600', '601', '603', '000', '002'):
                _beta = '中β'
            else:
                _beta = '低β'
            # 注意：此处 pct 已被主流程 `pct = quant_score['pct']` 覆盖为评分值，
            # 契约的涨跌幅/评分卡必须用 quote 原始 pct（2026-08-28 端到端验证发现）
            _quote_pct = quote.get('pct', 0) if quote else 0
            _contract = {
                'code': code,
                'name': (quote.get('name') if (quote and quote.get('name')) else stock_name),
                'price': price,
                'pct': _quote_pct,
                'arrow': arrow,
                'beta_level': _beta,
                'score_pct': quant_score.get('pct'),
                'final_advice': final_advice_text(quant_score.get('pct', 0)),
                'scorecard': (f"📊 评分卡 | {arrow} {price:.2f} ({_quote_pct:+.2f}%) | 趋势{score_tech} | "
                              f"多空{score_dk} | 信号{score_qs} | RSI(14)={score_rsi:.0f} | "
                              f"MACD{score_macd_detail} | ABC3={score_abc3:+.1f} | HG={score_hg:+.1f}% | "
                              f"PE={score_pe:.0f} | L2={score_l2:+.1f}亿 | 情绪{score_sent:+.0f} | "
                              f"量化{score_quant:.0f}分") if tech_ok else '',
                'top_overflow': bool(tech.get('top_overflow')) if tech_ok else False,
                'top_overflow_reason': (tech.get('top_overflow_reason', '') if tech_ok else ''),
                # P1-3（2026-09-01）：K线形态 + 事件驱动透传给专家——
                # 事件驱动标注是 08-31 网宿"题材日量化盲区"教训的直接对策
                'candle_pattern': (name_candle_pattern(quote) if quote else (None, None))[0],
                'candle_pattern_desc': (name_candle_pattern(quote) if quote else (None, None))[1],
                'event_driven': sorted({n['event'] for n in (sentiment.get('news') or [])
                                        if n.get('event_driven') and n.get('event')}),
                'bounce_suspect': bool(tech.get('bounce_suspect')) if tech_ok else False,
                'bounce_reason': (tech.get('bounce_reason', '') if tech_ok else ''),
                'observation': (['{} {}: {}'.format(v, c, a) for v, c, a in obs]
                                if (roundtable and roundtable.get('observation')) else []),
                'invalidations': (roundtable.get('invalidations', [])
                                  if roundtable else []),
                'timing': ({'net': roundtable.get('timing_net'),
                            'bull_pct': roundtable.get('timing_bull_pct'),
                            'note': roundtable.get('timing_note', '')}
                           if (roundtable and 'timing_net' in roundtable) else None),
                'trap': (roundtable.get('trap', '') if roundtable else ''),
                'anchor': ({'industry': anchor.get('industry'),
                            'median_pe': anchor.get('median_pe'),
                            'signal': anchor.get('signal'),
                            'peers': anchor.get('peers', [])}
                           if anchor.get('available') else None),
                'completeness': {'pct': completeness,
                                 'missing': missing_dims,
                                 'blind': blind_notes},
            }
            print('\nWORKBUDDY_CONTRACT: ' + json.dumps(_contract, ensure_ascii=False, default=str))
        except Exception as e:
            log.warning(f"Workbuddy 契约 JSON 输出失败（prepare 将回退文本提取）: {e}")


# ============================================================
# 主入口
# ============================================================
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description='A股十维量化分析系统',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python stock_quant.py 600519\n"
               "  python stock_quant.py 600036 --json          # 机器可读输出\n"
               "  python stock_quant.py 600519 --no-cache    # 跳过缓存强制刷新\n"
               "  python stock_quant.py --compare 600519 600036\n"
               "  python stock_quant.py --watchlist\n"
               "  python stock_quant.py --hot                # 大盘向好时拉强势股推荐\n"
    )
    parser.add_argument('query', nargs='?', help='股票名称或6位代码')
    parser.add_argument('--compare', '-c', nargs='+', help='多股票对比，如: --compare 600519 600036')
    parser.add_argument('--watchlist', '-w', action='store_true', help='分析自选股池')
    parser.add_argument('--hot', action='store_true', help='大盘向好时拉强势股池推荐（涨幅榜→板块反推→关注池兜底）')
    parser.add_argument('--export', '-e', choices=['csv', 'text', 'html'], help='导出格式（csv 表格 / html 可视化报告）')
    parser.add_argument('--html', action='store_true', help='导出 HTML 可视化报告（含雷达图+新闻卡片）')
    parser.add_argument('--no-cache', action='store_true', help='跳过所有缓存，强制实时拉取')
    parser.add_argument('--json', action='store_true', help='输出 JSON（机器可读，适合接入自动化）')
    parser.add_argument('--calibrate', action='store_true', help='评分权重回测校准（基于已平仓信号统计，诊断权重方向）')
    parser.add_argument('--track-base', action='store_true', help='筑基期资金追踪（每日记录三票+板块资金，累计总额供周五评估）')
    parser.add_argument('--chip-score', action='store_true', help='筹码三因子反弹评分（套牢盘/底部锁定/成本偏离 + 概率档位）')
    parser.add_argument('--workbuddy-contract', action='store_true',
                        help='报告末尾输出 Workbuddy 结构化契约 JSON（供 prepare_workbuddy_input 机器消费）')
    args = parser.parse_args()

    # --calibrate：评分权重回测校准（独立模式，不分析个股）
    if args.calibrate:
        calibrate_weights()
        sys.exit(0)

    # --track-base：筑基期资金追踪（独立模式，每日收盘后记录）
    if args.track_base:
        track_basebuilding()
        sys.exit(0)

    # 大盘询问关键词自动唤起 --hot：用户问"大盘怎么样/推荐股票/今天走势"等时自动切强势股推荐
    HOT_KEYWORDS = ['大盘', '推荐', '今天走势', '走势向好', '强势股', '领涨', '哪些股票', '推荐股票', '大盘向好']
    if args.query and not args.compare and not args.watchlist:
        # 大盘指数名（上证/深证/创业板等）询问 → 自动切 --hot
        is_macro_query = args.query in ('上证指数', '深证成指', '创业板指', '科创50', '沪深300', '大盘')
        is_hot_keyword = any(kw in args.query for kw in HOT_KEYWORDS)
        if is_macro_query or is_hot_keyword:
            args.hot = True

    # --no-cache：清空进程内缓存 + K 线磁盘缓存
    if args.no_cache:
        _MTF_CACHE.clear()
        # 删除 kline 磁盘缓存（quote 缓存 TTL 10s 本就短，不清）
        import glob as _glob
        for cf in _glob.glob(os.path.join(CACHE_DIR, '*.cache')):
            try:
                os.remove(cf)
            except Exception:
                pass
        log.info("--no-cache：已清空 K 线缓存")

    # 批量模式：自选股池
    if args.watchlist:
        watchlist = ['贵州茅台', '招商银行', '宁德时代', '半导体etf', '银行etf', '沪深300']
        print(f"\n{'='*72}")
        print(f"  📊 自选股池批量分析 ({len(watchlist)}只)")
        print(f"{'='*72}")
        table = []
        for name in watchlist:
            code, _ = resolve_stock(name)
            if not code: continue
            tech = analyze_technical(code, 0)
            if tech and 'error' not in tech:
                dk = tech.get('duo_kong', '未知')
                qs = '★' if tech.get('qiang_jin_qiang') else ('🟢' if tech.get('qiang_shi') else ('🟢' if dk=='做多' else '🔴'))
                a1 = tech.get('a1', 0)
                b1 = tech.get('b1', 0)
                abc3 = tech.get('abc3', 0)
                hg = tech.get('hg', 0)
                trend = tech.get('trend', '')
                table.append(f"  {name:8s} {qs}趋势{trend} 多空{dk} A1={a1:.2f} ABC3={abc3:+.1f} HG={hg:+.1f}%")
        for row in table:
            print(row)
        print()
        sys.exit(0)

    # 强势股推荐模式：大盘向好时拉走势向好股
    if args.hot:
        print(f"\n{'='*72}")
        print(f"  🔥 强势股推荐（大盘向好时拉走势向好股）")
        print(f"{'='*72}")
        # 先拉大盘宏观判断是否向好
        macro = fetch_macro()
        sh_idx = next((i for i in macro.get('indices', []) if i['name'] == '上证指数'), None)
        sz_idx = next((i for i in macro.get('indices', []) if i['name'] == '深证成指'), None)
        cy_idx = next((i for i in macro.get('indices', []) if i['name'] == '创业板指'), None)
        if sh_idx:
            sz_p = sz_idx['price'] if sz_idx else 0
            sz_pc = sz_idx['pct'] if sz_idx else 0
            cy_p = cy_idx['price'] if cy_idx else 0
            cy_pc = cy_idx['pct'] if cy_idx else 0
            print(f"  大盘: 上证 {sh_idx['price']} {sh_idx['pct']:+.2f}% | 深证 {sz_p} {sz_pc:+.2f}% | 创板 {cy_p} {cy_pc:+.2f}%")
            breadth = macro.get('breadth', {})
            print(f"  市场广度: 涨{breadth.get('up_count',0)} 跌{breadth.get('down_count',0)} 比例{breadth.get('ratio',0)}")
        print()
        # 拉强势股池
        hot = fetch_hot_stocks(top_n=10)
        if hot.get('available') and hot.get('stocks'):
            print(f"  📊 推荐股池（数据源: {hot['data_source']}）")
            print(f"  {'─'*68}")
            print(f"  {'代码':10s} {'名称':10s} {'现价':>8s} {'涨跌':>8s} {'推荐原因'}")
            print(f"  {'─'*68}")
            for s in hot['stocks']:
                print(f"  {s['code']:10s} {s.get('name','')[:8]:10s} {s['price']:>8.2f} {s.get('pct',0):>+7.2f}% {s.get('reason','')}")
            print(f"  {'─'*68}")
            print(f"  💡 提示: 数据源含'兜底'时为关注股池技术面评分筛选，建议结合东方财富涨停板池手动复盘")
        else:
            print(f"  ❌ 强势股池拉取失败（所有数据源限流/失效）")
            print(f"  💡 建议: 稍后重试（东方财富盘中限流高），或手动查看东方财富涨停板池")
        sys.exit(0)

    # 对比模式
    if args.compare:
        # 参数校验：所有股票名必须能解析
        unresolved = []
        for name in args.compare:
            code, _ = resolve_stock(name)
            if not code:
                unresolved.append(name)
        if unresolved:
            print(f"❌ 无法识别的股票: {', '.join(unresolved)}")
            print(f"   请检查名称拼写，或直接用6位代码（如 600519）")
            sys.exit(1)

        print(f"\n{'='*72}")
        print(f"  📊 多股票对比分析")
        print(f"{'='*72}")
        print(f"  {'标的':8s} {'现价':>8s} {'涨跌':>8s} {'趋势':6s} {'多空':6s} {'ABC3':>7s} {'HG':>7s} {'RSI':>5s} {'评分':>5s}")
        print(f"  {'─'*60}")
        for name in args.compare:
            code, _ = resolve_stock(name)
            if not code: continue
            quote = fetch_quote_tencent(code)
            if not quote: continue
            tech = analyze_technical(code, quote['price'])
            fund = analyze_fundamental(quote, tech)
            cap = analyze_capital_flow(quote, tech)
            sent = fetch_news_sina(code, name)
            score = calculate_quant_score(tech, fund, cap, sent, {}, {}, [])
            price = quote['price']
            pct = quote['pct']
            trend = tech.get('trend', '') if tech else ''
            dk = tech.get('duo_kong', '') if tech else ''
            abc3 = tech.get('abc3', 0) if tech else 0
            hg = tech.get('hg', 0) if tech else 0
            rsi = tech.get('rsi', 0) if tech else 0
            sc = score.get('pct', 0)
            print(f"  {name:8s} {price:>8.2f} {pct:>+7.2f}% {trend:6s} {dk:6s} {abc3:>+6.1f} {hg:>+6.1f}% {rsi:>4.0f} {sc:>4.0f}")
        print()
        sys.exit(0)

    # 单股票模式
    if not args.query:
        parser.print_help()
        sys.exit(1)

    query = args.query
    code, name = resolve_stock(query)

    if not code:
        print(f"❌ 无法识别股票: {query}")
        print(f"   支持的股票: {', '.join(STOCK_NAME_MAP.keys())[:100]}...")
        print(f"   或直接用6位代码（如 600519）")
        sys.exit(1)

    # --json 模式：输出机器可读 JSON
    if args.json:
        try:
            quote = fetch_quote_tencent(code)
            tech = analyze_technical(code, quote['price']) if quote else {}
            fund = analyze_fundamental(quote, tech) if quote else {}
            cap = analyze_capital_flow(quote, tech) if quote else {}
            sent = fetch_news_sina(code, name)
            score = calculate_quant_score(tech, fund, cap, sent, {}, {}, [], code)
            output = {
                'code': code, 'name': name, 'price': quote.get('price') if quote else None,
                'pct': quote.get('pct') if quote else None,
                # ⚠️ 2026-08-12 修复：透传 inactive（盘前/停牌/未开盘）与数据时点，避免 pct=0 被误读为平盘
                'inactive': bool(quote.get('inactive')) if quote else None,
                'data_note': ('盘前/停牌(未开盘)，行情为上一交易日收盘值，pct=0不代表平盘'
                              if quote and quote.get('inactive') else '正常交易时点'),
                'trend': tech.get('trend') if tech else None,
                'score': score, 'timestamp': datetime.now().isoformat(),
            }
            print(json.dumps(output, ensure_ascii=False, default=str))
        except Exception as e:
            print(json.dumps({'error': str(e)}, ensure_ascii=False))
            sys.exit(1)
        sys.exit(0)

    # --chip-score 模式：筹码三因子反弹评分（独立输出，不跑十维完整报告）
    if args.chip_score:
        run_chip_score(code, name)
        sys.exit(0)

    generate_report(code, name,
                    contract_json=getattr(args, 'workbuddy_contract', False))
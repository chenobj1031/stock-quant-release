#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_lifecycle.py — 信号生命周期管理（无效信号自动淘汰）
=====================================================================
背景（2026-08-25 系统能力建设 P0-2）：信号库赔率加权净值 -69.45、主线筹码信号
3 条全亏（含成本 -10.73%），但信号仍照常触发——"负期望信号"没有被系统识别并
降级/停发。本脚本把 mainline_backtest / --calibrate 的验证结论变成自动动作：

  1. 读 signals.db 已平仓信号，按策略分组统计 双口径绩效（价格/含成本）
  2. 与 config.SIGNAL_LIFECYCLE 的淘汰阈值对比，给出裁决：
      保留 / 降权（confidence_multiplier 调低）/ 停发（enabled=False）
  3. 支持 --apply 把裁决写回 config 的运行时缓存（或输出建议供人工确认）
  4. 支持 --check 在 record_signal 前调用：某信号是否允许触发

淘汰规则（写死可证伪，阈值在 config.py 集中管理）：
  - 样本 < min_samples        → 继续积累，不做结论
  - 均收益(含成本) < max_loss_rate → 触发淘汰（均收益低于亏损线）
  - 胜率 < min_win_rate       → 触发淘汰（min_win_rate 为 None 时不检查）

用法：
  python3 signal_lifecycle.py --report          # 诊断：各信号绩效 + 裁决建议
  python3 signal_lifecycle.py --report --apply  # 裁决结果写回 data/signal_lifecycle.json（运行时缓存）
  python3 signal_lifecycle.py --check 主升擒龙  # 单信号检查（供 record_signal 前置调用）

裁决缓存（data/signal_lifecycle.json）：
  {"主升擒龙": {"verdict": "保留|降权|停发|积累中", "enabled": true,
                "confidence_multiplier": 1.0, "checked_at": "..."}}
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
SIGNAL_DB = os.path.join(DATA_DIR, 'signals.db')
CACHE_FILE = os.path.join(DATA_DIR, 'signal_lifecycle.json')

sys.path.insert(0, BASE_DIR)
from config import SIGNAL_LIFECYCLE, signal_config
from sim_trade import SimAccount


def _conn():
    if not os.path.exists(SIGNAL_DB):
        return None
    return sqlite3.connect(SIGNAL_DB)


def cost_adjusted_returns(strategy, limit=None):
    """按策略取已平仓信号的 价格口径收益 与 含成本口径收益（sim_trade 重算）
    P2-12(2026-09-30)：新增 limit 滚动窗口——生命周期自动升降档只看最近 N 单
    （limit=None 全量，向后兼容 --report 展示口径）。返回 [(price_ret, cost_ret), ...]
    """
    conn = _conn()
    if conn is None:
        return []
    sql = ("SELECT code, date, price, exit_price, exit_date, return_pct FROM signals "
           "WHERE status='closed' AND return_pct IS NOT NULL AND COALESCE(strategy, signal_type)=? "
           "ORDER BY COALESCE(exit_date, date) DESC")
    params = [strategy]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    out = []
    for code, sdate, entry, exit_, exdate, price_ret in rows:
        if not entry or entry <= 0 or not exit_:
            continue
        acc = SimAccount(init_capital=100000, trade_cost={'t0_mode': True})
        acc.new_day(str(sdate)[:10] or '2026-01-01')
        # 高价股（688/300 高位）固定1000股会资金不足被跳过 → 按资金比例买入
        vol = acc.max_buy_volume(code, float(entry), cash_ratio=1.0)
        if vol <= 0:
            continue
        b = acc.execute(code, 'buy', float(entry), vol, date_str=str(sdate)[:10])
        if not b['filled']:
            continue
        s = acc.execute(code, 'sell', float(exit_), vol,
                        date_str=str(exdate or sdate)[:10])
        if s['filled']:
            buy_total = b['actual_price'] * vol + b['trade_cost']
            sell_net = s['actual_price'] * vol - s['trade_cost']
            cost_ret = (sell_net - buy_total) / buy_total * 100
        else:
            cost_ret = float(price_ret or 0)
        out.append((float(price_ret or 0), cost_ret))
    return out


def verdict_for(strategy, pairs):
    """对单个策略的收益对数组做淘汰裁决
    返回 dict: verdict/enabled/confidence_multiplier/说明
    """
    cfg = signal_config(strategy)
    n = len(pairs)
    if n == 0:
        return {'verdict': '无样本', 'enabled': cfg['enabled'],
                'confidence_multiplier': cfg['confidence_multiplier'],
                'n': 0, 'note': '无已平仓信号'}
    if n < cfg.get('min_samples', 30):
        return {'verdict': '积累中', 'enabled': cfg['enabled'],
                'confidence_multiplier': cfg['confidence_multiplier'],
                'n': n, 'note': f'样本{n}<{cfg.get("min_samples")}，继续积累'}
    cost_rets = [c for _, c in pairs]
    avg_cost = sum(cost_rets) / n
    win_rate = sum(1 for c in cost_rets if c > 0) / n * 100
    max_loss = cfg.get('max_loss_rate', -1.5)
    min_wr = cfg.get('min_win_rate')
    triggers = []
    if avg_cost < max_loss:
        triggers.append(f'均收益{avg_cost:+.2f}% < 亏损线{max_loss}%')
    if min_wr is not None and win_rate < min_wr:
        triggers.append(f'胜率{win_rate:.1f}% < 底线{min_wr}%')
    if triggers:
        return {'verdict': '停发', 'enabled': False,
                'confidence_multiplier': 0.0,
                'n': n, 'note': '；'.join(triggers),
                'avg_cost': round(avg_cost, 2), 'win_rate': round(win_rate, 1)}
    # 预警区：接近阈值但未触发（均收益介于 亏损线 与 0 之间且胜率贴近底线）
    warn = []
    if avg_cost < 0:
        warn.append(f'均收益{avg_cost:+.2f}%仍为负（距亏损线{max_loss}%: {abs(avg_cost - max_loss):.2f}pct）')
    if min_wr is not None and win_rate < min_wr + 10:
        warn.append(f'胜率{win_rate:.1f}%贴近底线{min_wr}%')
    return {'verdict': '保留' if not warn else '降权观察', 'enabled': True,
            # P2-12：降权观察档实际调低乘数（原实现维持原乘数=降权不生效），下限0.3防归零
            'confidence_multiplier': (max(0.3, round(cfg['confidence_multiplier'] * 0.5, 2))
                                      if warn else cfg['confidence_multiplier']),
            'n': n, 'note': '；'.join(warn) if warn else '绩效达标',
            'avg_cost': round(avg_cost, 2), 'win_rate': round(win_rate, 1)}


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            return json.load(open(CACHE_FILE, encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def report(apply=False):
    """诊断全部配置内信号 + 可选写回缓存"""
    print(f"{'='*76}")
    print("  🔄 信号生命周期诊断（无效信号自动淘汰）")
    print(f"{'='*76}")
    cache = load_cache()
    for strategy in SIGNAL_LIFECYCLE:
        cfg = SIGNAL_LIFECYCLE[strategy]
        pairs = cost_adjusted_returns(strategy)
        v = verdict_for(strategy, pairs)
        print(f"\n  【{strategy}】enabled={cfg['enabled']} 置信度乘数={cfg['confidence_multiplier']}")
        if v['n'] == 0:
            print(f"    {v['verdict']}｜{v['note']}")
            continue
        if 'avg_cost' in v:
            print(f"    样本{v['n']} 均收益(含成本){v['avg_cost']:+.2f}% 胜率{v['win_rate']:.1f}%")
        print(f"    裁决: {v['verdict']}｜{v['note']}")
        cache[strategy] = {
            'verdict': v['verdict'],
            'enabled': v['enabled'],
            'confidence_multiplier': v['confidence_multiplier'],
            'n': v.get('n', 0),
            'checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    if apply:
        save_cache(cache)
        print(f"\n✅ 裁决已写回: {CACHE_FILE}")


def auto_verdict(strategy, window=60):
    """P2-12(2026-09-30)：滚动 window 单自动升降档 + 停发恢复滞后
    - 升降档：只看最近 window 单滚动绩效（新旧表现分窗，避免陈旧样本拖累/美化）
    - 恢复滞后（防 whipsaw）：此前停发/停发观察的策略，需硬证据才恢复触发——
      均收益(含成本)≥0 或 胜率≥底线+15pct；仅回到预警区(降权观察)不恢复
    """
    prev = load_cache().get(strategy, {})
    prev_verdict = prev.get('verdict', '')
    v = verdict_for(strategy, cost_adjusted_returns(strategy, limit=window))
    if prev_verdict in ('停发', '停发观察') and v.get('enabled'):
        wr = v.get('win_rate')
        avg = v.get('avg_cost')
        min_wr = signal_config(strategy).get('min_win_rate')
        hard_clear = ((avg is not None and avg >= 0)
                      or (min_wr is not None and wr is not None and wr >= min_wr + 15))
        if not hard_clear:
            v = dict(v)
            v['verdict'] = '停发观察'
            v['enabled'] = False
            v['confidence_multiplier'] = 0.0
            v['note'] = (v.get('note', '')
                         + '；恢复滞后：硬证据不足（需均收益≥0 或 胜率≥底线+15pct），维持停发观察').strip('；')
    return v


def check(strategy):
    """record_signal 前调用：该信号是否允许触发 + 置信度乘数
    P2-12: 缓存缺失或超龄(>24h) → 自动重算滚动升降档并写回（无需人工 --apply）
    """
    cache = load_cache()
    entry = cache.get(strategy)
    need_refresh = True
    if entry and entry.get('checked_at'):
        try:
            t = datetime.strptime(str(entry['checked_at']), '%Y-%m-%d %H:%M:%S')
            need_refresh = (datetime.now() - t).total_seconds() > 24 * 3600
        except Exception:
            need_refresh = True
    if need_refresh:
        try:
            v = auto_verdict(strategy)
            cache[strategy] = {
                'verdict': v['verdict'], 'enabled': v['enabled'],
                'confidence_multiplier': v['confidence_multiplier'],
                'n': v.get('n', 0),
                'checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'auto': True,
            }
            save_cache(cache)
            return bool(v['enabled']), float(v['confidence_multiplier'])
        except Exception:
            pass  # 自动刷新失败 → 回退旧缓存/配置（不阻断信号入库主流程）
    if entry:
        return bool(entry.get('enabled', True)), float(entry.get('confidence_multiplier', 1.0))
    v = verdict_for(strategy, cost_adjusted_returns(strategy))
    return bool(v['enabled']), float(v['confidence_multiplier'])


def main():
    ap = argparse.ArgumentParser(description='信号生命周期管理（无效信号淘汰）')
    ap.add_argument('--report', action='store_true', help='诊断全部信号绩效+裁决')
    ap.add_argument('--apply', action='store_true', help='裁决写回缓存（配合 --report）')
    ap.add_argument('--check', default=None, help='单信号检查（enabled/multiplier）')
    args = ap.parse_args()
    if args.check:
        enabled, mult = check(args.check)
        print(f'  {args.check}: enabled={enabled} 置信度乘数={mult}')
        print(f'  → {"✅ 允许触发" if enabled else "⛔ 已停发（负期望信号）"}')
    else:
        report(apply=args.apply)


if __name__ == '__main__':
    main()

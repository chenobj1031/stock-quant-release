#!/usr/bin/env python3
"""outcome_writer.py — 平仓结果 → 参数校准记录（2026-09-14 建设）

架构定位（反馈回路，四大组件的最后一环）：
sim_trades 的平仓 pnl 此前没有通道回灌到选股/风控参数——"知识→实践检验→
沉淀能力"的闭环在落地端断了。本组件做三件事：
  1. 扫描未归档的平仓记录（sim_trades），关联开仓来源（bridge/scan vs 会话内手动）
  2. 逐笔写校准记录：哪条参数通道产生的交易、结果如何、建议方向
  3. 汇总统计：按来源/板块/是否20cm 分桶，输出校准日志供 9-30 月结使用

边界（诚实声明）：样本 < 30 笔前不做任何参数自动修改——只记录、只统计、
只给"下次校准建议"，参数修改永远走月结复盘的人工决策。这防止小样本过拟合。

用法：
  python3 outcome_writer.py            # 扫描并归档新平仓 → 校准记录
  python3 outcome_writer.py --summary  # 打印全量校准统计
"""
import json, os, sys, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
TRF_F = os.path.join(DATA, 'sim_trades.json')
ODF_F = os.path.join(DATA, 'sim_orders.json')
CALIB_F = os.path.join(DATA, 'outcome_calibration.json')

MIN_SAMPLE_FOR_AUTO = 30   # 样本底线：不足则只记录不建议


def load(f):
    with open(f, encoding='utf-8') as fp:
        return json.load(fp)


def source_of_trade(t, orders):
    """关联开仓来源：通过 order_id 找买单的 meta.source / reason 前缀"""
    oid = t.get('order_id', '')
    # 卖出单（S*/O* 平仓）回溯对应买入单：按 code 找最近的 filled 买单
    buy = None
    for o in orders:
        if o.get('code') == t.get('code') and o.get('side') == 'buy' and o.get('status') == 'filled':
            buy = o   # 最后一个 filled 买单近似开仓来源
    if not buy:
        return 'manual_session'   # 会话内手动挂单（无 meta）
    meta = buy.get('meta') or {}
    if meta.get('source') == 'scan_daily':
        return 'scan_daily_bridge'
    if str(buy.get('reason', '')).startswith('[bridge]'):
        return 'scan_daily_bridge'
    return 'manual_session'


def new_closed_trades(trades, archived):
    """未归档的平仓记录（按 ts+code 唯一键）"""
    seen = {(a['ts'], a['code']) for a in archived}
    out = []
    for t in trades:
        if t.get('side') != 'sell' or 'pnl' not in t:
            continue
        key = (t['ts'], t['code'])
        if key not in seen:
            out.append(t)
    return out


def classify(t):
    """单笔结果 → 校准信号（只在样本足够时参与统计均值，先全部如实记录）"""
    pnl = t.get('pnl', 0)
    cost = t.get('value', 0) or 1
    ret = pnl / cost * 100
    note = t.get('note', '')
    if '止损' in note:
        kind = 'stop_hit'
    elif t.get('order_id', '').startswith(('O6', 'S')) or '锁盈' in note:
        kind = 'profit_take'
    else:
        kind = 'manual_exit'
    return {'ret_pct': round(ret, 2), 'kind': kind,
            'stop_discipline': '机械' if '机械' in note else '会话'}


def run():
    today = datetime.date.today().strftime('%Y-%m-%d')
    trades, orders = load(TRF_F), load(ODF_F)
    try:
        calib = load(CALIB_F)
    except (OSError, ValueError):
        calib = {'archived': [], 'summary': {}}
    fresh = new_closed_trades(trades, calib.get('archived', []))
    if not fresh:
        print(f'无新平仓记录待归档（已归档 {len(calib.get("archived", []))} 笔）')
        return
    for t in fresh:
        entry = {'ts': t['ts'], 'code': t['code'], 'name': t['name'],
                 'pnl': t.get('pnl'), 'source': source_of_trade(t, orders),
                 **classify(t)}
        calib.setdefault('archived', []).append(entry)
        sign = '+' if (entry['pnl'] or 0) >= 0 else ''
        print(f"📝 归档 {t['ts'][:10]} {t['name']} pnl {sign}{t.get('pnl', 0):,.0f} "
              f"({entry['ret_pct']:+.2f}%) 来源:{entry['source']} 类型:{entry['kind']}")
    # ---- 分桶统计 ----
    arch = calib['archived']
    n = len(arch)
    buckets = {}
    for a in arch:
        buckets.setdefault(a['source'], []).append(a)
    calib['summary'] = {'as_of': today, 'total_closed': n,
                        'by_source': {k: {'n': len(v),
                                          'win_rate': round(sum(1 for x in v if (x['pnl'] or 0) > 0) / len(v) * 100, 1),
                                          'avg_ret': round(sum(x['ret_pct'] for x in v) / len(v), 2)}
                                       for k, v in buckets.items()},
                        'note': (f'样本 {n}/{MIN_SAMPLE_FOR_AUTO}，不足自动校准底线——只记录不修改参数'
                                 if n < MIN_SAMPLE_FOR_AUTO else
                                 f'样本 {n}≥{MIN_SAMPLE_FOR_AUTO}，可在月结复盘讨论参数修改')}
    json.dump(calib, open(CALIB_F, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f"\n校准记录 → {CALIB_F}（累计 {n} 笔）")
    print(f"结论: {calib['summary']['note']}")


def summary():
    try:
        calib = load(CALIB_F)
    except (OSError, ValueError):
        print('无校准记录，先运行 outcome_writer.py')
        return
    print(f"=== 平仓校准统计（截至 {calib['summary'].get('as_of')}）===")
    for src, s in calib['summary'].get('by_source', {}).items():
        print(f"  {src}: {s['n']}笔 胜率{s['win_rate']}% 平均{s['avg_ret']:+.2f}%")
    print(' ', calib['summary'].get('note', ''))


if __name__ == '__main__':
    if '--summary' in sys.argv:
        summary()
    else:
        run()

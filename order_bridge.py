#!/usr/bin/env python3
"""order_bridge.py — 候选/评级 → sim_orders 转换器（2026-09-14 建设）

架构定位：daily_review 产出评级、scan_daily 产出候选，但 sim_loop 只认
sim_orders.json——判断与交易之间缺转换层，导致 09-08~09-14 五个交易日
0 张新委托单（用户指出的"停滞"）。本组件做三件事：
  1. 20cm 风控自适应：30/68 开头标的止损按波动放宽、仓位折半
  2. 候选→买单：scan_daily 达标候选（score≥门槛）生成条件买单
  3. 停滞检查：当日 0 新委托单且非主动满仓 → 告警留痕（每日必检）

下单权边界：本组件只写委托单（pending，for_date=今日），执行永远在
sim_loop 的机械评估（sr.evaluate），AI 不直接成交——与 sim_rules v2.0 一致。

用法：
  python3 order_bridge.py            # 基于今日 candidates.json 生成候选买单
  python3 order_bridge.py --dryrun   # 只打印不落盘
"""
import json, os, sys, datetime

import sim_report as sr   # 2026-09-21 并发打架修复：账本锁（ledger_lock）+ 原子写（save）

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
CAND_F = os.path.join(DATA, 'candidates.json')
ODF_F = os.path.join(DATA, 'sim_orders.json')
ACC_F = os.path.join(DATA, 'sim_account.json')
BRIDGE_LOG = os.path.join(BASE, '复盘', 'sim_log', 'exec.log')

# ---- 委托生成参数（保守起步，9-30 月结用首批池外数据校准）----
SCORE_MIN = 55            # 候选买单评分门槛（0-100 绝对分）
MAX_OPEN_ORDERS = 2       # 同日待成交买单上限（防滥挂）
MAX_POSITION_PCT = 0.20   # 单票占净值上限（20cm 标的自动减半）
RISK_PCT = 0.02           # 单笔风险预算 2%（与 calc_position_size 同口径）
BUY_BUFFER_PCT = 0.005    # 买单触发价 = 现价上浮 0.5%（放量突破确认）
MAX_CHASE_PCT = 7.0       # 追高禁入全局上限（百分数口径）：涨幅>7% 只观察不挂单；20cm 板块上限另由 limit_pct 放宽至17%


def limit_pct(code6):
    return 0.20 if code6[:2] in ('30', '68') else 0.10


def risk_params(c):
    """20cm 风控自适应：止损放宽（波动大，2×ATR 天然更远）、仓位折半"""
    is20 = c.get('is_20cm', c['code6'][:2] in ('30', '68'))
    return {'max_pos_pct': MAX_POSITION_PCT / 2 if is20 else MAX_POSITION_PCT,
            'atr_mult': 2.5 if is20 else 2.0,   # 20cm 止损放宽到 2.5×ATR
            'is20': is20}


def nav_now():
    """当前净值（现金+持仓市值，持仓用成本近似——bridge 只做预算，不需精确）"""
    try:
        acc = json.load(open(ACC_F, encoding='utf-8'))
    except (OSError, ValueError):
        return 200000.0
    mv = sum(p['cost'] * p['shares'] for p in acc.get('positions', {}).values())
    return acc.get('cash', 0) + mv


def build_buy_order(c, nav):
    """候选 → 买单 dict。不达标返回 (None, 原因)"""
    rp = risk_params(c)
    px = c.get('px') or c.get('price')
    stop_sug = c.get('stop_sug')
    if not px or not stop_sug or px <= 0:
        return None, '缺少价格/止损建议'
    pct = c.get('pct', 0)
    chase_line = (limit_pct(c['code6']) - 0.03) * 100   # 追高禁入线（板块口径）：主板7% 创业/科创17%
    if pct > chase_line:
        return None, f'涨幅{pct}% 超追高禁入线（只观察）'
    # 风险预算 sizing：股数 = 可承受亏损 / 每股止损距离，再受单票仓位上限封顶
    # （修复：原直接按 max_pos_pct 满配，RISK_PCT 定义未用，风险预算失效）
    stop_exec = round(px - (px - stop_sug) * (rp['atr_mult'] / 2.0), 2)
    per_share_risk = px - stop_exec
    risk_shares = int(nav * RISK_PCT / per_share_risk / 100) * 100 if per_share_risk > 0 else 0
    cap_shares = int(nav * rp['max_pos_pct'] / px / 100) * 100
    shares = min(risk_shares, cap_shares)
    if shares < 100:
        return None, f'预算股数不足100股（风险{risk_shares}/上限{cap_shares}）'
    trigger = round(px * (1 + BUY_BUFFER_PCT), 2)
    tag = '20cm' if rp['is20'] else '10cm'
    return {
        'id': f"B{c['code6']}", 'for_date': datetime.date.today().strftime('%Y-%m-%d'),
        'code': c['code6'], 'full': c.get('full') or ('sh' if c['code6'][0] in '569' else 'sz') + c['code6'],
        'name': c['name'], 'side': 'buy', 'shares': shares,
        'cond': {'min': trigger}, 'manual_confirm': False,
        'stop': round(px - (px - stop_sug) * (rp['atr_mult'] / 2.0), 2),
        'reason': (f"[bridge] 全市场scan score={c.get('score')} ({tag}) "
                   f"涨幅{pct}% 量比{c.get('vol_ratio')} 主力净额{(c.get('main_net') or 0)/1e8:.1f}亿 "
                   f"止损2×ATR建议{stop_sug}"),
        'status': 'pending',
        'meta': {'source': 'scan_daily', 'score': c.get('score'),
                 'in_pool': c.get('in_pool', False), 'board': tag}}, None


def yesterday_downgrades(today):
    """昨日盘中证伪降分清单（alert_engine ALERT_FALSIFIED 写入）——次日评分减分依据"""
    try:
        dg = json.load(open(os.path.join(DATA, 'downgrade_list.json'), encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if dg.get('date') == today:
        return {}   # 当日的降分不作用于今日（防同日循环），只作用于次日
    return dg.get('codes') or {}


def stale_orders(orders, today):
    """昨日及更早的 pending 单视为过期（for_date 纪律），由 bridge 清理
    架构核查修复(2026-09-22)：原只清 side=='buy'——停牌股的昨日 pending 卖单
    无任何清理路径（evaluate 只处理今日单、force_close 只处理今日卖单），
    永久滞留并污染 open_buys/仓位占用统计。买卖单统一纳入过期清理。"""
    stale = [o for o in orders if o['status'] == 'pending'
             and o.get('for_date', today) < today]
    for o in stale:
        o['status'] = 'expired'
        o['expire_note'] = 'bridge 过期清理（for_date 已过）'
    return stale


def _run_impl(dryrun=False):
    today = datetime.date.today().strftime('%Y-%m-%d')
    try:
        cand = json.load(open(CAND_F, encoding='utf-8'))
    except (OSError, ValueError):
        print('❌ 无 candidates.json，先跑 scan_daily.py')
        return []
    if cand.get('date') != today:
        print(f'❌ candidates.json 是 {cand.get("date")} 的旧数据（今日 {today}），拒绝基于陈旧候选下单')
        return []
    orders = json.load(open(ODF_F, encoding='utf-8'))
    stale = stale_orders(orders, today)
    nav = nav_now()
    have_ids = {o['id'] for o in orders}
    open_buys = [o for o in orders if o['side'] == 'buy' and o['status'] == 'pending']
    acc = json.load(open(ACC_F, encoding='utf-8'))
    held = {c for c in acc.get('positions', {})}
    downgrades = yesterday_downgrades(today)   # 昨日盘中证伪 → 评分减分（纠偏闭环）
    created, skipped = [], []
    for c in cand.get('candidates', []):
        if c['code6'] in held:
            skipped.append((c['name'], '已持仓'))
            continue
        eff_score = c.get('score', 0)
        dg = downgrades.get(c['code6'])
        if dg:
            eff_score -= 10   # 证伪减分幅度：10分（月结用跟随率数据校准）
            skipped.append((c['name'], f"昨日盘中证伪{dg.get('move')}% → 有效评分{eff_score}(原{c.get('score')})"))
        if eff_score < SCORE_MIN:
            if not dg:
                skipped.append((c['name'], f"score {c.get('score')}<{SCORE_MIN}"))
            continue
        oid = f"B{c['code6']}"
        if oid in have_ids or any(o['code'] == c['code6'] for o in open_buys):
            skipped.append((c['name'], '已有待成交单'))
            continue
        if len(open_buys) >= MAX_OPEN_ORDERS:
            skipped.append((c['name'], f'待成交买单已达{MAX_OPEN_ORDERS}张上限'))
            continue
        o, why = build_buy_order(c, nav)
        if not o:
            skipped.append((c['name'], why))
            continue
        orders.append(o)
        open_buys.append(o)
        created.append(o)
    if not dryrun:
        sr.save(ODF_F, orders)   # 2026-09-21 并发打架修复：原子写（防半截 JSON）
    # ---- 停滞检查（每日必检，含"主动不出手"的显式留痕）----
    new_today = [o for o in orders if o.get('for_date') == today and o['side'] == 'buy']
    lines = [f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] BRIDGE {today}: "
             f"候选{len(cand.get('candidates', []))}只"
             f"(池内{cand.get('in_pool_count')}/池外{cand.get('out_pool_count')}) "
             f"新买单{len(created)}张 过期清理{len(stale)}张"]
    for o in created:
        lines.append(f"  → {o['id']} {o['name']} {o['shares']}股 触发≥{o['cond'].get('min')} "
                     f"止损{o['stop']} [{o['meta']['board']}]")
    for nm, why in skipped:
        lines.append(f"  ✗ {nm}: {why}")
    if not new_today:
        lines.append('  ⚠️ 停滞检查: 今日 0 张新买单 —— 若为主动防守需在此留痕理由')
    print('\n'.join(lines))
    if not dryrun:
        os.makedirs(os.path.dirname(BRIDGE_LOG), exist_ok=True)
        with open(BRIDGE_LOG, 'a', encoding='utf-8') as fp:
            fp.write('\n'.join(lines) + '\n')
    return created


# 2026-09-21 并发打架修复：run 整函数=账本临界区（与 sim_loop/node_report 共享 data/.sim.lock）
run = sr.locked(_run_impl)


if __name__ == '__main__':
    run(dryrun='--dryrun' in sys.argv)

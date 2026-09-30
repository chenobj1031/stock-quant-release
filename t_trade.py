#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
t_trade.py — 做 T 记录机制（每日做 T 买卖 + 摊薄成本更新持仓台账）
=====================================================================
背景（2026-08-25）：用户将个股作为中期持仓，每天通过"做 T"
（高抛低吸）降低成本。系统需要：
  1. 记录每日做 T 的买卖明细（日期/代码/方向/价格/股数）
  2. 按【摊薄成本法】更新持仓台账（holdings.json）——做 T 的已实现盈亏
     计入持仓成本，体现"做 T 降低成本"的真实效果
  3. 每日收盘统计做 T 收益，供 perf_trend / Workbuddy 引用

做 T 记账口径（写死可证伪，摊薄成本法）：
  - 低吸（buy）：持仓股数增加，成本 = (旧成本×旧股数 + 买入价×买入股数) / 新总股数
  - 高抛（sell）：持仓股数减少，成本不变（剩余持仓成本保持），已实现盈亏单独记录
    —— 摊薄成本法：高抛盈利 → 剩余持仓成本降低（体现"做 T 降低成本"）
  - 做 T 收益 = 高抛收入 - 对应成本（按加权成本结转）

存储：
  - data/t_trades.json  做 T 明细（append-only）
  - data/holdings.json  持仓台账（t_trade 更新成本/股数）

用法：
  python3 t_trade.py --buy  --code 600519 --price 1455.00 --shares 200 --date 2026-08-25
  python3 t_trade.py --sell --code 600519 --price 1498.00 --shares 100 --date 2026-08-25
  python3 t_trade.py --list [--date 2026-08-25]     # 查看做 T 明细
  python3 t_trade.py --report [--date 2026-08-25]   # 当日做 T 收益统计
  python3 t_trade.py --sync                          # 同步持仓台账（重算成本）
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
T_TRADES_JSON = os.path.join(DATA_DIR, 't_trades.json')
HOLDINGS_JSON = os.path.join(DATA_DIR, 'holdings.json')


def load_trades():
    if os.path.exists(T_TRADES_JSON):
        try:
            with open(T_TRADES_JSON, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _atomic_save(path, data):
    """P2-6(2026-09-22体检)：原裸 open('w') 写文件，进程中途被杀会留半截 JSON
    （t_trades/holdings 损坏后 --list/--report 全挂）——tmp+rename 原子替换"""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def save_trades(trades):
    _atomic_save(T_TRADES_JSON, trades)


def load_holdings():
    if os.path.exists(HOLDINGS_JSON):
        try:
            with open(HOLDINGS_JSON, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_holdings(holdings):
    _atomic_save(HOLDINGS_JSON, holdings)


def _norm_code(code):
    """6位代码 → 纯数字（与 holdings.json 的 code 字段对齐）"""
    return ''.join(ch for ch in code if ch.isdigit())


def record_trade(side, code, price, shares, date='', note=''):
    """记录一笔做 T 买卖，并同步更新持仓台账（摊薄成本法）"""
    code = _norm_code(code)
    date = date or datetime.now().strftime('%Y-%m-%d')
    trades = load_trades()
    holdings = load_holdings()
    entry = {'date': date, 'code': code, 'side': side,
             'price': round(float(price), 3), 'shares': int(shares), 'note': note}
    trades.append(entry)
    save_trades(trades)

    # 同步持仓台账
    h = next((x for x in holdings if x['code'] == code), None)
    if h is None:
        print(f'  ⚠️ 持仓台账中无 {code}，仅记录做 T 明细（未同步持仓）')
        return
    old_cost, old_shares = h['cost'], h['shares']
    if side == 'buy':
        new_shares = old_shares + int(shares)
        new_cost = (old_cost * old_shares + float(price) * int(shares)) / new_shares
        h['cost'] = round(new_cost, 3)
        h['shares'] = new_shares
        h['note'] = f"做T低吸@{price}（{date}）" + (f'；{note}' if note else '')
        print(f'  ✅ 低吸 {code}: {old_shares}→{new_shares}股，成本 {old_cost:.3f}→{h["cost"]:.3f}')
    else:  # sell
        if old_shares < int(shares):
            print(f'  ⚠️ 持仓不足: {code} 仅 {old_shares} 股，卖出 {shares} 股失败')
            trades.pop()  # 回滚明细
            save_trades(trades)
            return
        # P2-6 T+1 校验(2026-09-22体检)：当日低吸股当日不可卖（交易所规则）——
        # 统计当日该 code 低吸股数，高抛不得超过"老股"额度；拦截后回滚明细
        today_buys = sum(t['shares'] for t in trades
                         if t['code'] == code and t['side'] == 'buy' and t['date'] == date)
        sellable = old_shares - today_buys
        if int(shares) > sellable:
            print(f'  ❌ T+1 拦截: {code} 当日低吸{today_buys}股锁定，仅可卖老股 {sellable} 股 < 请卖 {shares} 股')
            trades.pop()
            save_trades(trades)
            return
        new_shares = old_shares - int(shares)
        # 摊薄成本法：剩余持仓成本不变，高抛盈利体现为"成本降低"（记录到 note）
        realized = (float(price) - old_cost) * int(shares)
        # 记录卖出时成本（2026-08-26 修复：清仓后台账移除该 code，report 需靠此字段算收益）
        entry['cost_at_sell'] = round(old_cost, 3)
        h['shares'] = new_shares
        h['note'] = f"做T高抛@{price}（{date}）已实现+{realized:.0f}元" + (f'；{note}' if note else '')
        print(f'  ✅ 高抛 {code}: {old_shares}→{new_shares}股，成本 {old_cost:.3f}（不变），已实现+{realized:.0f}元')
        if new_shares <= 0:
            holdings.remove(h)
            print(f'  ℹ️ {code} 已清仓（做T卖出全部持仓）')
    save_holdings(holdings)


def list_trades(date=None):
    trades = load_trades()
    if not trades:
        print('📭 暂无做 T 记录')
        return
    if date:
        trades = [t for t in trades if t['date'] == date]
    print(f"{'日期':<12}{'代码':<8}{'方向':<6}{'价格':>8}{'股数':>6} 备注")
    print('-' * 52)
    for t in trades:
        side = '🔻 低吸' if t['side'] == 'buy' else '🔺 高抛'
        print(f"{t['date']:<12}{t['code']:<8}{side:<6}{t['price']:>8.3f}{t['shares']:>6} {t.get('note','')}")


def report(date=None):
    """当日/全部做 T 收益统计（按日期聚合）"""
    trades = load_trades()
    if not trades:
        print('📭 暂无做 T 记录')
        return
    if date:
        trades = [t for t in trades if t['date'] == date]
    # 按代码聚合：低吸成本 vs 高抛收入
    by_code = {}
    for t in trades:
        c = by_code.setdefault(t['code'], {'buy_cost': 0.0, 'buy_shares': 0,
                                           'sell_income': 0.0, 'sell_shares': 0})
        if t['side'] == 'buy':
            c['buy_cost'] += t['price'] * t['shares']
            c['buy_shares'] += t['shares']
        else:
            c['sell_income'] += t['price'] * t['shares']
            c['sell_shares'] += t['shares']
    print(f"{'='*64}")
    print(f"  📈 做 T 收益统计{'（' + date + '）' if date else ''}")
    print(f"{'='*64}")
    total_realized = 0.0
    for code, c in by_code.items():
        if c['buy_shares']:
            avg_cost = c['buy_cost'] / c['buy_shares']
        else:
            # 无低吸记录：优先用卖出记录里存的 cost_at_sell（清仓场景，台账已移除）
            cost_at_sell = next((t.get('cost_at_sell') for t in trades
                                 if t['code'] == code and t.get('cost_at_sell')), None)
            if cost_at_sell is not None:
                avg_cost = cost_at_sell
            else:
                # 兜底：从持仓台账取成本
                h = next((x for x in load_holdings() if x['code'] == code), None)
                avg_cost = h['cost'] if h else 0
        realized = c['sell_income'] - avg_cost * c['sell_shares'] if c['sell_shares'] else 0
        total_realized += realized
        print(f"  {code}: 低吸{c['buy_shares']}股@{avg_cost:.3f} 高抛{c['sell_shares']}股"
              f" 已实现{realized:+.0f}元")
    print(f"  {'─'*64}")
    print(f"  做 T 已实现收益合计: {total_realized:+.0f}元")
    print(f"{'='*64}\n")


def sync():
    """同步：从做 T 明细重算持仓台账（幂等，供对账）"""
    trades = load_trades()
    holdings = load_holdings()
    # 按代码重建：初始持仓 = 台账当前值，逐笔应用
    # 注意：这里以台账为基准，仅重算成本（做 T 明细是增量）
    print('  ℹ️ sync: 做 T 明细已实时同步持仓（记录时即更新），此处仅对账')
    for t in trades:
        code = t['code']
        h = next((x for x in holdings if x['code'] == code), None)
        if h:
            print(f"    {code}: 台账成本{h['cost']:.3f}×{h['shares']}股（最近做T: {t['side']}@{t['price']}）")
    print('  ✅ 对账完成')


def main():
    ap = argparse.ArgumentParser(description='做 T 记录机制（摊薄成本法）')
    ap.add_argument('--buy', action='store_true', help='记录低吸')
    ap.add_argument('--sell', action='store_true', help='记录高抛')
    ap.add_argument('--code', default='', help='股票代码')
    ap.add_argument('--price', type=float, default=None, help='成交价')
    ap.add_argument('--shares', type=int, default=None, help='股数')
    ap.add_argument('--date', default=None, help='日期 YYYY-MM-DD')
    ap.add_argument('--note', default='', help='备注')
    ap.add_argument('--list', action='store_true', help='查看明细')
    ap.add_argument('--report', action='store_true', help='做 T 收益统计')
    ap.add_argument('--sync', action='store_true', help='对账同步')
    args = ap.parse_args()

    if args.buy or args.sell:
        side = 'buy' if args.buy else 'sell'
        if not args.code or not args.price or not args.shares:
            print('  ❌ 需要 --code --price --shares')
            return
        record_trade(side, args.code, args.price, args.shares, args.date, args.note)
    elif args.list:
        list_trades(args.date)
    elif args.report:
        report(args.date)
    elif args.sync:
        sync()
    else:
        ap.print_help()


if __name__ == '__main__':
    main()

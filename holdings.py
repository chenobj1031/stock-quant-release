#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
holdings.py — 持仓台账（成本视角接入个股评估）
=====================================================================
背景（2026-08-25）：用户提供实际持仓（成本价/股数/建仓日），系统从此能在
评估个股时带上"你的成本视角"——判断当前价位对你是浮盈还是浮亏、止损止盈
触发线在哪、持仓性质（套牢/获利/成本区）如何，而不是只看现价与均线。

存储：data/holdings.json
  [{"code":"600000","name":"示例股票","cost":10.0,"shares":1000,
    "buy_date":"2026-08-xx","note":""}, ...]

功能：
  1. --add 录入持仓（成本/股数/建仓日）
  2. --list 查看持仓清单
  3. --report 拉当前行情，计算每只浮盈浮亏 + 止损止盈线（基于成本）
  4. --del 删除持仓

止损止盈线（写死可证伪，基于成本价）：
  - 止损线 = 成本 × (1 - 8%)   （浮亏 -8% 触发，与信号库止损口径一致）
  - 止盈保护 = 成本 × (1 + 15%)（浮盈 +15% 触发保护，与信号库止盈口径一致）
  - 成本区 = 现价在 [成本×0.95, 成本×1.05] 内 → 贴成本，方向选择敏感

用法：
  python3 holdings.py --add --code 600000 --name 示例股票 --cost 10.0 --shares 1000 --date 2026-01-01
  python3 holdings.py --list
  python3 holdings.py --report          # 拉行情算浮盈浮亏
  python3 holdings.py --del --code 600000
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
HOLDINGS_JSON = os.path.join(DATA_DIR, 'holdings.json')

STOP_LOSS_PCT = 0.08    # 止损 -8%（与信号库止损口径一致）
TAKE_PROFIT_PCT = 0.15  # 止盈保护 +15%（与信号库止盈口径一致）
COST_ZONE_PCT = 0.05    # 成本区 ±5%


def load_holdings():
    if os.path.exists(HOLDINGS_JSON):
        try:
            with open(HOLDINGS_JSON, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_holdings(holdings):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HOLDINGS_JSON, 'w', encoding='utf-8') as f:
        json.dump(holdings, f, ensure_ascii=False, indent=2)


def add_holding(code, name, cost, shares, buy_date='', note=''):
    """录入/更新持仓（同 code 覆盖）"""
    holdings = load_holdings()
    entry = {'code': code, 'name': name, 'cost': round(float(cost), 3),
             'shares': int(shares), 'buy_date': buy_date, 'note': note}
    for i, h in enumerate(holdings):
        if h['code'] == code:
            holdings[i] = entry
            save_holdings(holdings)
            print(f'✅ 已更新持仓: {name}({code}) 成本{entry["cost"]}×{entry["shares"]}股')
            return
    holdings.append(entry)
    save_holdings(holdings)
    print(f'✅ 已录入持仓: {name}({code}) 成本{entry["cost"]}×{entry["shares"]}股')


def delete_holding(code):
    holdings = load_holdings()
    before = len(holdings)
    holdings = [h for h in holdings if h['code'] != code]
    if len(holdings) == before:
        print(f'  ⚠️ 未找到 {code}')
        return
    save_holdings(holdings)
    print(f'✅ 已删除持仓: {code}')


def list_holdings():
    holdings = load_holdings()
    if not holdings:
        print('📭 暂无持仓记录')
        return
    print(f"{'代码':<8}{'名称':<10}{'成本':>8}{'股数':>6}{'建仓日':<12}备注")
    print('-' * 56)
    for h in holdings:
        print(f"{h['code']:<8}{h['name']:<10}{h['cost']:>8.3f}{h['shares']:>6}"
              f"{h.get('buy_date',''):<12}{h.get('note','')}")


def report():
    """拉当前行情，计算每只持仓浮盈浮亏 + 止损止盈线"""
    sys.path.insert(0, BASE_DIR)
    import stock_quant as sq
    holdings = load_holdings()
    if not holdings:
        print('📭 暂无持仓记录，先 --add 录入')
        return

    print(f"{'='*88}")
    print(f"  💼 持仓台账 · 成本视角（{datetime.now().strftime('%Y-%m-%d %H:%M')}）")
    print(f"{'='*88}")
    total_cost = total_mv = 0.0
    for h in holdings:
        code = h['code']
        pure = code[2:] if code.startswith(('sh', 'sz')) else code
        full = ('sh' if pure.startswith('6') else 'sz') + pure
        q = sq.fetch_quote_tencent(full)
        if not q or q.get('price', 0) <= 0:
            print(f"  ⚠️ {h['name']}({code}) 行情获取失败")
            continue
        price = q['price']
        cost = h['cost']
        shares = h['shares']
        mv = price * shares
        cost_total = cost * shares
        pnl = mv - cost_total
        pnl_pct = (price - cost) / cost * 100
        total_cost += cost_total
        total_mv += mv
        # 状态判定
        if pnl_pct <= -STOP_LOSS_PCT * 100:
            status = '🔴 止损区'
        elif pnl_pct >= TAKE_PROFIT_PCT * 100:
            status = '🟢 止盈保护区'
        elif abs(pnl_pct) <= COST_ZONE_PCT * 100:
            status = '⚪ 成本区'
        elif pnl_pct > 0:
            status = '🟡 浮盈'
        else:
            status = '🟠 浮亏'
        stop = cost * (1 - STOP_LOSS_PCT)
        tp = cost * (1 + TAKE_PROFIT_PCT)
        print(f"\n  {h['name']}({code}) 现价{price:.2f} {q.get('pct', 0):+.2f}%")
        print(f"    成本 {cost:.3f} × {shares}股 = {cost_total:,.0f}元")
        print(f"    市值 {mv:,.0f}元 ｜ 浮盈 {pnl:+,.0f}元（{pnl_pct:+.2f}%）｜ {status}")
        print(f"    止损线 {stop:.2f}（-8%）｜ 止盈保护 {tp:.2f}（+15%）"
              + (f"｜ 现价距止损 {((price-stop)/price*100):.1f}%" if price > stop else ''))
    print(f"\n  {'─'*88}")
    print(f"  总成本 {total_cost:,.0f}元 ｜ 总市值 {total_mv:,.0f}元 ｜ "
          f"总浮盈 {total_mv-total_cost:+,.0f}元（{(total_mv-total_cost)/total_cost*100:+.2f}%）")
    print(f"{'='*88}\n")


def main():
    ap = argparse.ArgumentParser(description='持仓台账（成本视角）')
    ap.add_argument('--add', action='store_true', help='录入/更新持仓')
    ap.add_argument('--list', action='store_true', help='查看持仓清单')
    ap.add_argument('--report', action='store_true', help='拉行情算浮盈浮亏')
    ap.add_argument('--del', dest='delete', action='store_true', help='删除持仓')
    ap.add_argument('--code', default='', help='股票代码（6位或带前缀）')
    ap.add_argument('--name', default='', help='股票名')
    ap.add_argument('--cost', type=float, default=None, help='成本价')
    ap.add_argument('--shares', type=int, default=None, help='股数')
    ap.add_argument('--date', default='', help='建仓日期 YYYY-MM-DD')
    ap.add_argument('--note', default='', help='备注')
    args = ap.parse_args()

    if args.add:
        if not args.code or not args.cost or not args.shares:
            print('  ❌ --add 需要 --code --cost --shares')
            return
        add_holding(args.code, args.name, args.cost, args.shares, args.date, args.note)
    elif args.delete:
        delete_holding(args.code)
    elif args.report:
        report()
    else:
        list_holdings()


if __name__ == '__main__':
    main()

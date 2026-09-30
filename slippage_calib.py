#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
slippage_calib.py — 真实滑点录入与成本率校准（交易执行反馈闭环）
=====================================================================
背景（2026-08-25 系统能力建设 P2-9）：sim_trade 滑点默认 0.1% 比例（ratio），
round_trip_cost_rate 的滑点项也是理论值——从未用真实成交数据校准过。本脚本：

  1. 录入真实成交记录：触发价（信号价/盘前价）vs 实际成交价（手动录入或批量导入）
  2. 计算实测滑点：买=(实价-触发价)/触发价；卖=(触发价-实价)/触发价（都取正值为成本）
  3. 统计滑点分布（均值/中位数/P90），给出校准建议（ratio）
  4. --apply 写回 data/slippage_calib.json，sim_trade 的 TRADE_COST_DEFAULT 读取后覆盖滑点
     （round_trip_cost_rate 亦随之更新，做空口径的对称成本扣减同步校准）

存储：data/slippage_calib.db（录入）+ data/slippage_calib.json（校准结果）

用法：
  # 录入一笔真实成交（收盘后补录当天实际买卖价）
  python3 slippage_calib.py --add --date 2026-08-25 --code 600036 \
      --trigger 60.15 --exec-price 60.18 --side buy --note "盘前价买入"
  # 统计 + 校准建议
  python3 slippage_calib.py --report
  # 写回校准结果（sim_trade 自动读取）
  python3 slippage_calib.py --apply

统计口径（写死可证伪）：
  - 滑点 = 触发价 与 实际成交价 的相对差（买/卖都折算为正成本）
  - 建议 ratio = min(P90, max(均值, 0.0001)) —— 用 P90 防个别极端成交拉高
  - 样本 < 5 时输出"继续积累"，不写回（避免小样本误导）
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
CALIB_DB = os.path.join(DATA_DIR, 'slippage_calib.db')
CALIB_JSON = os.path.join(DATA_DIR, 'slippage_calib.json')
MIN_SAMPLES = 5


def _conn():
    """统一连接（2026-08-26 架构优化：迁移到 db.py 单一 quant.db）"""
    from db import get_conn
    return get_conn()


def add_fill(date, code, trigger, exec_price, side='buy', note=''):
    """录入一笔真实成交"""
    if not trigger or trigger <= 0 or not exec_price or exec_price <= 0:
        print('  ❌ 触发价/成交价必须为正')
        return
    conn = _conn()
    conn.execute("INSERT INTO fills (date, code, trigger_price, exec_price, side, note) "
                 "VALUES (?,?,?,?,?,?)",
                 (date, code, float(trigger), float(exec_price), side, note))
    conn.commit()
    conn.close()
    # 计算单笔滑点
    if side == 'buy':
        slip = (float(exec_price) - float(trigger)) / float(trigger)
    else:
        slip = (float(trigger) - float(exec_price)) / float(trigger)
    print(f'  ✅ 已录入: {date} {code} {side} 触发{trigger}→实价{exec_price} '
          f'滑点{slip*100:+.3f}%')


def import_csv(path, side='buy'):
    """批量导入：每行 date,code,trigger_price,exec_price[,note]"""
    conn = _conn()
    n = 0
    with open(path, encoding='utf-8') as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith('#'):
                continue
            parts = [p.strip() for p in ln.split(',')]
            if len(parts) < 4:
                continue
            date, code, trg, ex = parts[0], parts[1], parts[2], parts[3]
            note = parts[4] if len(parts) > 4 else ''
            try:
                trg, ex = float(trg), float(ex)
            except ValueError:
                continue
            conn.execute("INSERT INTO fills (date, code, trigger_price, exec_price, side, note) "
                         "VALUES (?,?,?,?,?,?)", (date, code, trg, ex, side, note))
            n += 1
    conn.commit()
    conn.close()
    print(f'  ✅ 已从 {path} 导入 {n} 笔（{side}）')


def load_fills():
    conn = _conn()
    rows = conn.execute("SELECT date, code, trigger_price, exec_price, side, note "
                        "FROM fills ORDER BY date, id").fetchall()
    conn.close()
    return rows


def compute_slips(fills):
    """计算每笔滑点（买/卖都折算为正成本）"""
    slips = []
    for date, code, trg, ex, side, note in fills:
        if not trg or trg <= 0:
            continue
        if side == 'buy':
            s = (ex - trg) / trg
        else:
            s = (trg - ex) / trg
        slips.append(s)
    return slips


def report():
    fills = load_fills()
    if not fills:
        print('📭 暂无真实成交记录。先 --add 录入或 --import-csv 批量导入。')
        return
    slips = compute_slips(fills)
    print(f"{'='*72}")
    print(f"  🎯 真实滑点统计（共 {len(fills)} 笔）")
    print(f"{'='*72}")
    print(f"{'日期':<12}{'代码':<8}{'方向':<5}{'触发价':>8}{'成交价':>8}{'滑点%':>9} 备注")
    print('-' * 72)
    for fill, s in zip(fills, slips):
        d, code, trg, ex, side, note = fill
        print(f"{d:<12}{code:<8}{side:<5}{trg:>8.2f}{ex:>8.2f}{s*100:>+8.3f}% {note or ''}")
    if not slips:
        print('  ⚠️ 无有效滑点样本')
        return
    n = len(slips)
    mean = sum(slips) / n
    sorted_s = sorted(slips)
    median = sorted_s[n // 2]
    p90 = sorted_s[int(n * 0.9) - 1] if n >= 10 else sorted_s[-1]
    print('-' * 72)
    print(f"  样本: {n}｜均值 {mean*100:.3f}%｜中位数 {median*100:.3f}%｜P90 {p90*100:.3f}%")
    print(f"  建议滑点 ratio: {max(mean, 0.0001)*100:.3f}%（样本>={MIN_SAMPLES} 后可 --apply 写回）"
          if n >= MIN_SAMPLES else
          f"  ⚠️ 样本 < {MIN_SAMPLES}，继续积累后再校准（当前 {n} 笔）")


def apply():
    fills = load_fills()
    slips = compute_slips(fills)
    if len(slips) < MIN_SAMPLES:
        print(f'  ❌ 样本 {len(slips)} < {MIN_SAMPLES}，不写回（避免小样本误导）')
        return
    mean = sum(slips) / len(slips)
    sorted_s = sorted(slips)
    p90 = sorted_s[int(len(slips) * 0.9) - 1] if len(slips) >= 10 else sorted_s[-1]
    # 建议 ratio：用 P90 防个别极端成交拉高；下限 0.01%
    ratio = max(min(p90, mean * 1.5), 0.0001)
    calib = {
        'ratio': round(ratio, 6),
        'mean': round(mean, 6),
        'median': round(sorted_s[len(slips) // 2], 6),
        'p90': round(p90, 6),
        'n': len(slips),
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(CALIB_JSON, 'w', encoding='utf-8') as f:
        json.dump(calib, f, ensure_ascii=False, indent=2)
    print(f'  ✅ 校准结果已写回: {CALIB_JSON}')
    print(f'  ratio={calib["ratio"]*100:.3f}%（{len(slips)} 笔样本，P90 口径）')
    print(f'  → sim_trade 将自动读取覆盖滑点参数（round_trip_cost_rate 同步校准）')


def main():
    ap = argparse.ArgumentParser(description='真实滑点录入与成本率校准')
    ap.add_argument('--add', action='store_true', help='录入一笔真实成交')
    ap.add_argument('--import-csv', dest='csv', default=None, help='批量导入CSV')
    ap.add_argument('--report', action='store_true', help='统计滑点分布+校准建议')
    ap.add_argument('--apply', action='store_true', help='写回校准结果')
    ap.add_argument('--date', default=None, help='成交日期 YYYY-MM-DD')
    ap.add_argument('--code', default='', help='股票代码')
    ap.add_argument('--trigger', dest='trigger', type=float, default=None, help='触发价（信号价）')
    ap.add_argument('--exec-price', dest='exec', type=float, default=None, help='实际成交价')
    ap.add_argument('--side', default='buy', choices=['buy', 'sell'], help='买卖方向')
    ap.add_argument('--note', default='', help='备注')
    args = ap.parse_args()

    if args.add:
        add_fill(args.date or datetime.now().strftime('%Y-%m-%d'), args.code,
                 args.trigger, args.exec, args.side, args.note)
    elif args.csv:
        import_csv(args.csv, args.side)
    elif args.report:
        report()
    elif args.apply:
        apply()
    else:
        ap.print_help()


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
perf_trend.py — 绩效趋势跟踪（赔率加权口径，月度判定依据）
=====================================================================
背景（2026-08-25 专家模式讨论结论）：专家研判模式的意义不在"预测命中率"，
而在规则沉淀 + 可证伪 + 绩效度量。核心 KPI 从"命中率"改为"赔率加权净值趋势"：
连续 2 个月赔率加权净值为负 → 停止加维度，回头修信号定义/换因子，而非继续每天跑。

本脚本把三个绩效口径统一汇总到 `复盘/绩效趋势.csv`（每周/每日追加一行）：

  1. premarket      盘前预测绩效（读 复盘/{date}盘前绩效.json，daily_review --close 落盘）
  2. signal_library 信号库已平仓统计（读 signals.db，stock_quant.get_signal_stats）
  3. expert_plan    专家预案打分绩效（复用 score_expert_plan.compute_performance）

CSV 列：date, source, n, win_rate, pl_ratio, odds_net, avg_return, max_drawdown, sharpe, illusion, note
（同 date+source 幂等：已存在则覆盖更新，不会重复追加）

用法：
  python3 perf_trend.py --record [--date 2026-08-25]      # 记录今日 盘前+信号库 绩效
  python3 perf_trend.py --record --plan plans/plan_xxx.txt --date 2026-08-18   # 追加专家预案绩效
  python3 perf_trend.py --report                          # 输出趋势表 + 月度判定
  python3 perf_trend.py --report --source premarket       # 只看某口径
"""
import argparse
import csv
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_quant as sq

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REVIEW_DIR = os.path.join(BASE_DIR, '复盘')
TREND_CSV = os.path.join(REVIEW_DIR, '绩效趋势.csv')
# 判定阈值（写死可证伪）：连续 N 个月赔率加权净值为负 → 建议重构
NEG_MONTHS_LIMIT = 2

FIELDS = ['date', 'source', 'n', 'win_rate', 'pl_ratio', 'odds_net',
          'avg_return', 'max_drawdown', 'sharpe', 'illusion', 'note']


def today_str():
    return datetime.datetime.now().strftime('%Y-%m-%d')


def _load_trend():
    """读 CSV → list[dict]；文件不存在返回 []"""
    if not os.path.exists(TREND_CSV):
        return []
    try:
        with open(TREND_CSV, encoding='utf-8') as f:
            return list(csv.DictReader(f))
    except Exception as e:
        print(f'  ⚠️ 读取趋势CSV失败: {e}')
        return []


def _save_trend(rows):
    os.makedirs(REVIEW_DIR, exist_ok=True)
    with open(TREND_CSV, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in FIELDS})


def upsert_row(rows, row):
    """同 date+source 幂等更新；否则追加"""
    for i, r in enumerate(rows):
        if r.get('date') == row['date'] and r.get('source') == row['source']:
            rows[i] = row
            return rows, False
    rows.append(row)
    return rows, True


def collect_premarket(date_str):
    """读 复盘/{date}盘前绩效.json → CSV 行"""
    p = os.path.join(REVIEW_DIR, f'{date_str}盘前绩效.json')
    if not os.path.exists(p):
        print(f'  ⚠️ 未找到 {p}（先运行 daily_review.py --close）')
        return None
    with open(p, encoding='utf-8') as f:
        per = json.load(f)
    n = per.get('n', 0)
    win_rate = round(per.get('win_n', 0) / n * 100, 1) if n else 0
    odds_net = round(per.get('total_profit', 0) - per.get('total_loss', 0), 2)
    return {
        'date': date_str, 'source': 'premarket',
        'n': n, 'win_rate': win_rate,
        'pl_ratio': per.get('pl_ratio', ''),
        'odds_net': odds_net,
        'avg_return': per.get('avg_return', ''),
        'max_drawdown': per.get('max_drawdown', ''),
        'sharpe': per.get('sharpe', ''),
        'illusion': per.get('illusion', ''),
        'note': '盘前预测绩效（daily_review --close）',
    }


def collect_signal_library(date_str):
    """读 signals.db 已平仓统计 → CSV 行"""
    stats = sq.get_signal_stats()
    overall = stats.get('overall', {})
    total = overall.get('total', 0)
    if total == 0:
        print('  ⚠️ 信号库无已平仓信号，跳过 signal_library')
        return None
    return {
        'date': date_str, 'source': 'signal_library',
        'n': total,
        'win_rate': overall.get('win_rate', 0),
        'pl_ratio': overall.get('pl_ratio', ''),
        'odds_net': overall.get('odds_net', 0),
        'avg_return': overall.get('avg_return', ''),
        'max_drawdown': '',
        'sharpe': '',
        'illusion': overall.get('illusion', ''),
        'note': '信号库已平仓统计（signals.db）',
    }


def collect_expert_plan(plan_file, plan_date):
    """跑 score_expert_plan.compute_performance → CSV 行（复用其模拟撮合口径）"""
    try:
        import score_expert_plan as sep
    except ImportError as e:
        print(f'  ⚠️ score_expert_plan 导入失败: {e}')
        return None
    with open(plan_file, encoding='utf-8') as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith('#')]
    rows = []
    for ln in lines:
        code, cond, action, note = sep.parse_plan_line(ln)
        symbol = sep.sina_code(code)
        price = sep.extract_price(cond)
        kind = sep.cond_kind(cond)
        bias = sep.action_bias(action)
        try:
            klines = sep.fetch_kline(symbol, 40)
        except Exception:
            continue
        plan_i = next((i for i, k in enumerate(klines) if k['day'] == plan_date), None)
        if plan_i is None or plan_i + 1 >= len(klines):
            continue
        nxt = klines[plan_i + 1]
        ohlc = {'open': float(nxt['open']), 'high': float(nxt['high']),
                'low': float(nxt['low']), 'close': float(nxt['close'])}
        triggered = price is not None and sep.check_trigger(kind, price, ohlc)
        if not triggered or bias == 0:
            continue
        exit_i = plan_i + 1 + 3
        exit_price = float(klines[exit_i]['close']) if exit_i < len(klines) else float(klines[-1]['close'])
        rows.append({'code': code, 'bias': bias, 'date': nxt['day'],
                     'entry_price': ohlc['open'], 'exit_price': exit_price})
    if not rows:
        print('  ⚠️ 预案无触发且有方向的样本，无法计算绩效')
        return None
    per = sep.compute_performance(rows, capital=100000, hold=3)
    n = per.get('n', 0)
    win_rate = round(per.get('win_n', 0) / n * 100, 1) if n else 0
    odds_net = round(per.get('total_profit', 0) - per.get('total_loss', 0), 2)
    return {
        'date': plan_date, 'source': 'expert_plan',
        'n': n, 'win_rate': win_rate,
        'pl_ratio': per.get('pl_ratio', ''),
        'odds_net': odds_net,
        'avg_return': per.get('avg_return', ''),
        'max_drawdown': per.get('max_drawdown', ''),
        'sharpe': per.get('sharpe', ''),
        'illusion': per.get('illusion', ''),
        'note': f'专家预案打分（{os.path.basename(plan_file)}）',
    }


def record(date_str, plan_file=None):
    rows = _load_trend()
    added = 0
    # 1. 盘前绩效
    r1 = collect_premarket(date_str)
    if r1:
        rows, is_new = upsert_row(rows, r1)
        added += is_new
        print(f'  ✅ premarket {date_str}: n={r1["n"]} 胜率{r1["win_rate"]}% '
              f'盈亏比{r1["pl_ratio"]} 赔率加权{r1["odds_net"]:+.2f}')
    # 2. 信号库
    r2 = collect_signal_library(date_str)
    if r2:
        rows, is_new = upsert_row(rows, r2)
        added += is_new
        print(f'  ✅ signal_library {date_str}: n={r2["n"]} 胜率{r2["win_rate"]}% '
              f'盈亏比{r2["pl_ratio"]} 赔率加权{r2["odds_net"]:+.2f}')
    # 3. 专家预案（可选）
    if plan_file:
        r3 = collect_expert_plan(plan_file, date_str)
        if r3:
            rows, is_new = upsert_row(rows, r3)
            added += is_new
            print(f'  ✅ expert_plan {date_str}: n={r3["n"]} 胜率{r3["win_rate"]}% '
                  f'盈亏比{r3["pl_ratio"]} 赔率加权{r3["odds_net"]:+.2f}')
    _save_trend(rows)
    print(f'\n📊 绩效趋势已更新: {TREND_CSV}（本次新增 {added} 行，共 {len(rows)} 行）')


def report(source_filter=None):
    rows = _load_trend()
    if not rows:
        print('暂无绩效趋势数据，先运行: python3 perf_trend.py --record')
        return
    if source_filter:
        rows = [r for r in rows if r.get('source') == source_filter]
    # 按 date 排序
    rows.sort(key=lambda r: (r.get('date', ''), r.get('source', '')))
    print(f'{"="*92}')
    print('  📈 绩效趋势（赔率加权口径，核心 KPI = odds_net）')
    print(f'{"="*92}')
    print(f'{"日期":<12}{"口径":<16}{"n":>4}{"胜率%":>8}{"盈亏比":>8}{"赔率加权":>10}{"均收益%":>9}{"回撤%":>8}{"夏普":>7}')
    print('-' * 92)
    for r in rows:
        try:
            wn = f"{float(r.get('win_rate', 0) or 0):.1f}"
        except ValueError:
            wn = '-'
        try:
            pr = f"{float(r.get('pl_ratio', 0) or 0):.2f}"
        except ValueError:
            pr = '-'
        try:
            on = f"{float(r.get('odds_net', 0) or 0):+.2f}"
        except ValueError:
            on = '-'
        try:
            ar = f"{float(r.get('avg_return', 0) or 0):+.2f}"
        except ValueError:
            ar = '-'
        try:
            md = f"{float(r.get('max_drawdown', 0) or 0):.1f}"
        except ValueError:
            md = '-'
        try:
            sp = f"{float(r.get('sharpe', 0) or 0):.1f}"
        except ValueError:
            sp = '-'
        print(f"{r.get('date',''):<12}{r.get('source',''):<16}{str(r.get('n','')):>4}"
              f"{wn:>8}{pr:>8}{on:>10}{ar:>9}{md:>8}{sp:>7}")
    # 月度判定（按 signal_library 的赔率加权净值）
    print()
    verdict_monthly(rows)


def verdict_monthly(rows):
    """按月聚合 signal_library 赔率加权净值，判定是否连续 N 月为负"""
    sig = [r for r in rows if r.get('source') == 'signal_library' and r.get('odds_net') != '']
    if not sig:
        print('  ℹ️ 暂无 signal_library 数据，月度判定跳过')
        return
    monthly = {}
    for r in sig:
        m = str(r.get('date', ''))[:7]
        try:
            on = float(r.get('odds_net', 0) or 0)
        except ValueError:
            continue
        monthly.setdefault(m, []).append(on)
    months = sorted(monthly)
    print('  【月度 signal_library 赔率加权净值】')
    for m in months:
        vals = monthly[m]
        avg = sum(vals) / len(vals)
        print(f'    {m}: {avg:+.2f}（{len(vals)} 次记录）')
    # 连续负月判定
    neg_streak = 0
    for m in months:
        avg = sum(monthly[m]) / len(monthly[m])
        if avg < 0:
            neg_streak += 1
        else:
            neg_streak = 0
    if neg_streak >= NEG_MONTHS_LIMIT:
        print(f'\n  🔴 连续 {neg_streak} 个月赔率加权净值为负（≥{NEG_MONTHS_LIMIT}）→ '
              f'按 2026-08-25 讨论约定：停止增加新维度，回去修信号定义/换因子，'
              f'而非继续每天跑专家研判')
    elif neg_streak == 1:
        print(f'\n  🟡 最近 1 个月赔率加权净值为负，继续跟踪；连续 {NEG_MONTHS_LIMIT} 个月为负则触发重构判定')
    else:
        print(f'\n  🟢 最近赔率加权净值非负（或最近月为正），按当前方向继续积累')


def main():
    ap = argparse.ArgumentParser(description='绩效趋势跟踪（赔率加权口径）')
    ap.add_argument('--record', action='store_true', help='记录今日 盘前+信号库 绩效')
    ap.add_argument('--plan', default=None, help='--record 时附带：预案文件路径，追加 expert_plan 绩效')
    ap.add_argument('--date', default=None, help='记录日期 YYYY-MM-DD（默认今天）')
    ap.add_argument('--report', action='store_true', help='输出趋势表 + 月度判定')
    ap.add_argument('--source', default=None, help='--report 时按口径过滤：premarket/signal_library/expert_plan')
    args = ap.parse_args()

    if args.record:
        d = args.date or today_str()
        record(d, plan_file=args.plan)
    elif args.report:
        report(source_filter=args.source)
    else:
        ap.print_help()


if __name__ == '__main__':
    main()

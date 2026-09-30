#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py — 一键全流程入口（盘前预测 → 收盘复盘 → Workbuddy → 月度绩效）
=====================================================================
背景（2026-08-25 系统能力建设 P2-7）：9+ 个独立脚本各自参数，盘前要手动跑
daily_review --premarket、收盘要跑 --close、预案要跑 score_expert_plan、月末要跑
perf_trend——流程碎片化、前置产物依赖靠人记。本入口串联各阶段并校验前置产物。

阶段：
  premarket   盘前竞价后预测基准（daily_review --premarket → 复盘/{date}盘前预测基准_auto.md + JSON）
  close       收盘复盘对照（daily_review --close → 复盘/{date}收盘复盘报告_auto.md + 盘前绩效.json）
              ⚠️ 前置：当日盘前基准 JSON 必须存在（close 依赖它做对照）
  workbuddy   Workbuddy 预填生成（prepare_workbuddy_input.py → ~/Workbuddy/workbuddy-daily-input/{date}.md）
              ⚠️ 前置：收盘复盘报告（量化背书段引用其盘前绩效）
  monthly     月度绩效汇总（perf_trend --record + --report）
  all         盘前 → 收盘 → Workbuddy 全链（--date 指定时）
  plan        专家预案打分 + 绩效记录（score_expert_plan + perf_trend --plan）

用法：
  python3 pipeline.py premarket [--pool 600519 600036 ...]
  python3 pipeline.py close [--no-cache]
  python3 pipeline.py workbuddy [--date 2026-08-25]
  python3 pipeline.py monthly [--month 2026-08]
  python3 pipeline.py all --date 2026-08-25        # premarket→close→workbuddy
  python3 pipeline.py plan --plan plans/plan_xxx.txt --date 2026-08-18
"""
import argparse
import datetime
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REVIEW_DIR = os.path.join(BASE_DIR, '复盘')


def today():
    return datetime.date.today().strftime('%Y-%m-%d')


def run(args, label, timeout=600):
    """运行子命令并打印结果"""
    print(f"\n{'='*60}")
    print(f"  ▶ {label}")
    print(f"{'='*60}")
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=BASE_DIR)
        out = (r.stdout or '') + (('\n[stderr] ' + r.stderr[-800:]) if r.stderr else '')
        print(out[-2500:])  # 只显示尾部，避免刷屏
        ok = r.returncode == 0
        print(f"  {'✅' if ok else '❌'} {label} {'成功' if ok else f'失败(exit {r.returncode})'}")
        return ok
    except subprocess.TimeoutExpired:
        print(f'  ❌ {label} 超时（>{timeout}s）')
        return False
    except Exception as e:
        print(f'  ❌ {label} 异常: {e}')
        return False


def check_exists(path, label):
    """前置产物校验"""
    if os.path.exists(path):
        print(f'  ✅ 前置产物存在: {label}')
        return True
    print(f'  ❌ 前置产物缺失: {label}（{path}）')
    return False


def stage_premarket(pool, no_cache):
    args = [sys.executable, 'daily_review.py', '--premarket']
    if pool:
        args += ['--pool'] + pool
    if no_cache:
        args.append('--no-cache')
    return run(args, '盘前预测基准')


def stage_close(no_cache):
    date = today()
    # 前置校验：当日盘前基准
    ok = check_exists(os.path.join(REVIEW_DIR, f'{date}盘前基准.json'),
                      f'{date}盘前基准.json（先跑 premarket）')
    if not ok:
        return False
    args = [sys.executable, 'daily_review.py', '--close']
    if no_cache:
        args.append('--no-cache')
    return run(args, '收盘复盘对照')


def stage_workbuddy(date):
    ok = check_exists(os.path.join(REVIEW_DIR, f'{date}收盘复盘报告_auto.md'),
                      f'{date}收盘复盘报告_auto.md（先跑 close）')
    if not ok:
        return False
    return run([sys.executable, 'prepare_workbuddy_input.py', date], 'Workbuddy 预填生成')


def stage_monthly(month):
    date = today()
    ok1 = run([sys.executable, 'perf_trend.py', '--record', '--date', date], '绩效趋势记录')
    ok2 = run([sys.executable, 'perf_trend.py', '--report'] + (['--source', month] if month else []),
              f'绩效趋势报表{("（" + month + "）") if month else ""}')
    return ok1 and ok2


def stage_plan(plan_file, date):
    if not plan_file:
        print('  ❌ --plan 需要指定预案文件路径')
        return False
    ok1 = run([sys.executable, 'score_expert_plan.py', '--plan', plan_file,
               '--date', date], '专家预案打分')
    ok2 = run([sys.executable, 'perf_trend.py', '--record', '--date', date,
               '--plan', plan_file], '预案绩效记录')
    return ok1 and ok2


def stage_all(date, pool, no_cache):
    results = []
    results.append(stage_premarket(pool, no_cache))
    results.append(stage_close(no_cache))
    results.append(stage_workbuddy(date))
    ok_all = all(results)
    print(f"\n{'='*60}")
    print(f"  📊 全流程{'全部成功 ✅' if ok_all else '存在失败 ❌（见上）'}")
    print(f"{'='*60}")
    return ok_all


def main():
    ap = argparse.ArgumentParser(description='一键全流程入口')
    ap.add_argument('stage', choices=['premarket', 'close', 'workbuddy', 'monthly',
                                      'plan', 'all'], help='执行阶段')
    ap.add_argument('--pool', nargs='+', default=None, help='股票池（premarket/all 用）')
    ap.add_argument('--no-cache', action='store_true', help='跳过缓存强制刷新')
    ap.add_argument('--date', default=None, help='日期 YYYY-MM-DD（默认今天）')
    ap.add_argument('--month', default=None, help='月度 YYYY-MM（monthly 用）')
    ap.add_argument('--plan', default=None, help='预案文件路径（plan 用）')
    args = ap.parse_args()

    date = args.date or today()
    ok = False
    if args.stage == 'premarket':
        ok = stage_premarket(args.pool, args.no_cache)
    elif args.stage == 'close':
        ok = stage_close(args.no_cache)
    elif args.stage == 'workbuddy':
        ok = stage_workbuddy(date)
    elif args.stage == 'monthly':
        ok = stage_monthly(args.month)
    elif args.stage == 'plan':
        ok = stage_plan(args.plan, date)
    elif args.stage == 'all':
        ok = stage_all(date, args.pool, args.no_cache)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()

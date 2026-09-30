#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
score_expert_plan.py — 专家次日预案命中率自动打分 + 绩效三件套（闭环验证）
=====================================================================
用途：把 Workbuddy 专家报告【结尾B：次日开盘操作清单】结构化成 plan 文件，
      次日收盘后用实际行情自动判定每条触发线是否触发、动作方向是否正确，
      输出命中率统计 + 绩效指标（盈亏比/最大回撤/夏普）—— 让专家模式从
      "产出报告"升级为"可度量的决策系统"。

绩效三件套（2026-08-25 借鉴 khQuant backtest_result_window 口径新增）：
  1. 盈亏比 = 盈利笔总盈亏 / 亏损笔总盈亏（赔率加权，堵住"胜率幻觉"盲区：
     胜率及格但赔率加权净值为负 = 高赔率日错误集中，实际亏损）
  2. 最大回撤 = 按预案顺序累乘收益的净值曲线 cummax 算法（khQuant 口径）
  3. 夏普 = 收益序列均值/标准差 × sqrt(年化期数)，年化期数 = 250/hold
  模拟撮合（sim_trade.SimAccount）：买入含滑点上浮+佣金+最低佣金，卖出含
  印花税+过户费+流量费，T+N 持有，整百股约束 —— 收益口径贴近真实可交易性。

用法：
  python3 score_expert_plan.py --plan plan.txt [--date 2026-08-18] [--hold 3] [--capital 100000]

plan 文件格式（每行一条预案，用 | 分隔）：
  代码|触发线条件|动作|备注
  例：
  sh600519|低开跌破1398或最低破1210|减仓/观望|示例A：资金流出减仓
  sh600036|最低破35.20|减仓|示例B：破MA20反弹结束
  sz300750|高开站上150.00|减仓机会|示例C：反抽减仓

条件文本识别关键词（大小写不敏感）：
  - 触发方向：'高开'→开盘价判定；'低开'→开盘价判定；'站上/突破/收复/冲高'→最高价≥X；
              '跌破/破'→最低价≤X；'回踩...不破'→最低价≥X
  - 价位：第一个数字（可带小数点，支持 "13.98"、">13.98"、"13.98以上"）
  - 动作方向：含 加仓/买入/低吸/持有/回补/试多 → 看多；
              含 减仓/止损/清仓/离场/止盈/回避/卖 → 看空；
              其他/含观望 → 中性（不计方向，只计触发）
判定（写死可证伪）：
  - 触发命中：触发线条件被次日行情满足
  - 方向正确：看多动作 → 次日收盘>预案日收盘（涨）；看空动作 → 次日收盘<预案日收盘（跌）
  - 命中率 = 方向正确数 / 触发数；另报 触发率 = 触发数 / 预案总数
绩效（写死可证伪）：
  - 看多触发：触发日开盘买入（含滑点/成本）→ 触发日+T 收盘卖出 → 收益%
  - 看空触发：做空收益口径 = (触发日开盘 - 触发日+T收盘)/触发日开盘 - 成本
    （仅度量"看空判断是否正确"，非实际可交易；A股无裸做空）
  - 每笔虚拟本金 = capital；盈亏比/回撤/夏普基于触发且有方向的预案

数据源：新浪日K（CN_MarketData.getKLineData，scale=240），预案日+次日+持有窗口的真实OHLC。
"""
import argparse
import json
import re
import sys
import urllib.request
import datetime
import logging

sys.path.insert(0, __file__.rsplit('/', 1)[0] if '/' in __file__ else '.')
from sim_trade import SimAccount, round_trip_cost_rate, sharpe_ratio

# 绩效计算日志（DEBUG=每笔信号，INFO=批次汇总，WARNING=兜底异常）；排查时设 level=logging.DEBUG
logger = logging.getLogger('score_expert_plan')

SINA_KLINE = ('https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
              'CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}')


def sina_code(code):
    """6位代码 → 新浪符号（sz/sh 前缀）"""
    code = ''.join(ch for ch in code if ch.isdigit())
    if code.startswith(('60', '68', '51', '58', '11')):
        return 'sh' + code
    return 'sz' + code


def fetch_kline(symbol, n=40):
    url = SINA_KLINE.format(sym=symbol, n=n)
    req = urllib.request.Request(url, headers={
        'Referer': 'https://finance.sina.com.cn',
        'User-Agent': 'Mozilla/5.0',
    })
    raw = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', 'ignore')
    return json.loads(raw)


def parse_plan_line(line):
    """解析一行预案 → (code, cond, action, note)"""
    parts = [p.strip() for p in line.split('|')]
    while len(parts) < 4:
        parts.append('')
    code, cond, action, note = parts[:4]
    return code, cond, action, note


def extract_price(cond):
    """从条件文本提取第一个数字价位；没有返回 None"""
    m = re.search(r'(\d+\.?\d*)', cond)
    return float(m.group(1)) if m else None


def cond_kind(cond):
    """判定条件类型 → ('open_high'|'open_low'|'high_ge'|'low_le'|'low_ge')"""
    c = cond
    if '回踩' in c and ('不破' in c or '守住' in c):
        return 'low_ge'
    if '高开' in c:
        return 'open_high'
    if '低开' in c:
        return 'open_low'
    if any(w in c for w in ('站上', '突破', '收复', '冲高', '站回', '重回', '高于')):
        return 'high_ge'
    if any(w in c for w in ('跌破', '破位', '击穿', '下破', '低于', '破 ')):
        return 'low_le'
    # 兜底：出现多个数字（如"13.98或12.10"）时取最严的跌破判定
    return 'low_le'


def action_bias(action):
    """动作方向 → 1(看多)/-1(看空)/0(中性)"""
    if any(w in action for w in ('加仓', '买入', '低吸', '持有', '回补', '试多', '买回', '建仓')):
        return 1
    if any(w in action for w in ('减仓', '止损', '清仓', '离场', '止盈', '回避', '卖出', '卖', '兑现', '不追')):
        return -1
    return 0


def check_trigger(kind, price, ohlc):
    """判定触发线是否被次日行情满足"""
    o, h, l = ohlc['open'], ohlc['high'], ohlc['low']
    if kind == 'open_high':
        return o >= price
    if kind == 'open_low':
        return o <= price
    if kind == 'high_ge':
        return h >= price
    if kind == 'low_le':
        return l <= price
    if kind == 'low_ge':
        return l >= price
    return False


# ── 绩效三件套（khQuant 口径，2026-08-25 新增）────────────
def compute_performance(traded_rows, capital=100000.0, hold=3):
    """基于触发且有方向的预案模拟交易，计算绩效三件套。
    输入 traded_rows: 每笔含 {code, bias, entry_price, exit_price, date}
    返回 dict: 盈亏比/最大回撤/夏普/期望值/净值曲线 等
    """
    per = {'n': 0, 'win_n': 0, 'loss_n': 0, 'total_profit': 0.0, 'total_loss': 0.0,
           'returns': [], 'equity': [], 'max_drawdown': 0.0, 'sharpe': None,
           'avg_return': 0.0, 'net_value': 0.0, 'pl_ratio': None, 'illusion': ''}
    if not traded_rows:
        return per

    # 双向成本率（做空口径对称扣减，替代硬编码 0.2%；按代码取沪市过户费）
    rt_cost_rate = round_trip_cost_rate()
    equity = 1.0
    logger.info('[compute_performance] 开始 样本=%d 资本=%d 持有T+%d 双向成本率=%.4f',
                len(traded_rows), capital, hold, rt_cost_rate)
    for t in traded_rows:
        entry, exit_ = t['entry_price'], t['exit_price']
        if entry <= 0:
            logger.warning('[compute_performance] %s 入场价<=0 跳过: entry=%s', t.get('code'), entry)
            continue
        # 用模拟账户精确计算含成本收益：看多=买入持有，看空=卖出回避（做空收益口径）
        # T+0 模式：绩效度量同一持有窗口内买卖；T+1 会拒卖导致漏算成本（原 P0 bug）
        acc = SimAccount(init_capital=capital, trade_cost={'t0_mode': True})
        acc.new_day(t['date'])
        if t['bias'] == 1:
            # 触发日开盘买入 → 窗口末收盘卖出
            r1 = acc.execute(t['code'], 'buy', entry, 1000, date_str=t['date'])
            if not r1['filled']:
                logger.warning('[compute_performance] %s 买入失败跳过: %s', t['code'], r1['reason'])
                continue
            r2 = acc.execute(t['code'], 'sell', exit_, 1000, date_str=t['date'])
            if r2['filled']:
                # 真实含成本收益：(卖出净额 - 买入总成本) / 买入总成本
                buy_total = r1['actual_price'] * 1000 + r1['trade_cost']
                sell_net = r2['actual_price'] * 1000 - r2['trade_cost']
                ret = (sell_net - buy_total) / buy_total * 100
            else:  # 卖出失败兜底（t0 模式不应发生）
                ret = (exit_ - entry) / entry * 100
                logger.warning('[compute_performance] %s 卖出失败兜底走无成本口径: %s',
                               t['code'], r2['reason'])
        else:
            # 做空收益口径：触发日开盘价 vs 窗口末收盘价，扣双向成本率（与看多口径对称）
            ret = (entry - exit_) / entry * 100 - rt_cost_rate * 100
        per['n'] += 1
        per['returns'].append(ret)
        # 全仓滚动口径：pnl 按滚动资金算，与累乘净值口径一致
        pnl = equity * ret / 100
        equity *= (1 + ret / 100)
        per['equity'].append(round(equity, 4))
        logger.debug('[compute_performance] %s bias=%s entry=%s exit=%s ret=%.2f%% pnl=%.2f equity=%.4f',
                     t['code'], t['bias'], entry, exit_, ret, pnl, equity)
        if ret > 0:
            per['win_n'] += 1
            per['total_profit'] += pnl
        else:
            per['loss_n'] += 1
            per['total_loss'] += -pnl

    if per['n'] == 0:
        logger.info('[compute_performance] 无有效样本')
        return per
    per['avg_return'] = round(sum(per['returns']) / per['n'], 2)
    per['net_value'] = round(equity, 4)

    # 1. 盈亏比（赔率加权：总盈利/总亏损）
    if per['total_loss'] > 0:
        per['pl_ratio'] = round(per['total_profit'] / per['total_loss'], 2)
        if per['win_n'] >= per['n'] / 2 and per['total_profit'] < per['total_loss']:
            per['illusion'] = ('⚠️ 胜率幻觉：胜率看似及格但赔率加权净值为负——'
                               '错误集中于高赔率日，按信号操作实际亏损，须用赔率加权记分')
    else:
        per['pl_ratio'] = None

    # 2. 最大回撤（净值曲线 cummax 算法，khQuant 口径）
    peak = per['equity'][0]
    mdd = 0.0
    for v in per['equity']:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > mdd:
            mdd = dd
    per['max_drawdown'] = round(mdd, 2)

    # 3. 夏普（年化：250/hold 期数折算，统一走 sim_trade.sharpe_ratio，减无风险利率）
    per['sharpe'] = sharpe_ratio(per['returns'], periods_per_year=max(250 // max(hold, 1), 1))
    logger.info('[compute_performance] 完成 n=%d 胜率=%.1f%% 均收益=%.2f%% 盈亏比=%s 净值=%.4f 回撤=%.2f%% 夏普=%s',
                per['n'], per['win_n'] / per['n'] * 100, per['avg_return'],
                per['pl_ratio'], per['net_value'], per['max_drawdown'], per['sharpe'])
    return per


def render_performance(per, hold):
    """渲染绩效三件套文本"""
    lines = ['\n=== 绩效三件套（模拟撮合，含成本/滑点/T+N持有）===',
             f'  有方向触发信号: {per["n"]} 笔（看多/做空收益口径）',
             f'  胜率: {per["win_n"] / per["n"] * 100:.1f}%（{per["win_n"]}盈/{per["loss_n"]}亏）' if per['n'] else '  无样本',
             f'  平均收益: {per["avg_return"]:+.2f}%' if per['n'] else '',
             f'  盈亏比(总盈利/总亏损): {per["pl_ratio"] if per["pl_ratio"] is not None else "N/A（无亏损笔）"}',
             f'  净值(累乘): {per["net_value"]}',
             f'  最大回撤: {per["max_drawdown"]:.2f}%',
             f'  夏普(年化,期数{250 // max(hold, 1)}): {per["sharpe"] if per["sharpe"] is not None else "N/A（样本<3）"}',
             f'  持有窗口: T+{hold} ｜ 虚拟本金: 10万/笔']
    if per.get('illusion'):
        lines.append(f'  {per["illusion"]}')
    if per['n'] < 5:
        lines.append('  ⚠️ 样本<5，绩效指标仅供参考，继续积累预案后再采信')
    return '\n'.join([ln for ln in lines if ln])


def main():
    ap = argparse.ArgumentParser(description='专家次日预案命中率打分 + 绩效三件套')
    ap.add_argument('--plan', required=True, help='预案文件（每行: 代码|条件|动作|备注）')
    ap.add_argument('--date', default=None,
                    help='预案生成日（YYYY-MM-DD），缺省取预案文件 mtime 当日；脚本自动找次日K线')
    ap.add_argument('--hold', type=int, default=3, help='持有窗口 T+N（默认3个交易日）')
    ap.add_argument('--capital', type=float, default=100000, help='每笔虚拟本金（默认10万）')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    args = ap.parse_args()

    with open(args.plan, encoding='utf-8') as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith('#')]

    plan_date = args.date
    if not plan_date:
        # 预案文件修改时间推断预案日
        import os
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(args.plan))
        plan_date = mt.strftime('%Y-%m-%d')

    # ── 参数快照（2026-08-25 新增：打分闭环可复现，随结果归档）──
    params = {
        'plan_file': os.path.basename(args.plan) if 'os' in dir() else args.plan,
        'plan_date': plan_date,
        'hold_days': args.hold,
        'capital_per_trade': args.capital,
        'cost_model': '佣金万3/最低5元+卖出印花税0.1%+过户费万0.1+流量费0.1/笔+滑点0.1%',
        'direction_threshold': '看多:次日收>预案日收; 看空:次日收<预案日收',
        'trade_model': '看多=触发日开盘买入T+N卖出; 看空=做空收益口径(仅度量判断)',
        'run_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    if not args.json:
        print(f'📋 预案日: {plan_date}（共 {len(lines)} 条预案）｜持有窗口 T+{args.hold}\n')
        print(f'  [参数快照] {json.dumps(params, ensure_ascii=False)}')
    # --json 模式下抑制提示文本，只输出纯 JSON（机器可读，供自动化接入）

    rows = []
    for ln in lines:
        code, cond, action, note = parse_plan_line(ln)
        symbol = sina_code(code)
        price = extract_price(cond)
        kind = cond_kind(cond)
        bias = action_bias(action)
        try:
            klines = fetch_kline(symbol, 40)  # 多取K线以支持持有窗口
        except Exception as e:
            print(f'  ⚠️ {code} 行情获取失败: {e}')
            rows.append({'code': code, 'cond': cond, 'action': action, 'note': note,
                         'triggered': None, 'correct': None, 'error': str(e)})
            continue
        # 找预案日与次日
        plan_i = None
        for i, k in enumerate(klines):
            if k['day'] == plan_date:
                plan_i = i
                break
        if plan_i is None:
            print(f'  ⚠️ {code} 预案日 {plan_date} 无K线（停牌/未上市）')
            rows.append({'code': code, 'cond': cond, 'action': action, 'note': note,
                         'triggered': None, 'correct': None, 'error': '预案日无K线'})
            continue
        prev_close = float(klines[plan_i]['close'])
        if plan_i + 1 >= len(klines):
            print(f'  ⚠️ {code} 次日K线未返回（可能未到次日收盘或停牌）')
            rows.append({'code': code, 'cond': cond, 'action': action, 'note': note,
                         'triggered': None, 'correct': None, 'error': '无次日K线'})
            continue
        nxt = klines[plan_i + 1]
        ohlc = {'open': float(nxt['open']), 'high': float(nxt['high']),
                'low': float(nxt['low']), 'close': float(nxt['close'])}
        next_date = nxt['day']
        chg = (ohlc['close'] - prev_close) / prev_close * 100

        triggered = False
        if price is not None:
            triggered = check_trigger(kind, price, ohlc)
        correct = None
        if triggered and bias != 0:
            correct = (bias == 1 and ohlc['close'] > prev_close) or \
                      (bias == -1 and ohlc['close'] < prev_close)

        # 绩效模拟：窗口末价格（触发日 + hold 个交易日）
        exit_price = None
        exit_date = None
        if triggered and bias != 0:
            exit_i = plan_i + 1 + args.hold
            if exit_i < len(klines):
                exit_price = float(klines[exit_i]['close'])
                exit_date = klines[exit_i]['day']
            else:
                exit_price = float(klines[-1]['close'])
                exit_date = klines[-1]['day']

        rows.append({
            'code': code, 'cond': cond, 'action': action, 'note': note,
            'kind': kind, 'price': price, 'bias': bias,
            'prev_close': round(prev_close, 2), 'next_date': next_date,
            'next_ohlc': f"{ohlc['open']}/{ohlc['high']}/{ohlc['low']}/{ohlc['close']}",
            'next_chg': round(chg, 2), 'triggered': triggered, 'correct': correct,
            'entry_price': ohlc['open'] if triggered and bias != 0 else None,
            'exit_price': exit_price, 'exit_date': exit_date,
        })

    # 统计
    total = len(rows)
    trig = [r for r in rows if r.get('triggered') is True]
    correct_rows = [r for r in trig if r.get('correct') is True]
    wrong_rows = [r for r in trig if r.get('correct') is False]
    neutral_trig = [r for r in trig if r.get('bias') == 0]
    trig_rate = len(trig) / total * 100 if total else 0
    hit_rate = len(correct_rows) / len(trig) * 100 if trig else 0

    # 绩效三件套（基于触发且有方向的预案）
    traded = [{'code': r['code'], 'bias': r['bias'], 'date': r['next_date'],
               'entry_price': r['entry_price'], 'exit_price': r['exit_price']}
              for r in trig if r.get('bias') != 0 and r.get('entry_price') and r.get('exit_price')]
    per = compute_performance(traded, capital=args.capital, hold=args.hold)

    if args.json:
        print(json.dumps({'params': params, 'total': total, 'triggered': len(trig),
                          'trigger_rate': round(trig_rate, 1),
                          'correct': len(correct_rows), 'wrong': len(wrong_rows),
                          'neutral_triggered': len(neutral_trig), 'hit_rate': round(hit_rate, 1),
                          'performance': per, 'rows': rows}, ensure_ascii=False, indent=2))
        return

    print('=== 逐条判定 ===')
    for r in rows:
        if r.get('error'):
            print(f"  {r['code']} {r['cond']}｜{r['action']}｜⚠️ {r['error']}")
            continue
        tag = '未触发'
        if r['triggered']:
            tag = '✅ 触发'
        if r['triggered'] and r['correct'] is True:
            tag = '✅ 触发·方向正确'
        elif r['triggered'] and r['correct'] is False:
            tag = '❌ 触发·方向错误'
        elif r['triggered'] and r['bias'] == 0:
            tag = '🟡 触发·中性(不计方向)'
        extra = ''
        if r.get('entry_price') and r.get('exit_price'):
            extra = f"｜模拟: 入{r['entry_price']}→出{r['exit_price']}@{r['exit_date']}"
        print(f"  {r['code']} {r['cond']} → {r['action']}｜{tag}"
              f"｜次日({r['next_date']}) {r['next_ohlc']} {r['next_chg']:+.2f}%"
              + extra + (f"｜{r['note']}" if r.get('note') else ''))

    print('\n=== 命中率统计 ===')
    print(f'  预案总数: {total}')
    print(f'  触发数: {len(trig)}（触发率 {trig_rate:.1f}%）')
    print(f'  方向正确: {len(correct_rows)}｜方向错误: {len(wrong_rows)}｜中性触发: {len(neutral_trig)}')
    print(f'  ★ 命中率(方向正确/触发数): {hit_rate:.1f}%')

    print(render_performance(per, args.hold))
    print()


if __name__ == '__main__':
    main()

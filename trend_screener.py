#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trend_screener.py — 股票趋势筛选工作台（四层漏斗，不推荐个股，只分类+展示触发原因）
======================================================================================
设计（2026-08-15，与用户确认的 spec v1）：
  L1 大盘环境      → 强 / 正常 / 偏弱（涨停家数 + 涨跌比 + 两市量能倍数）
  L2 板块强弱      → 强势板块（进L3）/ 观察 / 排除
                     （板块20日/5日涨幅 + 量比 + 上涨家数占比 + 当日走强数 + 主力资金）
  L3 强板块内筛个股 → 趋势(MA20/MA60) + 5/10/20日涨幅 + 量比 + 价格位置 + RS
  L4 五类分类      → 启动观察 / 趋势观察 / 高位观察 / 回调观察 / 排除
                     + ATR 涨幅校验（当日/5日涨幅 vs 正常波动，附加标注不改分类）

用法：
  python3 trend_screener.py [--top N] [--min-amount 3] [--save]
    --top N         进 L3 的强势板块数量（默认 5，最多 10）
    --min-amount X  成分股成交额过滤（亿元，默认 3；--min-amount 0 关闭）
    --save          同时落盘 md 到 复盘/ 目录（默认只打印终端）
  python3 trend_screener.py --evaluate   # 仅校验数据源可用性（调试）

输出原则：只做状态分类 + 触发原因，不给出买卖建议。
"""
import sys
import os
import json
import time
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_quant as sq

# ── 阈值常量（写死可证伪，可调） ─────────────────────────────
L1_STRONG_ZT = 80        # 强：涨停家数 ≥ 80
L1_STRONG_RATIO = 1.5    # 强：涨跌比 ≥ 1.5
L1_STRONG_VOL = 1.2      # 强：量能倍数 ≥ 1.2
L1_WEAK_ZT = 30          # 偏弱：涨停 ≤ 30
L1_WEAK_RATIO = 0.7      # 偏弱：涨跌比 ≤ 0.7
L1_WEAK_VOL = 0.8        # 偏弱：量能倍数 ≤ 0.8

L2_MIN_20D = 0.0         # 强势：板块20日涨幅 > 0
L2_MIN_5D = 0.0          # 强势：5日涨幅 > 0
L2_VOL_RATIO = 1.5       # 强势：量比 ≥ 1.5
L2_UP_RATIO = 0.60       # 强势：上涨家数占比 ≥ 60%
L2_HOT_NUM = 3           # 强势：当日走强数（涨幅≥5%）≥ 3
L2_EXCLUDE_20D = -5.0    # 排除：20日跌幅 < -5%
TOP_SECTORS = 5          # 进 L3 的强势板块数（--top 可调）

L4_MA20_HIGH = 25.0      # 高位：距MA20 > 25%
L4_HIGH_20D = 40.0       # 高位：20日涨幅 > 40%
L4_HIGH_NEAR_HIGH = 3.0  # 高位：距20日高点 < 3%
L4_START_20D = 15.0      # 启动：20日涨幅 ≤ 15%
L4_START_5D = 3.0        # 启动：5日涨幅 > 3%
L4_START_VOL = 1.3       # 启动：量比 ≥ 1.3
L4_START_LOW20 = 15.0    # 启动：距20日低点 < 15%（首次突破约束，防已涨一段的误当启动）
L4_TREND_5D = 0.0        # 趋势：5日涨幅 > 0
L4_TREND_MA20 = 20.0     # 趋势：距MA20 < 20%
L4_PULLBACK_MA20 = 8.0   # 回调：距MA20 < 8%（允许略跌破）

ATR_DAY_MULT = 2.0       # ATR校验：当日涨幅 > 2×ATR%
ATR_5D_MULT = 5.0        # ATR校验：5日涨幅 > 5×ATR%

REVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '复盘')


# ════════════════════════════════════════════════════════════
# L1 大盘环境
# ════════════════════════════════════════════════════════════
def analyze_l1():
    """大盘环境：强/正常/偏弱 + 情绪温度计。返回 dict + 证据数值。不阻断后续层。"""
    r = {'level': '', 'evidence': {}, 'note': '', 'sentiment': None}
    bd = sq.fetch_market_breadth()
    vt = sq.analyze_market_volume()
    # 情绪温度计（复用已拉的 breadth，不重复请求）
    sent = sq.analyze_market_sentiment(bd)
    if sent and sent.get('available'):
        r['sentiment'] = {'score': sent.get('score'), 'temp': sent.get('temp'),
                          'detail': sent.get('detail')}
    bd_ok = bd and bd.get('available')
    # 量能可用性（analyze_market_volume 仅在算出量比时 available=True）：
    # 不可用时 vol=0 不是"缩量"证据，不得参与强弱判定（2026-09-13 审查修复：
    # 双源限流时 vol=0 < L1_WEAK_VOL 被误判"偏弱"）
    vol_ok = bool(vt and vt.get('available'))
    zt = bd.get('zt_count', 0) if bd else 0
    # 市场宽度不可用时 ratio 无意义：置 None，输出层显示 N/A 而非裸显 0
    ratio = bd.get('ratio', 0) if bd and bd_ok else None
    vol = vt.get('vol_ratio', 0) if vt else 0
    amount = vt.get('today_amount', 0) if vt else 0
    r['evidence'] = {'zt': zt, 'ratio': ratio, 'vol': vol, 'amount': amount,
                     'up': bd.get('up_count', 0) if bd else 0,
                     'down': bd.get('down_count', 0) if bd else 0}
    if not bd_ok and not vol_ok:
        # 双源不可用：无有效证据，不降级为"偏弱"（避免限流误报系统性风险）
        r['level'] = '无法判定'
        r['note'] = '市场宽度与量能数据均不可用（可能盘前/限流），环境级别无法判定'
        return r
    if not bd_ok:
        # 市场宽度不可用：不把 ratio=0 当作"偏弱"证据，降级为仅按量能判定
        r['level'] = '正常' if vol >= L1_WEAK_VOL else '偏弱'
        r['note'] = '市场宽度数据不可用（可能盘前/限流），环境级别按量能降级判定'
        return r
    strong = zt >= L1_STRONG_ZT and ratio >= L1_STRONG_RATIO and vol_ok and vol >= L1_STRONG_VOL
    weak = zt <= L1_WEAK_ZT or ratio <= L1_WEAK_RATIO or (vol_ok and vol <= L1_WEAK_VOL)
    if strong:
        r['level'] = '强'
    elif weak:
        r['level'] = '偏弱'
    else:
        r['level'] = '正常'
    if not vol_ok:
        r['note'] = '量能数据不可用（限流），强弱判定仅按涨停/涨跌比'
    return r


# ════════════════════════════════════════════════════════════
# L2 板块强弱
# ════════════════════════════════════════════════════════════
def board_list_full():
    """全部行业板块：名称→{bk,f3当日涨跌,f62主力净额亿}（东财 clist m:90+t:2）
    走 push2 实时镜像（92.push2 实测比 push2his 稳定，2026-08-15）
    2026-08-28：收敛到 sq._em_probe_all 统一轮询（冷却标记与库内入口一致）"""
    params = ("pn=1&pz=500&po=1&np=1&fltt=2&invt=2"
              "&fields=f2,f3,f6,f12,f14,f62"
              "&fs=m:90+t:2")
    try:
        data = sq._em_probe_all(
            lambda s, h: f"{s}://{h}/api/qt/clist/get?{params}",
            sq._EM_PUSH2_HOSTS, timeout=8,
            valid=lambda d: bool(d.get('data') and d['data'].get('diff')))
        if data:
            out = {}
            for it in data['data']['diff'] or []:
                bk = it.get('f12', '')
                name = it.get('f14', '')
                if bk and name:
                    out[name] = {
                        'bk': bk,
                        'pct': float(it.get('f3', 0) or 0),
                        'fund': round(float(it.get('f62', 0) or 0) / 1e8, 2),
                    }
            return out
    except Exception as e:
        sq.log.warning(f'板块列表失败: {e}')
    return {}


def _safe_float(v, default=0.0):
    """东财接口停牌/无数据时返回 '-'，安全转换"""
    try:
        if v is None or v == '-':
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def sector_members_full(bk):
    """板块成分股（fs=b:BKxxxx）：[{code,name,pct,amount}]，code 为纯6位
    走 push2 实时镜像（2026-08-15 实测：92.push2 可用，push2his 被限流）
    2026-08-28：收敛到 sq._em_probe_all 统一轮询"""
    params = ("pn=1&pz=1000&po=1&np=1&fltt=2&invt=2"
              "&fields=f2,f3,f6,f12,f14&fs=b:{}").format(bk)
    try:
        data = sq._em_probe_all(
            lambda s, h: f"{s}://{h}/api/qt/clist/get?{params}",
            sq._EM_PUSH2_HOSTS, timeout=10,
            valid=lambda d: bool(d.get('data') and d['data'].get('diff')))
        if data:
            out = []
            for it in data['data']['diff'] or []:
                code = it.get('f12', '')
                if code:
                    out.append({
                        'code': code,
                        'name': it.get('f14', ''),
                        'pct': _safe_float(it.get('f3')),
                        'amount': _safe_float(it.get('f6')),
                    })
            return out
    except Exception as e:
        sq.log.warning(f'成分股失败({bk}): {e}')
    return []


def _ret_pct(closes, n):
    """N日区间涨跌幅（含当日）"""
    if len(closes) < n + 1 or not closes[-n - 1]:
        return None
    return (closes[-1] / closes[-n - 1] - 1) * 100


def _sector_metric_from_members(m, members, sample=10):
    """降级路径：push2his 板块K线限流时，用成分股聚合重建板块指标
    取成交额前 sample 只代表股（新浪个股K线，可用，并行拉取）：
      r5/r20 = 成分股区间涨幅中位数；vol_ratio = 成分股量比中位数；
      above_ma20 = 多数成分股站上MA20（>50%）
    返回 m（填充 r5/r20/vol_ratio/above_ma20/ma20 等，close 用成交额加权价）"""
    cands = sorted([x for x in members if x['amount'] > 0],
                   key=lambda x: x['amount'], reverse=True)[:sample]
    if not cands:
        return m
    codes = [(_full_code(x['code']), x) for x in cands]
    kl_map = sq.fetch_kline_batch([c for c, _ in codes], 240, 60)  # 并行10线程
    r5s, r20s, vrs, ma20_above, prices = [], [], [], [], []
    for full, x in codes:
        k = kl_map.get(full) or []
        if not k or len(k) < 25:
            continue
        closes = [float(v['close']) for v in k]
        vols = [float(v.get('volume', 0) or 0) for v in k]
        price = closes[-1]
        r5 = _ret_pct(closes, 5)
        r20 = _ret_pct(closes, 20)
        ma20 = sq.calc_ma(closes, 20)
        avg_vol20 = sum(vols[-25:-5]) / 20 if len(vols) >= 25 and sum(vols[-25:-5]) > 0 else 0
        vr = round(vols[-1] / avg_vol20, 2) if avg_vol20 > 0 else 1.0
        if r5 is not None:
            r5s.append(r5)
        if r20 is not None:
            r20s.append(r20)
        if vr:
            vrs.append(vr)
        ma20_above.append(1 if (ma20 and price > ma20) else 0)
        prices.append(price)
    if not r20s or not prices:
        return m
    r5s.sort(); r20s.sort(); vrs.sort()
    m['r5'] = round(r5s[len(r5s) // 2], 2)
    m['r20'] = round(r20s[len(r20s) // 2], 2)
    m['vol_ratio'] = round(vrs[len(vrs) // 2], 2) if vrs else 1.0
    m['above_ma20'] = (sum(ma20_above) / len(ma20_above)) > 0.5
    m['ma20'] = None  # 聚合模式无板块MA20，above_ma20 由成分股多数决定
    m['ma60'] = None
    m['close'] = round(sum(prices) / len(prices), 2)
    m['fallback'] = '成分股聚合（板块K线限流降级）'
    return m


def sector_metric(bk, name, pct=None):
    """板块量化指标：5/20日涨幅、量比、MA20/MA60、上涨占比、当日走强数
    降级(2026-08-15)：push2his 板块K线限流时，用成分股聚合重建（新浪个股K线可用）
    pct=板块当日涨跌幅（可选）：降级模式下当日未涨板块跳过聚合（减少请求量）"""
    m = {'name': name, 'bk': bk, 'available': False}
    kb = sq.fetch_sector_kline(bk, 60)
    if kb and len(kb) >= 25:
        closes = [float(d['close']) for d in kb]
        vols = [float(d.get('volume', 0) or 0) for d in kb]
        m['close'] = closes[-1]
        m['ma20'] = sq.calc_ma(closes, 20)
        m['ma60'] = sq.calc_ma(closes, 60)
        r5, r20 = _ret_pct(closes, 5), _ret_pct(closes, 20)
        m['r5'] = round(r5, 2) if r5 is not None else None
        m['r20'] = round(r20, 2) if r20 is not None else None
        avg_vol20 = sum(vols[-25:-5]) / 20 if len(vols) >= 25 and sum(vols[-25:-5]) > 0 else 0
        m['vol_ratio'] = round(vols[-1] / avg_vol20, 2) if avg_vol20 > 0 else 1.0
        m['above_ma20'] = m['close'] > (m['ma20'] or 9e9)
        m['above_ma60'] = m['close'] > (m['ma60'] or 9e9)
        # 板块趋势通道（第5强条件）：斜率>0 且 价格在中轨上方 = 持续强势
        tc = sq.analyze_trend_channel(kb, m['close'])
        m['tc_slope'] = tc.get('slope') if tc.get('available') else None
        m['tc_pos_pct'] = tc.get('pos_pct') if tc.get('available') else None
        m['tc_position'] = tc.get('position') if tc.get('available') else ''
        m['fallback'] = ''
    else:
        # 降级：板块K线不可用 → 成分股聚合重建（当日未涨的板块跳过，减少请求量）
        if pct is not None and pct <= 0:
            m['fallback'] = '板块K线限流+当日未涨（跳过聚合）'
            return m
        members = sector_members_full(bk)
        if not members:
            return m
        m = _sector_metric_from_members(m, members)
        if not m.get('r20'):
            return m
    # 成分股：上涨家数占比 + 当日走强数（涨幅≥5%）
    members = sector_members_full(bk)
    if members:
        up = sum(1 for x in members if x['pct'] > 0)
        hot = sum(1 for x in members if x['pct'] >= 5.0)
        m['up_ratio'] = round(up / len(members), 2)
        m['hot_num'] = hot
        m['member_cnt'] = len(members)
    else:
        m['up_ratio'] = None
        m['hot_num'] = 0
        m['member_cnt'] = 0
    m['available'] = True
    return m


def classify_sector(m):
    """板块分级：强势（进L3）/ 观察 / 排除。返回 (级别, 原因列表)"""
    if not m.get('available'):
        return ('不可用', ['板块K线或成分股数据不可用'])
    reasons = []
    cond_ret = (m.get('r20', -99) is not None and m.get('r20', -99) > L2_MIN_20D
                and m.get('r5', -99) is not None and m.get('r5', -99) > L2_MIN_5D
                and m.get('above_ma20'))
    cond_vol = (m.get('vol_ratio', 0) or 0) >= L2_VOL_RATIO
    cond_up = (m.get('up_ratio') or 0) >= L2_UP_RATIO
    cond_hot = (m.get('hot_num', 0) or 0) >= L2_HOT_NUM
    # 第5强条件：板块趋势通道 斜率>0 且 价格在中轨上方（pos_pct>50）= 持续强势
    cond_tc = (m.get('tc_slope') is not None and m['tc_slope'] > 0
               and (m.get('tc_pos_pct') or 0) > 50)
    # 排除：20日跌幅< -5% 且 主力净流出
    if (m.get('r20') is not None and m['r20'] < L2_EXCLUDE_20D):
        return ('排除', [f'20日涨幅{m["r20"]:.1f}%<{L2_EXCLUDE_20D}%'])
    if cond_ret:
        reasons.append(f'20日{m.get("r20")}%/5日{m.get("r5")}%且站上MA20')
        strong_extra = sum([cond_vol, cond_up, cond_hot, cond_tc])
        if strong_extra >= 2:
            if cond_vol:
                reasons.append(f'量比{m.get("vol_ratio")}≥{L2_VOL_RATIO}')
            if cond_up:
                reasons.append(f'上涨家数占比{m.get("up_ratio")*100:.0f}%≥{L2_UP_RATIO*100:.0f}%')
            if cond_hot:
                reasons.append(f'当日走强{m.get("hot_num")}只≥{L2_HOT_NUM}')
            if cond_tc:
                reasons.append(f'趋势通道向上(斜率{m.get("tc_slope")})且站上中轨')
            return ('强势', reasons)
        reasons.append('辅助条件不足2项（量比/上涨占比/走强数/趋势通道）')
        return ('观察', reasons)
    reasons.append(f'20日{m.get("r20")}%/5日{m.get("r5")}%未同时为正或未站上MA20')
    return ('观察', reasons)


# ════════════════════════════════════════════════════════════
# L3 个股量化
# ════════════════════════════════════════════════════════════
def _qianshi_signals(kline):
    """轻量主升擒龙信号（日线版，不拉分钟线/新闻）
    输入：与 fetch_kline_sina 结构对齐的 K 线列表
    输出：{duo_kong, qiang_shi, qiang_jin_qiang, abc3, hg, note}
      duo_kong      多空线方向（做多/做空/未知）
      qiang_shi     强势信号：HG>5 且 MACD>0 且 A1>B1 且 C>=EMA(C,5)
      qiang_jin_qiang ★强擒强：强势且 A1 刚上穿 B1（趋势由空转多）
      abc3          主力强度（价格偏离量价动态均线）
      hg            均价偏离度
    """
    res = {'duo_kong': '未知', 'qiang_shi': False, 'qiang_jin_qiang': False,
           'abc3': None, 'hg': None, 'note': ''}
    if not kline or len(kline) < 26:
        res['note'] = 'K线不足26根'
        return res
    closes = [float(d['close']) for d in kline]
    highs = [float(d['high']) for d in kline]
    lows = [float(d['low']) for d in kline]
    opens = [float(d['open']) for d in kline]
    volumes = [float(d.get('volume', 0) or 0) for d in kline]

    # 多空线 A1/B1：A1 = EMA(C,7)-EMA(C,21)；B1 平滑 A1
    ema7 = sq.calc_ema(closes, 7)
    ema21 = sq.calc_ema(closes, 21)
    a1 = b1 = None
    if ema7 and ema21 and len(ema7) == len(ema21) and len(ema7) >= 3:
        a1_series = [ema7[i] - ema21[i] for i in range(len(ema7))]
        a1 = a1_series[-1]
        b1 = 0.668 * a1_series[-2] + 0.333 * a1_series[-1] if len(a1_series) >= 2 else a1
        prev_a1 = a1_series[-2] if len(a1_series) >= 2 else a1
        prev_b1 = 0.668 * a1_series[-3] + 0.333 * a1_series[-2] if len(a1_series) >= 3 else prev_a1
        res['_a1'] = a1
        res['_b1'] = b1
        res['_prev_a1'] = prev_a1
        res['_prev_b1'] = prev_b1
        res['duo_kong'] = '做多' if a1 >= b1 else '做空'

    # ABC3 主力强度：ABC1=V/SUM(V,13) → ABC2=DMA(C,ABC1) → ABC3=(C-ABC2)/ABC2*40
    if len(volumes) >= 13 and len(closes) >= 13:
        vol_sum_13 = sum(volumes[-13:])
        if vol_sum_13 > 0:
            abc1_series = [v / vol_sum_13 for v in volumes[-13:]]
            abc2 = sq.calc_dma(closes[-13:], abc1_series)
            if abc2 and abc2 > 0:
                res['abc3'] = round((closes[-1] - abc2) / abc2 * 40, 2)

    # HG 均价偏离度：L2=MA(AMOUNT/(100*V),13)；HG=(C-L2)/L2*100
    if len(volumes) >= 13 and len(closes) >= 13:
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
            res['hg'] = round((closes[-1] - l2) / l2 * 100, 2)

    # 强势信号：HG>5 且 MACD>0 且 A1>B1 且 C>=EMA(C,5)
    hg = res.get('hg') or 0
    dif, dea, macd = sq.calc_macd(closes)
    ema5 = sq.calc_ma(closes, 5)
    price = closes[-1]
    if (hg > 5 and macd > 0 and a1 is not None and b1 is not None
            and a1 > b1 and ema5 and price >= ema5):
        res['qiang_shi'] = True
        # ★强擒强：强势且 A1 刚上穿 B1（前一日空头）
        if res['_prev_a1'] <= res['_prev_b1']:
            res['qiang_jin_qiang'] = True
    return res


def _full_code(code):
    """纯6位 → 带前缀（sh/sz；688/689 属沪）"""
    if len(code) == 6 and code.isdigit():
        return ('sh' if code.startswith(('6', '9')) else 'sz') + code
    return code


def analyze_stock(code, name, min_amount=3.0):
    """单只个股全量指标：趋势/5-10-20日涨幅/量比/价格位置/ATR/RS
    + 趋势通道（tc_*）+ 轻量主升擒龙（qs_*）"""
    d = {'code': code, 'name': name, 'available': False}
    full = _full_code(code)
    # 日线优先东财 push2his（新浪批量限流，2026-08-25 修复：screener 扫 1000+ 股时新浪反复失败）
    k = sq.fetch_kline_push2his(full, 60, fq=1) or sq.fetch_kline_sina(full, 240, 60)
    if not k or len(k) < 25:
        return d
    closes = [float(x['close']) for x in k]
    highs = [float(x['high']) for x in k]
    lows = [float(x['low']) for x in k]
    vols = [float(x.get('volume', 0) or 0) for x in k]
    opens = [float(x['open']) for x in k]
    price = closes[-1]
    d['price'] = round(price, 2)
    d['ma20'] = sq.calc_ma(closes, 20)
    d['ma60'] = sq.calc_ma(closes, 60)
    d['r_today'] = round((price / closes[-2] - 1) * 100, 2) if len(closes) >= 2 and closes[-2] else None
    d['r5'] = _ret_pct(closes, 5)
    d['r10'] = _ret_pct(closes, 10)
    d['r20'] = _ret_pct(closes, 20)
    d['above_ma20'] = d['ma20'] is not None and price > d['ma20']
    d['above_ma60'] = d['ma60'] is not None and price > d['ma60']
    d['dist_ma20'] = round((price / d['ma20'] - 1) * 100, 2) if d['ma20'] else None
    hi20 = max(highs[-20:]); lo20 = min(lows[-20:])
    d['dist_high20'] = round((price / hi20 - 1) * 100, 2) if hi20 else None
    d['dist_low20'] = round((price / lo20 - 1) * 100, 2) if lo20 else None
    avg_vol20 = sum(vols[-25:-5]) / 20 if len(vols) >= 25 and sum(vols[-25:-5]) > 0 else 0
    d['vol_ratio'] = round(vols[-1] / avg_vol20, 2) if avg_vol20 > 0 else 1.0
    d['atr'] = sq.calc_atr(k, 14)
    d['atr_pct'] = round(d['atr'] / price * 100, 2) if d['atr'] and price else None
    # 趋势通道（线性回归通道：斜率/位置/宽度）
    tc = sq.analyze_trend_channel(k, price)
    if tc.get('available'):
        d['tc_slope'] = tc.get('slope')
        d['tc_pos_pct'] = tc.get('pos_pct')
        d['tc_position'] = tc.get('position')
        d['tc_width_pct'] = tc.get('width_pct')
        d['tc_signal'] = tc.get('signal')
    # 轻量主升擒龙（日线版）
    qs = _qianshi_signals(k)
    d['qs_duo'] = qs.get('duo_kong', '未知')
    d['qs_qiang'] = qs.get('qiang_shi', False)
    d['qs_qjq'] = qs.get('qiang_jin_qiang', False)
    d['qs_abc3'] = qs.get('abc3')
    d['qs_hg'] = qs.get('hg')
    # 筹码分布（纯本地计算）：获利盘/成本峰/集中度/底部锁定
    chip = sq.analyze_chip_distribution(k, price)
    if chip.get('available'):
        d['chip_winner'] = chip.get('winner_pct')
        d['chip_conc'] = chip.get('concentration')
        d['chip_lock'] = chip.get('bottom_lock')
        d['chip_note'] = chip.get('chip_note', '')
    # K线形态识别（纯本地计算）：锤子线/红三兵/早晨之星等
    try:
        pats = sq.recognize_candlestick_patterns(closes, highs, lows, opens, vols)
        if pats:
            d['kline_patterns'] = pats
            d['kline_pattern_tag'] = ' '.join(p[0] for p in pats[:3])
    except Exception:
        pass
    # 板块筑底/反弹状态（analyze_sector_trend：所属板块 走强/反弹/弱势 + 确认信号）
    try:
        st = sq.analyze_sector_trend(full)
        if st and st.get('available'):
            d['sector_trend_state'] = st.get('state', '')
            d['sector_trend_signal'] = st.get('signal', '')
    except Exception:
        pass
    # 波段结构与浪数（analyze_wave_pattern：第几浪/上涨中/回调中/缩量回调到位）
    try:
        wv = sq.analyze_wave_pattern(full)
        if wv and wv.get('available'):
            d['wave_count'] = wv.get('wave_count')
            d['wave_current'] = wv.get('current_wave')
            d['wave_state'] = wv.get('state', '')
            d['wave_signal'] = wv.get('signal', '')
    except Exception:
        pass
    d['available'] = True
    return d


def rs_of(code):
    """个股 RS（5/20日超额收益 vs 参考指数），复用 stock_quant 结果"""
    try:
        rs = sq.analyze_relative_strength(_full_code(code))
        return {'rs5': rs.get('rs_5'), 'rs20': rs.get('rs_20'), 'ref': rs.get('ref_index', '')}
    except Exception:
        return {'rs5': None, 'rs20': None, 'ref': ''}


# ════════════════════════════════════════════════════════════
# L4 五类分类 + ATR 校验
# ════════════════════════════════════════════════════════════
def classify_stock(d):
    """五类分类（优先级：高位 > 启动 > 趋势 > 回调 > 排除），返回 (类别, 触发原因列表)
    确认开关（标注不强制）：主升擒龙/趋势通道信号追加为标注，不改变分类判定"""
    reasons = []
    r5, r10, r20 = d.get('r5'), d.get('r10'), d.get('r20')
    ma20 = d.get('ma20')
    dist = d.get('dist_ma20')
    if r20 is None or ma20 is None or dist is None:
        return ('排除', ['K线数据不足（<25根）'])
    # 高位观察
    if r20 > L4_HIGH_20D:
        reasons.append(f'20日涨幅{r20:.1f}%>{L4_HIGH_20D}%')
    if d.get('dist_high20') is not None and abs(d['dist_high20']) < L4_HIGH_NEAR_HIGH:
        reasons.append(f'距20日高点仅{d["dist_high20"]:+.1f}%<{L4_HIGH_NEAR_HIGH}%')
    if dist > L4_MA20_HIGH:
        reasons.append(f'距MA20达{dist:+.1f}%>{L4_MA20_HIGH}%')
    if reasons:
        # 确认开关：通道上轨附近/超买
        if d.get('tc_pos_pct') is not None and d['tc_pos_pct'] > 80:
            reasons.append(f'趋势通道上轨附近(pos{d["tc_pos_pct"]:.0f})')
        return ('高位观察', reasons)
    # 启动观察（含首次突破约束：距20日低点不能太远，防止已涨一段的误当启动）
    dist_low = d.get('dist_low20')
    if r20 <= L4_START_20D and r5 is not None and r5 > L4_START_5D and d.get('above_ma20') \
            and (d.get('vol_ratio') or 0) >= L4_START_VOL \
            and dist_low is not None and dist_low < L4_START_LOW20:
        rs = [
            f'20日涨幅{r20:.1f}%≤{L4_START_20D}%',
            f'5日涨幅{r5:+.1f}%>{L4_START_5D}%',
            '站上MA20',
            f'量比{d.get("vol_ratio")}≥{L4_START_VOL}',
            f'距20日低点{dist_low:+.1f}%<{L4_START_LOW20}%（首次突破）',
        ]
        # 确认开关：强擒强（A1刚上穿B1=趋势刚由空转多）→ 高置信启动
        if d.get('qs_qjq'):
            rs.append('⭐强擒强（主升擒龙A1刚上穿B1，趋势由空转多）')
        elif d.get('qs_qiang'):
            rs.append('✅主升擒龙强势信号(HG>5且MACD>0)')
        return ('启动观察', rs)
    # 趋势观察
    if L4_START_20D < r20 <= L4_HIGH_20D and r5 is not None and r5 > L4_TREND_5D \
            and d.get('above_ma20') and abs(dist) < L4_TREND_MA20:
        rs = [
            f'20日涨幅{r20:.1f}%∈({L4_START_20D},{L4_HIGH_20D}]',
            f'5日涨幅{r5:+.1f}%>0',
            f'站上MA20且距MA20{dist:+.1f}%<{L4_TREND_MA20}%',
        ]
        # 确认开关：强势信号 + 通道斜率向上
        if d.get('qs_qiang'):
            rs.append('✅主升擒龙强势信号(HG>5且MACD>0)')
        if d.get('tc_slope') is not None and d['tc_slope'] > 0:
            rs.append(f'↗️趋势通道向上(斜率{d["tc_slope"]:.4f})')
        return ('趋势观察', rs)
    # 回调观察
    if r5 is not None and r5 < 0 and r20 > 0 and (d.get('above_ma20') or abs(dist) < L4_PULLBACK_MA20):
        rs = [
            f'5日涨幅{r5:+.1f}%<0（回调）',
            f'20日涨幅{r20:.1f}%>0（中期仍强）',
            f'距MA20{dist:+.1f}%（仍在上方或接近）',
        ]
        # 确认开关：多空线仍做多（回调未破多空线）
        if d.get('qs_duo') == '做多':
            rs.append('✅主升擒龙多空线仍做多（回调未破A1/B1）')
        return ('回调观察', rs)
    # 排除
    if r20 <= 0 and not d.get('above_ma20'):
        rs = [f'20日涨幅{r20:.1f}%≤0且未站上MA20']
        if d.get('qs_duo') == '做空':
            rs.append('❌主升擒龙多空线做空')
        if d.get('tc_slope') is not None and d['tc_slope'] < 0:
            rs.append('↘️趋势通道向下')
        return ('排除', rs)
    if dist < -10:
        rs = [f'距MA20达{dist:.1f}%<-10%（深跌）']
        if d.get('qs_duo') == '做空':
            rs.append('❌主升擒龙多空线做空')
        return ('排除', rs)
    return ('排除', ['未命中任何观察类别（趋势中性/横盘）'])


def atr_check(d):
    """ATR 涨幅校验：当日/5日涨幅 vs 正常波动。返回标注文本或 None"""
    if not d.get('atr_pct') or d.get('r5') is None:
        return None
    atr = d['atr_pct']
    r5 = abs(d['r5'])
    today = d.get('r_today')
    notes = []
    if today is not None and abs(today) > ATR_DAY_MULT * atr:
        notes.append(f'当日{abs(today):.1f}%>2×ATR({atr:.1f}%)')
    if r5 > ATR_5D_MULT * atr:
        notes.append(f'5日{r5:.1f}%>5×ATR({atr:.1f}%)')
    if notes:
        return '⚡短期涨幅超正常波动（ATR校验）: ' + '；'.join(notes)
    return None


# ════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════
def run(top=TOP_SECTORS, min_amount=3.0):
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    lines = []

    def P(s=''):
        print(s)
        lines.append(s)

    # 数据日期标注：以新浪上证日K最新交易日为准（非交易日/盘前运行时数据停留在上一个交易日）
    data_date = today
    try:
        ref_k = sq.fetch_kline_sina('sh000001', 240, 5)
        if ref_k:
            dd = str(ref_k[-1].get('day', ''))
            if dd:
                data_date = dd
    except Exception:
        pass
    date_note = f'（数据日期 {data_date}）' if data_date != today else ''
    P(f'📊 趋势筛选工作台 | {today}{date_note}')
    P(f'{"─" * 64}')

    # ── L1 大盘环境 ──
    l1 = analyze_l1()
    ev = l1['evidence']
    ratio_txt = f'{ev["ratio"]}' if ev['ratio'] is not None else 'N/A'
    sent_txt = ''
    if l1.get('sentiment'):
        sent_txt = f" 情绪温度:{l1['sentiment']['temp']}({l1['sentiment']['score']}分)"
    lv = l1['level']
    lv_txt = {'强': '🟢 强', '偏弱': '🔴 偏弱', '无法判定': '⚪ 无法判定'}.get(lv, '🟡 正常')
    P(f'L1 大盘环境: {lv_txt} '
      f'(涨停{ev["zt"]}/涨跌比{ratio_txt}/量能{ev["vol"]}x/成交{ev["amount"]:.0f}亿){sent_txt}')
    if l1['note']:
        P(f'   ⚠️ {l1["note"]}')
    if l1['level'] == '偏弱':
        P('   ⚠️ 环境偏弱：下方筛选结果仅供观察，注意仓位与风险')

    # ── L2 板块强弱 ──
    P(f'\nL2 板块强弱扫描…')
    # 板块主线/资金配合（analyze_sector_rotation：领涨板块 + 主力资金验证）
    rot = sq.analyze_sector_rotation()
    if rot:
        if rot.get('main_line'):
            P(f'   {rot["main_line"]}')
        if rot.get('rotation_note'):
            P(f'   {rot["rotation_note"]}')
        fund_in = rot.get('fund_in_top') or []
        if fund_in:
            names = '、'.join(f"{s.get('name','')}({s.get('main_net',0):+.1f}亿)" for s in fund_in[:3])
            P(f'   资金流入TOP: {names}')
    boards = board_list_full()
    if not boards:
        P('   ❌ 板块列表不可用（东财限流或冷却），本次跳过 L2/L3')
        return lines
    sector_rows = []
    # 预过滤：当日跌幅>3% 的板块基本不可能满足"5日>0 且 量比/上涨占比"强条件，
    # 跳过K线拉取直接判观察，减少请求量、降低东财限流风险
    skipped = 0
    for name, info in boards.items():
        if info.get('pct', 0) <= -3.0:
            skipped += 1
            continue
        m = sector_metric(info['bk'], name, pct=info.get('pct'))
        m['fund'] = info.get('fund', 0)
        lv, rs = classify_sector(m)
        if lv in ('强势', '观察'):
            sector_rows.append({'m': m, 'lv': lv, 'reasons': rs, 'fund': info.get('fund', 0)})
        time.sleep(sq._jitter_delay(0.12))  # 板块间请求间隔，防限流
    # 排序：强势优先；强势板块内部按「涨幅×资金」综合分（避免只看涨幅、忽略主力配合）
    for r in sector_rows:
        r20 = r['m'].get('r20') or -99
        fund = r.get('fund', 0)
        # 综合分：20日涨幅(正贡献) + 主力净流入(正贡献)，涨幅为主、资金为辅
        ret_score = max(r20, -20)          # 涨幅分：下限-20防止深跌板块靠资金翻盘
        fund_score = min(max(fund * 2, -20), 40)  # 资金分：±10亿 → ±20分，上限40
        r['score'] = round(ret_score * 0.7 + fund_score * 0.3, 2)
    sector_rows.sort(key=lambda x: (x['lv'] == '强势', x.get('score', -99)), reverse=True)
    strong = [r for r in sector_rows if r['lv'] == '强势'][:top]
    P(f'   扫描板块 {len(boards)} 个 → 强势 {len([r for r in sector_rows if r["lv"]=="强势"])} 个'
      f'，进 L3 取前 {len(strong)} 个（排序=20日涨幅×主力资金）')
    for r in strong:
        m = r['m']
        fb = m.get('fallback') or ''
        fb_tag = f' [⚠️{fb}]' if fb else ''
        P(f'   🟢 {m["name"]}({m["bk"]}) 20日{m.get("r20")}%/5日{m.get("r5")}% '
          f'量比{m.get("vol_ratio")} 上涨{m.get("up_ratio")*100 if m.get("up_ratio") else 0:.0f}% '
          f'走强{m.get("hot_num")}只 主力{r.get("fund"):+.1f}亿 综合分{r.get("score")}{fb_tag}')
    if not strong:
        P('   ⚪ 无强势板块进入 L3（市场可能无主线）')
        return lines

    # ── L3 强板块内筛个股 ──
    P(f'\nL3 强势板块个股扫描（成分股成交额≥{min_amount}亿）…')
    stock_rows = []   # 每项: {d, cat, reasons, atr_note, rs}
    for r in strong:
        m = r['m']
        members = sector_members_full(m['bk'])
        cands = [x for x in members if x['amount'] >= min_amount * 1e8]
        P(f'   · {m["name"]}: 成分{m["member_cnt"]}只 → 候选{len(cands)}只（成交额≥{min_amount}亿）')
        for x in cands:
            d = analyze_stock(x['code'], x['name'])
            if not d.get('available'):
                continue
            # 兜底：analyze_sector_trend 不可用（板块K线限流）时，用 L2 板块 metric 推导板块状态
            if not d.get('sector_trend_state') and m.get('available'):
                if m.get('above_ma20') and m.get('above_ma60'):
                    d['sector_trend_state'] = '走强（站上双均线，反转向上）'
                elif m.get('above_ma20'):
                    d['sector_trend_state'] = '反弹（MA20上方，MA60下方）'
                else:
                    d['sector_trend_state'] = '弱势（双均线下方）'
            cat, reasons = classify_stock(d)
            atr_note = atr_check(d)
            rs = rs_of(x['code'])
            stock_rows.append({'d': d, 'cat': cat, 'reasons': reasons,
                               'atr_note': atr_note, 'rs': rs, 'sector': m['name'],
                               'sector_fb': m.get('fallback') or ''})
    if not stock_rows:
        P('   ⚪ 无候选个股（成分股数据不足）')
        return lines

    # 去重：同一股票可能属于多个强势板块（如通信/通信设备/通信服务成分重叠），按代码去重保留首个
    seen_codes = set()
    uniq_rows = []
    for r in stock_rows:
        c = r['d']['code']
        if c in seen_codes:
            continue
        seen_codes.add(c)
        uniq_rows.append(r)
    if len(uniq_rows) < len(stock_rows):
        P(f'   ⚠️ 成分重叠去重: {len(stock_rows)} → {len(uniq_rows)} 只（同一股票属多个强势板块）')
    stock_rows = uniq_rows

    # ── L4 分组输出 ──
    order = ['启动观察', '趋势观察', '高位观察', '回调观察', '排除']
    grouped = {c: [r for r in stock_rows if r['cat'] == c] for c in order}
    P(f'\n{"─" * 64}')
    icons = {'启动观察': '🚀', '趋势观察': '📈', '高位观察': '⚠️', '回调观察': '🔄', '排除': '❌'}
    for cat in order:
        rows = grouped[cat]
        if not rows:
            continue
        if cat == '排除':
            # 排除类折叠：只显数量 + 区分「弱势」/「横盘中性」，逐行仅代码+名称+原因摘要
            weak = [r for r in rows if '横盘' not in r['reasons'][0] and '中性' not in r['reasons'][0]]
            neutral = [r for r in rows if '横盘' in r['reasons'][0] or '中性' in r['reasons'][0]]
            P(f'\n{icons[cat]} {cat} ({len(rows)}只) — 弱势{len(weak)}只 / 横盘中性{len(neutral)}只')
            for tag, sub in (('弱势', weak), ('横盘中性', neutral)):
                if not sub:
                    continue
                for r in sub:
                    d = r['d']
                    P(f'  [{tag}] {d["code"]} {d["name"]:<8} 20日{d.get("r20"):+.1f}% '
                      f'距MA20 {d.get("dist_ma20"):+.1f}% — {r["reasons"][0]}')
            continue
        P(f'\n{icons[cat]} {cat} ({len(rows)}只)')
        for r in rows:
            d = r['d']
            rs = r['rs']
            # 主升擒龙/趋势通道状态标注（主行快速扫读）
            qs_tag = ''
            if d.get('qs_qjq'):
                qs_tag = ' ⭐强擒强'
            elif d.get('qs_qiang'):
                qs_tag = ' ✅强势'
            if d.get('qs_duo') in ('做多', '做空'):
                qs_tag += f' 多空:{d["qs_duo"]}'
            tc_tag = ''
            if d.get('tc_position'):
                tc_tag = f' {d["tc_position"]}'
            st_tag = ''
            if d.get('sector_trend_state'):
                # 板块状态简标：走强/反弹/弱势
                st_state = d['sector_trend_state']
                if '走强' in st_state:
                    st_tag = ' 🟢板块走强'
                elif '反弹' in st_state:
                    st_tag = ' 🟡板块反弹'
                elif '弱势' in st_state:
                    st_tag = ' 🔴板块弱势'
            chip_tag = ''
            if d.get('chip_winner') is not None:
                # 筹码简标：底部锁定（高位获利/集中）
                if d.get('chip_lock') is not None and d['chip_lock'] >= 40:
                    chip_tag = f' 筹码锁{d["chip_lock"]:.0f}%'
                elif d.get('chip_winner') >= 75:
                    chip_tag = f' 获利盘{d["chip_winner"]:.0f}%'
                elif d.get('chip_winner') < 50:
                    chip_tag = f' 套牢多(获利{d["chip_winner"]:.0f}%)'
            wave_tag = ''
            if d.get('wave_signal'):
                # 波段简标：第几浪 + 信号类型（放量启动/缩量回调/出货）
                ws = d['wave_signal']
                if '放量启动' in ws or '启动中' in ws:
                    wave_tag = f' 🌊{d.get("wave_current","?")}浪启动'
                elif '缩量回调到位' in ws:
                    wave_tag = f' 🌊缩量回调到位(待{d.get("wave_current",0)+1}浪)'
                elif '疑似出货' in ws or '放量下跌' in ws:
                    wave_tag = ' 🌊⚠️放量下跌(疑似出货)'
                elif '回调中' in ws:
                    wave_tag = ' 🌊回调中'
            pat_tag = ''
            if d.get('kline_pattern_tag'):
                pat_tag = f' {d["kline_pattern_tag"]}'
            P(f'  {d["code"]} {d["name"]:<8} 现价{d["price"]:<8.2f} '
              f'5日{d.get("r5"):+.1f}% 10日{d.get("r10"):+.1f}% 20日{d.get("r20"):+.1f}% '
              f'距MA20 {d.get("dist_ma20"):+.1f}% 量比{d.get("vol_ratio")} '
              f'RS5 {rs["rs5"] if rs["rs5"] is not None else 0:+.1f}{qs_tag}{tc_tag}{st_tag}{chip_tag}{wave_tag}{pat_tag}')
            P(f'    ├ 触发: ' + ' | '.join(r['reasons']))
            if r['atr_note']:
                P(f'    └ {r["atr_note"]}')
            else:
                sfb = f' [⚠️{r["sector_fb"]}]' if r.get('sector_fb') else ''
                P(f'    └ 板块: {r["sector"]}{sfb}')
    P(f'\n{"─" * 64}')
    P('⚠️ 以上仅为状态分类与触发原因，不构成买卖建议，请自行判断。')
    return lines


def save_md(lines, date_str=None):
    """落盘 md 到 复盘/ 目录"""
    os.makedirs(REVIEW_DIR, exist_ok=True)
    date_str = date_str or datetime.datetime.now().strftime('%Y-%m-%d')
    path = os.path.join(REVIEW_DIR, f'趋势筛选_{date_str}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f'# 📊 趋势筛选工作台 {date_str}\n\n')
        f.write('> 自动生成：trend_screener.py | 数据源：东财/新浪（含时效校验）\n>\n')
        f.write('> ⚠️ 仅状态分类+触发原因，不构成投资建议\n\n')
        f.write('\n'.join(lines).replace('\n', '\n'))
    print(f'  ✅ 已保存: {path}')
    return path


def main():
    ap = argparse.ArgumentParser(description='趋势筛选工作台（四层漏斗）')
    ap.add_argument('--top', type=int, default=TOP_SECTORS, help='进L3的强势板块数')
    ap.add_argument('--min-amount', type=float, default=3.0, help='成分股成交额过滤(亿)')
    ap.add_argument('--save', action='store_true', help='落盘md到复盘/目录')
    ap.add_argument('--evaluate', action='store_true', help='仅校验数据源可用性')
    args = ap.parse_args()

    if args.evaluate:
        print('— 数据源可用性校验 —')
        bd = sq.fetch_market_breadth()
        print('  市场宽度:', '✅' if bd and bd.get('available') else '❌', bd.get('note', ''))
        boards = board_list_full()
        print('  板块列表:', f'✅ {len(boards)}个' if boards else '❌')
        if boards:
            first = list(boards.items())[0]
            mb = sector_members_full(first[1]['bk'])
            print(f'  成分股[{first[0]}]:', f'✅ {len(mb)}只' if mb else '❌')
        return

    lines = run(top=min(args.top, 10), min_amount=max(args.min_amount, 0))
    if args.save and lines:
        save_md(lines)


if __name__ == '__main__':
    main()

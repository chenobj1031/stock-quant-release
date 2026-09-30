#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scoring_layer.py — 评分与圆桌分析层（架构优化 P0，2026-08-26）
=====================================================================
背景：从 stock_quant.py 机械拆分出"评分卡 + 圆桌多视角"两个纯计算函数，
解决巨石模块（7395 行）的职责不分层问题。本层只做评分/洞察计算，
不涉及数据获取（数据层在 data_layer.py）与业务编排（仍在 stock_quant.py）。

包含：
  calculate_quant_score  9 因子量化评分（动量/技术/基本面/量能/风险/舆情/资金流/Level2/相对强弱）
  analyze_roundtable     圆桌多视角增强（共识度/多空证据/辩论/时效加权/背离陷阱/概率路径/观察台/失效条件）

依赖策略（打破循环 import）：
  - calculate_quant_score 内部调用 stock_quant.calc_multi_timeframe（多周期共振），
    用【延迟 import】在函数体内引入，避免 scoring_layer ↔ stock_quant 循环依赖
  - analyze_roundtable 为纯 dict 逻辑，零外部依赖
  - 外部接口不变：stock_quant.py 顶部 `from scoring_layer import ...` re-export，
    22 个依赖方仍通过 stock_quant.calculate_quant_score / analyze_roundtable 调用
"""
import re


def calculate_quant_score(tech, fund, cap_flow, sentiment, margin_data, inst_views, patterns, code=''):
    """7因子量化评分系统（含主升擒龙多空线/主力强度/强势信号）"""
    scores = {}

    # 1. 动量因子
    m = 0
    if tech and 'error' not in tech:
        if tech['trend'] == '多头': m += 3
        elif tech['trend'] == '震荡': m += 1
        p5 = tech.get('pct_5d', 0)
        if p5 > 5: m += 2
        elif p5 > 0: m += 1
        elif p5 < -10: m -= 2
        elif p5 < -5: m -= 1
    scores['动量'] = min(max(m, -3), 5)

    # 2. 技术因子（含主升擒龙多空线/强势信号）
    t = 0
    if tech and 'error' not in tech:
        if tech.get('rsi_available', True) and tech.get('rsi', 50) < 35: t += 1
        if tech.get('macd', 0) > 0: t += 1
        if tech.get('kdj_j', 50) < 20: t += 1
        if tech.get('kdj_j', 50) > 80: t -= 1
        if tech.get('vol_ratio', 1) > 1.5: t += 1
        # 主升擒龙：多空线方向
        if tech.get('duo_kong') == '做多': t += 1
        elif tech.get('duo_kong') == '做空': t -= 1
        # 主升擒龙：强势信号
        if tech.get('qiang_shi'): t += 2
        if tech.get('qiang_jin_qiang'): t += 1  # ★强信号额外加分
        # 主升擒龙：主力强度（ABC3>0 表示价格高于量价均线，有主力介入）
        abc3 = tech.get('abc3', 0)
        if abc3 > 5: t += 1
        elif abc3 < -5: t -= 1
        # 波段形态：按确认强度分级计分（🟢完整确认+2 / 🟡降级+1 / 出货确认-2 / 疑似出货-1）
        wave_sig = tech.get('wave_signal', '') or ''
        if '确认出货' in wave_sig:
            t -= 2
        elif '疑似出货' in wave_sig or '放量下跌' in wave_sig:
            t -= 1
        elif '放量启动' in wave_sig or '缩量回调到位' in wave_sig:
            t += 2 if wave_sig.startswith('🟢') else 1
        # P0-2 规则2前置（2026-08-19 专家复盘教训）：乖离中轨>15% 或 KDJ_J>100 → 强制减半，技术因子扣3
        if tech.get('top_overflow'):
            t -= 3
    scores['技术'] = min(max(t, -3), 6)

    # 3. 基本面因子（PE/PB估值 + ROE + 营收/净利增速）
    # 使用 analyze_fundamental 的行业分桶信号，而非固定阈值
    f = 0
    if fund:
        pe_signal = fund.get('pe_signal', '')
        pb_signal = fund.get('pb_signal', '')
        if '低估值' in pe_signal: f += 2
        elif '亏损' in pe_signal or '无PE' in pe_signal: f -= 1  # 亏损股降级
        elif '高估值' in pe_signal: f -= 1
        # PB 信号（行业分桶）
        if '低PB' in pb_signal: f += 1
        elif '高PB' in pb_signal: f -= 1
        # ROE：>15% 优秀 +1，<5% 偏弱 -1
        roe = fund.get('roe')
        if roe is not None:
            if roe > 15: f += 1
            elif roe < 5: f -= 1
        # 营收增速：>20% 高增长 +1，<0 萎缩 -1
        rev_growth = fund.get('rev_growth')
        if rev_growth is not None:
            if rev_growth > 20: f += 1
            elif rev_growth < 0: f -= 1
        # 净利润增速：>30% +1，<0 -1
        profit_growth = fund.get('profit_growth')
        if profit_growth is not None:
            if profit_growth > 30: f += 1
            elif profit_growth < 0: f -= 1
    scores['基本面'] = min(max(f, -2), 4)

    # 4. 量能因子（检查数据源是否为估算，估算时降权）
    v = 0
    cap_estimated = cap_flow.get('data_estimated', False) if cap_flow else False
    if cap_flow:
        if cap_estimated:
            # 估算数据：限制量能因子范围 ±1，避免不可靠数据推高评分
            if cap_flow.get('ratio', 1) > 1.05: v += 0.5
            elif cap_flow.get('ratio', 1) < 0.95: v -= 0.5
            if cap_flow.get('main_net', 0) > 3: v += 0.5
            elif cap_flow.get('main_net', 0) < -3: v -= 0.5
        else:
            # 真实数据：正常权重
            if cap_flow.get('ratio', 1) > 1.05: v += 1
            elif cap_flow.get('ratio', 1) < 0.95: v -= 1
            if cap_flow.get('main_net', 0) > 3: v += 1
            elif cap_flow.get('main_net', 0) < -3: v -= 1
        vr = cap_flow.get('vol_ratio', 1)
        if vr > 2.0: v -= 1
        elif vr < 0.5: v += 1
    scores['量能'] = min(max(v, -2), 3)

    # 5. 风险因子
    r = 0
    if patterns:
        bearish = sum(1 for p in patterns if '看跌' in p[2])
        bullish = sum(1 for p in patterns if '看涨' in p[2])
        r = bullish - bearish
    if tech and 'error' not in tech:
        if tech.get('rsi_available', True) and tech.get('rsi', 50) > 75: r -= 1
    scores['风险'] = min(max(r, -2), 2)

    # 6. 舆情因子
    s = 0
    if sentiment:
        sc = sentiment.get('score', 0)
        if sc > 30: s = 2
        elif sc > 10: s = 1
        elif sc < -30: s = -2
        elif sc < -10: s = -1
    scores['舆情'] = s

    # 7. 资金流因子（用主力净额，不与 Level2 重复）
    c = 0
    if margin_data and margin_data.get('available'):
        if margin_data.get('net_flow', 0) > 0: c += 1
        elif margin_data.get('net_flow', 0) < 0: c -= 1
    if inst_views and inst_views.get('available'):
        if inst_views.get('buy_count', 0) > inst_views.get('total_count', 0) * 0.5: c += 1
    scores['资金流'] = min(max(c, -1), 2)

    # 8. Level2 主力资金因子（用超大单/大单分档，与量能因子去重）
    l2 = 0
    if cap_flow and cap_flow.get('level2_available'):
        # 用超大单占比而非 main_net（main_net 已在量能因子中计入）
        sl_pct = cap_flow.get('level2_super_pct', 0)
        if sl_pct > 20: l2 = 2
        elif sl_pct > 10: l2 = 1
        elif sl_pct < -20: l2 = -2
        elif sl_pct < -10: l2 = -1
    scores['Level2'] = l2

    # 9. 相对强弱因子（20日RS vs 参考指数；个股自身动量已在动量因子计入，此处只算超额）
    rs_f = 0
    if tech and 'error' not in tech:
        rs20 = tech.get('rs_20')
        if rs20 is not None:
            if rs20 > 15: rs_f = 3
            elif rs20 > 5: rs_f = 1
            elif rs20 < -15: rs_f = -2
            elif rs20 < -5: rs_f = -1
    scores['相对强弱'] = min(max(rs_f, -2), 3)

    total = sum(scores.values())
    # P2-10(2026-09-22体检)：归一化窗口与阈值常量化（此前魔法数散落，且中性窗口过窄
    # 无告警——raw 4.8 分即可跨越看多/看空线，单因子整数跳变直接影响方向判定）
    # 修正分母：各因子上限之和 = 5+6+4+3+2+2+2+2+3 = 29
    MAX_RAW = 29
    MIN_RAW = -19
    NORM_BULL = 55   # 与 daily_review.DIR_BULL 对齐：raw ≈ +4.8
    NORM_BEAR = 45   # raw ≈ -2.8；中性窗口宽度仅 10 normalized ≈ 4.8 raw 分
    normalized = (total - MIN_RAW) / (MAX_RAW - MIN_RAW) * 100
    pct = round(max(0, min(100, normalized)), 1)
    # P0 根因修复(2026-09-29体检)：告警文本原写入 scores['_neutral_edge']——
    # 字符串混入因子字典后，analyze_roundtable 对 values 求 v>0 抛
    # TypeError('>' not supported between 'str' and 'int')，即为 9/23~9/28
    # 盘前/收盘多只标的间歇性"分析失败"的根因（仅命中中性窄窗口的个股触发）。
    # 现改为独立返回字段 neutral_edge，不再污染数值字典。
    # P2-10 边界修复(2026-09-30审查)：原条件只覆盖窗口内侧(45≤pct≤55)，
    # 该区间 direction 恒为中性 → daily_review 的 edge_warning 永不触发（死代码）。
    # 真正危险区在方向线外侧：如 pct=56.3(看多) 距翻回中性仅 1 raw 分。
    # 现改为检测 pct 距任一方向线 <1.0 raw 分（两侧均覆盖）。
    neutral_edge = ''
    _RAW_PER_PCT = (MAX_RAW - MIN_RAW) / 100.0   # 1 百分点 ≈ 0.48 raw
    if pct >= NORM_BULL:
        raw_to_line = (pct - NORM_BULL) * _RAW_PER_PCT
    elif pct <= NORM_BEAR:
        raw_to_line = (NORM_BEAR - pct) * _RAW_PER_PCT
    else:
        raw_to_line = min(pct - NORM_BEAR, NORM_BULL - pct) * _RAW_PER_PCT
    if raw_to_line < 1.0:
        neutral_edge = (f"⚠️ 方向线敏感: pct={pct} 距方向线 raw 分差 {raw_to_line:.2f}<1.0"
                        f"（任一因子±1 跳变即改变方向判定），使用时需结合确认开关")

    if pct >= 70: level = '🟢🟢 优'
    elif pct >= 50: level = '🟢 良'
    elif pct >= 30: level = '🟡 中'
    else: level = '🔴 差'

    # 信号置信度打分
    confidence = 0
    if tech and 'error' not in tech:
        if tech.get('qiang_jin_qiang'): confidence += 3
        elif tech.get('qiang_shi'): confidence += 2
        elif tech.get('duo_kong') == '做多': confidence += 1
        # 多周期共振加分（需要 code 调用 calc_multi_timeframe）
        # 延迟 import 打破 scoring_layer ↔ stock_quant 循环依赖
        if code:
            from stock_quant import calc_multi_timeframe
            mtf = calc_multi_timeframe(code, tech.get('ma5', 0))
            if mtf.get('weekly', {}).get('trend') == '多头': confidence += 1
            if mtf.get('monthly', {}).get('trend') == '多头': confidence += 1
        # 市场环境加分
        if tech.get('market_signal', '').startswith('📈'): confidence += 1
    if confidence >= 4: conf_label = '🟢🟢 高置信度'
    elif confidence >= 2: conf_label = '🟢 中等置信度'
    elif confidence >= 1: conf_label = '🟡 低置信度'
    else: conf_label = '⚪ 无信号'

    # ── 择时过滤层：板块/大盘环境过滤 ──
    # MA60 下方（熊市）：所有信号降级一档（看多→观望）
    # MA20 下方（弱势）：评分上限 60 分（禁止"优"评级）
    # MA20 上方（健康）：正常评分
    timing_filter = ''
    original_pct = pct
    if tech and 'error' not in tech:
        ms = tech.get('market_signal', '')
        # 提取信号中的指数名称（如"创业板指""大盘"），用于错误提示
        idx_name = '大盘'
        m_name = re.search(r'(.*?)在MA', ms)
        if m_name:
            idx_name = m_name.group(1)
        if 'MA60' in ms and '下方' in ms:
            # 熊市环境：强制降级，pct 上限设为 40
            if pct > 40:
                pct = 40
            timing_filter = f'⛔ 熊市过滤：{idx_name}在MA60下方，信号降级'
            if level in ('🟢🟢 优', '🟢 良'):
                level = '🟡 中'
        elif 'MA20' in ms and '下方' in ms:
            # 弱势环境：pct 上限设为 60
            if pct > 60:
                pct = 60
            timing_filter = f'⚠️ 弱势过滤：{idx_name}在MA20下方，评分上限60'
            if level == '🟢🟢 优':
                level = '🟢 良'
        elif '上方' in ms:
            timing_filter = f'✅ 环境健康：{idx_name}在MA20上方'

    return {'scores': scores, 'total': total, 'pct': pct, 'level': level,
            'confidence': confidence, 'conf_label': conf_label,
            'timing_filter': timing_filter, 'original_pct': original_pct,
            'neutral_edge': neutral_edge}


def analyze_roundtable(quant_score, tech, cap_flow, price, sim=None):
    """圆桌多视角增强分析（与 9 因子评分互补，不重复计分，只做结构洞察）

    返回 dict：
      views:       视角方向 {'趋势':'多','估值':'空',...}
      consensus:   共识度标签（全票一致/分歧票数）
      bull/bear_count: 多/空视角票数
      bull_evidence / bear_evidence / bull_pct / bear_pct: 多空证据权重分离
      trap:        资金-价格背离陷阱提示（'' 表示无）
      path:        概率化路径 {'up','flat','down','avg_ret','n'} 或 None
      observation: 关键变量观察台 [(变量, 当前值, 触发线→动作), ...]
      invalidations: 结论失效条件 [str, ...]
    """
    result = {'views': {}, 'consensus': '', 'bull_count': 0, 'bear_count': 0,
              'neutral_count': 0, 'bull_evidence': 0, 'bear_evidence': 0,
              'bull_pct': 50, 'bear_pct': 50, 'trap': '', 'path': None,
              'observation': [], 'invalidations': []}
    scores = (quant_score or {}).get('scores', {})
    if not scores:
        return result

    # ── 1. 多视角共识度：9因子按视角归组 ──
    # 趋势视角（动量+技术+相对强弱）/ 估值视角（基本面）/ 资金视角（量能+资金流+Level2）
    # 情绪视角（舆情）/ 风险视角（风险）→ 共 5 个视角
    view_map = {
        '趋势': ['动量', '技术', '相对强弱'],
        '估值': ['基本面'],
        '资金': ['量能', '资金流', 'Level2'],
        '情绪': ['舆情'],
        '风险': ['风险'],
    }
    for vname, keys in view_map.items():
        vsum = sum(scores.get(k, 0) for k in keys)
        result['views'][vname] = '多' if vsum > 0 else ('空' if vsum < 0 else '中性')
    bull_v = sum(1 for d in result['views'].values() if d == '多')
    bear_v = sum(1 for d in result['views'].values() if d == '空')
    neu_v = sum(1 for d in result['views'].values() if d == '中性')
    result['bull_count'], result['bear_count'], result['neutral_count'] = bull_v, bear_v, neu_v
    total_v = len(view_map)
    if bull_v == total_v:
        result['consensus'] = '🟢 全票一致偏多（5/5 视角）'
    elif bear_v == total_v:
        result['consensus'] = '🔴 全票一致偏空（5/5 视角）'
    elif bull_v > bear_v and neu_v == 0:
        result['consensus'] = f'🟡 偏多分歧（{bull_v}多/{bear_v}空，无中性）'
    elif bear_v > bull_v and neu_v == 0:
        result['consensus'] = f'🟡 偏空分歧（{bull_v}多/{bear_v}空，无中性）'
    elif neu_v > 0 and bull_v == bear_v:
        result['consensus'] = f'⚪ 多空拉锯（{bull_v}多/{bear_v}空/{neu_v}中性）'
    else:
        result['consensus'] = f'🟡 混合分歧（{bull_v}多/{bear_v}空/{neu_v}中性）'

    # ── 2. 多空证据权重分离（净分相同的不同含义：95:5 vs 55:45）──
    # 纵深防御(2026-09-29体检)：跳过非数值项，防外部数据污染因子字典后
    # sum(v>0) 抛 TypeError 炸掉整条评分链（根因见 calculate_quant_score 修复注释）
    _numeric = {k: v for k, v in scores.items() if isinstance(v, (int, float))}
    bull_ev = sum(v for v in _numeric.values() if v > 0)
    bear_ev = sum(-v for v in _numeric.values() if v < 0)
    result['bull_evidence'] = round(bull_ev, 1)
    result['bear_evidence'] = round(bear_ev, 1)
    tot_ev = bull_ev + bear_ev
    if tot_ev > 0:
        result['bull_pct'] = round(bull_ev / tot_ev * 100)
        result['bear_pct'] = 100 - result['bull_pct']

    # ── 2b. 多空辩论权重梯度（Workbuddy 多头/空头研究员镜像梯度）──
    # 多头论据榜：正因子按贡献从高到低（强多→弱多）
    # 空头论据榜：负因子按绝对贡献从高到低（强空→弱空）
    # 参考 Workbuddy 权重梯度（9/7/5/4/2 vs 10/8/7/6/3 镜像反转）
    bull_items = sorted([(k, v) for k, v in scores.items() if v > 0], key=lambda x: -x[1])
    bear_items = sorted([(k, -v) for k, v in scores.items() if v < 0], key=lambda x: -x[1])
    result['bull_gradient'] = bull_items
    result['bear_gradient'] = bear_items
    # 多空辩论裁决：最强多头论据 vs 最强空头论据，谁更硬
    top_bull = bull_items[0][1] if bull_items else 0
    top_bear = bear_items[0][1] if bear_items else 0
    if top_bull >= 2 * top_bear and top_bull >= 3:
        debate = '🟢 多头论据占优（最强多头因子 > 2× 最强空头）'
    elif top_bear >= 2 * top_bull and top_bear >= 3:
        debate = '🔴 空头论据占优（最强空头因子 > 2× 最强多头）'
    elif bull_items and bear_items:
        debate = '⚪ 多空论据胶着（双方都有强论据）'
    elif bull_items:
        debate = '🟢 空方无有效论据（单边偏多）'
    elif bear_items:
        debate = '🔴 多方无有效论据（单边偏空）'
    else:
        debate = '⚪ 无多空论据（全部中性）'
    result['debate'] = debate

    # ── 2c. 时效加权裁决（Workbuddy：截面降权0.4× / 实时升权1.5×）──
    # 因子数据时效分类：
    #   实时（升权1.5×）：动量/技术/量能/Level2/资金流/舆情 → 反映当下博弈
    #   截面（降权0.4×）：基本面（PE/PB/ROE来自最新财报）
    #   半实时（0.7×）：相对强弱（历史区间统计）、风险（K线形态滞后中等）
    timing_weight = {
        '动量': 1.5, '技术': 1.5, '量能': 1.5, 'Level2': 1.5,
        '资金流': 1.5, '舆情': 1.5,
        '基本面': 0.4, '相对强弱': 0.7, '风险': 0.7,
    }
    timing_scores = {k: v * timing_weight.get(k, 1.0) for k, v in scores.items()}
    timing_bull = round(sum(v for v in timing_scores.values() if v > 0), 1)
    timing_bear = round(sum(-v for v in timing_scores.values() if v < 0), 1)
    result['timing_net'] = round(timing_bull - timing_bear, 1)
    t_tot = timing_bull + timing_bear
    result['timing_bull_pct'] = round(timing_bull / t_tot * 100) if t_tot > 0 else 50
    # 时效加权后的方向判定（对比原始净分，判断实时视角是否改变结论）
    raw_net = round(bull_ev - bear_ev, 1)
    t_net = result['timing_net']
    result['timing_note'] = ''
    if raw_net * t_net < 0:
        result['timing_note'] = '⚠️ 时效加权后方向反转：实时数据(资金/盘面)与截面数据(财报)结论相反，以实时为准'
    elif abs(t_net - raw_net) >= 3:
        result['timing_note'] = f'🔎 时效加权后强度变化（净分 {raw_net:+.1f} → {t_net:+.1f}）：实时数据权重更高，反映当下博弈'

    # ── 3. 资金-价格背离陷阱（Level2净流入 × 价格位置联动）──
    # 判定：主力资金净流入为正（或超大单占比>10%）但价格跌破 MA5/MA10 → 出货陷阱
    # 注意：出货陷阱与洗盘特征互斥（elif 链），避免极端情况互相覆盖
    if cap_flow and tech and 'error' not in tech and price > 0:
        l2_net = cap_flow.get('level2_main_total', 0) if cap_flow.get('level2_available') else cap_flow.get('main_net', 0)
        super_pct = cap_flow.get('level2_super_pct', 0)
        ma5 = tech.get('ma5', 0) or 0
        ma10 = tech.get('ma10', 0) or 0
        fund_in = (l2_net and l2_net > 0) or (super_pct and super_pct > 10)
        price_weak = (ma5 and price < ma5) or (ma10 and price < ma10)
        fund_out = (l2_net and l2_net < 0) or (super_pct and super_pct < -10)
        price_strong = (ma5 and price > ma5) and (ma10 and price > ma10)
        if fund_in and price_weak:
            # 2026-08-31 修复：文案原先硬编码"< MA5"，但触发条件是 MA5/MA10 任一跌破，
            # 仅跌破 MA10（仍在 MA5 上方）时文案数值矛盾（如某标的 43.77>MA5 43.44 却显示< MA5）。
            # 现按实际触发的均线如实标注，MA10-only 降级为弱信号提示。
            if ma5 and price < ma5:
                pos_desc = f'现价{price:.2f} < MA5 {ma5:.2f}'
            elif ma10 and price < ma10:
                pos_desc = f'现价{price:.2f} < MA10 {ma10:.2f}'
                if ma5 and price > ma5:
                    pos_desc += f'（仍在 MA5 {ma5:.2f} 上方，弱信号）'
            else:
                pos_desc = '价格跌破均线'
            result['trap'] = (f'⚠️ 资金-价格背离：主力资金净流入'
                              f'（L2 {l2_net:+.1f}亿/超大单{super_pct:+.0f}%）但价格跌破均线'
                              f'（{pos_desc}）——"资金进、价格跌"多为出货陷阱，'
                              f'别把资金流入当安全垫')
        elif fund_out and price_strong:
            # 反向背离：资金净流出但价格站稳均线 → 洗盘特征（提示但不判陷阱）
            result['trap'] = (f'🔎 资金-价格反向背离：主力资金净流出'
                              f'（L2 {l2_net:+.1f}亿）但价格站稳均线上方'
                              f'（现价{price:.2f} > MA5 {ma5:.2f}）——洗盘特征，关注回踩不破的二次确认')

    # ── 4. 概率化路径（历史相似K线的未来表现分布）──
    if sim and sim.get('matches'):
        rets = [m.get('future_ret', 0) for m in sim['matches']]
        if rets:
            n = len(rets)
            up = sum(1 for r in rets if r > 2)
            down = sum(1 for r in rets if r < -2)
            flat = n - up - down
            result['path'] = {
                'up': round(up / n * 100), 'flat': round(flat / n * 100),
                'down': round(down / n * 100),
                'avg_ret': round(sum(rets) / n, 2), 'n': n,
                # P1-8(2026-09-30)：小样本标记——n<10 仅作展示参考，禁止按信号采信
                # （path 本就不参与共识/证据权重计算，此处标记供展示层强制拦截）
                'low_sample': n < 10,
            }

    # ── 5. 失效条件 + 关键变量观察台 ──
    if tech and 'error' not in tech:
        ma5 = tech.get('ma5', 0) or 0
        ma10 = tech.get('ma10', 0) or 0
        ma20 = tech.get('ma20', 0) or 0
        ma60 = tech.get('ma60', 0) or 0
        support = tech.get('support', 0) or 0
        resistance = tech.get('resistance', 0) or 0
        macd = tech.get('macd', 0)
        duo_kong = tech.get('duo_kong', '')
        pct = (quant_score or {}).get('pct', 50)

        # 观察台：关键变量 + 触发线→动作
        obs = []
        if ma10:
            obs.append(('MA10', f'{ma10:.2f}', f'站上 {ma10:.2f} → 短线转强确认可持有；跌破 → 多头逻辑动摇减仓'))
        if ma20:
            obs.append(('MA20', f'{ma20:.2f}', f'放量站上 {ma20:.2f} → 趋势反转确认；跌破 → 反弹结束'))
        if ma60:
            obs.append(('MA60', f'{ma60:.2f}', f'跌破 {ma60:.2f} → 中期走弱，看空信号加强'))
        if support:
            obs.append(('支撑位', f'{support:.2f}', f'有效跌破 {support:.2f} → 下看更低，止损参考'))
        if resistance:
            obs.append(('压力位', f'{resistance:.2f}', f'放量突破 {resistance:.2f} → 打开上行空间'))
        result['observation'] = obs[:5]

        # 失效条件（结论证伪框架）：按当前最终方向生成
        inv = []
        if pct >= 50:
            # 偏多结论的失效条件
            if support:
                inv.append(f'放量跌破支撑 {support:.2f} 且 MACD 死叉 → 偏多结论失效')
            elif ma10:
                inv.append(f'跌破 MA10 {ma10:.2f} 且 MACD 未修复 → 偏多结论失效')
            if macd < 0 and duo_kong == '做空':
                inv.append('MACD 零轴下 + 多空线做空 → 反弹性质存疑，按反抽对待')
        else:
            # 偏空结论的失效条件
            if resistance:
                inv.append(f'放量突破压力 {resistance:.2f} 且 MACD 金叉 → 偏空结论失效')
            elif ma10:
                inv.append(f'站上 MA10 {ma10:.2f} 并持续 3 日 → 偏空结论失效')
            if duo_kong == '做多':
                inv.append('多空线转做多 → 下跌趋势可能反转')
        result['invalidations'] = inv[:3]

    return result

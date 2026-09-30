# -*- coding: utf-8 -*-
"""
每日复盘自动化三件套（盘前基准 → 收盘对照 → 归因复盘）
=========================================================
用法:
  python daily_review.py --premarket [--pool 600519 600036 ...] [--no-cache]
  python daily_review.py --close [--no-cache]

设计原则：
  - 盘前把"预测方向 + 确认开关 + 失效条件"写死落盘（可证伪）
  - 收盘按同一规则逐只对照实际，判定 符合/偏差，归因 环境突变/数据问题/框架问题
  - 全部输出到 复盘/ 目录，JSON 为机器可读基准（供后续校准），MD 为人读报告
数据口径：
  - 盘前(9:30前)：行情为竞价后实时值，资金面为上一交易日口径（外盘/内盘无效）
  - 收盘(15:00后)：行情为当日收盘值，资金面为当日口径
"""
import argparse
import datetime
import json
import logging
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_quant as sq
from config import LOCK_MIN, VR_MIN
import style_rotation

# 复盘日志（DEBUG=每笔盘前预测绩效，INFO=批次汇总，WARNING=异常）；排查时设 level=logging.DEBUG
logger = logging.getLogger('daily_review')

REVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '复盘')
# 股票池单源（2026-08-28 收敛）：统一读 config.get_watch_pool()（data/pool.json），
# 不再本地硬编码——历史教训：本地硬编码池与 prepare/持仓台账不一致，导致池定义漂移
from config import get_watch_pool
DEFAULT_POOL = [n for n, _ in get_watch_pool()]
# 自定义观察台配置文件（手动维护的额外触发线，盘前基准渲染时合并）
CUSTOM_WATCH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'watch_custom.json')

# ── 判定规则（2026-08-26 架构优化：已集中到 config.py，此处引用）──
# 方向阈值：score.pct 归一化 0-100
from config import DIR_BULL, DIR_BEAR, RET_BIG

# ── 政策事件扫描（2026-09-29 体检新增）──
# 背景：09-28 盘中固态电池政策(财联社电报)未进复盘视野，电池线抗跌被误归因。
# 规则：部委/发文字样 × 政策动作字样 双命中才算政策事件；失败静默降级不阻断主流程。
_POLICY_MINISTRIES = ('工信部', '发改委', '国务院', '财政部', '商务部', '央行', '人民银行',
                      '证监会', '能源局', '市监总局', '住建部', '交通运输部', '农业农村部',
                      '中办', '国办')
_POLICY_ACTIONS = ('印发', '发布', '出台', '印发通知', '指导意见', '实施方案', '行动方案',
                   '规划', '指南', '标准', '征求意见', '若干措施', '白皮书', '征求意见稿')
# ── P2-11(2026-09-30)：事件分类器扩展——政策文件之外的数据发布/产业事件类 ──
# 背景：PMI 50.1%（9-30）曾靠人工从电报原文匹配，分类器只认部委×动作，漏覆盖。
_DATA_RELEASE_SOURCES = ('国家统计局', '央行', '海关总署', '财政部', '中国物流与采购联合会')
_DATA_RELEASE_KEYWORDS = ('PMI', '采购经理指数', 'CPI', 'PPI', '社融', 'M2', '进出口',
                          '贸易顺差', '外汇储备', 'LPR', '金融数据', '工业增加值',
                          '社会消费品零售', '固定资产投资', ' GDP')
_INDUSTRY_EVENT_KEYWORDS = ('量产', '投产', '中标', '订单', '涨价', '涨价函', '扩产',
                            '签约', '并购', '重组', '获批', '集采', '纳入指数', '调仓')


def classify_cls_item(title):
    """财联社电报单条标题 → 事件分类
    返回 dict: {'kind': '政策文件'|'数据发布'|'产业事件'|'', 'ministry'/'source': str,
                'action'/'keyword': str}
    优先级: 政策文件 > 数据发布 > 产业事件（政策含金量最高，先判）"""
    t = title or ''
    ministry = next((m for m in _POLICY_MINISTRIES if m in t), '')
    action = next((a for a in _POLICY_ACTIONS if a in t), '')
    if ministry and action:
        return {'kind': '政策文件', 'ministry': ministry, 'action': action}
    source = next((s for s in _DATA_RELEASE_SOURCES if s in t), '')
    keyword = next((k for k in _DATA_RELEASE_KEYWORDS if k in t), '')
    if source and keyword:
        return {'kind': '数据发布', 'ministry': source, 'action': keyword}
    keyword2 = next((k for k in _INDUSTRY_EVENT_KEYWORDS if k in t), '')
    if keyword2:
        return {'kind': '产业事件', 'ministry': '', 'action': keyword2}
    return {'kind': '', 'ministry': '', 'action': ''}


def policy_event_scan(limit=30):
    """财联社电报 → 事件扫描（2026-09-30 P2-11 扩展：政策文件+数据发布+产业事件）
    返回 list[{'title','time','kind','ministry','action'}]；电报不可用/无命中返回空列表"""
    try:
        items, ok = sq.summarize_cls_telegraph(limit=limit, category='red')
    except Exception as e:
        print(f'⚠️ 政策事件扫描失败(不影响主流程): {e}')
        return []
    if not ok:
        print('⚠️ 财联社电报不可用，政策事件扫描跳过（盘中政策盲区，人工核对财联社App）')
        return []
    hits = []
    for it in items:
        c = classify_cls_item(it.get('title', ''))
        if c['kind']:
            hits.append({'title': it.get('title', ''), 'time': it.get('time', ''),
                         'kind': c['kind'], 'ministry': c['ministry'], 'action': c['action']})
    return hits


def today_str():
    return datetime.datetime.now().strftime('%Y-%m-%d')


def now_iso():
    return datetime.datetime.now().isoformat(timespec='seconds')


def resolve_pool(names):
    """股票名/代码 → (name, code)"""
    out = []
    for n in names:
        code, _name = sq.resolve_stock(n)
        if code:
            out.append((n, code))
        else:
            print(f'  ⚠️ 无法解析标的: {n}')
    return out


def direction_of(pct):
    """评分 → 方向（写死可证伪：≥55看多、≤45看空、中间中性）"""
    if pct >= DIR_BULL:
        return '看多'
    if pct <= DIR_BEAR:
        return '看空'
    return '中性'


def build_exec_plan(direction, pct_score, tech, rt):
    """预测可执行化（2026-08-25 P0-1 新增）：生成结构化预测计划
    返回 dict:
      trigger      触发条件（方向确认信号，写死可证伪）
      target_up    上方目标位（看多兑现位）
      target_down  下方目标位（看空兑现位）
      invalidation 失效条件（预测作废）
      stop         止损/失效参考位
    规则：
      - 看多：放量突破压力/站上MA20 确认 → 目标=压力位/前高；失效=跌破支撑且MACD死叉
      - 看空：有效跌破支撑/跌破MA20 确认 → 目标=支撑位/前低；失效=放量突破压力且MACD金叉
      - 中性：等待方向选择（突破压力或跌破支撑再定）
    """
    plan = {'trigger': '', 'target_up': None, 'target_down': None,
            'invalidation': '', 'stop': None}
    if not tech or 'error' in tech:
        return plan
    ma10 = tech.get('ma10', 0) or 0
    ma20 = tech.get('ma20', 0) or 0
    support = tech.get('support', 0) or 0
    resistance = tech.get('resistance', 0) or 0
    macd = tech.get('macd', 0)
    duo_kong = tech.get('duo_kong', '')

    if direction == '看多':
        if resistance:
            plan['trigger'] = f'放量突破压力 {resistance:.2f} → 打开上行空间（看多确认）'
            plan['target_up'] = round(resistance, 2)
        elif ma20:
            plan['trigger'] = f'放量站上 MA20 {ma20:.2f} → 趋势反转确认（看多确认）'
            plan['target_up'] = round(ma20 * 1.05, 2)
        plan['target_down'] = round(support, 2) if support else None
        plan['stop'] = round(support, 2) if support else round(ma10, 2) if ma10 else None
    elif direction == '看空':
        if support:
            plan['trigger'] = f'有效跌破支撑 {support:.2f} → 下看更低（看空确认）'
            plan['target_down'] = round(support, 2)
        elif ma20:
            plan['trigger'] = f'跌破 MA20 {ma20:.2f} → 反弹结束（看空确认）'
            plan['target_down'] = round(ma20 * 0.95, 2)
        plan['target_up'] = round(resistance, 2) if resistance else None
        plan['stop'] = round(resistance, 2) if resistance else round(ma10, 2) if ma10 else None
    else:  # 中性
        plan['trigger'] = '等待方向选择：放量突破压力 或 有效跌破支撑 再定方向'
        plan['target_up'] = round(resistance, 2) if resistance else None
        plan['target_down'] = round(support, 2) if support else None

    # 失效条件：优先用圆桌结论证伪框架（已有 invalidations），否则按方向兜底
    inv = rt.get('invalidations', []) if rt else []
    if inv:
        plan['invalidation'] = inv[0]
    elif direction == '看多':
        plan['invalidation'] = (f'放量跌破支撑 {support:.2f} 且 MACD 死叉 → 看多失效'
                                if support else '跌破 MA10 且 MACD 未修复 → 看多失效')
    elif direction == '看空':
        plan['invalidation'] = (f'放量突破压力 {resistance:.2f} 且 MACD 金叉 → 看空失效'
                                if resistance else '站上 MA10 并持续 3 日 → 看空失效')
    else:
        plan['invalidation'] = '放量突破压力或跌破支撑 → 中性结论失效（方向选择完成）'
    return plan


def analyze_one(name, code, no_cache=False, phase='premarket'):
    """复用 stock_quant 分析链，提取预测所需关键字段"""
    if no_cache:
        sq.CACHE_TTL = {k: 0 for k in sq.CACHE_TTL}
    quote = sq.fetch_quote_tencent(code)
    if not quote or quote.get('price', 0) == 0:
        return {'name': name, 'code': code, 'error': '行情获取失败'}
    price = quote['price']
    pct = quote.get('pct', 0)
    # ── 故障隔离(2026-09-29体检)：9/23~9/28 premarket/close 日志中多只标的因上游数据
    # 返回异常类型（str/int 混型）抛 TypeError，整只记录变 error 无法参与对照。
    # 技术面是方向判断的核心，失败仍按 error 处理；基本面/资金面/舆情/K线为辅助因子，
    # 失败降级为空 dict（scoring_layer 对空 dict 容忍，测试已覆盖），不炸整只记录。
    try:
        tech = sq.analyze_technical(code, price)
    except Exception as e:
        print(f'  ⚠️ {name} 技术面分析异常: {e}')
        return {'name': name, 'code': code, 'error': f'技术面分析异常: {e}'}
    tech_ok = bool(tech) and 'error' not in tech
    try:
        fund = sq.analyze_fundamental(quote, tech) if tech_ok else {}
    except Exception as e:
        print(f'  ⚠️ {name} 基本面分析异常降级为空: {e}')
        fund = {}
    try:
        cap = sq.analyze_capital_flow(quote, tech) if tech_ok else {}
    except Exception as e:
        print(f'  ⚠️ {name} 资金面分析异常降级为空: {e}')
        cap = {}
    try:
        sent = sq.fetch_news_sina(code, name)
    except Exception as e:
        print(f'  ⚠️ {name} 舆情获取异常降级为空: {e}')
        sent = {}
    try:
        kline_data = sq.fetch_kline_sina(code, 240, 60)
    except Exception as e:
        print(f'  ⚠️ {name} K线获取异常降级为空: {e}')
        kline_data = []
    try:
        patterns = sq.analyze_candlestick(tech, kline_data) if tech_ok else []
        score = sq.calculate_quant_score(tech, fund, cap, sent, {}, {}, patterns, code)
        rt = sq.analyze_roundtable(score, tech, cap, price) if tech_ok else {}
    except Exception as e:
        print(f'  ⚠️ {name} 评分链异常: {e}')
        return {'name': name, 'code': code, 'error': f'评分链异常: {e}'}

    pct_score = score.get('pct', 50)
    direction = direction_of(pct_score)
    # ── P2-10 评分带边缘告警（2026-09-30）：中性窗口过窄（45-55≈4.8 raw分），
    # 单因子±1跳变即可翻转方向。scoring_layer 已生成 neutral_edge 文本，
    # 此处透传进记录（JSON 可追溯）+ 触发方向稳定性降档：边缘带内"看多/看空"
    # 若恰在窗口内则视为弱方向，盘前展示带 ⚠️ 标注（不改方向判定本身——判定规则写死可证伪）
    neutral_edge = score.get('neutral_edge', '')
    edge_warning = ''
    if neutral_edge and direction in ('看多', '看空'):
        edge_warning = f"{neutral_edge}（当前{direction}处于评分带边缘，方向稳定性弱，确认开关必须先于动作）"

    # ── P0-2 规则2前置（2026-08-19 专家复盘教训）：乖离超阈值时禁止看多、压低评分 ──
    # 背景：8/18 瑞丰乖离中轨 32.7% 已超阈值，盘前仍给看多 66.7 分 → 框架缺陷。
    # 前置目的：规则2（顶部止盈）触发时，方向评分不得给出"看多"。
    top_overflow = bool(tech.get('top_overflow')) if tech_ok else False
    rule2_note = ''
    if top_overflow:
        if direction == '看多':
            direction = '中性'  # 禁止看多
            pct_score = min(pct_score, DIR_BULL - 1)  # 压到看多线以下（≤54）
        rule2_note = (f"⛔ 规则2前置（顶部止盈）: {tech.get('top_overflow_reason', '乖离超阈值')} "
                      f"→ 强制减半/不追高，禁止看多")
    # ── β 敞口标注（2026-08-19 专家复盘教训）：创业板/科创板=高β，系统日默认降档 ──
    code_digits = ''.join(ch for ch in code if ch.isdigit())
    if code_digits[:3] in ('300', '301', '688'):
        beta_level = '高β'
    elif code_digits[:3] in ('600', '601', '603', '000', '002'):
        beta_level = '中β'
    else:
        beta_level = '低β'

    # ── P0-4 高β波动警示（2026-08-21 专项分析落地）：高β+中性 → 附波动警示 ──
    # 背景：8/12~8/21 部分偏差中 B 类 9 条里 8 条是高β（89%）——"中性 × 高β × 大盘波动日"
    #   三重叠加导致中性评级失效（实际可达 ±6~10%）。高β标的给"中性"时必须警示波动风险。
    beta_warning = ''
    if beta_level == '高β' and direction == '中性':
        beta_warning = (f"⚠️ 高β波动警示（2026-08-21 新增）: 中性·高β——大盘波动>2% 时实际可达 "
                        f"±5%+，不建议作为仓位核心；若大盘|涨跌|>3%（Beta闸门）则信号降档")

    # ── P0-3 规则9仲裁（2026-08-21 五专家复盘教训）：破MA60但资金未坏 → 中性而非看空 ──
    # 背景：8/21 瑞丰破 MA60(17.02) 但 5日主力资金+1.33亿、20日涨幅+27%（资金结构未坏），
    #   系统盘前给"看空40"，五专家一致修正为"中性/中性偏多"（乖离回归+资金未坏，超跌反弹非趋势走弱）。
    # 仲裁条件（收紧版）：方向看空 + 破MA60 + 20日涨幅>10%（中期真实强势，非弱势微反弹）+
    #   当日主力未极端流出 + 非亏损股（亏损股五维共振向下，不仲裁）→ 看空降中性。
    rule9_note = ''
    if direction == '看空' and tech_ok:
        pct20 = tech.get('pct_20d', 0) or 0
        main_net = (cap or {}).get('main_net', 0) or 0
        ma60 = tech.get('ma60')
        pe = (fund or {}).get('pe')
        loss_stock = pe is not None and pe < 0  # 亏损股不适用"超跌反弹"仲裁
        broke_ma60 = bool(ma60) and price < ma60
        if broke_ma60 and pct20 > 10 and main_net > -2 and not loss_stock:
            direction = '中性'  # 超跌反弹非趋势走弱
            pct_score = max(pct_score, DIR_BEAR + 1)  # 至少拉到中性区（>45）
            rule9_note = (f"⛔ 规则9仲裁（反弹可靠性）: 破MA60但20日{pct20:+.1f}%为正（资金结构未坏）"
                          f"+主力{main_net:+.1f}亿未大幅流出 → 看空降中性，超跌反弹非趋势走弱")

    # ── P0-3 盘前资金面 T-1 口径降权（2026-08-25 新增）──
    # 背景：某标的 8/25 误判主因——盘前用昨日(T-1)资金面(-0.32亿)支撑看空，
    #   实际当日资金面基本持平 → 看空权重过高。竞价后(9:15起) fetch_main_flow
    #   已把昨日数据标记 stale 并尝试新浪实时兜底；若仍拿不到当日数据则走此降权。
    # 规则（写死可证伪）：盘前 + 资金面 stale（昨日）→ 看空评分向中性拉回一半（×0.5），
    #   拉回后越过看空线(45)则降为中性。
    fund_stale_note = ''
    if phase == 'premarket' and cap and cap.get('data_stale') and direction == '看空':
        old_score = pct_score
        pct_score = old_score + (50 - old_score) * 0.5  # 向中性50拉回一半
        if pct_score > DIR_BEAR:  # 越过看空线 → 中性
            direction = '中性'
            pct_score = max(pct_score, DIR_BEAR + 1)
        fund_stale_note = (f"⛔ 盘前资金面T-1降权(2026-08-25): 资金面为昨日口径(stale)未拉到当日实时，"
                           f"看空评分{old_score:.0f}→{pct_score:.0f}（×0.5降权），"
                           f"资金面对看空判定的支撑减半")

    record = {
        'name': name, 'code': code,
        'phase': phase, 'timestamp': now_iso(),
        'price': round(price, 2),
        # P1-4 修复(2026-09-22体检)：触发判定需盘中触及价（当日最高/最低），
        # 只传收盘价会使"触发"与"达标"判定条件完全相同，'触发未达标'分支永不可达
        'high': quote.get('high'),
        'low': quote.get('low'),
        'pct': round(pct, 2) if pct is not None else None,
        'trend': tech.get('trend') if tech_ok else None,
        'score_pct': pct_score,
        'level': score.get('level', ''),
        'direction': direction,
        'scores': score.get('scores', {}),
        'market_signal': tech.get('market_signal', '') if tech_ok else '',
        # P2-10 评分带边缘告警（2026-09-30）：中性窄窗口内的方向为弱方向，JSON可追溯
        'neutral_edge': neutral_edge,
        'edge_warning': edge_warning,
        # 走势描述字段（与 HTML 走势解读一致）
        'wave_count': tech.get('wave_count') if tech_ok else None,
        'current_wave': tech.get('current_wave') if tech_ok else None,
        'wave_state': tech.get('wave_state') if tech_ok else '',
        'wave_signal': tech.get('wave_signal') if tech_ok else '',
        'divergence': tech.get('divergence') if tech_ok else '',
        'rs_5': tech.get('rs_5') if tech_ok else None,
        'rs_20': tech.get('rs_20') if tech_ok else None,
        'rs_ref': tech.get('rs_ref') if tech_ok else '',
        'rs_signal': tech.get('rs_signal') if tech_ok else '',
        'duo_kong': tech.get('duo_kong') if tech_ok else '',
        'pct_5d': tech.get('pct_5d') if tech_ok else None,
        'pct_20d': tech.get('pct_20d') if tech_ok else None,
        # P0-2 规则2前置（顶部止盈）与 β 敞口（2026-08-19 专家复盘教训）
        'top_overflow': top_overflow,
        'rule2_note': rule2_note,
        'beta_level': beta_level,
        # P0-3 规则9仲裁（2026-08-21 五专家复盘教训：破MA60但资金未坏 → 中性）
        'rule9_note': rule9_note,
        # P0-4 高β波动警示（2026-08-21 专项分析落地：高β+中性 → 波动警示）
        'beta_warning': beta_warning,
        # 筹码分布（与 HTML 走势解读一致）
        'chip_winner_pct': tech.get('chip_winner_pct') if tech_ok else None,
        'chip_cost_peak': tech.get('chip_cost_peak') if tech_ok else None,
        'chip_cost_avg': tech.get('chip_cost_avg') if tech_ok else None,
        'chip_concentration': tech.get('chip_concentration') if tech_ok else None,
        'chip_bottom_lock': tech.get('chip_bottom_lock') if tech_ok else None,
        'chip_note': tech.get('chip_note') if tech_ok else '',
        'chip_60_note': tech.get('chip_60_note') if tech_ok else '',
        'vol_ratio': tech.get('vol_ratio') if tech_ok else None,
        # 关键价位
        'ma20': round(tech['ma20'], 2) if tech_ok and tech.get('ma20') else None,
        'ma60': round(tech['ma60'], 2) if tech_ok and tech.get('ma60') else None,
        'support': round(tech['support'], 2) if tech_ok and tech.get('support') else None,
        'resistance': round(tech['resistance'], 2) if tech_ok and tech.get('resistance') else None,
        # 圆桌：观察台(触发线) + 失效条件 + 共识
        'observation': rt.get('observation', []),
        'invalidations': rt.get('invalidations', []),
        'consensus': rt.get('consensus', ''),
        'timing_note': rt.get('timing_note', ''),
        'trap': rt.get('trap', ''),
        # P0-1 预测可执行化（2026-08-25 新增）：触发条件/目标位/失效条件/止损位
        'exec_plan': build_exec_plan(direction, pct_score, tech, rt),
        # 数据口径标注
        'data_note': ('盘前口径: 行情=竞价后实时, 资金面=上一交易日' if phase == 'premarket'
                      else '收盘口径: 行情=当日收盘, 资金面=当日'),
    }
    return record


def format_obs(obs):
    """观察台 → md 行"""
    if not obs:
        return ['  - (无触发线)']
    lines = []
    for item in obs[:5]:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            lines.append(f'  - {item[0]} {item[1]} → {item[2]}')
        else:
            lines.append(f'  - {item}')
    return lines


def chip_breakout_signal(r):
    """底部锁定 + 放量突破 组合信号（2026-08-12 新增）
    判定（写死可证伪）：
      - 底部锁定度 >= 40%（底部筹码稳定）
      - 量比 vol_ratio >= 1.5（新资金进入）
      - 现价 >= 成本峰（向上冲刺/突破）
    返回标注文本；条件不足时返回空字符串。
    """
    if r.get('chip_bottom_lock') is None or r.get('chip_cost_peak') is None:
        return ''
    lock = r.get('chip_bottom_lock', 0)
    vr = r.get('vol_ratio') or 0
    price = r.get('price') or 0
    peak = r.get('chip_cost_peak', 0)
    if lock < LOCK_MIN:
        return ''
    if vr >= VR_MIN and price >= peak:
        return (f'🚀 底部锁定+放量突破组合信号：底部锁定{lock}% + 量比{vr:.1f} + '
                f'现价{price:.2f}站上成本峰{peak:.2f}——深V/冲新高的筹码基础，重点跟踪确认')
    if vr >= VR_MIN:
        return f'🔎 底部锁定{lock}% + 放量{vr:.1f}，但现价{price:.2f}未站上成本峰{peak:.2f}——观察突破'
    return ''


def load_custom_watch():
    """读取自定义观察台配置 {code: [触发线文本, ...]}，文件不存在返回空 dict"""
    try:
        if os.path.exists(CUSTOM_WATCH_FILE):
            import json as _json
            with open(CUSTOM_WATCH_FILE, encoding='utf-8') as f:
                data = _json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def fetch_sector_top(top_n=8):
    """获取行业板块涨幅 TOP（东财 clist m:90+t:2，含主力净额），用于盘前主线确认
    返回 [(name, pct, main_net亿), ...]；失败返回 []
    2026-08-28：收敛到 sq._em_probe_all 统一轮询（冷却标记与库内入口一致）
    """
    try:
        data = sq._em_probe_all(
            lambda s, h: (f"{s}://{h}/api/qt/clist/get?pn=1&pz=200&po=1&np=1&fltt=2&invt=2"
                          f"&fields=f3,f12,f14,f62&fs=m:90+t:2"),
            sq._EM_FFLOW_HOSTS, timeout=10,
            valid=lambda d: bool(d.get('data') and d['data'].get('diff')))
        if data:
            diff = data['data']['diff'] or []
            rows = [(it.get('f14', ''), it.get('f3'), (it.get('f62') or 0) / 1e8)
                    for it in diff if it.get('f3') is not None]
            rows.sort(key=lambda x: -(x[1] or 0))
            return [(n, pct, mn) for n, pct, mn in rows[:top_n]]
    except Exception:
        pass
    return []


def compute_env_warning(records):
    """环境降档预警（2026-08-28 观察周机制，建议3 待转正；2026-09-01 抽为独立函数）
    观察期(08-21~08-28)8笔反向中6笔集中在"环境偏多/反弹日+盘前看空"，
    偏多日对看空/中性标的显式提示，降低"强空撞反弹"的硬伤（降档制再观察一周）。
    返回 (warning_str, record_names)；无预警返回 ('', [])。
    2026-09-01：MD 展示与 JSON 顶层 env_warning 共用此函数，供 Workbuddy 预填透传给专家。
    """
    bull = [r for r in records
            if '环境偏多' in (r.get('market_signal') or '')
            and r.get('direction') in ('看空', '中性')]
    if not bull:
        return '', []
    names = [r['name'] for r in bull]
    warn = (f"环境偏多日盘前看空/中性 {len(bull)} 只（{'、'.join(names)}）——"
            f"反弹日偏空方向需降档看待，触发确认前不建议按强空口径操作（观察周机制，待数据转正）")
    return warn, names


def render_premarket_md(records, env_note=''):
    # 架构修复(2026-09-24)：部分标的分析失败时 records 含 {'error':...} 无 price 键的残缺记录，
    # 原直接 r['price'] → KeyError 崩在写盘前 → 当天盘前基准整体缺失（09-23/09-24 连续两天
    # 基准丢失的致命根因）。失败记录在渲染层过滤并集中标注，不得拖垮整份基准。
    ok_records = [r for r in records if 'price' in r]
    failed_records = [r for r in records if r not in ok_records]
    records = ok_records
    """渲染盘前预测基准 MD"""
    cw_map = load_custom_watch()  # 自定义观察台配置（按 code 索引）
    lines = []
    lines.append(f'# {today_str()} 盘前预测基准（供收盘复盘对照）')
    lines.append('')
    lines.append(f'> 生成时间: {now_iso()} (盘前)')
    lines.append(f'> 数据口径: 行情为竞价后实时值；资金面为上一交易日口径（外盘/内盘无效）')
    lines.append(f'> 用途: 收盘后对照实际行情，复盘预测对错与偏差原因')
    lines.append(f'> 参数快照: 方向阈值 看多≥{DIR_BULL}/看空≤{DIR_BEAR} ｜ 涨跌判定 |pct|≥{RET_BIG}%'
                 f' ｜ 池 {len(records)} 只')
    lines.append('')
    lines.append('## 一、盘前环境判断')
    lines.append('')
    if env_note:
        lines.append(env_note)
    else:
        lines.append('- （运行时自动获取大盘状态，见各股 market_signal）')
    # 环境降档预警（2026-08-28 观察周机制；2026-09-01 抽为 compute_env_warning 共用）
    _env_warn, _env_warn_names = compute_env_warning(records)
    if _env_warn:
        lines.append(f"- ⚠️ **环境降档预警**：{_env_warn}")
    # 全局宏观观察（__global__ key，非个股触发线，如 CPI/汇率/政策联动）
    global_watch = cw_map.get('__global__')
    if global_watch:
        lines.append('- **宏观观察台**（全局，非个股）：')
        for item in global_watch:
            prefix = '' if item.strip().startswith(('📌', '🔴', '🟢', '🔵', '⚠️')) else '📌 '
            lines.append(f'  {prefix}{item}')
    # 板块主线确认（涨幅 TOP + 主力净额，用于识别当期主线）
    try:
        sector_top = fetch_sector_top(8)
        if sector_top:
            lines.append('- **板块主线确认**（涨幅TOP+主力净额，盘前主线参考）：')
            for name, pct, mnet in sector_top:
                icon = '🟢' if mnet > 0 else '🔴'
                lines.append(f'  {icon} {name:<12} {pct:+.2f}%  主力{mnet:+.1f}亿')
    except Exception:
        pass
    lines.append('')
    lines.append('## 二、逐股预测与操作思路（基准）')
    lines.append('')
    lines.append('| 标的 | 盘前价 | 方向 | 评分 | 多头确认开关 | 失效信号 |')
    lines.append('|---|---|---|---|---|---|')
    for r in records:
        obs = '; '.join(
            f"{o[0]}{o[1]}→{o[2]}" for o in r.get('observation', [])[:3]
            if isinstance(o, (list, tuple)) and len(o) >= 3
        ) or '—'
        inv = '; '.join(r.get('invalidations', [])[:3]) or '—'
        lines.append(
            f"| {r['name']} | {r['price']} | {r['direction']} | "
            f"{r['score_pct']}({r['level']}) | {obs} | {inv} |"
        )
    lines.append('')
    lines.append('## 三、每只标的详细触发线')
    lines.append('')
    for r in records:
        lines.append(f"### {r['name']} ({r['code']})｜现价 {r['price']}｜方向 {r['direction']}｜评分 {r['score_pct']}")
        lines.append(f"- 市场信号: {r.get('market_signal') or '—'}")
        # 走势描述（与 HTML 走势解读一致）
        trend_desc = []
        if r.get('trend'):
            trend_desc.append(f"趋势{r['trend']}")
        if r.get('duo_kong'):
            trend_desc.append(f"多空线{r['duo_kong']}")
        if r.get('wave_count') is not None:
            trend_desc.append(f"近60日{r['wave_count']}波/当前第{r.get('current_wave', 0)}浪[{r.get('wave_state', '')}]")
        if r.get('wave_signal'):
            trend_desc.append(r['wave_signal'])
        if r.get('divergence') and r['divergence'] != '无背离':
            trend_desc.append(r['divergence'])
        if r.get('rs_20') is not None:
            trend_desc.append(f"RS vs {r.get('rs_ref') or '大盘'}: 5日{r.get('rs_5', 0):+.1f}% 20日{r['rs_20']:+.1f}% {r.get('rs_signal') or ''}")
        if trend_desc:
            lines.append(f"- 走势解读: {'；'.join(trend_desc)}")
        # 筹码分布解读（日线 + 60分钟）
        if r.get('chip_winner_pct') is not None:
            lines.append(f"- 筹码分布: 获利盘{r['chip_winner_pct']}% 成本峰{r.get('chip_cost_peak', 0)} "
                         f"平均成本{r.get('chip_cost_avg', 0)} 集中度{r.get('chip_concentration', 0)}% "
                         f"底部锁定{r.get('chip_bottom_lock', 0)}%")
            if r.get('chip_note'):
                lines.append(f"  {r['chip_note']}")
            if r.get('chip_60_note'):
                lines.append(f"  ⏱ {r['chip_60_note']}")
            # 底部锁定+放量突破 组合信号单独标注
            sig = chip_breakout_signal(r)
            if sig:
                lines.append(f"  {sig}")
        lines.append(f"- 圆桌共识: {r.get('consensus') or '—'}")
        lines.append(f"- 数据口径: {r.get('data_note')}")
        # P0-2 规则2前置 + β 敞口标注（2026-08-19 专家复盘教训）
        if r.get('rule2_note'):
            lines.append(f"- {r['rule2_note']}")
        if r.get('beta_level'):
            hint = '（创业板/科创板高β标的在系统日默认降档，不新开仓）' if r['beta_level'] == '高β' else ''
            lines.append(f"- β敞口: {r['beta_level']}{hint}")
        # P0-4 高β波动警示（2026-08-21 专项分析落地：高β+中性 → 波动警示）
        if r.get('beta_warning'):
            lines.append(f"- {r['beta_warning']}")
        # P2-10 评分带边缘告警（2026-09-30）：中性窄窗口内方向为弱方向，确认开关先于动作
        if r.get('edge_warning'):
            lines.append(f"- ⚠️ {r['edge_warning']}")
        lines.append(f"- 确认开关（观察台）:")
        lines.extend(format_obs(r.get('observation')))
        # P0-1 预测可执行化（2026-08-25 新增）：触发条件/目标位/失效条件
        ep = r.get('exec_plan') or {}
        if ep:
            if ep.get('trigger'):
                lines.append(f"- 🎯 触发条件: {ep['trigger']}")
            tgt = []
            if ep.get('target_up'):
                tgt.append(f"上方 {ep['target_up']}")
            if ep.get('target_down'):
                tgt.append(f"下方 {ep['target_down']}")
            if tgt:
                lines.append(f"- 📍 目标位: {'｜'.join(tgt)}")
            if ep.get('invalidation'):
                lines.append(f"- 🚫 失效条件: {ep['invalidation']}")
            if ep.get('stop'):
                lines.append(f"- 🛑 止损/失效参考位: {ep['stop']}")
        # 自定义观察台（手动维护的额外触发线）
        cw = cw_map.get(r.get('code'))
        if cw:
            for item in cw:
                # 配置内容可能自带 emoji，仅补空格对齐，避免双 emoji
                prefix = '' if item.strip().startswith(('📌', '🔴', '🟢', '🔵')) else '📌 '
                lines.append(f"  {prefix}{item}")
        lines.append(f"- 失效条件:")
        if r.get('invalidations'):
            for i in r['invalidations'][:3]:
                lines.append(f'  - {i}')
        else:
            lines.append('  - (无)')
        if r.get('timing_note'):
            lines.append(f"- ⏱ {r['timing_note']}")
        if r.get('trap'):
            lines.append(f"- ⚠️ {r['trap']}")
        lines.append('')
    lines.append('*本基准为盘前自动生成，规则写死可证伪：评分≥55看多、≤45看空、中间中性。*')
    return '\n'.join(lines)


def fetch_index_env():
    """获取大盘指数当日表现，用于环境突变归因
    2026-09-30 增强(盘中对照P0改造)：补 open/high/amount_yi 字段——
    amount_yi(指数成交额,亿)用于两市量能外推(规则A·无增量检测)，
    open/high 用于脉冲回吐检测的数据基础。原 price/pct 消费方不受影响。"""
    env = {}
    for name, code in [('上证指数', 'sh000001'), ('深证成指', 'sz399001'),
                       ('创业板指', 'sz399006'), ('科创50', 'sh000688')]:
        try:
            q = sq.fetch_quote_tencent(code)
            if q and q.get('price'):
                env[name] = {'price': q['price'], 'pct': q.get('pct', 0),
                             'open': q.get('open'), 'high': q.get('high'),
                             'amount_yi': q.get('amount')}
        except Exception:
            pass
    return env


# ── 盘中对照 P0 改造(2026-09-30)：环境前置否决检测 + 持仓映射 ──
# 来源：实盘操作逻辑复盘——出清持仓(环境证伪先于个股价格确认)、
# 跌停买入(实时资金行为触发)。系统缺口：盘中对照缺"持仓映射"段、
# 无环境级否决检测。检测不到的数据(分钟级盘口大单)诚实标注盲区。
INTRADAY_STATE_DIR = os.path.join(REVIEW_DIR, 'intraday_state')
MARKET_AMT_HIST = os.path.join(REVIEW_DIR, 'market_amount_history.json')


def _load_intraday_state(date):
    """读当日上一次盘中对照快照（脉冲检测需要≥2次对照）"""
    p = os.path.join(INTRADAY_STATE_DIR, f'{date}.json')
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _save_intraday_state(date, payload):
    os.makedirs(INTRADAY_STATE_DIR, exist_ok=True)
    p = os.path.join(INTRADAY_STATE_DIR, f'{date}.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def _load_market_amount_hist():
    try:
        with open(MARKET_AMT_HIST, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def detect_env_rejection(prev_state, env, rows, now_dt):
    """规则A·环境前置否决检测（2026-09-30 新增）
    两条件：
      ① 主线脉冲失败——与上一快照比，创业板指/科创50 或 盘前看多/中性标的
         回吐 ≥2pct（上一快照时间须≤10:30，即脉冲窗口内发生的回落）
      ② 无增量——按时间进度外推全日两市成交额 < 上一交易日基线×0.9
    两者齐备 → 环境证伪：修复类假设（盘前看多/中性）直接按失败处理
    （出场/不建仓），不等个股价格确认。检测不到的数据诚实标注盲区。
    返回 dict: {pulse_failures, pool_failures, volume_flag, market_amount_yi,
                verdict, notes}"""
    res = {'pulse_failures': [], 'pool_failures': [], 'volume_flag': '',
           'market_amount_yi': None, 'verdict': '', 'notes': []}
    hm = int(now_dt.strftime('%H%M'))
    minutes = now_dt.hour * 60 + now_dt.minute
    # ① 脉冲失败检测（需上一快照，且上一快照在脉冲窗口内）
    if prev_state and isinstance(prev_state.get('hm'), str) and prev_state['hm'] <= '1030':
        for name in ('创业板指', '科创50'):
            prev_pct = (prev_state.get('env') or {}).get(name, {}).get('pct')
            cur = env.get(name)
            if isinstance(prev_pct, (int, float)) and cur and isinstance(cur.get('pct'), (int, float)):
                if cur['pct'] <= prev_pct - 2.0:
                    res['pulse_failures'].append(
                        f"{name} 脉冲失败: 上一快照({prev_state['hm'][:2]}:{prev_state['hm'][2:]}) {prev_pct:+.2f}% → 现 {cur['pct']:+.2f}%（回吐 {prev_pct - cur['pct']:.1f}pct）")
        n_pool = 0
        for r in rows:
            if n_pool >= 3:
                break
            if r['pre_dir'] not in ('看多', '中性'):
                continue
            prev_pct = (prev_state.get('pool_pcts') or {}).get(r['code'])
            pct = r.get('pct')
            if isinstance(prev_pct, (int, float)) and isinstance(pct, (int, float)) and pct <= prev_pct - 2.0:
                res['pool_failures'].append(f"{r['name']} {prev_pct:+.2f}%→{pct:+.2f}%")
                n_pool += 1
    elif not prev_state:
        res['notes'].append('脉冲检测需当日≥2次盘中对照（本次为首采，仅落盘快照）')
    else:
        res['notes'].append(f"上一快照({prev_state.get('hm')})晚于10:30脉冲窗口，脉冲检测跳过")
    # ② 无增量检测（两市成交额按时间进度外推 vs 上一交易日基线）
    sh = env.get('上证指数') or {}
    sz = env.get('深证成指') or {}
    a1, a2 = sh.get('amount_yi'), sz.get('amount_yi')
    if isinstance(a1, (int, float)) and isinstance(a2, (int, float)) and a1 > 0 and a2 > 0:
        amount = a1 + a2
        res['market_amount_yi'] = amount
        progress = max(0.15, min(1.0, minutes / 240.0))
        today_est = amount / progress
        hist = _load_market_amount_hist()
        today = now_dt.strftime('%Y-%m-%d')
        past = {d: v for d, v in hist.items() if d < today}
        if past:
            base_date = max(past)
            baseline = past[base_date]
            if today_est < baseline * 0.9:
                res['volume_flag'] = f'无增量轨迹（外推全日≈{today_est:,.0f}亿 < 基线{baseline:,.0f}亿×0.9，基线日 {base_date}）'
            elif today_est > baseline * 1.05:
                res['volume_flag'] = f'放量轨迹（外推全日≈{today_est:,.0f}亿 > 基线{baseline:,.0f}亿×1.05）'
            else:
                res['volume_flag'] = f'平量轨迹（外推全日≈{today_est:,.0f}亿 vs 基线{baseline:,.0f}亿）'
        else:
            res['notes'].append('无增量检测缺基线（market_amount_history.json 无历史两市额），本次落盘后次日可用')
    else:
        res['notes'].append('两市成交额获取失败，无增量检测本轮跳过（盲区）')
    # 裁决
    has_pulse = bool(res['pulse_failures'])
    no_volume = res['volume_flag'].startswith('无增量')
    if has_pulse and no_volume:
        res['verdict'] = '⛔ 环境前置否决触发（规则A）：脉冲失败+无增量——修复类假设（盘前看多/中性标的）直接按失败处理：出场/不建仓，无需等待个股价格确认'
    elif has_pulse:
        res['verdict'] = '⚠️ 部分触发：主线脉冲失败但量能未确认无增量——修复类假设降级观察，建仓冻结，持仓者收紧止损'
    elif no_volume:
        res['verdict'] = '⚠️ 部分触发：量能无增量但未见脉冲失败——不开新仓，存量持仓按既有开关执行'
    else:
        res['verdict'] = '⚪ 环境未见证伪（脉冲/量能均未触发）——按预演轨道执行'
    return res


def render_env_rejection_section(flags):
    """环境前置否决检测结果 → md 段落"""
    lines = ['', '## 二c、环境前置否决检测（规则A）', '']
    lines.append(f'**裁决：{flags["verdict"]}**')
    lines.append('')
    for f_ in flags['pulse_failures']:
        lines.append(f'- 🔻 {f_}')
    for f_ in flags['pool_failures']:
        lines.append(f'- 🔻 个股同步回吐: {f_}')
    if flags['volume_flag']:
        lines.append(f'- 💧 量能: {flags["volume_flag"]}')
    for n in flags['notes']:
        lines.append(f'- ℹ️ {n}')
    lines.append('- 🕳️ 盲区声明: 分钟级盘口大单/封单信号本系统不覆盖（数据源限制），依赖人工盘口观察')
    lines.append('')
    return lines


def render_position_mapping(rows, flags):
    """持仓映射段（2026-09-30 新增）：每条观察必须回答"对持仓意味着什么"
    数据源: sim_account.json 实际持仓 + 盘前 rows 的方向映射"""
    lines = ['', '## 二d、持仓映射（观察→动作）', '']
    full_reject = flags['verdict'].startswith('⛔')
    part_reject = flags['verdict'].startswith('⚠️')
    # 1) sim 实际持仓
    try:
        acc = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          'data', 'sim_account.json'), encoding='utf-8'))
        positions = acc.get('positions', {})
        if positions:
            lines.append(f'**sim 持仓（{len(positions)} 只）**：')
            for code, p in positions.items():
                lines.append(f"- {p.get('name', code)}({code}) {p.get('shares')}股："
                             f"止损 {p.get('stop', '—')} 生效中；"
                             + ('环境否决触发——评估是否为修复类持仓，是则按规则A出场' if full_reject
                                else '今日结构下按既有开关执行，对照时点无新增动作'))
        else:
            lines.append('- sim 无持仓')
    except Exception as e:
        lines.append(f'- sim 持仓读取失败（盲区）: {e}')
    lines.append('')
    # 2) 盘前方向 → 动作映射
    bulls = [r for r in rows if r['pre_dir'] == '看多'][:5]
    bears = [r for r in rows if r['pre_dir'] == '看空'][:5]
    neus = [r for r in rows if r['pre_dir'] == '中性'][:5]
    if full_reject:
        lines.append(f'**盘前看多（修复类假设按失败处理，不建仓）**：' + ('、'.join(r['name'] for r in bulls) if bulls else '无'))
        lines.append(f'**盘前中性（冻结建仓，等环境修复信号）**：' + ('、'.join(r['name'] for r in neus) if neus else '无'))
    elif part_reject:
        lines.append(f'**盘前看多（降级观察，建仓冻结，收紧止损）**：' + ('、'.join(r['name'] for r in bulls) if bulls else '无'))
        lines.append(f'**盘前中性（暂缓，等脉冲/量能确认）**：' + ('、'.join(r['name'] for r in neus) if neus else '无'))
    else:
        lines.append(f'**盘前看多（按预演轨道，脉冲/量能确认后执行）**：' + ('、'.join(r['name'] for r in bulls) if bulls else '无'))
        lines.append(f'**盘前中性（等方向选择）**：' + ('、'.join(r['name'] for r in neus) if neus else '无'))
    if bears:
        lines.append(f'**盘前看空（反弹即兑现窗口，规则9口径）**：' + ('、'.join(r['name'] for r in bears)))
    lines.append('')
    return lines


def judge_record(pre, cur, env):
    """单只标的判定：符合 / 方向符合 / 偏差（方向反）/ 部分偏差（方向未兑现）

    规则（写死可证伪，2026-08-21 修订）：
      - 方向命中：看多 & 实际>0 / 看空 & 实际<0 → 同号即"方向符合"（不论幅度，修复 A类误判）
      - 幅度命中：方向符合 且 |pct| >= 1% → "完全符合"（✅）
      - 方向反：看多 & 实际<0 / 看空 & 实际>0 → 方向偏差（❌）
      - 中性 & |pct|<1% → 符合；中性 & |pct|>=1% → 部分偏差
    """
    if 'error' in cur:
        return '无法判定', '收盘数据获取失败'
    if not pre or 'error' in pre:
        return '无法判定', '无盘前基准'
    pred = pre.get('direction', '中性')
    pct = cur.get('pct')
    if pct is None:
        return '无法判定', '无实际涨跌幅'

    if pred == '中性':
        if abs(pct) < RET_BIG:
            return '✅ 符合', '中性判断正确（小幅波动）'
        return '⚠️ 部分偏差', f'中性判断但实际 {pct:+.2f}%'
    # 方向命中：同号（不看幅度阈值）
    if (pred == '看多' and pct > 0) or (pred == '看空' and pct < 0):
        if abs(pct) >= RET_BIG:
            return '✅ 符合', f'{pred} & 实际 {pct:+.2f}%（幅度达 1%+）'
        return '✅ 方向符合', f'{pred} & 实际 {pct:+.2f}%（方向对，幅度未达 1%+）'
    # 方向反
    if (pred == '看多' and pct < 0) or (pred == '看空' and pct > 0):
        return '❌ 方向偏差', f'{pred}但实际 {pct:+.2f}%'
    return '⚠️ 部分偏差', f'{pred}但实际 {pct:+.2f}%（方向未兑现）'


def judge_exec_plan(pre, cur):
    """预测可执行化对照（2026-08-25 P0-1 新增）：用盘前 exec_plan 逐条对照收盘
    判定（写死可证伪）：
      - 触发：收盘价 满足 触发条件（看多→≥压力/目标；看空→≤支撑/目标；中性→突破或跌破）
      - 达标：收盘价 到达 目标位（target_up 且 现价≥ / target_down 且 现价≤）
      - 失效：收盘价 触发 失效条件（看多→≤止损位；看空→≥止损位）
    返回 dict: {'triggered': bool, 'hit_target': bool, 'invalidated': bool,
                'detail': str, 'verdict': '触发并达标|触发未达标|未触发|已失效'}
    """
    plan = (pre or {}).get('exec_plan') or {}
    price = cur.get('price')
    if not plan or not plan.get('trigger') or not isinstance(price, (int, float)):
        return {'triggered': False, 'hit_target': False, 'invalidated': False,
                'detail': '无结构化预测计划或缺少有效收盘价', 'verdict': '—'}
    pred = pre.get('direction', '中性')
    tu = plan.get('target_up')
    td = plan.get('target_down')
    stop = plan.get('stop')
    # 目标位双缺失（支撑/压力位均缺 → target 全 None）：无法对照，避免 {None:.2f} 格式化崩溃
    if tu is None and td is None:
        return {'triggered': False, 'hit_target': False, 'invalidated': False,
                'detail': f'现价{price:.2f} 无有效目标位（支撑/压力缺失），无法对照', 'verdict': '未触发'}

    # 触发判定 — P1-4 修复(2026-09-22体检)：原用收盘价判触发，与达标判定条件完全
    # 相同（hit 恒等 triggered），'触发未达标'分支永不可达。触发=盘中触及（当日
    # high/low），达标=收盘价站住——两者语义分离，五档判定恢复完整。
    day_high = cur.get('high') or price
    day_low = cur.get('low') or price
    triggered = False
    if pred == '看多' and tu and day_high >= tu:
        triggered = True
    elif pred == '看空' and td and day_low <= td:
        triggered = True
    elif pred == '中性':
        if (tu and day_high >= tu) or (td and day_low <= td):
            triggered = True
    # 达标判定（中性：突破上沿或跌破下沿即视为到达目标）
    hit = False
    if pred == '看多' and tu and price >= tu:
        hit = True
    elif pred == '看空' and td and price <= td:
        hit = True
    elif pred == '中性':
        if (tu and price >= tu) or (td and price <= td):
            hit = True
    # 失效判定
    invalidated = False
    if pred == '看多' and stop and price <= stop:
        invalidated = True
    elif pred == '看空' and stop and price >= stop:
        invalidated = True

    if invalidated:
        verdict = '已失效'
        detail = f'现价{price:.2f} 触发失效条件（止损位 {stop:.2f}）'
    elif triggered and hit:
        verdict = '触发并达标'
        # 2026-09-01 修复：原文案固定 {"上" if tu else "下"}方 {tu or td}——中性双向目标
        # 实际命中下方（鼎龙 09-01：69.37 跌破 69.47）时误显示"上方 81.15"。按实际命中的目标位标注
        if tu and price >= tu:
            _hit_desc = f'上方 {tu:.2f}'
        elif td and price <= td:
            _hit_desc = f'下方 {td:.2f}'
        else:
            _hit_desc = f'{"上" if tu else "下"}方 {tu or td:.2f}'
        detail = f'现价{price:.2f} 到达目标位（{_hit_desc}）'
    elif triggered:
        verdict = '触发未达标'
        detail = f'现价{price:.2f} 触发方向但未达目标位'
    else:
        # 2026-08-31 新增：结论证伪豁免（题材日/单日大涨口径）
        # 原问题：网宿主盘前看空、实际 +6.86%，方向已被大幅证伪，但因收盘 16.36
        # 未达失效线 17.07 仍判"未触发"——失效条件双确认（破线）在题材日过于苛刻。
        # 豁免口径：未破线，但实际走势与盘前预测反向且幅度 ≥3pct → 结论已被市场证伪，
        # 标记"已证伪(豁免)"供复盘归因，与"已失效(破线)"区分。
        pct = cur.get('pct') or 0
        disproven = (
            (pred == '看空' and pct >= 3)
            or (pred == '看多' and pct <= -3)
            or (pred == '中性' and abs(pct) >= 5)
        )
        if disproven:
            verdict = '已证伪(豁免)'
            detail = (f'现价{price:.2f} 未达失效线，但实际 {pct:+.2f}% 与盘前"{pred}"反向 '
                      f'≥3pct——结论已被市场证伪（未破线的方向性证伪，题材/消息日常见）')
        else:
            verdict = '未触发'
            detail = f'现价{price:.2f} 未触发（目标 {"上" if tu else "下"}方 {tu or td:.2f}）'
    return {'triggered': triggered, 'hit_target': hit, 'invalidated': invalidated,
            'detail': detail, 'verdict': verdict}


def attribute_bias(pre, cur, env, verdict):
    """偏差归因：环境突变 / 数据问题 / 框架问题
    2026-08-31 修复：原逻辑只判"方向与大盘一致+|大盘|>=0.5%"即归因环境突变，
    未检查相对强度——个股跑赢大盘 6pct 的题材驱动（如网宿 +6.86% vs 大盘 +0.87%）
    被误标"环境突变"。现加超额收益判据：同向但跑赢/跑输大盘 >3pct → 个股驱动（框架问题）
    """
    if verdict != '❌ 方向偏差':
        return '—'
    pct = cur.get('pct') or 0
    market_pcts = [e['pct'] for e in env.values() if e.get('pct') is not None]
    if market_pcts:
        avg_mkt = sum(market_pcts) / len(market_pcts)
        if pct * avg_mkt > 0 and abs(avg_mkt) >= 0.5:
            excess = pct - avg_mkt
            if excess > 3:
                return (f'框架问题（跑赢大盘{excess:+.1f}pct，个股驱动——'
                        f'疑似盘前框架未覆盖的消息/题材催化）')
            if excess < -3:
                return (f'框架问题（跑输大盘{excess:+.1f}pct，个股驱动——'
                        f'疑似个股利空/盘前框架未覆盖的负面消息）')
            return f'环境突变(大盘{avg_mkt:+.2f}%)'
    # 数据问题：盘前口径标注（资金面昨日）
    if pre and pre.get('phase') == 'premarket':
        return '数据口径(盘前资金面为昨日)或框架问题'
    return '框架问题'


def compute_premarket_perf(rows, capital=100000.0):
    """盘前预测 → 收盘实际 的模拟交易绩效（2026-08-25 借鉴 khQuant sim_trade 新增）
    对每只标的的盘前方向做模拟交易（含滑点/佣金/印花税/过户费，T+1 约束）：
      - 看多：盘前价买入 → 收盘价卖出（持有当日，T+0 模拟用于度量方向）
      - 看空：做空收益口径 = (盘前价-收盘价)/盘前价 - 成本（仅度量判断正确性）
      - 中性：不计（无方向）
    返回绩效三件套 dict（盈亏比/最大回撤/夏普），供收盘复盘报告渲染。
    """
    per = {'n': 0, 'win_n': 0, 'loss_n': 0, 'total_profit': 0.0, 'total_loss': 0.0,
           'returns': [], 'equity': [], 'max_drawdown': 0.0, 'sharpe': None,
           'avg_return': 0.0, 'pl_ratio': None, 'illusion': ''}
    try:
        from sim_trade import SimAccount, round_trip_cost_rate, sharpe_ratio
    except Exception as e:
        print(f'  ⚠️ sim_trade 导入失败: {e}')
        return per

    rt_cost_rate = round_trip_cost_rate()
    equity = 1.0
    logger.info('[compute_premarket_perf] 开始 样本=%d 资本=%d 双向成本率=%.4f',
                len(rows), capital, rt_cost_rate)
    for r in rows:
        pred = str(r.get('pre_dir', '中性'))
        if pred not in ('看多', '看空'):
            continue
        entry = r.get('pre_price')
        exit_ = r.get('price')
        if (not isinstance(entry, (int, float)) or not isinstance(exit_, (int, float))
                or entry <= 0 or exit_ <= 0):
            logger.warning('[compute_premarket_perf] %s 跳过: pred=%s entry=%r exit=%r',
                           r.get('code'), pred, entry, exit_)
            continue
        acc = SimAccount(init_capital=capital, trade_cost={'t0_mode': True})
        date_str = str(r.get('date', ''))[:10] or datetime.date.today().strftime('%Y-%m-%d')
        acc.new_day(date_str)
        # P2-9(2026-09-30)：股数按价格自适应（整百）——原固定1000股使高价股
        #（如三环118元×1000=11.8万>10万本金）必然"资金不足"被剔除，绩效样本系统性偏向低价股
        shares = int(capital * 0.95 / float(entry) // 100) * 100
        if shares < 100:
            logger.warning('[compute_premarket_perf] %s 跳过: 单手%s元超单笔预算%s',
                           r['code'], round(float(entry) * 100, 2), capital)
            continue
        if pred == '看多':
            b = acc.execute(r['code'], 'buy', float(entry), shares, date_str=date_str)
            if not b['filled']:
                logger.warning('[compute_premarket_perf] %s 买入失败跳过: %s', r['code'], b['reason'])
                continue
            s = acc.execute(r['code'], 'sell', float(exit_), shares, date_str=date_str)
            if s['filled']:
                # 真实含成本收益（原 P0 bug：漏 trade_cost 导致偏乐观）
                buy_total = b['actual_price'] * shares + b['trade_cost']
                sell_net = s['actual_price'] * shares - s['trade_cost']
                ret = (sell_net - buy_total) / buy_total * 100
            else:
                ret = (float(exit_) - b['actual_price']) / b['actual_price'] * 100
                logger.warning('[compute_premarket_perf] %s 卖出失败兜底走无成本口径: %s',
                               r['code'], s['reason'])
        else:  # 看空：做空收益口径，扣双向成本率（与看多对称，原硬编码 0.2%）
            ret = (float(entry) - float(exit_)) / float(entry) * 100 - rt_cost_rate * 100
        per['n'] += 1
        per['returns'].append(ret)
        # 全仓滚动口径：pnl 按滚动资金算，与累乘净值口径一致
        pnl = equity * ret / 100
        equity *= (1 + ret / 100)
        per['equity'].append(round(equity, 4))
        logger.debug('[compute_premarket_perf] %s pred=%s entry=%s exit=%s ret=%.2f%% pnl=%.2f equity=%.4f',
                     r.get('code'), pred, entry, exit_, ret, pnl, equity)
        if ret > 0:
            per['win_n'] += 1
            per['total_profit'] += pnl
        else:
            per['loss_n'] += 1
            per['total_loss'] += -pnl

    if per['n'] == 0:
        logger.info('[compute_premarket_perf] 无有效样本')
        return per
    per['avg_return'] = round(sum(per['returns']) / per['n'], 2)
    if per['total_loss'] > 0:
        per['pl_ratio'] = round(per['total_profit'] / per['total_loss'], 2)
        if per['win_n'] >= per['n'] / 2 and per['total_profit'] < per['total_loss']:
            per['illusion'] = ('⚠️ 胜率幻觉：胜率看似及格但赔率加权净值为负——错误集中于高赔率日，'
                               '按信号操作实际亏损，须用赔率加权记分')
    else:
        per['pl_ratio'] = None
    # 最大回撤（净值曲线 cummax）
    peak = per['equity'][0]
    mdd = 0.0
    for v in per['equity']:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > mdd:
            mdd = dd
    per['max_drawdown'] = round(mdd, 2)
    # 夏普（日频近似：单日持有，年化 250 期，统一走 sim_trade.sharpe_ratio，减无风险利率）
    per['sharpe'] = sharpe_ratio(per['returns'], periods_per_year=250)
    logger.info('[compute_premarket_perf] 完成 n=%d 胜率=%.1f%% 均收益=%.2f%% 盈亏比=%s 净值=%.4f 回撤=%.2f%% 夏普=%s',
                per['n'], per['win_n'] / per['n'] * 100, per['avg_return'],
                per['pl_ratio'], per['net_value'] if 'net_value' in per else round(equity, 4),
                per['max_drawdown'], per['sharpe'])
    return per


def render_perf_section(per):
    """渲染盘前预测绩效段（收盘复盘报告附录）"""
    lines = ['', '## 六、盘前预测绩效评估（模拟撮合）', '',
             '- 口径: 看多=盘前价买入→收盘价卖出(含滑点/佣金/印花税/过户费)；'
             '看空=做空收益口径(仅度量判断)；中性不计。虚拟本金10万/笔',
             f'- 有方向样本: {per["n"]} 笔'
             + (f'（{per["win_n"]}盈/{per["loss_n"]}亏，胜率 {per["win_n"]/per["n"]*100:.1f}%）'
                if per['n'] else '')]
    if per['n']:
        lines += [f'- 平均收益: {per["avg_return"]:+.2f}%',
                  f'- 盈亏比(总盈利/总亏损): {per["pl_ratio"] if per["pl_ratio"] is not None else "N/A（无亏损笔）"}',
                  f'- 最大回撤: {per["max_drawdown"]:.2f}%',
                  f'- 夏普(年化): {per["sharpe"] if per["sharpe"] is not None else "N/A（样本<3）"}']
        if per.get('illusion'):
            lines.append(f'- {per["illusion"]}')
        if per['n'] < 5:
            lines.append('- ⚠️ 样本<5，绩效指标仅供参考，继续积累后再采信')
    else:
        lines.append('- 今日无有方向预测（全部中性/数据缺失），绩效评估跳过')
    lines.append('')
    return '\n'.join(lines)


def render_exec_section(rows, heading='## 二b、预测可执行化对照（触发/达标/失效）'):
    """渲染 exec_plan 可执行化对照段（收盘/盘中对照共用，2026-08-28 抽出复用）"""
    lines = [heading, '']
    has_exec = any(r.get('exec_verdict') and r['exec_verdict'] != '—' for r in rows)
    if has_exec:
        lines.append('| 标的 | 盘前方向 | 触发条件 | 目标位 | 失效条件 | 现价 | 对照判定 |')
        lines.append('|---|---|---|---|---|---|---|')
        for r in rows:
            ep = r.get('exec_plan') or {}
            trigger = ep.get('trigger', '—')
            tgt = '｜'.join(filter(None, [
                f"上{ep['target_up']}" if ep.get('target_up') else '',
                f"下{ep['target_down']}" if ep.get('target_down') else '']))
            inv = ep.get('invalidation', '—')
            ev = r.get('exec_verdict', '—')
            icon = {'触发并达标': '🟢', '触发未达标': '🟡', '未触发': '⚪', '已失效': '🔴',
                    '已证伪(豁免)': '🟠'}.get(ev, '—')
            lines.append(f"| {r['name']} | {r['pre_dir']} | {trigger} | {tgt or '—'} | "
                         f"{inv} | {r['price']} | {icon} {ev} |")
        lines.append('')
        # 明细
        for r in rows:
            if r.get('exec_detail'):
                lines.append(f"- **{r['name']}**（{r['exec_verdict']}）: {r['exec_detail']}")
        lines.append('')
    else:
        lines.append('- 盘前基准未生成结构化预测计划（exec_plan），跳过可执行化对照。')
        lines.append('')
    return lines


def render_close_md(rows, env, date, perf=None):
    """渲染收盘复盘报告 MD"""
    lines = []
    lines.append(f'# {date} 收盘复盘报告（预测 vs 实际）')
    lines.append('')
    lines.append(f'> 复盘时间: {now_iso()}（收盘后）')
    lines.append(f'> 对照基准: `{date}盘前预测基准.json`（盘前自动留档）')
    lines.append(f'> 参数快照: 方向阈值 看多≥{DIR_BULL}/看空≤{DIR_BEAR} ｜ 涨跌判定 |pct|≥{RET_BIG}%'
                 f' ｜ 对照池 {len(rows)} 只')
    lines.append('')
    lines.append('## 一、今日大盘环境（归因依据）')
    lines.append('')
    if env:
        lines.append('| 指数 | 收盘 | 涨跌 |')
        lines.append('|---|---|---|')
        for name, e in env.items():
            lines.append(f'| {name} | {e["price"]:.2f} | {e["pct"]:+.2f}% |')
    else:
        lines.append('- （大盘指数获取失败）')
    lines.append('')
    lines.append('## 二、预测 vs 实际 逐股对照')
    lines.append('')
    lines.append('| 标的 | 盘前方向 | 盘前评分 | 现价 | 实际涨跌 | 判定 | 归因 |')
    lines.append('|---|---|---|---|---|---|---|')
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['pre_dir']} | {r['pre_score']} | {r['price']} | "
            f"{r['pct']:+.2f}% | {r['verdict']} | {r['attribution']} |"
        )
    lines.append('')
    # P0-1 预测可执行化对照（2026-08-25 新增：触发/达标/失效）
    lines += render_exec_section(rows)
    n_ok = sum(1 for r in rows if r['verdict'].startswith('✅'))
    n_bad = sum(1 for r in rows if r['verdict'].startswith('❌'))
    n_part = sum(1 for r in rows if r['verdict'].startswith('⚠️'))
    lines.append(f'**总判定**：{len(rows)} 只中 {n_ok} 符合、{n_bad} 方向偏差、{n_part} 部分偏差。')
    lines.append('')
    lines.append('## 三、偏差明细与归因')
    lines.append('')
    for r in rows:
        if not r['verdict'].startswith('✅'):
            lines.append(f"### {r['name']}｜{r['verdict']}")
            lines.append(f"- 盘前: {r['pre_dir']}（评分{r['pre_score']}）→ 实际 {r['pct']:+.2f}%")
            lines.append(f"- 判定理由: {r['reason']}")
            lines.append(f"- 归因: {r['attribution']}")
            lines.append('')
    # 底部锁定+放量突破 组合信号标注（收盘口径）
    lines.append('## 四、筹码组合信号（底部锁定+放量突破）')
    lines.append('')
    sig_found = False
    for r in rows:
        sig = chip_breakout_signal(r)
        if sig:
            sig_found = True
            lines.append(f"### {r['name']}｜{r['verdict']}")
            lines.append(f"- 筹码: 获利盘{r.get('chip_winner_pct', '—')}% 底部锁定{r.get('chip_bottom_lock', '—')}% "
                         f"成本峰{r.get('chip_cost_peak', '—')} 量比{r.get('vol_ratio', '—')}")
            lines.append(f"- {sig}")
            if r.get('chip_note'):
                lines.append(f"  {r['chip_note']}")
            lines.append('')
    if not sig_found:
        lines.append('- 今日无标的触发"底部锁定+放量突破"组合信号（需底部锁定≥40% + 量比≥1.5 + 现价≥成本峰）。')
        lines.append('')
    # 风格动量择强（2026-08-24 新增，借鉴风格轮动.py 的动量择强理念 + 系统规则）
    lines.append('## 五、风格动量择强（20日动量）')
    lines.append('')
    try:
        sr_text = style_rotation.build_report(20)
        lines.append(sr_text)
    except Exception as e:
        lines.append(f'- 风格动量获取失败: {e}')
        lines.append('')
    # 盘前预测绩效评估（2026-08-25 借鉴 khQuant sim_trade 新增：赔率加权度量预测质量）
    if perf is not None:
        lines.append(render_perf_section(perf))
    # 主线预注册核验（2026-09-22 新增：盘前预注册候选 vs 当日实际，闭环验证主线判断）
    try:
        score_md = os.path.join(REVIEW_DIR, 'mainline_prereg', f'score_{date}.md')
        if os.path.exists(score_md):
            lines.append('## 主线预注册核验（事前预注册 vs 当日实际）')
            lines.append('')
            with open(score_md, encoding='utf-8') as f:
                for ln in f.read().splitlines():
                    if not ln.startswith('#'):
                        lines.append(ln)
            lines.append('')
    except Exception:
        pass
    # P2-4 缓存磁盘回收（2026-09-22体检）：每日收盘时清理>7天过期缓存，防数千 .cache 无限增长
    try:
        import data_layer
        _n = data_layer.get_cache().prune(max_age_days=7, max_files=5000)
        if _n:
            lines.append(f"*缓存回收: 清理 {_n} 个过期缓存文件（P2-4 prune, >7天）*")
    except Exception:
        pass
    lines.append('*本报告由 daily_review.py --close 自动生成，判定规则写死可证伪（≥1%为涨跌）。*')
    return '\n'.join(lines)


def close_review(pool_names=None, no_cache=False, date=None):
    """收盘对照判定+归因：读盘前基准 → 重新取收盘数据 → 逐只判定 → 归因 → 生成报告
    P1-5 修复(2026-09-22体检)：date 可由 --date 指定（次日补跑昨日复盘）；
    当日基准缺失时回退到最近一份 盘前基准.json 并生成占位报告（原来直接 return，
    整天报告静默缺失）"""
    os.makedirs(REVIEW_DIR, exist_ok=True)
    # 架构核查修复(2026-09-22)：pending 单的 expire/stale 终结原依赖 sim.loop 尾段
    # 14:55-15:02 那唯一一次 evaluate——launchd 恰逢休眠/错过则当日单全天挂死。
    # close 任务（15:35）兜底再跑一次 evaluate，双保险收口当日委托单状态机。
    try:
        import sim_report as _sr
        _res = _sr.evaluate(quiet=True)
        for _r in _res:
            if '⏰' in _r or 'stale' in _r or '⛔' in _r:
                print(f'  [close兜底evaluate] {_r}')
    except Exception as e:
        print(f'⚠️ close兜底evaluate失败（不影响复盘主流程）: {e}')
    date = date or today_str()
    json_path = os.path.join(REVIEW_DIR, f'{date}盘前基准.json')
    # 架构修复(2026-09-24)：原 `date = fallback` 把报告文件名也绑到旧基准日期——
    # 09-24 复盘报告两次写进 09-22 文件名，09-22 原报告被覆盖丢失。
    # 报告文件名用运行日期(date)，基准数据用回退基准(fallback_date)分离。
    fallback_date = None
    if not os.path.exists(json_path):
        # 回退：找最近一份盘前基准（跨日补跑场景）
        import glob as _glob
        cands = sorted(_glob.glob(os.path.join(REVIEW_DIR, '*盘前基准.json')))
        if cands:
            fallback_date = os.path.basename(cands[-1]).replace('盘前基准.json', '')
            print(f'⚠️ 未找到 {date}盘前基准.json，回退到最近基准 {fallback_date}（报告仍按 {date} 命名）')
            json_path = cands[-1]
        else:
            print(f'❌ 未找到任何盘前基准.json，无法复盘。请先运行 --premarket')
            return
    with open(json_path, encoding='utf-8') as f:
        baseline = json.load(f)
    pre_records = {r['code']: r for r in baseline.get('records', []) if 'error' not in r}
    print(f'📂 读取盘前基准 {len(pre_records)} 只')

    # 收盘重新取数
    pool = [(r['name'], r['code']) for r in baseline.get('records', []) if 'error' not in r]
    if pool_names:
        resolved = resolve_pool(pool_names)
        pool = pool if not resolved else resolved
    print(f'📡 收盘重新取数 {len(pool)} 只...')
    env = fetch_index_env()
    # ── 2026-09-30审查修复：两市额基线双保险 ──
    # 基线原只挂在盘中对照≥14:57运行时写入；若当日该时段未跑（launchd休眠/无盘中对照），
    # 次日"无增量检测"将缺基线。收盘复盘(15:35 launchd)兜底写入，保证次日必有基线。
    # 守卫同盘中口径（≥14:57）：早于该时刻手动补跑时 amount 为半日值，写入会低估基线。
    try:
        _sh = env.get('上证指数') or {}
        _sz = env.get('深证成指') or {}
        _a1, _a2 = _sh.get('amount_yi'), _sz.get('amount_yi')
        if (isinstance(_a1, (int, float)) and isinstance(_a2, (int, float))
                and _a1 > 0 and _a2 > 0 and datetime.datetime.now().strftime('%H%M') >= '1457'):
            _hist = _load_market_amount_hist()
            if date not in _hist:  # 盘中14:57已写入则保留先值
                _hist[date] = round(_a1 + _a2, 0)
                with open(MARKET_AMT_HIST, 'w', encoding='utf-8') as _f:
                    json.dump(_hist, _f, ensure_ascii=False, indent=2)
                print(f'  💧 两市额基线已记录: {date} = {_hist[date]:,.0f}亿')
    except Exception as _e:
        print(f'⚠️ 两市额基线记录失败（不影响主流程）: {_e}')
    cur_records = {}
    for name, code in pool:
        try:
            r = analyze_one(name, code, no_cache, phase='close')
            cur_records[code] = r
            print(f"  {'✅' if 'error' not in r else '❌'} {name} 现价{r.get('price','-')} {r.get('pct','-')}%")
        except Exception as e:
            print(f'  ❌ {name} 收盘取数失败: {e}')
            cur_records[code] = {'name': name, 'code': code, 'error': str(e)}

    rows = []
    for code, pre in pre_records.items():
        cur = cur_records.get(code, {'name': pre.get('name', code), 'code': code, 'error': '收盘未取到'})
        verdict, reason = judge_record(pre, cur, env)
        attribution = attribute_bias(pre, cur, env, verdict)
        exec_judge = judge_exec_plan(pre, cur)  # P0-1 预测可执行化对照
        rows.append({
            'name': pre.get('name', code), 'code': code,
            'pre_dir': pre.get('direction', '—'),
            'pre_score': pre.get('score_pct', '—'),
            'pre_price': pre.get('price'),  # 盘前价（供绩效模拟）
            'price': cur.get('price', '—'),
            'pct': cur.get('pct', 0),
            'verdict': verdict, 'reason': reason, 'attribution': attribution,
            # P0-1 预测可执行化对照（触发/达标/失效）
            'exec_plan': pre.get('exec_plan') or {},  # 盘前结构化计划（供对照表渲染）
            'exec_verdict': exec_judge.get('verdict', '—'),
            'exec_detail': exec_judge.get('detail', ''),
            'exec_triggered': exec_judge.get('triggered', False),
            'exec_hit': exec_judge.get('hit_target', False),
            'exec_invalidated': exec_judge.get('invalidated', False),
            # 收盘筹码字段（供组合信号标注）
            'chip_bottom_lock': cur.get('chip_bottom_lock'),
            'chip_cost_peak': cur.get('chip_cost_peak'),
            'chip_winner_pct': cur.get('chip_winner_pct'),
            'chip_note': cur.get('chip_note', ''),
            'vol_ratio': cur.get('vol_ratio'),
        })

    # 盘前预测绩效评估（2026-08-25 借鉴 khQuant sim_trade：赔率加权度量预测质量）
    perf = compute_premarket_perf(rows)

    md_text = render_close_md(rows, env, date, perf)
    md_path = os.path.join(REVIEW_DIR, f'{date}收盘复盘报告_auto.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md_text)
    # 盘前绩效落盘（2026-08-25 新增：供 perf_trend.py --record 汇总绩效趋势）
    # 含 n/胜率/盈亏比/最大回撤/夏普/平均收益/胜率幻觉检测，与渲染口径一致
    perf_snapshot = dict(perf)
    perf_snapshot['date'] = date
    perf_snapshot['generated_at'] = now_iso()
    perf_path = os.path.join(REVIEW_DIR, f'{date}盘前绩效.json')
    with open(perf_path, 'w', encoding='utf-8') as f:
        json.dump(perf_snapshot, f, ensure_ascii=False, indent=2, default=str)
    print(f'✅ 盘前绩效已落盘: {perf_path}')
    print(f'\n✅ 收盘复盘已生成: {md_path}')

    # ── 尾部 smoke test（2026-08-25 CI 守护新增：收盘流程收尾自检）──
    # 快速校验本次产出物完整性（20秒内），失败即提示但不阻断报告
    try:
        smoke_errors = []
        if not os.path.exists(md_path) or os.path.getsize(md_path) < 500:
            smoke_errors.append('收盘复盘 MD 缺失或过小')
        if not os.path.exists(json_path) or os.path.getsize(json_path) < 200:
            smoke_errors.append('盘前基准 JSON 缺失或过小')
        if not os.path.exists(perf_path):
            smoke_errors.append('盘前绩效 JSON 缺失')
        if not rows:
            smoke_errors.append('对照 rows 为空（盘前基准可能无有效记录）')
        if smoke_errors:
            print(f"  ⚠️ [smoke] 产出物自检异常: {'；'.join(smoke_errors)}")
        else:
            print(f'  ✅ [smoke] 产出物自检通过（{len(rows)} 条对照，MD/JSON 完整）')
    except Exception as e:
        print(f'  ⚠️ [smoke] 自检执行异常: {e}')


def intraday_review(pool_names=None, no_cache=False, date=None):
    """盘中对照（2026-08-28 P0 落位：08-27 起临时脚本跑的流程正式收编）
    读当日盘前基准 → 重新取盘中实时行情 → 复用 judge_record/judge_exec_plan 判定
    → 生成 复盘/{date}盘中对照_{HHMM}.md（不写绩效 JSON，绩效以收盘 --close 为准）
    P1-5 修复(2026-09-22体检)：date 可由 --date 指定，跨日补跑不再静默缺报告
    """
    os.makedirs(REVIEW_DIR, exist_ok=True)
    date = date or today_str()
    json_path = os.path.join(REVIEW_DIR, f'{date}盘前基准.json')
    if not os.path.exists(json_path):
        print(f'⚠️ 未找到 {date}盘前基准.json，无法盘中对照。请先运行 --premarket')
        return
    with open(json_path, encoding='utf-8') as f:
        baseline = json.load(f)
    pre_records = {r['code']: r for r in baseline.get('records', []) if 'error' not in r}
    if not pre_records:
        print('⚠️ 盘前基准无有效记录，跳过盘中对照')
        return
    print(f'📂 读取盘前基准 {len(pre_records)} 只')

    pool = [(r['name'], r['code']) for r in baseline.get('records', []) if 'error' not in r]
    if pool_names:
        resolved = resolve_pool(pool_names)
        pool = pool if not resolved else resolved
    print(f'📡 盘中实时取数 {len(pool)} 只...')
    env = fetch_index_env()
    cur_records = {}
    for name, code in pool:
        try:
            r = analyze_one(name, code, no_cache, phase='close')
            cur_records[code] = r
            print(f"  {'✅' if 'error' not in r else '❌'} {name} 现价{r.get('price','-')} {r.get('pct','-')}%")
        except Exception as e:
            print(f'  ❌ {name} 盘中取数失败: {e}')
            cur_records[code] = {'name': name, 'code': code, 'error': str(e)}

    rows = []
    for code, pre in pre_records.items():
        cur = cur_records.get(code, {'name': pre.get('name', code), 'code': code, 'error': '盘中未取到'})
        verdict, reason = judge_record(pre, cur, env)
        attribution = attribute_bias(pre, cur, env, verdict)
        exec_judge = judge_exec_plan(pre, cur)
        rows.append({
            'name': pre.get('name', code), 'code': code,
            'pre_dir': pre.get('direction', '—'),
            'pre_score': pre.get('score_pct', '—'),
            'pre_price': pre.get('price'),
            'price': cur.get('price', '—'),
            'pct': cur.get('pct', 0),
            'verdict': verdict, 'reason': reason, 'attribution': attribution,
            'exec_plan': pre.get('exec_plan') or {},
            'exec_verdict': exec_judge.get('verdict', '—'),
            'exec_detail': exec_judge.get('exec_detail') or exec_judge.get('detail', ''),
        })

    now_dt = datetime.datetime.now()
    # ── P0 改造(2026-09-30)：环境前置否决检测 + 状态快照 + 持仓映射 ──
    # （now_dt 先于检测定义——初版接线曾把 detect 放在 now_dt 之前导致 NameError）
    prev_state = _load_intraday_state(date)
    flags = detect_env_rejection(prev_state, env, rows, now_dt)
    md_lines = []
    md_lines.append(f'# {date} 盘中对照（{now_dt.strftime("%H:%M")}）')
    md_lines.append('')
    progress = 0
    try:
        m, s = now_dt.hour, now_dt.minute
        minutes = m * 60 + s
        if minutes >= 570 and minutes <= 690:      # 09:30-11:30
            progress = (minutes - 570) // 120 + 1   # 1-2h
        elif minutes > 780 and minutes <= 900:     # 13:00-15:00
            progress = 2 + (minutes - 780) // 60 + 1  # 3-4h
    except Exception:
        pass
    md_lines.append(f'> 对照时间: {now_iso()}（交易日进度 {progress}/4h）')
    md_lines.append(f'> 对照基准: `{date}盘前基准.json`（{baseline.get("generated_at", "盘前")}）')
    md_lines.append('> 数据口径: 行情=盘中实时；资金面=上一交易日口径（新浪资金流晚间才更新当日）')
    md_lines.append('')
    md_lines.append('## 一、大盘环境（盘中实时）')
    md_lines.append('')
    if env:
        md_lines.append('| 指数 | 现价 | 涨跌 |')
        md_lines.append('|---|---|---|')
        for name, e in env.items():
            md_lines.append(f'| {name} | {e["price"]:.2f} | {e["pct"]:+.2f}% |')
    else:
        md_lines.append('- （大盘指数获取失败）')
    md_lines.append('')
    md_lines.append('## 二、预测 vs 盘中 逐股对照')
    md_lines.append('')
    md_lines.append('| 标的 | 盘前方向 | 盘前评分 | 现价 | 盘中涨跌 | 判定 | 归因 |')
    md_lines.append('|---|---|---|---|---|---|---|')
    for r in rows:
        pct = r['pct'] if isinstance(r['pct'], (int, float)) else 0
        md_lines.append(
            f"| {r['name']} | {r['pre_dir']} | {r['pre_score']} | {r['price']} | "
            f"{pct:+.2f}% | {r['verdict']} | {r['attribution']} |"
        )
    md_lines.append('')
    md_lines += render_exec_section(rows, heading='## 二b、预测可执行化对照（触发/达标/失效）')
    n_ok = sum(1 for r in rows if r['verdict'].startswith('✅'))
    n_bad = sum(1 for r in rows if r['verdict'].startswith('❌'))
    n_part = sum(1 for r in rows if r['verdict'].startswith('⚠️'))
    md_lines.append(f'**总判定**：{len(rows)} 只中 {n_ok} 符合、{n_bad} 方向偏差、{n_part} 部分偏差。')
    md_lines.append('')
    for r in rows:
        if not r['verdict'].startswith('✅'):
            pct = r['pct'] if isinstance(r['pct'], (int, float)) else 0
            md_lines.append(f"### {r['name']}｜{r['verdict']}")
            md_lines.append(f"- 盘前: {r['pre_dir']}（评分{r['pre_score']}）→ 盘中 {pct:+.2f}%")
            md_lines.append(f"- 判定理由: {r['reason']}")
            md_lines.append(f"- 归因: {r['attribution']}")
            md_lines.append('')
    # ── P0 改造(2026-09-30)：二c 环境前置否决 + 二d 持仓映射 ──
    md_lines += render_env_rejection_section(flags)
    md_lines += render_position_mapping(rows, flags)
    md_lines.append('*盘中对照由 daily_review.py --intraday 生成（复用收盘判定函数）；收盘后以 --close 正式复盘为准。*')

    md_path = os.path.join(REVIEW_DIR, f'{date}盘中对照_{now_dt.strftime("%H%M")}.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines))
    # 状态快照落盘（供下次对照做脉冲回吐对比）+ 两市额历史基线（收盘后写入）
    _save_intraday_state(date, {
        'hm': now_dt.strftime('%H%M'),
        'env': {k: {'pct': v.get('pct')} for k, v in env.items()},
        'pool_pcts': {r['code']: r.get('pct') for r in rows
                      if isinstance(r.get('pct'), (int, float))},
        'market_amount_yi': flags.get('market_amount_yi'),
        'verdict': flags.get('verdict', ''),
        'md': md_path,
    })
    hm_today = now_dt.strftime('%H%M')
    if hm_today >= '1457' and flags.get('market_amount_yi'):
        hist = _load_market_amount_hist()
        hist[date] = round(flags['market_amount_yi'], 0)
        with open(MARKET_AMT_HIST, 'w', encoding='utf-8') as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
    print(f'\n✅ 盘中对照已生成: {md_path}')


def premarket(pool_names, no_cache=False):
    os.makedirs(REVIEW_DIR, exist_ok=True)
    # P2-2(2026-09-22体检)：原依赖"mainline 09:10 任务先于本任务 09:28 跑完"的
    # 硬编码分钟差，launchd 错过/延迟时静默拿不到预注册——改为产物存在性检查，
    # 缺失时显式告警（方向判断退化由消费方知悉，而非隐性失联）
    _date = today_str()
    _ml_path = os.path.join(REVIEW_DIR, 'mainline_prereg', f'{_date}.json')
    if os.path.exists(_ml_path):
        print(f'✅ 主线预注册产物存在: {_ml_path}')
    else:
        print(f'⚠️ 主线预注册产物缺失({_ml_path})——mainline 09:10 任务未跑或失败，'
              f'今日方向判断退化为盘中实时涨幅榜（脆弱依赖告警，可手动补跑: '
              f'python3 mainline_prereg.py）')
    pool = resolve_pool(pool_names)
    print(f'📋 盘前分析 {len(pool)} 只标的: {[n for n, _ in pool]}')
    records = []
    for name, code in pool:
        try:
            r = analyze_one(name, code, no_cache, phase='premarket')
            records.append(r)
            print(f"  {'✅' if 'error' not in r else '❌'} {name} {r.get('direction','')} "
                  f"评分{r.get('score_pct','-')} 现价{r.get('price','-')}")
        except Exception as e:
            print(f'  ❌ {name} 分析失败: {e}')
            records.append({'name': name, 'code': code, 'error': str(e)})

    # 环境判断：取上证/创业板 market_signal（用第一只有效记录的代表）
    env_note = ''
    for r in records:
        if r.get('market_signal'):
            env_note = f'- 代表环境信号: {r["market_signal"]}'
            break

    # 指数分化检测（2026-08-21 五专家复盘教训）：创业板与上证显著分化时，
    # 高β标的（300/688）的个股看空信号会被 β 掩盖——提示盘前勿过度看空高β标的
    try:
        sh = sq.fetch_quote_tencent('sh000001')
        cyb = sq.fetch_quote_tencent('sz399006')
        sh_pct = sh.get('pct', 0) if sh else 0
        cyb_pct = cyb.get('pct', 0) if cyb else 0
        if sh_pct is not None and cyb_pct is not None and abs(cyb_pct - sh_pct) > 1.0:
            stronger = '创业板' if cyb_pct > sh_pct else '上证'
            weaker = '上证' if cyb_pct > sh_pct else '创业板'
            env_note += (f"\n- ⚠️ 指数分化提示（2026-08-21 新增）: 创业板{cyb_pct:+.2f}% vs 上证{sh_pct:+.2f}% "
                         f"（{stronger}强于{weaker}{abs(cyb_pct - sh_pct):.1f}pct）——高β标的（300/688）个股看空信号"
                         f"可能被 β 掩盖，盘前对高β标的不宜过度看空，以中性/观察为主")
    except Exception:
        pass

    # 港股时段口径标注（2026-09-30 P0-7）：竞价阶段引用 hkHSI 实为上一交易日收盘
    try:
        hk = sq.hk_market_session()
        env_note += f"\n- 港股口径: {hk['note']}"
    except Exception:
        pass

    # 政策事件扫描（2026-09-29 新增）：部委级政策盘中发布 → 提示纳入轻微博弈观察
    policy_hits = policy_event_scan()
    if policy_hits:
        print(f'📢 政策事件扫描: 命中 {len(policy_hits)} 条')
        _pol_lines = []
        for h in policy_hits[:5]:
            print(f"  📢 {h['time']} {h['ministry']}·{h['action']}: {h['title'][:60]}")
            _pol_lines.append(f"- 📢 政策事件（{h['time']} {h['ministry']}）: {h['title'][:80]}"
                              f" —— 若与持仓/股票池相关，纳入次日轻微博弈仓观察（≤1成）")
        if _pol_lines:
            env_note += ('\n' + '\n'.join(_pol_lines))

    date = today_str()
    md_text = render_premarket_md(records, env_note)
    # P0-2（2026-09-01）：env_warning 写入 JSON 顶层——Workbuddy 预填透传给专家的
    # 环境级纪律输入（收盘预填只贴复盘报告，专家此前拿不到降档预警）
    _env_warn_json, _env_warn_names_json = compute_env_warning(records)
    # 用 _auto 后缀避免覆盖用户手写的 {date}盘前预测基准.md
    md_path = os.path.join(REVIEW_DIR, f'{date}盘前预测基准_auto.md')
    json_path = os.path.join(REVIEW_DIR, f'{date}盘前基准.json')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md_text)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({'date': date, 'phase': 'premarket', 'generated_at': now_iso(),
                   'env_warning': _env_warn_json, 'env_warning_names': _env_warn_names_json,
                   'records': records}, f, ensure_ascii=False, indent=2, default=str)
    print(f'\n✅ 盘前基准已落盘:')
    print(f'  MD  : {md_path}')
    print(f'  JSON: {json_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='每日复盘自动化三件套')
    parser.add_argument('--premarket', action='store_true', help='盘前生成预测基准')
    parser.add_argument('--intraday', action='store_true', help='盘中对照（读盘前基准+盘中实时行情，生成盘中对照报告）')
    parser.add_argument('--close', action='store_true', help='收盘生成复盘对照')
    parser.add_argument('--date', default=None, help='指定日期YYYY-MM-DD（P1-5: 跨日补跑昨日复盘；默认今日）')
    parser.add_argument('--pool', nargs='+', help='股票池（默认: 关注池）')
    parser.add_argument('--no-cache', action='store_true', help='跳过缓存强制刷新')
    args = parser.parse_args()

    if args.premarket:
        premarket(args.pool or DEFAULT_POOL, args.no_cache)
    elif args.intraday:
        intraday_review(args.pool, args.no_cache, date=args.date)
    elif args.close:
        close_review(args.pool, args.no_cache, date=args.date)
    else:
        parser.print_help()

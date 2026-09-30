#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare_workbuddy_input.py — 每日生成 Workbuddy 预填文本（收盘专属）
=====================================================================
用途：每天收盘后运行一次，生成一个可直接粘贴到 Workbuddy 提示词
      【stock_quant 预填输出】与【stock-quant-close 自动复盘报告】的文本文件。
流程（8/17~8/21 暂定）：
  1. 运行 stock_quant.py --compare 获取股票池对比表（标的数取自 pool.json 单源）
  2. 对每只标的提取：评分卡行 / 关键变量观察台 / 结论失效条件 / 最终建议
  3. 读取当日收盘复盘报告（复盘/YYYY-MM-DD收盘复盘报告_auto.md）
  4. 拼装成 markdown 输出到 ~/Workbuddy/workbuddy-daily-input/YYYY-MM-DD.md

用法：
  python3 prepare_workbuddy_input.py [YYYY-MM-DD]   # 默认今天
示例：
  python3 prepare_workbuddy_input.py 2026-08-14
"""
import subprocess
import sys
import os
import re
import datetime
import json

# 股票池（2026-08-28 收敛：单源 data/pool.json，经 config.get_watch_pool() 读取）
# 原本地 STOCKS 硬编码与 daily_review/持仓台账多处不一致（池定义漂移的历史根因）；
# 现统一从池单源读取，增删股票只改 data/pool.json。
from config import get_watch_pool
STOCKS = get_watch_pool()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # ~/stock-quant
REVIEW_DIR = os.path.join(BASE_DIR, '复盘')
OUT_DIR = os.path.expanduser('~/Workbuddy/workbuddy-daily-input')
PROMPT_PATH = os.path.expanduser('~/Workbuddy/workbuddy-single-expert-prompt.md')  # 提示词模板（V1.3+，版本随模板头部）


def load_prompt_template():
    """读取提示词模板，返回【提示词正文】部分（不含头部说明与设计要点）"""
    try:
        with open(PROMPT_PATH, encoding='utf-8') as f:
            content = f.read()
        # 提取【提示词正文】到 第一个 '---' 分隔线
        marker = '## 【提示词正文】（直接复制）'
        idx = content.find(marker)
        if idx == -1:
            return None
        body = content[idx + len(marker):]
        end = body.find('\n---')
        if end != -1:
            body = body[:end]
        return body.strip()
    except Exception as e:
        print(f'  ⚠️ 读取提示词模板失败: {e}')
        return None


def run_cmd(args, timeout=280):
    """运行命令并返回 stdout 文本"""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ''
    except Exception as e:
        return f'[运行失败: {e}]'


def extract_block(text, start_marker, end_markers, max_lines=14):
    """提取两个标记之间的文本块（含 start_marker 行）"""
    lines = text.split('\n')
    out = []
    in_block = False
    for i, ln in enumerate(lines):
        if start_marker in ln:
            in_block = True
            out.append(ln)
            continue
        if in_block:
            if any(m in ln for m in end_markers):
                break
            out.append(ln)
            if len(out) >= max_lines:
                break
    return '\n'.join(out)


def parse_contract(text):
    """从 stock_quant 输出中解析 WORKBUDDY_CONTRACT: 单行 JSON（2026-08-28 结构化契约）
    返回 dict 或 None（未输出/解析失败 → 调用方回退文本提取）
    """
    for ln in text.split('\n'):
        ln = ln.strip()
        if ln.startswith('WORKBUDDY_CONTRACT:'):
            try:
                return json.loads(ln[len('WORKBUDDY_CONTRACT:'):])
            except Exception:
                return None
    return None


def render_from_contract(c):
    """由结构化契约渲染预填段落（与文本提取版式一致，字段来自 JSON 非正则）"""
    parts = []
    if c.get('scorecard'):
        parts.append(f'**评分卡**: {c["scorecard"]}')
    if c.get('observation'):
        parts.append(f'**关键变量观察台**:\n' + '\n'.join(f'    · {v}' for v in c['observation']))
    if c.get('invalidations'):
        parts.append(f"**结论失效条件**:\n" + '\n'.join(f"    {i}. {x}" for i, x in enumerate(c['invalidations'], 1)))
    if c.get('final_advice'):
        parts.append(f'**最终建议**: {c["final_advice"]}')
    anchor = c.get('anchor')
    if anchor:
        lines = [f"  ⚓ 定价锚对标（{anchor.get('industry','')} · 同业中位PE {anchor.get('median_pe') or 0:.1f}）"]
        for p in (anchor.get('peers') or []):
            self_tag = ' ← 本股' if p.get('is_self') else ''
            pe_txt = f"{p['pe']:.1f}" if p.get('pe') and p.get('pe') > 0 else '亏损/无'
            lines.append(f"  {p['name']:10s} {pe_txt:>8s} {p.get('price',0):>8.2f} {p.get('pct',0):>+7.2f}%{self_tag}")
        lines.append(f"  → {anchor.get('signal','')}")
        parts.append(f'**定价锚对标**:\n' + '\n'.join(lines))
    new_sigs = []
    if c.get('top_overflow'):
        new_sigs.append(f"顶部止盈信号(规则2): {c.get('top_overflow_reason','')}")
    if c.get('bounce_suspect'):
        new_sigs.append(f"反弹可靠性预警(规则9): {c.get('bounce_reason','')}")
    if new_sigs:
        parts.append(f'**今日新信号**: ' + ' ｜ '.join(new_sigs))
    # ── 系统沉淀字段显式单列（供专家直接消费，勿重造）──
    sys_fields = []
    if c.get('top_overflow'):
        sys_fields.append(f"**top_overflow**: True ｜ {c.get('top_overflow_reason','')}")
    else:
        sys_fields.append('**top_overflow**: False（未触发规则2顶部止盈）')
    beta_level = c.get('beta_level') or '中β'
    beta_warn = ''
    if beta_level == '高β':
        beta_warn = '（高β标的在系统日默认降档、不新开仓；大盘|跌|>3% Beta闸门则信号降级）'
    sys_fields.append(f'**beta_level**: {beta_level}{beta_warn}')
    timing = c.get('timing')
    if timing:
        sys_fields.append(f"**timing_note**: 时效加权裁决: 净分 {timing.get('net',0):+.1f}（实时多头 {timing.get('bull_pct',50)}%）"
                          + (f" {timing.get('note','')}".strip()))
    if c.get('trap'):
        sys_fields.append(f"**trap**: {c['trap']}")
    # P1-3（2026-09-01）：K线形态 + 事件驱动透传——
    # 事件驱动标注是 08-31 网宿"题材日量化盲区"教训的直接对策（题材日量化评分对消息催化不敏感）
    if c.get('candle_pattern'):
        sys_fields.append(f"**K线形态**: {c['candle_pattern']}"
                          + (f" ｜ {c['candle_pattern_desc']}" if c.get('candle_pattern_desc') else ''))
    if c.get('event_driven'):
        _ev_str = '、'.join(c['event_driven'])
        sys_fields.append(f"**事件驱动**: 命中事件触发词（{_ev_str}）——题材定价日，"
                          f"量化评分对消息催化不敏感，研判需结合事件催化强度")
    comp = c.get('completeness') or {}
    if comp.get('missing'):
        sys_fields.append(f"**数据缺失**: {', '.join(comp['missing'])}（{comp.get('pct',0):.0f}% 完整度）")
    if len(sys_fields) >= 3:
        parts.append('**系统沉淀字段**（供专家直接消费，勿重造）:\n' + '\n'.join(f'  {x}' for x in sys_fields))
    return '\n'.join(parts) if parts else None


def collect_stock_summary(name, code):
    """对单只标的运行 stock_quant.py，提取关键段落
    2026-08-28：优先消费 --workbuddy-contract 结构化 JSON（机器契约，不再靠 emoji 正则）；
    契约缺失/解析失败时回退原文本块提取（双轨过渡期）
    """
    result = run_cmd([sys.executable, os.path.join(BASE_DIR, 'stock_quant.py'), code,
                      '--workbuddy-contract'])
    contract = parse_contract(result)
    if contract:
        rendered = render_from_contract(contract)
        if rendered:
            return rendered
    # ── 回退：原文本块提取（契约漂移/旧版本兼容）──
    parts = []
    # 评分卡行（一行浓缩）
    for ln in result.split('\n'):
        if '评分卡' in ln:
            parts.append(f'**评分卡**: {ln.strip()}')
            break
    # 关键变量观察台
    obs = extract_block(result, '🔭 关键变量观察台', ['⛔', '历史胜率', '信号追踪', '数据完整度'])
    if obs:
        parts.append(f'**关键变量观察台**:\n{obs}')
    # 结论失效条件
    inv = extract_block(result, '⛔ 结论失效条件', ['数据完整度', '风险提示', '免责'])
    if inv:
        parts.append(f'**结论失效条件**:\n{inv}')
    # 最终建议
    for ln in result.split('\n'):
        if '最终建议' in ln:
            parts.append(f'**最终建议**: {ln.strip()}')
            break
    # 定价锚对标（2026-08-24 新增：估值按同行业/同环节对比，供专家引用）
    anchor = extract_block(result, '⚓ 定价锚对标', ['💰 四、资金面', '四、资金面'])
    if anchor:
        parts.append(f'**定价锚对标**:\n{anchor}')
    # 顶部止盈 / 反弹可靠性（新信号）
    for ln in result.split('\n'):
        if ('顶部止盈' in ln or '反弹可靠性' in ln) and ('信号' in ln or '预警' in ln):
            parts.append(f'**今日新信号**: {ln.strip()}')
    # ── 系统沉淀字段显式单列（2026-08-25 新增：专家反馈预填未显式输出
    #    top_overflow/rule2_note/beta_level/timing_note/trap，只能据KDJ/价格独立推导）──
    sys_fields = []
    # 1. top_overflow + rule2_note（规则2顶部止盈）
    top_line = ''
    for ln in result.split('\n'):
        if '顶部止盈信号(规则2)' in ln:
            top_line = ln.strip()
            break
    if top_line:
        sys_fields.append(f'**top_overflow**: True ｜ {top_line}')
    else:
        sys_fields.append('**top_overflow**: False（未触发规则2顶部止盈）')
    # 2. beta_level（按代码前缀推导，与 daily_review 同口径）
    digits = ''.join(ch for ch in code if ch.isdigit())
    if digits[:3] in ('300', '301', '688'):
        beta_level = '高β'
    elif digits[:3] in ('600', '601', '603', '000', '002'):
        beta_level = '中β'
    else:
        beta_level = '低β'
    beta_warn = ''
    if beta_level == '高β':
        beta_warn = '（高β标的在系统日默认降档、不新开仓；大盘|跌|>3% Beta闸门则信号降级）'
    sys_fields.append(f'**beta_level**: {beta_level}{beta_warn}')
    # 3. timing_note（时效加权裁决）
    timing_note = ''
    for ln in result.split('\n'):
        if '时效加权' in ln and ('净分' in ln or '强度变化' in ln):
            timing_note = ln.strip()
            break
    if timing_note:
        sys_fields.append(f'**timing_note**: {timing_note}')
    # 4. trap（资金-价格背离陷阱 / 洗盘特征）
    trap = ''
    for ln in result.split('\n'):
        if '资金-价格背离' in ln or '资金-价格反向背离' in ln:
            trap = ln.strip()
            break
    if trap:
        sys_fields.append(f'**trap**: {trap}')
    if len(sys_fields) >= 3:
        parts.append('**系统沉淀字段**（供专家直接消费，勿重造）:\n' + '\n'.join(f'  {x}' for x in sys_fields))
    return '\n'.join(parts) if parts else f'（{name} 预填提取失败，请手动补充或删除）'


def collect_signal_stats():
    """提取信号库赔率加权统计（2026-08-25 借鉴 khQuant 绩效口径新增）
    供专家第二段 A 量化背书：不仅看胜率，还要看赔率加权净值/盈亏比，
    堵住"胜率幻觉"（胜率及格但赔率加权净值为负）。
    """
    try:
        import stock_quant as sq
        stats = sq.get_signal_stats()
        overall = stats.get('overall', {})
        total = overall.get('total', 0)
        if total == 0:
            return '**信号库统计**: （暂无已平仓信号，积累中）'
        lines = [
            f'**信号库统计**（已平仓 {total} 条）: 胜率{overall.get("win_rate", 0)}% '
            f'均收益{overall.get("avg_return", 0):+.2f}% 盈亏比{overall.get("pl_ratio", 0)} '
            f'赔率加权净值{overall.get("odds_net", 0):+.2f}',
        ]
        if overall.get('illusion'):
            lines.append(f'  ⚠️ {overall["illusion"]}')
        # 按策略分组明细（供专家逐策略引用）
        for s in stats.get('stats', []):
            lines.append(
                f'  - {s["strategy"]}: {s["total"]}次 胜率{s["win_rate"]}% '
                f'盈亏比{s["pl_ratio"]} 赔率加权{s["odds_net"]:+.2f}'
            )
        return '\n'.join(lines)
    except Exception as e:
        return f'**信号库统计**: （获取失败: {e}）'


def collect_premarket_perf(date_str):
    """提取收盘复盘报告中的盘前预测绩效评估段（六、盘前预测绩效评估）
    2026-08-25 新增：让专家看到"预测质量"的赔率加权度量，而非只看方向对错。
    """
    review_path = os.path.join(REVIEW_DIR, f'{date_str}收盘复盘报告_auto.md')
    if not os.path.exists(review_path):
        return None
    try:
        with open(review_path, encoding='utf-8') as f:
            text = f.read()
        # 从"六、盘前预测绩效评估"到"本报告由 daily_review"之间的段落
        block = extract_block(text, '## 六、盘前预测绩效评估', ['本报告由 daily_review'], max_lines=25)
        if block and '盘前预测绩效评估' in block:
            return block
    except Exception:
        pass
    return None


def collect_param_snapshot(date_str):
    """提取收盘复盘报告头部参数快照行（2026-08-25 新增，保证复盘可复现）"""
    review_path = os.path.join(REVIEW_DIR, f'{date_str}收盘复盘报告_auto.md')
    if not os.path.exists(review_path):
        return None
    try:
        with open(review_path, encoding='utf-8') as f:
            for ln in f:
                if '参数快照' in ln:
                    return ln.strip()
    except Exception:
        pass
    return None


def collect_exec_plan(date_str):
    """预测可执行化块（2026-08-25 P0-1 新增）：从盘前基准 JSON 提取每只标的的
    结构化预测计划（触发条件/目标位/失效条件/止损位），供专家直接消费——
    让专家基于"触发/达标/失效"四要素给可执行建议，而非泛泛分析方向。
    """
    json_path = os.path.join(REVIEW_DIR, f'{date_str}盘前基准.json')
    if not os.path.exists(json_path):
        return ''
    try:
        with open(json_path, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return ''
    recs = d.get('records', [])
    lines = ['**预测可执行化（盘前 exec_plan：触发条件/目标位/失效条件，供专家按四要素给建议）**:']
    n = 0
    for r in recs:
        ep = r.get('exec_plan') or {}
        if not ep or not ep.get('trigger'):
            continue
        n += 1
        tgt = '｜'.join(filter(None, [
            f"上{ep['target_up']}" if ep.get('target_up') else '',
            f"下{ep['target_down']}" if ep.get('target_down') else '']))
        lines.append(f"- **{r.get('name')}**（{r.get('direction', '—')}）: "
                     f"触发={ep.get('trigger', '—')}｜目标={tgt or '—'}｜"
                     f"失效={ep.get('invalidation', '—')}｜止损={ep.get('stop', '—')}")
    if n == 0:
        return ''
    return '\n'.join(lines)


def main():
    # 日期解析
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        date_str = datetime.date.today().strftime('%Y-%m-%d')
    # 校验日期格式
    try:
        datetime.datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        print(f'❌ 日期格式错误: {date_str}（应为 YYYY-MM-DD）')
        sys.exit(1)

    print(f'📋 生成 {date_str} Workbuddy 预填文本...')

    # 1. 持仓对比表（用代码而非名称，避免名称识别失败）
    print('  ⏳ 运行 stock_quant.py --compare ...')
    compare_args = [sys.executable, os.path.join(BASE_DIR, 'stock_quant.py'), '--compare'] + \
                   [c for _, c in STOCKS]
    compare_text = run_cmd(compare_args)
    # 截取对比表部分
    cmp_block = extract_block(compare_text, '📊 多股票对比分析', [], max_lines=20) or compare_text

    # 2. 每只标的详细段落
    stock_sections = []
    for name, code in STOCKS:
        print(f'  ⏳ 提取 {name}({code}) 关键段落 ...')
        stock_sections.append(f'### {name} ({code})\n{collect_stock_summary(name, code)}')

    # 3. 读取当日收盘复盘报告
    review_path = os.path.join(REVIEW_DIR, f'{date_str}收盘复盘报告_auto.md')
    review_text = ''
    if os.path.exists(review_path):
        with open(review_path, encoding='utf-8') as f:
            review_text = f.read()
    else:
        review_text = f'（未找到 {review_path}，可能尚未运行 stock-quant-close）'

    # 3.5 附加量化背书块（2026-08-25 借鉴 khQuant 绩效口径新增）
    # 信号库赔率加权统计 + 盘前预测绩效评估 + 参数快照 —— 让专家不只看到"方向对错"，
    # 还能看到赔率加权净值（堵住胜率幻觉）与最近预测质量
    extra_blocks = []
    # P0-2（2026-09-01）：环境降档预警透传——读盘前基准 JSON 顶层 env_warning，
    # 作为全局纪律块注入（此前只存在于盘前 MD，收盘预填贴的是复盘报告，专家拿不到）
    try:
        with open(os.path.join(REVIEW_DIR, f'{date_str}盘前基准.json'), encoding='utf-8') as f:
            _pre_j = json.load(f)
        _ew = (_pre_j.get('env_warning') or '').strip()
        if not _ew:
            # 2026-09-01：兼容修复前生成的旧快照（JSON 无 env_warning 字段）——
            # 从 records 现场计算，避免专家漏掉环境降档纪律输入
            try:
                from daily_review import compute_env_warning
                _ew, _ = compute_env_warning(_pre_j.get('records', []))
            except Exception:
                pass
        if _ew:
            extra_blocks.append(
                f'**环境降档预警（盘前基准透传）**：{_ew}——给结尾B次日预案时，'
                f'上述标的偏空方向按降档口径处理（只按"不追高"，不按强空）')
    except Exception:
        pass
    sig_stats = collect_signal_stats()
    if sig_stats:
        extra_blocks.append(sig_stats)
    pre_perf = collect_premarket_perf(date_str)
    if pre_perf:
        extra_blocks.append(pre_perf)
    param_snap = collect_param_snapshot(date_str)
    if param_snap:
        extra_blocks.append(f'**参数快照**: {param_snap}')
    # P0-1 预测可执行化块（2026-08-25 新增：触发条件/目标位/失效条件，供专家按四要素给建议）
    exec_plan_block = collect_exec_plan(date_str)
    if exec_plan_block:
        extra_blocks.append(exec_plan_block)
    extra_text = '\n\n'.join(extra_blocks)

    # 4. 拼装输出：读取提示词模板，把预填数据/复盘报告填入占位符
    prompt_body = load_prompt_template()
    # P0-1（2026-09-01）：标的池动态注入——模板中的静态标的列表已改为 {{POOL_*}} 占位符，
    # 从 pool.json 单源生成，根治"模板手改股票名单"漂移（模板曾硬编码漏标的）
    try:
        from config import get_watch_pool_with_role
        pool = get_watch_pool_with_role()
        all_str = '、'.join(f'{n}({c[2:]})' for n, c, _ in pool)
        # 2026-09-03 脱敏口径：不再输出持仓/观察归属，全池统一口径
        hold_str = watch_str = '（已移除持仓口径，全池统一研判）'
        n_hold = len(pool)
    except Exception:
        all_str = '、'.join(f'{n}({c[2:]})' for n, c in STOCKS)
        hold_str = watch_str = '（pool.json 读取失败，以预填为准）'
        n_hold = len(STOCKS)
    prefilled = '\n'.join([
        f'# {date_str} stock_quant 预填输出（{len(STOCKS)}只标的对比 + 逐股关键段落）',
        '',
        f'## 一、{len(STOCKS)}只标的对比表',
        '',
        cmp_block.strip(),
        '',
        '## 二、逐股关键段落',
        '',
        *stock_sections,
        '',
        '## 三、量化背书（信号库赔率加权 + 盘前预测绩效 + 参数快照）',
        '',
        extra_text if extra_text else '（暂无量化背书数据）',
        '',
    ])
    if prompt_body:
        # 替换【stock_quant 预填输出】占位符行（精确匹配"（可选：粘贴"行，避免误伤正文引用）
        body = re.sub(
            r'【stock_quant 预填输出】\s*（可选：粘贴.*',
            f'【stock_quant 预填输出】\n{prefilled}',
            prompt_body,
            count=1,
        )
        # 替换【stock-quant-close 自动复盘报告】占位符行
        body = re.sub(
            r'【stock-quant-close 自动复盘报告】\s*（可选：粘贴.*',
            f'【stock-quant-close 自动复盘报告】\n{review_text.strip()}',
            body,
            count=1,
        )
        # P0-1：标的池占位符替换（模板静态名单 → pool.json 单源动态生成）
        body = (body.replace('{{POOL_STOCKS}}', all_str)
                    .replace('{{POOL_HOLDINGS}}', hold_str)
                    .replace('{{POOL_WATCH}}', watch_str))
        out_lines = [
            f'# {date_str} Workbuddy 单一专家收盘研判（完整提示词 + 预填数据，全文复制即可）',
            '',
            '> 生成时间: ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            '> 用法: 以下内容已包含提示词模板全文（版本见模板头部）与预填数据，直接全文复制到 Workbuddy 自动化任务即可。',
            '',
            body,
            '',
        ]
    else:
        # 模板读取失败时的兜底：仍输出纯预填文本
        out_lines = [
            f'# {date_str} stock_quant 预填输出（供 Workbuddy 单一专家收盘研判）',
            '',
            f'> 生成时间: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            '> ⚠️ 提示词模板读取失败，以下仅为预填数据，请手动拼接提示词模板',
            '',
            prefilled,
            '',
            '## 三、stock-quant-close 自动复盘报告',
            '',
            review_text.strip(),
            '',
        ]
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f'{date_str}.md')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out_lines))
    print(f'✅ 已生成: {out_path}')
    return out_path


if __name__ == '__main__':
    main()

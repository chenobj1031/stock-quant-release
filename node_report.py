#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""node_report.py — 盘中 5 节点自动快报 + 持仓动作级指令（2026-09-03 用户授权落地）

节点：0926(竞价后) / 1000(验真) / 1130(半日定格) / 1305(午间承接) / 1445(收盘动作)
数据：腾讯行情(4指数+7池) + 东财宽度 + 东财板块流(限流时降级) + 盘前基准 + 最新预案
动作规则：解析 plans/plan_YYYYMMDD.txt（代码|触发条件|动作|备注）触发线，
          按全池口径输出触发状态/预案动作（报告不展示持仓信息，2026-09-03 用户要求）；
          预案缺失时降级为最新可用预案并标注。输出：stdout + 复盘/node_reports/{date}_{HHMM}.md

用法：
  python3 node_report.py            # 按当前时间自动选最近节点
  python3 node_report.py 0926       # 指定节点（回测/补跑）
  python3 node_report.py 1130 --dry # 只打印不落盘
"""
import json, os, re, sys, subprocess, urllib.request, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import stock_quant as sq
from config import get_watch_pool

NODES = ['0926', '1000', '1130', '1305', '1445']
NODE_LABEL = {
    '0926': '竞价价·定剧本', '1000': '10:00·验真', '1130': '11:30·半日定格',
    '1305': '13:05·午间承接', '1445': '14:45·收盘动作',
}
INDICES = [('sh000001', '上证'), ('sz399001', '深成指'), ('sz399006', '创业板'), ('sh000688', '科创50'), ('bj899050', '北证50')]
# 2026-09-08 用户提示后加入北证50（小盘/专精特新风向标，"个股活跃"广度验证维度；bj899050 接口已验证）

# 触发线关键词 → (评估用价, 触发条件函数)
# kw: 突破/站上/收复/回踩/跌破/低于/冲高/减/清仓
TRIG_PATTERNS = [
    (re.compile(r'(?:放量)?突破\s*(\d+(?:\.\d+)?)'), 'above'),
    (re.compile(r'站上\s*(\d+(?:\.\d+)?)'), 'above'),
    (re.compile(r'收复\s*(\d+(?:\.\d+)?)'), 'above'),
    (re.compile(r'回踩(?:不破)?\s*(\d+(?:\.\d+)?)'), 'dip'),
    (re.compile(r'跌破\s*(\d+(?:\.\d+)?)'), 'below'),
    (re.compile(r'低于\s*(\d+(?:\.\d+)?)'), 'below'),
    (re.compile(r'冲高\s*(\d+(?:\.\d+)?)'), 'high'),
]


def tencent_quotes(codes):
    """批量腾讯行情 → {code: {price,prev,open,high,low,pct,time}}"""
    url = 'https://web.sqt.gtimg.cn/q=' + ','.join(codes)
    raw = urllib.request.urlopen(url, timeout=10).read().decode('gbk', errors='replace')
    out = {}
    for line in raw.strip().split(';'):
        if '~' not in line:
            continue
        p = line.split('~')
        if len(p) < 35:
            continue
        try:
            full = p[2]
            pure = full[2:] if re.match(r'^[a-z]{2}\d{6}$', full) else full
            out[pure] = {
                'full': full, 'name': p[1], 'price': float(p[3]), 'prev': float(p[4]),
                'open': float(p[5]), 'high': float(p[33]), 'low': float(p[34]),
                'pct': float(p[32]), 'time': p[30],
            }
        except (ValueError, IndexError):
            continue
    return out


def latest_plan(today):
    """找最新预案文件：优先昨日(交易日)的 plan，降级最近存在的。返回 (path, date_str) 或 (None, None)"""
    plans_dir = os.path.join(BASE, 'plans')
    if not os.path.isdir(plans_dir):
        return None, None
    files = sorted(os.listdir(plans_dir))
    candidates = [f for f in files if re.match(r'plan_\d{8}\.txt$', f)]
    if not candidates:
        return None, None
    # 优先取日期 == 最近一个非未来日的
    best = None
    for f in candidates:
        d = f[5:13]
        if d <= today.strftime('%Y%m%d'):
            best = f
    return (os.path.join(plans_dir, best), best[5:13]) if best else (None, None)


def parse_plan(path):
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '|' not in line:
                continue
            parts = line.split('|')
            if len(parts) < 3:
                continue
            code, cond, action = parts[0].strip(), parts[1].strip(), parts[2].strip()
            note = parts[3].strip() if len(parts) > 3 else ''
            # 2026-09-04 修复：预案代码兼容 sh/sz 前缀（V1.3.3 格式）与纯 6 位码（旧格式）
            if len(code) == 8 and code[:2] in ('sh', 'sz'):
                code = code[2:]
            rows.append({'code': code, 'cond': cond, 'action': action, 'note': note})
    return rows


def eval_triggers(row, q):
    """对一条预案，用当前价/日内高低价评估触发。返回 [(关键词, 线, 状态)]
    状态: 触发 / 未触发 / 回踩确认 / 回踩破位 / 冲高触及"""
    if not q:
        return []
    res = []
    text = row['cond'] + ' ' + row['note']
    price, hi, lo = q['price'], q['high'], q['low']
    for pat, kind in TRIG_PATTERNS:
        for m in pat.finditer(text):
            lvl = float(m.group(1))
            kw = m.group(0)
            if kind == 'above':
                st = '触发' if price >= lvl else '未触发'
            elif kind == 'below':
                st = '触发' if (price <= lvl or lo <= lvl) else '未触发'
            elif kind == 'dip':
                if lo <= lvl * 1.005 and price > lvl:
                    st = '回踩确认'
                elif lo < lvl:
                    st = '回踩破位'
                else:
                    st = '未触发'
            elif kind == 'high':
                st = '冲高触及' if hi >= lvl else '未触发'
            res.append((kw, lvl, st))
    return res


def fmt_line(name, code, q, trig):
    if not q:
        return f'- {name}({code})：无行情'
    t = ' ' + '；'.join(f'{kw}@{lvl:g}:{st}' for kw, lvl, st in trig if st != '未触发') if trig else ''
    return f"- **{name}** {q['price']:.2f} {q['pct']:+.2f}%（开{q['open']:.2f} 高{q['high']:.2f} 低{q['low']:.2f}）{t}"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry = '--dry' in sys.argv
    now = datetime.datetime.now()
    today = now.strftime('%Y%m%d')

    # 选节点
    if args and args[0] in NODES:
        node = args[0]
    else:
        hm = now.strftime('%H%M')
        node = min(NODES, key=lambda n: abs((int(hm) - int(n)) % 2400))
        # 简单取时间上最近的
        if int(hm) < 926:
            node = '0926'

    # 1) 行情（4指数 + 池）——quotes 统一用纯 6 位代码作 key（holdings/plan 同源）
    pool = get_watch_pool()
    pool_pairs = []
    for item in pool:
        a, b = str(item[0]), str(item[1])
        full = a if re.match(r'^[sz]\d{6}$', a) else b
        pure = full[2:]
        name = b if full == a else a
        pool_pairs.append((name, pure, full))
    all_codes = [c for c, _ in INDICES] + [full for _, _, full in pool_pairs]
    quotes = tencent_quotes(all_codes)

    # 休市检测：指数行情时间不是今天 → 休市/非交易时段
    idx_q = quotes.get('000001') or quotes.get('sh000001')
    mkt_time = idx_q['time'] if idx_q else ''
    mkt_date = mkt_time[:8] if len(mkt_time) >= 8 else ''
    is_market_day = (mkt_date == today)

    # state 子命令：播报前市场状态核验（2026-09-07 事故：盘前编造"09:26开盘3929.38"）
    # 用法：python3 node_report.py state —— 输出一行机器判定，agent 播报含实时价前必须先跑
    if args and args[0] == 'state':
        hm = now.strftime('%H%M')
        if is_market_day:
            state = '交易时段（行情为当日数据）' if '0925' <= hm <= '1500' else '⚠️行情为当日数据但处于非交易边界，人工核验'
        elif hm < '1500':
            state = '未开盘（盘前；行情接口返回上一交易日收盘，严禁当作今日行情播报）'
        else:
            state = '已收盘/非交易日（行情为上一交易日收盘）'
        rpt = os.path.join(BASE, '复盘', 'node_reports', f'{today}_{node}.md')
        print(f"市场状态: {state} | 行情时间戳: {mkt_time or '无'} | 当前: {now.strftime('%Y-%m-%d %H:%M:%S')} | 今日{node}快报: {'已生成' if os.path.exists(rpt) else '未生成'}")
        return

    # 2) 预案 + 触发（报告不展示持仓，口径=全池）
    plan_path, plan_date = latest_plan(now)
    plan_rows = parse_plan(plan_path)
    plan_code_set = {r['code'] for r in plan_rows}

    # 4) 宽度 + 板块（降级容错）
    try:
        breadth = sq.fetch_market_breadth()
    except Exception:
        breadth = {}
    sector_top, sector_n = None, 0
    try:
        sf = sq.fetch_sector_flow()
        ss = sf.get('sectors') or []
        sector_n = len(ss)
        if ss:
            sector_top = ' '.join(
                f"{s.get('name')}{s.get('main_net', 0):+.1f}亿"
                for s in sorted(ss, key=lambda x: -x.get('main_net', 0))[:3])
    except Exception:
        pass

    # 5) 组装
    L = []
    L.append(f"# 盘中节点快报 {now.strftime('%Y-%m-%d %H:%M')} · {NODE_LABEL[node]}")
    if not is_market_day:
        L.append(f"\n⚠️ 指数行情时间 {mkt_time or '无'} ≠ 今日 {today}，**疑似休市或非交易时段**，本快报无效。")
        print('\n'.join(L))
        return
    L.append('')
    L.append('## 市场')
    idx_line = ' | '.join(
        f"{nm} {quotes[c[2:]]['price']:.2f} {quotes[c[2:]]['pct']:+.2f}%"
        for c, nm in INDICES if c[2:] in quotes)
    L.append(idx_line)
    if breadth:
        L.append(f"宽度：涨停{breadth.get('zt_count')} 跌停{breadth.get('dt_count')} "
                 f"炸板{breadth.get('zbc_count')} 连板{breadth.get('lb_count')}")
    if sector_top:
        L.append(f"板块流入TOP3（{sector_n}块口径）：{sector_top}"
                 + ('（⚠️限流，口径不全）' if sector_n < 20 else ''))
    L.append('')
    L.append('## 标的池')
    pool_by_code = {pure: name for name, pure, full in pool_pairs}
    for name, pure, full in pool_pairs:
        q = quotes.get(pure)
        row = next((r for r in plan_rows if r['code'] == pure), None)
        trig = eval_triggers(row, q) if row else []
        L.append(fmt_line(name, full, q, trig))
    # 预案中有但不在池的标的
    pool_pure_set = set(pool_by_code)
    for r in plan_rows:
        if r['code'] not in pool_pure_set and r['code'] in quotes:
            q = quotes[r['code']]
            L.append(fmt_line(q['name'], r['code'], q, eval_triggers(r, q)))
    L.append('')
    # 6) 动作指令（仅持仓标的 + 触发项）
    L.append('## 动作指令')
    actions = []
    for r in plan_rows:
        code = r['code']
        if code not in pool_by_code and code not in quotes:
            continue
        q = quotes.get(code)
        trig = eval_triggers(r, q) if q else []
        fired = [t for t in trig if t[2] in ('触发', '回踩确认', '回踩破位', '冲高触及')]
        nm = pool_by_code.get(code, q['name'] if q else code)
        if fired:
            detail = '，'.join(f'{kw}@{lvl:g}→{st}' for kw, lvl, st in fired)
            actions.append(f"- **{nm}**：触发 [{detail}] → **{r['action']}**"
                           + (f"（{r['note'][:40]}）" if r['note'] else ''))
        elif q:
            # 未触发：给最近触发的参考线
            ref = trig[0] if trig else None
            ref_s = f"（最近线 {ref[0]}@{ref[1]:g} {ref[2]}）" if ref else ''
            actions.append(f"- {nm}：预案未触发{ref_s} → 观察")
    if not actions:
        actions.append('- 无标的触发预案动作，全部观察')
    L.extend(actions)
    L.append('')
    # 6.5) 模拟盘段（2026-09 AI 模拟盘，规则 sim_rules.md）
    L.append('## 模拟盘（2026-09 AI，20万）')
    try:
        import sim_report
        sim_results = sim_report.evaluate(quiet=True)
        if sim_results:
            L.extend('- ' + r for r in sim_results)
        else:
            acc = json.load(open(os.path.join(BASE, 'data', 'sim_account.json')))
            odf = os.path.join(BASE, 'data', 'sim_orders.json')
            today_s = now.strftime('%Y-%m-%d')
            pend = [o for o in (json.load(open(odf)) if os.path.exists(odf) else [])
                    if o['status'] in ('pending', 'awaiting_confirm') and o.get('for_date') == today_s]
            if pend:
                L.append('- 今日委托: ' + ' | '.join(
                    f"{o['id']} {o['name']} {o['side']} {o['shares']}股 条件{o['cond']}[{o['status']}]" for o in pend))
            elif acc.get('positions'):
                L.append('- 持仓无变动（快照见 14:45 后 sim_report.py snapshot）')
            else:
                L.append('- 模拟盘未启动（首个交易日 2026-09-04）')
    except Exception as e:
        L.append(f'- ⚠️ 模拟盘模块异常: {e}')
    L.append('')
    # 7) 下一节点关注
    nxt = NODES[NODES.index(node) + 1] if NODES.index(node) + 1 < len(NODES) else None
    if nxt:
        L.append(f"## 下一节点 {nxt} 前关注")
        watch = [c for c in pool_by_code if c in plan_code_set]
        L.append('- 上证日内高低点区间守不守开盘价区' +
                 ('；' + '；'.join(f"{pool_by_code[c]} 触发线" for c in watch) if watch else ''))
        L.append('- 参考位（用户口径）：上证短线支撑 3900 / 阶段目标 4070-4100')
    L.append('')
    L.append('---')
    L.append(f"预案来源：{os.path.basename(plan_path) if plan_path else '无（Workbuddy未落盘）'}"
             f"{'（⚠️降级：非今日对应预案）' if plan_date and plan_date != str(now - datetime.timedelta(days=1)).replace('-', '')[:8] and plan_date != (now - datetime.timedelta(days=1)).strftime('%Y%m%d') else ''}"
             f" | 生成 {now.strftime('%H:%M:%S')} | 数据：腾讯实时+东财宽度/板块流")

    text = '\n'.join(L)
    print(text)
    if not dry:
        out_dir = os.path.join(BASE, '复盘', 'node_reports')
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f'{today}_{node}.md'), 'w', encoding='utf-8') as f:
            f.write(text + '\n')


if __name__ == '__main__':
    main()

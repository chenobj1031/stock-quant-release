#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mainline_prereg.py — 盘前主线预注册（事前可证伪，替代"事后贴标签"）

背景（2026-09-22 立项）：主线识别曾依赖盘中实时涨幅榜事后贴标签（后视偏差）——
9-22 盘前预演把 ★1 给了存储，结果医药接棒。本脚本在 09:10 盘前用 T-1 定盘数据
预注册"当日主线候选 + 确认开关 + 证伪条件"，收盘后 --score 打分闭环。

用法：
  python3 mainline_prereg.py                 # 盘前生成今日预注册（数据=T-1收盘）
  python3 mainline_prereg.py --date 2026-09-22
  python3 mainline_prereg.py --score         # 收盘后：预注册 vs 当日实际 打分
  python3 mainline_prereg.py --score --date 2026-09-21

输出：
  复盘/mainline_prereg/YYYY-MM-DD.md    人读预注册（候选/确认开关/证伪条件）
  复盘/mainline_prereg/YYYY-MM-DD.json  结构化（--score 消费）
  复盘/mainline_prereg/score_YYYY-MM-DD.md  打分结果（命中/偏差/教训）

数据来源：东财 clist（t:2 行业 + t:3 概念，盘前 f3=T-1 涨幅）+ 板块日K（fetch_sector_kline）
"""
import os, sys, json, datetime, argparse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
OUT_DIR = os.path.join(BASE, '复盘', 'mainline_prereg')

import stock_quant as sq


def _f(v, default=0.0):
    try:
        return float(v) if v not in (None, '', '-') else default
    except (TypeError, ValueError):
        return default


def _fetch_tag(tag, max_wait=150):
    """单tag拉取（东财pz上限100，第一页=涨幅前100，已够候选池）
    限流应对(2026-09-22)：东财IP级间歇限流+60s源级冷却——空响应/冷却期内等待重试，
    总等待上限 max_wait 秒；仍失败返回 []（由调用方决定报错）
    架构修复(2026-09-24)：① 镜像清单扩充 17.push2（实测主镜像 83/48 全灭时仍可用——
    2026-09-23/24 预注册全灭根因）；② 零值质量门：东财限流时曾返回结构完整但
    f3/f62 全 0 的脏数据（09-23 候选"主力 +0.0亿"即此），全零值按失败重试。
    注：push2his 不支持 clist 端点（2026-09-24 实测 Remote end closed），不作镜像。"""
    import time
    start = time.time()
    while time.time() - start < max_wait:
        if sq._em_cooldown_check('eastmoney'):
            time.sleep(20)   # 冷却期内等过期（TTL 60s）
            continue
        params = ("pn=1&pz=200&po=1&np=1&fltt=2&invt=2"
                  "&fields=f2,f3,f12,f14,f62&fs=%s" % tag)
        try:
            data = sq._em_probe_all(
                lambda s, h, p=params: f"{s}://{h}/api/qt/clist/get?{p}",
                sq._EM_PUSH2_HOSTS, timeout=8,
                valid=lambda d: bool(d.get('data') and d['data'].get('diff')))
        except Exception:
            data = None
        diff = (data or {}).get('data', {}).get('diff', [])
        if diff:
            rows = [{
                'name': it.get('f14', ''), 'bk': it.get('f12', ''),
                'pct': _f(it.get('f3')), 'main_net': _f(it.get('f62')) / 1e8,
            } for it in diff]
            # 零值质量门(2026-09-24)：限流脏包（结构完整但数值全零）不得进判断层
            if sum(1 for r in rows if r['pct'] != 0 or r['main_net'] != 0) < max(5, len(rows) // 4):
                log.warning(f"_fetch_tag {tag}: 返回数据疑似全零值(限流脏包)，视为失败重试")
                time.sleep(20)
                continue
            return rows
        time.sleep(20)   # 空响应=疑似限流，退避重试
    return []


def fetch_all_sectors():
    """拉全量行业(t:2)+概念(t:3)板块：name/bk/pct/main_net(亿)。盘前=T-1定盘数据。"""
    import time
    out, seen = [], set()
    for tag in ('m:90+t:2', 'm:90+t:3'):
        for it in _fetch_tag(tag):
            bk = it['bk']
            if not bk or bk in seen:
                continue
            seen.add(bk)
            out.append(it)
        time.sleep(3)   # tag 间间隔，降低突发限流概率
    return out


def _fetch_kline_retry(bk, max_wait=90):
    """板块K线（限流重试）：冷却期内等待过期；仍失败返回 []
    T-1口径(2026-09-22)：已开盘时东财K线尾行为今日未收盘K——剔除，保证打分基线=T-1定盘"""
    import time
    start = time.time()
    while time.time() - start < max_wait:
        if sq._em_cooldown_check('eastmoney'):
            time.sleep(20)
            continue
        k = sq.fetch_sector_kline(bk, 60)
        if k:
            today = datetime.date.today().strftime('%Y-%m-%d')
            if k and k[-1].get('date') == today:
                k = k[:-1]   # 剔除今日未收盘K，锁定T-1基线
            return k
        time.sleep(15)
    return []


def score_candidate(sec, kline):
    """四维打分（满分100）：T-1涨幅30 / 3日动量20 / MA20结构20 / 资金配合20 / 量能10"""
    if len(kline) < 21:
        return 0, {'note': 'K线不足21根'}
    closes = [k['close'] for k in kline]
    vols = [k['volume'] for k in kline]
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-min(60, len(closes)):]) / min(60, len(closes))
    last = kline[-1]
    pct3 = (closes[-1] / closes[-4] - 1) * 100
    vol_ratio = last['volume'] / (sum(vols[-6:-1]) / 5) if sum(vols[-6:-1]) > 0 else 1.0

    s = 0
    s += 30 if last['pct'] > 2 else (20 if last['pct'] > 1 else (10 if last['pct'] > 0.5 else 0))
    s += 20 if pct3 > 3 else (12 if pct3 > 1.5 else (6 if pct3 > 0 else 0))
    s += 20 if last['close'] > ma20 else (10 if last['close'] > ma20 * 0.97 else 0)
    if sec['main_net'] > 0:
        s += 20 if sec['main_net'] > 5 else 10
    if vol_ratio > 1.2:
        s += 10
    return s, {
        'ma5': round(ma5, 2), 'ma20': round(ma20, 2), 'ma60': round(ma60, 2),
        'pct3': round(pct3, 2), 'vol_ratio': round(vol_ratio, 2),
        'last_close': last['close'],
    }


def build_pre(date_str):
    sectors = fetch_all_sectors()
    if not sectors:
        raise SystemExit('❌ 板块数据获取失败（东财限流？）——预注册无法生成，需人工处理')
    sectors.sort(key=lambda x: x['pct'], reverse=True)
    # 控制K线请求数：行业top8 + 概念top8
    cand_pool = [s for s in sectors[:8]] + [s for s in sectors[8:] if s['pct'] > 0.5][:8]
    results = []
    for sec in cand_pool:
        kline = _fetch_kline_retry(sec['bk'])
        score, tech = score_candidate(sec, kline)
        if score < 40:   # 低于40分不入候选池
            continue
        results.append({
            **sec, 'score': score, **tech,
            # P0-2 修复(2026-09-22体检)：confirm/falsify 从纯文本改为结构化规则
            # （指标名+阈值+方向），score_day 才能逐条机器核验——文本规则从未被执行过
            'confirm_rules': [
                {'metric': 'close_vs_ma20', 'op': '>=', 'value': tech['ma20'],
                 'desc': f"板块指数站上MA20({tech['ma20']})"},
                {'metric': 'pct', 'op': '>=', 'value': 1.0,
                 'desc': '板块涨幅延续>1%'},
                {'metric': 'main_net', 'op': '>=', 'value': 0.0,
                 'desc': '板块主力净额为正'},
            ],
            'falsify_rules': [
                {'metric': 'close_vs_ma20', 'op': '<', 'value': tech['ma20'] * 0.99,
                 'desc': f"板块指数跌破MA20({tech['ma20']})"},
                {'metric': 'main_net', 'op': '<', 'value': 0.0,
                 'desc': '板块主力净额转负'},
                {'metric': 'pct', 'op': '<', 'value': 0.5,
                 'desc': '涨幅收窄至<0.5%（动能衰竭）'},
            ],
            'expect': '延续强势（高开或平开后上攻）',
        })
    results.sort(key=lambda x: x['score'], reverse=True)
    top3 = results[:3]
    if not top3:
        raise SystemExit('❌ 今日无满足40分门槛的主线候选（数据异常或市场无主线）')
    os.makedirs(OUT_DIR, exist_ok=True)
    js = {'date': date_str, 'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
          'candidates': top3, 'pool_size': len(sectors), 'ranked': len(results)}
    with open(os.path.join(OUT_DIR, f'{date_str}.json'), 'w', encoding='utf-8') as f:
        json.dump(js, f, ensure_ascii=False, indent=1)

    # 架构核查修复(2026-09-22)：原判据 hour<9 恒 False——launchd 09:10 触发时
    # hour==9，盘前生成的产物被系统性标成"盘中生成"，预注册口径标签失真。
    # 改为 09:15 竞价前 = 盘前（T-1 定盘口径）
    _now = datetime.datetime.now()
    is_premarket = (_now.hour, _now.minute) < (9, 15)
    caliber = '盘前生成，clist=上一交易日(T-1)定盘数据' if is_premarket else '盘中生成，clist=今日实时涨幅（K线已剔除今日未收盘K锁定T-1基线）'
    perf_label = 'T-1表现' if is_premarket else '盘中实时'
    lines = [f"# 主线预注册 · {date_str}", '',
             f"> 生成于 {js['generated_at']}｜口径：{caliber}（事前可证伪）", '',
             f"扫描 {js['pool_size']} 个板块，{js['ranked']} 个入池，前3候选：", '']
    for i, c in enumerate(top3, 1):
        lines += [
            f"## ★{i} {c['name']}（{c['bk']}） 评分 {c['score']}/100",
            f"- {perf_label}：{c['pct']:+.2f}%，3日 {c['pct3']:+.2f}%，量比 {c['vol_ratio']}",
            f"- 结构：收盘 {c['last_close']} vs MA20 {c['ma20']} / MA60 {c['ma60']}",
            f"- 资金：主力净额 {c['main_net']:+.1f} 亿",
            f"- **确认开关**（全满足才成立）：",
            *[f"  - {r['desc']}" for r in c['confirm_rules']],
            f"- **证伪条件**（任一触发即降级）：",
            *[f"  - {r['desc']}" for r in c['falsify_rules']],
            f"- 预期：{c['expect']}", '',
        ]
    lines += ['---', '纪律：确认开关未满足前，对应方向只挂回踩单、不追高开；证伪条件触发当日该方向仓位清零思路。']
    md_path = os.path.join(OUT_DIR, f'{date_str}.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"✅ 预注册已生成: {md_path}")
    for i, c in enumerate(top3, 1):
        print(f"  ★{i} {c['name']} {c['score']}分 (T-1 {c['pct']:+.2f}%, MA20 {c['ma20']}, 主力 {c['main_net']:+.1f}亿)")
    return js


def _eval_rules(rules, metrics):
    """执行结构化规则集，返回 (全部通过?, [逐条判定]) — P0-2: confirm/falsify 机器核验"""
    detail, all_ok = [], True
    for r in rules:
        v = metrics.get(r['metric'])
        if v is None:
            detail.append(f"○ {r['desc']}: 数据缺失")
            all_ok = False
            continue
        ok = v >= r['value'] if r['op'] == '>=' else v < r['value']
        detail.append(f"{'✅' if ok else '❌'} {r['desc']}: 实际{v:.2f} vs 阈值{r['value']:.2f}")
        all_ok = all_ok and ok
    return all_ok, detail


def score_day(date_str):
    """收盘后打分：预注册候选 vs 当日实际板块表现
    P0-2 修复(2026-09-22体检)：①死代码 hit 覆盖 bug 修复；②confirm/falsify 结构化规则
    逐条机器核验（原来只是文本从未执行）；③命中改超额口径——候选选自涨幅榜 top8，
    命中若也定义为"涨幅>0.5%"则与筛选变量同源（普涨日自动全命中=β偏差），
    改为跑赢全市场中位数才计命中，同时保留绝对口径供参考"""
    p = os.path.join(OUT_DIR, f'{date_str}.json')
    if not os.path.exists(p):
        raise SystemExit(f'❌ 无 {date_str} 预注册文件（未跑盘前预注册？）')
    pre = json.load(open(p, encoding='utf-8'))
    actual = {s['bk']: s for s in fetch_all_sectors()}
    if not actual:
        raise SystemExit('❌ 当日板块数据获取失败（东财限流？）——打分无法进行')
    # 超额口径基准：全市场板块涨幅中位数
    all_pcts = sorted(s['pct'] for s in actual.values())
    median_pct = all_pcts[len(all_pcts) // 2]
    lines = [f"# 主线预注册打分 · {date_str}", '',
             f"> 打分时间 {datetime.datetime.now():%Y-%m-%d %H:%M}（数据=当日收盘/实时）",
             f"> 超额口径基准：全市场板块涨幅中位数 {median_pct:+.2f}%", '']
    hits, total = 0, 0
    rows = []
    rule_details = []
    for i, c in enumerate(pre['candidates'], 1):
        a = actual.get(c['bk'])
        if not a:
            rows.append((i, c['name'], 'n/a', 'n/a', '数据缺失'))
            continue
        total += 1
        # 超额口径命中：跑赢全市场中位数（绝对口径 >0.5% 仅作参考展示）
        hit = a['pct'] > median_pct
        hits += int(hit)
        tag = '✅命中(超额)' if hit else '❌偏差'
        rows.append((i, c['name'], f"{c['pct']:+.2f}%",
                     f"{a['pct']:+.2f}% ({a['main_net']:+.1f}亿, 超额{a['pct']-median_pct:+.2f})", tag))
        # 逐条核验证伪/确认规则（metrics: pct/main_net 来自 clist；close_vs_ma20 需当日K线尾行）
        k = _fetch_kline_retry(c['bk'], max_wait=30)
        close_vs_ma20 = None
        if len(k) >= 21:
            closes = [x['close'] for x in k]
            close_vs_ma20 = closes[-1] - (sum(closes[-20:]) / 20)
        metrics = {'pct': a['pct'], 'main_net': a['main_net'], 'close_vs_ma20': close_vs_ma20}
        f_ok, f_detail = _eval_rules(c.get('falsify_rules', []), metrics)
        c_ok, c_detail = _eval_rules(c.get('confirm_rules', []), metrics)
        rule_details.append((i, c['name'], f_ok, f_detail, c_ok, c_detail))
    ranked_actual = sorted(actual.values(), key=lambda x: x['pct'], reverse=True)[:3]
    lines += ['| # | 预注册候选 | 预注册涨幅 | 当日实际 | 判定 |',
              '|---|---|---|---|---|']
    for r in rows:
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |")
    abs_hits = sum(1 for r in rows if r[4] not in ('数据缺失',) and r[3] != 'n/a' and
                   float(r[3].split('%')[0].replace('+', '')) > 0.5)
    lines += ['', f"**超额口径命中率 {hits}/{total}**（命中=跑赢全市场中位数{median_pct:+.2f}%）",
              f"绝对口径参考：{abs_hits}/{total}（涨幅>0.5%，与筛选变量同源仅作参考）", '',
              f"当日实际涨幅前3：{' / '.join(x['name'] + ' ' + format(x['pct'], '+.2f') + '%' for x in ranked_actual)}", '']
    # 规则逐条核验明细
    lines += ['## 证伪/确认规则逐条核验', '']
    for i, name, f_ok, f_detail, c_ok, c_detail in rule_details:
        lines.append(f"### ★{i} {name}（证伪{'未触发✅' if f_ok else '触发❌'}｜确认{'满足✅' if c_ok else '未满足○'}）")
        lines += [f"- {d}" for d in f_detail]
        lines.append('')
    if rows and hits == 0:
        lines += ['⚠️ **全错复盘**：预注册候选与当日实际完全背离——检查是数据问题（T-1口径错位）还是框架问题（动量延续假设失效），写教训。']
    elif total and hits < total:
        lines += ['⚠️ 部分偏差：上方证伪规则核验中触发条目=预注册已预警（非失分）；未触发但落选=框架问题需归因。']
    else:
        lines += ['✅ 全部命中：核对确认规则是否盘中满足——满足=框架强化；未满足但涨=靠β非主线，记录。']
    sp = os.path.join(OUT_DIR, f'score_{date_str}.md')
    with open(sp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"✅ 打分完成: {sp}（超额命中 {hits}/{total}，绝对口径 {abs_hits}/{total}）")
    return hits, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--score', action='store_true', help='收盘后打分模式')
    ap.add_argument('--date', default=datetime.date.today().strftime('%Y-%m-%d'))
    a = ap.parse_args()
    if a.score:
        score_day(a.date)
    else:
        build_pre(a.date)


if __name__ == '__main__':
    main()

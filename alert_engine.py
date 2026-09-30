#!/usr/bin/env python3
"""alert_engine.py — 盘中动态纠偏引擎（2026-09-14 立项，用户 A 档方案）

背景（莲花 09-14 案例）：盘前看空 39.6 → 09:34 对照标"方向偏差" → 12:41 标
"已失效"（突破压力 12.94）→ 全程零行动。检测与行动之间断链。用户方法论输入：
"只看一个时间段没法做好预测，市场交易期间变化动荡，需动态看问题及时纠偏。"

三层机制（A 档 = 草案档，FOMC 前观察模式）：
  ① 检测自动化：每 5 分钟（由 sim_loop 调用）读当日盘前基准的 exec_plan，
     现价 vs target_up/target_down 突破检测 → ALERT 去重落盘 exec.log
  ② 事件→行动映射（写死规则，防临场漂移）：
     - ALERT_LOST（向上突破压力=看空失效）→ 生成反向条件单【草案】(draft=True,
       待会话确认后转正式单) + 持仓止损复核提示
     - ALERT_FALSIFIED（反向 ≥3pct 未破线=方向证伪）→ 记录降分清单，
       次日盘前基准自动减分（经 order_bridge/premarket 钩子）
     - ALERT_SUPPORT（跌破支撑）→ 已有止损线兜底，只留痕不加动作
  ③ 快照对照：10:00 / 13:00 两个整点自动重评分全池 → 盘中快照文件（diff 视图）

边界：本引擎【永不直接成交】——草案单仅供确认；样本统计在 outcome_writer 侧。
"""
import json, os, sys, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
CAND_F = os.path.join(DATA, 'candidates.json')
EXEC_LOG = os.path.join(BASE, '复盘', 'sim_log', 'exec.log')
SNAP_DIR = os.path.join(BASE, '复盘', 'snapshot')

FALSIFY_PCT = 3.0     # 证伪阈值：反向变动 ≥3pct
DRAFT_MAX = 3         # 单日草案单上限（防告警风暴）

# 当日状态（进程内去重 + 落盘去重双保险；sim_loop 每5分钟调用本进程为一次性调用，
# 故以落盘文件为准）
STATE_F = os.path.join(DATA, 'alert_state.json')


def load_json(f, default):
    try:
        with open(f, encoding='utf-8') as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return default


def save_json(f, obj):
    with open(f, 'w', encoding='utf-8') as fp:
        json.dump(obj, fp, ensure_ascii=False, indent=1)


def today_str():
    return datetime.date.today().strftime('%Y-%m-%d')


def baseline_path(date=None):
    d = date or today_str()
    return os.path.join(BASE, '复盘', f'{d}盘前基准.json')


def load_baseline(date=None):
    """读当日盘前基准 records。无基准返回 None（盘前未跑/非交易日）"""
    p = baseline_path(date)
    d = load_json(p, None)
    if not d or not isinstance(d, dict):
        return None
    return d.get('records') or []


def log(line):
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(EXEC_LOG, 'a', encoding='utf-8') as fp:
        fp.write(f"[{stamp}] {line}\n")


# ---------------------------------------------------------------
# ① 检测 + ② 映射
# ---------------------------------------------------------------

def detect(records, px_map):
    """对全池做突破/证伪检测。
    px_map: {code6: {'price': float, 'pct': float}}（调用方从行情源组装）
    返回 alerts: [dict(kind, code, name, price, level, detail)]"""
    today = today_str()
    state = load_json(STATE_F, {'date': today, 'fired': {}})
    if state.get('date') != today:            # 跨日重置
        state = {'date': today, 'fired': {}}
    fired = state.get('fired', {})
    alerts = []
    for r in records:
        code = str(r.get('code', '')).replace('sh', '').replace('sz', '')
        if code not in px_map:
            continue
        px = px_map[code].get('price')
        if not isinstance(px, (int, float)) or px <= 0:
            continue
        ep = r.get('exec_plan') or {}
        tu, td = ep.get('target_up'), ep.get('target_down')
        base_px = r.get('price')
        if tu and px >= tu and fired.get(code) != 'LOST':
            fired[code] = 'LOST'
            a = {'kind': 'ALERT_LOST', 'code': code, 'name': r.get('name'),
                 'price': px, 'level': tu,
                 'detail': f'向上突破压力位{tu}（盘前方向{r.get("direction")}失效→方向反转候选）'}
            alerts.append(a)
        elif td and px <= td and fired.get(code) != 'SUPPORT':
            fired[code] = 'SUPPORT'
            a = {'kind': 'ALERT_SUPPORT', 'code': code, 'name': r.get('name'),
                 'price': px, 'level': td,
                 'detail': f'跌破支撑{td}（止损线兜底，仅留痕）'}
            alerts.append(a)
        elif (tu and td and px < tu and fired.get(code) not in ('LOST', 'SUPPORT')):
            # 证伪检测：方向看空但反向 ≥3pct（未破线）——首轮 fired 为空也放行
            # （修复：原条件 fired.get(code)=='NONE' 在首轮恒 False，证伪永不触发）
            if r.get('direction') == '看空' and isinstance(base_px, (int, float)):
                move = (px - base_px) / base_px * 100
                if move >= FALSIFY_PCT and fired.get(code + '_F') != 'F':
                    fired[code + '_F'] = 'F'
                    alerts.append({'kind': 'ALERT_FALSIFIED', 'code': code,
                                   'name': r.get('name'), 'price': px, 'level': round(move, 2),
                                   'detail': f'看空方向反向+{move:.1f}%（未破线，方向证伪→次日评级降分）'})
        if code not in fired:
            fired[code] = 'NONE'
    state['fired'] = fired
    save_json(STATE_F, state)
    return alerts


def act_on_alerts(alerts):
    """② 事件→行动映射（A 档：草案单 + 降分清单，永不直接成交）"""
    today = today_str()
    drafts = load_json(CAND_F, {}).get('candidates') or []   # 仅复用文件路径结构
    draft_orders = load_json(os.path.join(DATA, 'draft_orders.json'), [])
    draft_ids = {d.get('id') for d in draft_orders}
    downgrades = load_json(os.path.join(DATA, 'downgrade_list.json'), {})
    if downgrades.get('date') != today:
        downgrades = {'date': today, 'codes': {}}
    n_new = 0
    for a in alerts:
        log(f"🚨 {a['kind']} {a['name']}({a['code']}) 现价{a['price']} — {a['detail']}")
        if a['kind'] == 'ALERT_LOST' and n_new < DRAFT_MAX:
            # 反向条件单【草案】：以现价上浮0.5%作触发（突破确认），不带止损（草案阶段由会话补）
            did = f"D{a['code']}"
            if did not in draft_ids:
                draft_orders.append({
                    'id': did, 'for_date': today, 'code': a['code'],
                    'name': a['name'], 'side': 'buy', 'shares': 0,   # shares=0: 草案未定尺寸
                    'cond': {'min': round(a['price'] * 1.005, 2)},
                    'status': 'draft', 'draft': True,
                    'reason': f"[alert] {a['detail']} — 草案（A档：需会话确认后才转正式单+定尺寸+止损）",
                    'created_at': datetime.datetime.now().strftime('%H:%M:%S')})
                draft_ids.add(did)
                n_new += 1
                log(f"  ↳ 草案单 {did} 已生成（pending 区 draft 状态，未确认不执行）")
        elif a['kind'] == 'ALERT_FALSIFIED':
            downgrades['codes'][a['code']] = {'name': a['name'], 'move': a['level'],
                                              'note': '盘中方向证伪，次日盘前评分减分'}
    if n_new:
        save_json(os.path.join(DATA, 'draft_orders.json'), draft_orders)
    if downgrades['codes']:
        save_json(os.path.join(DATA, 'downgrade_list.json'), downgrades)
    return n_new


# ---------------------------------------------------------------
# ③ 盘中快照（10:00 / 13:00 整点）
# ---------------------------------------------------------------

def snapshot_if_due(px_map):
    """10:00 / 13:00 前后 5 分钟窗口内触发一次快照（与盘前基准 diff）"""
    now = datetime.datetime.now()
    hhmm = now.strftime('%H:%M')
    due = None
    for t in ('10:00', '13:00'):
        t0 = datetime.datetime.strptime(t, '%H:%M').time()
        delta = (datetime.datetime.combine(now.date(), t0) - now).total_seconds()
        if -300 <= delta <= 300:
            due = t
            break
    if not due:
        return None
    os.makedirs(SNAP_DIR, exist_ok=True)
    f = os.path.join(SNAP_DIR, f'{today_str()}_snapshot_{due.replace(":", "")}.json')
    if os.path.exists(f):
        return None                      # 已快照，不重复
    records = load_baseline() or []
    rows = []
    for r in records:
        code = str(r.get('code', '')).replace('sh', '').replace('sz', '')
        p = px_map.get(code) or {}
        base_pct = r.get('pct')
        cur_pct = p.get('pct')
        rows.append({'name': r.get('name'), 'code': code,
                     'pre_direction': r.get('direction'), 'pre_score': r.get('score'),
                     'pre_pct': base_pct, 'cur_pct': cur_pct,
                     'drift': (round((cur_pct or 0) - (base_pct or 0), 2)
                               if isinstance(cur_pct, (int, float)) else None)})
    snap = {'date': today_str(), 'time': due, 'generated_at': hhmm, 'rows': rows}
    save_json(f, snap)
    log(f"📸 盘中快照 {due} 落盘（{len(rows)} 只，drift=现涨跌-盘前涨跌）")
    return f


# ---------------------------------------------------------------
# 主入口（sim_loop 每 5 分钟调用；行情组装失败则静默跳过）
# ---------------------------------------------------------------

def run():
    records = load_baseline()
    if not records:
        return []                        # 无当日基准（盘前未跑），静默
    # 行情组装：腾讯批量（一次请求，全池 codes）
    import urllib.request
    codes = [str(r.get('code', '')).replace('sh', '').replace('sz', '')
             for r in records]
    fulls = [('sh' if c[0] in '569' else 'sz') + c for c in codes if len(c) == 6]
    if not fulls:
        return []
    try:
        url = 'https://qt.gtimg.cn/q=' + ','.join(fulls)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        raw = urllib.request.urlopen(req, timeout=8).read().decode('gbk', 'ignore')
    except OSError:
        return []
    px_map = {}
    for line in raw.strip().split(';'):
        if '~' not in line:
            continue
        parts = line.split('~')
        if len(parts) < 34:
            continue
        code6 = parts[2]
        try:
            px_map[code6] = {'price': float(parts[3]), 'pct': float(parts[32])}
        except (ValueError, IndexError):
            continue
    # 数据时效守卫（当日数据才检测——与 sim_loop 同一口径）
    ts = ''
    for line in raw.strip().split(';'):
        if '~' in line:
            parts = line.split('~')
            if len(parts) > 30:
                ts = parts[30]
                break
    today = today_str().replace('-', '')
    if ts[:8] != today:
        log(f'⚠️ ALERT检测跳过: 行情非今日({ts[:8]})')
        return []
    alerts = detect(records, px_map)
    if alerts:
        act_on_alerts(alerts)
    snapshot_if_due(px_map)
    return alerts


if __name__ == '__main__':
    a = run()
    print(f'alerts: {len(a)}')

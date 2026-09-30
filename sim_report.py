#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim_report.py — 2026-09 AI 模拟盘引擎（规则见 sim_rules.md，月中不改）

防作弊核心：成交价 = 执行时点腾讯真实价 + 0.1% 逆向滑点；禁止用当日高/低/收盘回填。

用法：
  python3 sim_report.py snapshot          # 盘后快照（14:45 节点后/收盘后跑）
  python3 sim_report.py report            # 当前账户报表
  python3 sim_report.py evaluate          # 机械评估今日 pending 委托单（节点调用）
  python3 sim_report.py confirm O2 72.3            # 人工核验后确认执行（留痕；价格为参考，实际按实时价）
  python3 sim_report.py cancel O1 --reason "竞价高开不接"
  python3 sim_report.py addorder sh510050 buy --pct 0.5 --max 2.93 --stop 2.83 --note "回补预案"
                                                   # 盘中预案→委托单（pending，sim_loop 节点自动执行）
"""
import json, os, sys, time, fcntl, tempfile, contextlib, datetime, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
ACC_F = os.path.join(DATA, 'sim_account.json')
TRF_F = os.path.join(DATA, 'sim_trades.json')
ODF_F = os.path.join(DATA, 'sim_orders.json')
HIST_F = os.path.join(DATA, 'sim_history.json')
LOCK_F = os.path.join(DATA, '.sim.lock')


@contextlib.contextmanager
def ledger_lock(timeout=120.0):
    """跨进程文件锁（2026-09-21 并发打架修复）：sim_loop / node_report / order_bridge
    三方对同一批账本做读-改-写，无锁时存在互相覆盖窗口（lost update）。
    锁对象为独立 .sim.lock（非账本文件本身）——原子 save（os.replace）后
    账本文件 inode 会更换，对账本文件加锁会失效。超时抛 RuntimeError，由调用方捕获留痕。"""
    fd = os.open(LOCK_F, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                os.close(fd)
                raise RuntimeError(f'ledger_lock: {int(timeout)}s 内未获锁（并发进程持锁）')
            time.sleep(0.3)
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def locked(fn):
    """整函数=账本临界区 装饰器（2026-09-21 并发打架修复）"""
    def wrapper(*args, **kwargs):
        with ledger_lock():
            return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper

COMMISSION_RATE, COMMISSION_MIN = 0.000025, 5.0   # 佣金 0.25‰ 最低5元（双边）
STAMP = 0.0005                                     # 印花税 0.05%（仅卖出）
TRANSFER = 0.00001                                 # 过户费 0.001%（双边）
SLIP = 0.001                                       # 滑点 0.1% 逆向
LIMIT = lambda code: (0.20 if code[:2] in ('30', '68') else 0.10)


def load(f):
    with open(f, encoding='utf-8') as fp:
        return json.load(fp)


def save(f, obj):
    """原子写（2026-09-21 并发打架修复）：临时文件 + os.replace，
    防锁外读者读到半截 JSON（锁保护业务临界区，原子写是最后一道防线）"""
    d = os.path.dirname(f) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.tmp_', suffix='.json', dir=d)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            json.dump(obj, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, f)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def tencent_px(full):
    raw = urllib.request.urlopen(f'https://web.sqt.gtimg.cn/q={full}', timeout=10).read().decode('gbk', errors='replace')
    p = raw.split('~')
    return float(p[3]), float(p[4]), float(p[33]), float(p[34]), p[1]


def tencent_full(full):
    """(现价, 昨收, 高, 低, 名称, 量比, 时间戳) —— 委托单评估用"""
    raw = urllib.request.urlopen(f'https://web.sqt.gtimg.cn/q={full}', timeout=10).read().decode('gbk', errors='replace')
    p = raw.split('~')
    try:
        vr = float(p[49])
    except (ValueError, IndexError):
        vr = None
    return float(p[3]), float(p[4]), float(p[33]), float(p[34]), p[1], vr, p[30]


def main_net_today(full, today):
    """当日主力净额（亿）——data_date≠今日返回 None（防 T-1 数据污染决策）"""
    try:
        import stock_quant as sq
        mf = sq.fetch_main_flow(full)
        if not mf:
            return None
        d = mf.get('fflow_date') or mf.get('data_date') or ''
        if d != today:
            return None
        return mf.get('main_net')
    except Exception:
        return None


def exec_price(px, side):
    return px * (1 + SLIP) if side == 'buy' else px * (1 - SLIP)


def cond_met(order, px, vol_ratio=None, main_net=None):
    """机械条件全过才 True：价格(max/min) + 量比(min_vol_ratio) + 主力净额(main_net_min, 亿)"""
    c = order.get('cond') or {}
    if 'max' in c and px > c['max']:
        return False
    if 'min' in c and px < c['min']:
        return False
    if 'min_vol_ratio' in c and (vol_ratio is None or vol_ratio < c['min_vol_ratio']):
        return False
    if 'main_net_min' in c and (main_net is None or main_net < c['main_net_min']):
        return False
    return True


def trade_cost(value, side):
    comm = max(COMMISSION_RATE * value, COMMISSION_MIN)
    trans = TRANSFER * value
    stamp = STAMP * value if side == 'sell' else 0.0
    return comm + trans + stamp


def do_trade(acc, trades, orders, order, px_now, note='', px_limit=None):
    """执行一笔委托（T+1/涨跌停/资金校验）。返回 (成交dict|None, 说明)
    P2-8(2026-09-22体检)：原函数锁内固定调 tencent_px 做涨跌停校验（网络 10-15s），
    行情抖动时整轮节点排队超时——调用方已持有当日行情快照时经 px_limit=(prev,) 传入，
    prev 为涨跌停基准价（昨收）；未传时保持原网络校验（兼容旧调用点）。"""
    full, side, shares, code = order['full'], order['side'], order['shares'], order['code']
    px = exec_price(px_now, side)
    dt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # 涨跌停校验（px_limit 传参复用调用方已有行情，避免锁内二次联网）
    if px_limit is not None:
        prev = float(px_limit[0])
    else:
        p_, prev, hi, lo, _ = tencent_px(full)
    lim = round(prev * (1 + LIMIT(code)), 2)
    limd = round(prev * (1 - LIMIT(code)), 2)
    if side == 'buy' and px_now >= lim:
        return None, f"涨停{lim}无法买入"
    if side == 'sell' and px_now <= limd:
        return None, f"跌停{limd}无法卖出"
    value = px * shares
    cost = trade_cost(value, side)
    if side == 'buy':
        if value + cost > acc['cash']:
            # 资金不足：按可用资金缩减到整百股
            max_sh = int(acc['cash'] / (px * (1 + COMMISSION_RATE + TRANSFER)) // 100) * 100
            if max_sh < 100:
                return None, '资金不足（<100股）'
            shares = max_sh
            order['shares'] = shares
            value = px * shares
            cost = trade_cost(value, side)
            # P0-3 修复(2026-09-22体检)：trade_cost 有 5 元最低佣金下限，比例费率估算
            # 在小金额场景低估成本，缩减后不复核会穿透现金为负——补最终防线
            if value + cost > acc['cash']:
                max_sh = int((max_sh // 100) - 1) * 100
                if max_sh < 100:
                    return None, '资金不足（含最低佣金后<100股）'
                shares = max_sh
                order['shares'] = shares
                value = px * shares
                cost = trade_cost(value, side)
                if value + cost > acc['cash']:
                    return None, '资金不足（最低佣金复核后仍超）'
            note += f'；资金缩减至{shares}股'
        acc['cash'] -= (value + cost)
        acc['cash'] = round(acc['cash'], 2)   # P2-11 顺手修：阻断浮点累积漂移
        if acc['cash'] < -0.005:
            return None, '现金校验失败(负值拦截)'
        pos = acc['positions'].setdefault(code, {'full': full, 'name': order['name'], 'shares': 0, 'cost': 0.0, 'buy_date': None})
        new_sh = pos['shares'] + shares
        pos['cost'] = (pos['cost'] * pos['shares'] + px * shares) / new_sh
        pos['shares'] = new_sh
        # 2026-09-18 修复(T+1按笔精确)：原"每次买入刷新buy_date"会把老股也锁死——
        # 做T买腿成交后整仓当日不可卖，与交易所规则相反(今买股锁定、老股可卖)
        if pos.get('buy_date') == dt[:10]:
            pos['today_bought'] = pos.get('today_bought', 0) + shares
        else:
            pos['buy_date'] = dt[:10]
            pos['today_bought'] = shares
        # P2-11(2026-09-22体检)：原 pos['locked']=True 写入后全库无读取方（T+1 锁定
        # 实际由 buy_date/today_bought 按笔计算），纯脏数据——已从存量账本清除，不再写入
        # 2026-09-04 修复：委托单止损线写入持仓，sim_loop 机械止损依赖此字段
        if order.get('stop'):
            pos['stop'] = order['stop']
    else:
        pos = acc['positions'].get(code)
        if not pos or pos['shares'] < shares:
            return None, f'持仓不足({pos["shares"] if pos else 0}<{shares})'
        # 2026-09-18 修复：T+1锁定只限当日买入的股(today_bought)，老股当日可卖(做T卖腿依赖此)
        today_bought = pos.get('today_bought', 0) if pos.get('buy_date') == dt[:10] else 0
        sellable = pos['shares'] - today_bought
        if sellable < shares:
            return None, f'可卖股数不足(可卖{sellable}<{shares}, 其中今买锁定{today_bought})'
        acc['cash'] += (value - cost)
        pnl = (px - pos['cost']) * shares - cost
        pos['shares'] -= shares
        if pos['shares'] == 0:
            del acc['positions'][code]
    t = {'ts': dt, 'order_id': order['id'], 'code': code, 'full': full, 'name': order['name'],
         'side': side, 'shares': shares, 'px_now': round(px_now, 3), 'px_exec': round(px, 3),
         'value': round(value, 2), 'cost': round(cost, 2), 'note': note}
    if side == 'sell':
        t['pnl'] = round(pnl, 2)
    trades.append(t)
    order['status'] = 'filled'
    order['filled_at'] = dt
    return t, '成交'


@locked
def reconcile(acc=None, trades=None, auto_fix=False):
    """对账守门（2026-09-18 新增，防9-15/9-18事故复发）：
    以 sim_trades.json 流水为唯一真值，独立重算现金与持仓，与账本比对。
    审计模式(auto_fix=False)：仅返回差异清单(告警用，不改账本)——
    漂移说明有人/某处绕过了 do_trade 手工改账，必须人工介入。
    auto_fix=True：以流水为准覆盖账本(cash/positions)，留痕。
    返回 [(code|'cash', ledger, recomputed, diff), ...]，空列表=一致。"""
    if acc is None:
        acc = load(ACC_F)
    if trades is None:
        trades = load(TRF_F)
    cash = acc['initial']
    pos = {}
    names = {}
    for t in trades:
        side, sh, code = t['side'], t['shares'], t['code']
        names[code] = t['name']
        if side == 'buy':
            cash -= (t['value'] + t['cost'])
            pos[code] = pos.get(code, 0) + sh
        elif side == 'sell':
            cash += (t['value'] - t['cost'])
            pos[code] = pos.get(code, 0) - sh
    diffs = []
    if abs(cash - acc['cash']) > 0.05:
        diffs.append(('cash', acc['cash'], round(cash, 2), round(cash - acc['cash'], 2)))
    ledger_pos = {c: p['shares'] for c, p in acc['positions'].items()}
    for code in sorted(set(list(pos.keys()) + list(ledger_pos.keys()))):
        want = pos.get(code, 0)
        got = ledger_pos.get(code, 0)
        if want != got:
            diffs.append((code, got, want, want - got))
    if auto_fix and diffs:
        acc['cash'] = round(cash, 2)
        new_pos = {}
        for code, sh in pos.items():
            if sh > 0:
                new_pos[code] = acc['positions'].get(code, {
                    'full': ('sh' if code[0] in '56' else 'sz') + code,
                    'name': names.get(code, code), 'shares': 0, 'cost': 0.0})
                new_pos[code]['shares'] = sh
        acc['positions'] = new_pos
        acc.setdefault('t_log', []).append({
            'date': datetime.date.today().strftime('%Y-%m-%d'), 'pair': 'AUTO_FIX',
            'note': f'reconcile auto_fix 以流水为准覆盖账本: {diffs}'})
        save(ACC_F, acc)
    return diffs


@locked
def evaluate(quiet=False):
    """机械评估今日 pending 委托单（全自主口径 2026-09-03 升级）：
    价格+量比+主力净额(当日)全过 → 直接成交；14:55 后未触发 → 自动失效。
    manual_confirm=True 的单保留人工核验通道（仅当我主动要求）。"""
    acc, trades, orders = load(ACC_F), load(TRF_F), load(ODF_F)
    today = datetime.date.today().strftime('%Y-%m-%d')
    hm = datetime.datetime.now().strftime('%H%M')
    after_close = hm >= '1455'
    results = []
    for o in orders:
        if o['status'] != 'pending' or o['for_date'] != today:
            continue
        try:
            px_now, prev, hi, lo, nm, vr, ts = tencent_full(o['full'])
        except Exception as e:
            results.append(f"⚠️ {o['id']} {o['name']}: 行情获取失败 {e}")
            continue
        if not ts.replace('-', '')[:8] == today.replace('-', ''):
            # P0-1 修复(2026-09-22体检)：原逻辑直接置 status='stale' 且 evaluate 只处理
            # pending → 9:15-9:30 竞价阶段行情时间戳仍是昨日时，一次运行就把当日全部
            # 挂单永久作废。改为保持 pending 仅跳过本轮，行情恢复为今日后继续正常评估；
            # 仅收盘后(14:55)才允许置 stale 终结。
            if after_close:
                o['status'] = 'stale'
            results.append(f"⚠️ {o['id']} {nm}: 行情数据非今日({ts})，本轮跳过(保持pending)")
            continue
        c = o.get('cond') or {}
        # 架构核查修复(2026-09-22)：原仅 px_now 判触发，do_trade 再加 SLIP 滑点成交
        # → 207.99 触发买入实际成交 208.20，越过触发上限（系统性买贵）。触发判定
        # 改用"含滑点成交价"口径：买单 px_now*(1+SLIP) 仍 ≤ max 才触发；
        # 卖单同理 px_now*(1-SLIP) ≥ min。保证成交价不越过策略设定的触发线。
        px_exec_est = exec_price(px_now, o['side'])
        price_ok = (('max' not in c or px_exec_est <= c['max'])
                    and ('min' not in c or px_exec_est >= c['min']))
        main_net = None
        if price_ok and 'main_net_min' in c:
            main_net = main_net_today(o['full'], today)
        if cond_met(o, px_now, vr, main_net):
            if o.get('manual_confirm'):
                o['status'] = 'awaiting_confirm'
                o['await_px'] = round(px_now, 3)
                results.append(f"⚠️ {o['id']} {nm} 条件触发（现{px_now} 量比{vr} 主力{main_net}亿）→ 待人工核验后 confirm")
                continue
            t, msg = do_trade(acc, trades, orders, o, px_now, note=f'量比{vr} 主力{main_net if main_net is not None else "n/a"}亿', px_limit=(prev,))
            if t:
                results.append(f"✅ {o['id']} {nm} {o['side']} {t['shares']}股 @{t['px_exec']} {msg}")
            else:
                results.append(f"⛔ {o['id']} {nm}: {msg}")
        else:
            if after_close:
                o['status'] = 'expired'
                results.append(f"⏰ {o['id']} {nm} 收盘未触发，自动失效（现{px_now} 条件{c}）")
            else:
                results.append(f"○ {o['id']} {nm} 未触发（现{px_now} 量比{vr} 条件{c}）")
    save(ACC_F, acc); save(TRF_F, trades); save(ODF_F, orders)
    if not quiet:
        for r in results:
            print(r)
    return results


def report():
    acc, trades, orders = load(ACC_F), load(TRF_F), load(ODF_F)
    print('=' * 60)
    print(f"  📈 2026-09 AI 模拟盘报表  {datetime.datetime.now():%Y-%m-%d %H:%M}")
    print('=' * 60)
    total_mv = 0.0
    print(f"现金: {acc['cash']:,.0f}")
    for code, pos in acc['positions'].items():
        try:
            px, _, _, _, _ = tencent_px(pos['full'])
        except Exception:
            px = pos['cost']
        mv = px * pos['shares']
        total_mv += mv
        pnl = (px - pos['cost']) * pos['shares']
        # 2026-09-13 修复：locked 标志买入后永不清除，次日仍误显 [T+1锁定]；
        # 按 buy_date 与今日比较才是真实 T+1 状态（do_trade 的卖出拦截本就以此为准）
        tag = ' [T+1锁定]' if pos.get('buy_date') == datetime.datetime.now().strftime('%Y-%m-%d') else ''
        print(f"  {pos['name']} {pos['shares']}股 成本{pos['cost']:.2f} 现{px:.2f} "
              f"浮盈{pnl:+,.0f} ({(px/pos['cost']-1)*100:+.1f}%){tag}")
    nav = acc['cash'] + total_mv
    r = (nav / acc['initial'] - 1) * 100
    pos_pct = total_mv / nav * 100
    print(f"总净值: {nav:,.0f}  收益 {r:+.2f}%  仓位 {pos_pct:.0f}%")
    sells = [t for t in trades if t['side'] == 'sell']
    if sells:
        wins = [t for t in sells if t.get('pnl', 0) > 0]
        avg_w = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t['pnl'] for t in sells if t['pnl'] <= 0) / len([t for t in sells if t['pnl'] <= 0]) if len(wins) < len(sells) else 0
        print(f"已平仓 {len(sells)} 笔 胜率 {len(wins)/len(sells)*100:.0f}% 平均盈 {avg_w:,.0f} 平均亏 {avg_l:,.0f}")
    pend = [o for o in orders if o['status'] in ('pending', 'awaiting_confirm')]
    if pend:
        print('委托单:', ' | '.join(f"{o['id']} {o['name']} {o['side']} {o['shares']}股 条件{o['cond']} [{o['status']}]" for o in pend))
    print('=' * 60)
    return nav


def snapshot():
    """盘后快照：净值+基准指数（基线=09-04 开盘）"""
    acc = load(ACC_F)
    nav = 0.0
    for code, pos in acc['positions'].items():
        try:
            px, _, _, _, _ = tencent_px(pos['full'])
        except Exception:
            px = pos['cost']
        nav += px * pos['shares']
    nav += acc['cash']
    hist = load(HIST_F)
    if not hist['base'].get('sh_open'):
        sh_px, sh_prev, sh_hi, sh_lo, _ = tencent_px('sh000001')
        cyb_px, cyb_prev, _, _, _ = tencent_px('sz399006')
        hist['base']['sh_open'] = sh_prev   # 09-04 首快照：昨收=09-04开盘基准（近似，首快照当日说明）
        hist['base']['cyb_open'] = cyb_prev
        save(HIST_F, hist)
        print(f"📌 基线记录: 上证 {sh_prev} / 创业板 {cyb_prev}（09-04 昨收近似开盘）")
    sh_px, sh_prev, sh_hi, sh_lo, _ = tencent_px('sh000001')
    cyb_px, cyb_prev, _, _, _ = tencent_px('sz399006')
    row = {'date': datetime.date.today().strftime('%Y-%m-%d'), 'nav': round(nav, 2),
           'cash': round(acc['cash'], 2),
           'sh': round(sh_px, 2), 'cyb': round(cyb_px, 2),
           'vs_init_pct': round((nav / acc['initial'] - 1) * 100, 3),
           'vs_sh_pct': round((sh_px / hist['base']['sh_open'] - 1) * 100, 3),
           'vs_cyb_pct': round((cyb_px / hist['base']['cyb_open'] - 1) * 100, 3),
           'positions': {c: p['shares'] for c, p in acc['positions'].items()}}
    days = hist.setdefault('days', [])
    days[:] = [d for d in days if d['date'] != row['date']]
    days.append(row)
    save(HIST_F, hist)
    print(f"📸 快照 {row['date']}: 净值 {nav:,.0f} ({row['vs_init_pct']:+.2f}%) | "
          f"上证 {row['vs_sh_pct']:+.2f}% / 创业板 {row['vs_cyb_pct']:+.2f}%")
    if row['vs_cyb_pct'] < -1:
        print('⚠️ 净值落后基准 >1%，需过程归因（见决策日志）')


def scan(topn=15):
    """全市场候选扫描（盘前用，供 AI 自主选股）：
    东财全市场按主力净流入排序 → 过滤（涨停/量比/市值区间）→ 拉腾讯量比+MA5 回踩位。
    输出候选池，选股决策仍由 AI 写进决策日志（scan 只负责"找鱼"不负责"开枪"）。"""
    import json as _json
    print('⏳ 扫描全市场主力净流入排名...')
    raw = urllib.request.urlopen(
        'http://92.push2.eastmoney.com/api/qt/clist/get?pn=1&pz=80&po=1&np=1&fltt=2&invt=2'
        '&fid=f62&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14,f2,f3,f62,f66,f184,f10,f8',
        timeout=15).read().decode('utf-8', errors='replace')
    items = _json.loads(raw)['data']['diff']
    today = datetime.date.today().strftime('%Y-%m-%d')
    cands = []
    for it in items:
        code, nm = it.get('f12', ''), it.get('f14', '')
        px, pct = it.get('f2'), it.get('f3')
        main_net = it.get('f62')          # 主力净额（元）
        turnover = it.get('f8')           # 换手率%
        mcap = it.get('f20') or 0
        if not isinstance(px, (int, float)) or not isinstance(main_net, (int, float)):
            continue
        if px < 2 or px > 300:            # 剔除异常价
            continue
        if pct is not None and pct >= (19.5 if code[:2] in ('30', '68') else 9.8):
            continue                       # 涨停不追
        if main_net < 5e7:                 # 主力净流入 < 5000万 不要
            continue
        if turnover is None or not (0.5 < turnover < 15):
            continue                       # 换手 0.5%~15%
        if mcap and not (2e10 < mcap < 3e11):
            continue                       # 市值 200~3000亿
        cands.append({'code': code, 'name': nm, 'px': px, 'pct': pct,
                      'main_net_yi': round(main_net / 1e8, 2), 'turnover': turnover})
    print(f'⏳ 初筛 {len(cands)} 只，拉取量比/MA5 细节...')
    out = []
    for c in cands[:topn]:
        full = ('sh' if c['code'][0] in '56' else 'sz') + c['code']
        try:
            px, prev, hi, lo, nm, vr, ts = tencent_full(full)
            if not ts.replace('-', '')[:8] == today.replace('-', ''):
                continue
        except Exception:
            continue
        try:
            import stock_quant as sq
            k = sq.fetch_kline_sina(full, scale=240, datalen=8)
            ma5 = sum(float(d['close']) for d in k[-5:]) / min(5, len(k))
            pullback = (px / ma5 - 1) * 100   # >0 站上MA5 / <0 回踩MA5下方
        except Exception:
            ma5, pullback = None, None
        c.update({'vol_ratio': vr, 'ma5': round(ma5, 2) if ma5 else None,
                  'vs_ma5_pct': round(pullback, 2) if pullback is not None else None})
        out.append(c)
    out.sort(key=lambda x: -x['main_net_yi'])
    print()
    print(f"  {'代码':<8}{'名称':<10}{'现价':>8}{'涨跌%':>8}{'主力亿':>8}{'量比':>7}{'MA5':>8}{'距MA5%':>8}{'换手%':>7}")
    for c in out:
        print(f"  {c['code']:<8}{c['name']:<10}{c['px']:>8.2f}{c['pct']:>8.2f}"
              f"{c['main_net_yi']:>8.2f}{str(c['vol_ratio']):>7}"
              f"{str(c['ma5']):>8}{str(c['vs_ma5_pct']):>8}{c['turnover']:>7.2f}")
    print(f"\n（{len(out)} 只候选，scan 只找鱼，开枪由决策日志定）")
    return out


def addorder(args):
    """盘中预案→委托单（pending，sim_loop 节点自动评估执行）
    修复(2026-09-29 执行缺口)：盘中对照的口头预案（如 14:45 回补三条件成立）
    原先只能事后人工补录，方案与账户脱节。本命令让预案即时落单：
      - --pct 按【净值比例】自动算股数（0.269=26.9%≈2.7成），消除人工换算错误
        （O17 曾把 0.5 成误执行为 1.75 成——比例口径由系统统一计算）
      - --for-date 支持挂次日单（节前定版等场景）
      - 默认 pending → evaluate 节点自动执行；--manual 才走人工核验通道
    用法: addorder <full> <buy|sell> [--pct 0.05 (=5%≈0.5成) | --shares N] [--max P] [--min P]
            [--min_vol_ratio V] [--main_net_min M] [--stop S] [--for-date YYYY-MM-DD]
            [--manual] [--note "..."]"""
    if len(args) < 2:
        print(__doc__); return
    full, side = args[0], args[1]
    code = full[2:]
    if side not in ('buy', 'sell'):
        print('❌ side 必须为 buy/sell'); return
    opts, note_parts = {}, []
    i = 2
    while i < len(args):
        a = args[i]
        if a == '--pct': opts['pct'] = float(args[i+1]); i += 2
        elif a == '--shares': opts['shares'] = int(float(args[i+1])); i += 2
        elif a == '--max': opts['max'] = float(args[i+1]); i += 2
        elif a == '--min': opts['min'] = float(args[i+1]); i += 2
        elif a == '--min_vol_ratio': opts['min_vol_ratio'] = float(args[i+1]); i += 2
        elif a == '--main_net_min': opts['main_net_min'] = float(args[i+1]); i += 2
        elif a == '--stop': opts['stop'] = float(args[i+1]); i += 2
        elif a == '--for-date': opts['for_date'] = args[i+1]; i += 2
        elif a == '--manual': opts['manual'] = True; i += 1
        elif a == '--note': note_parts = args[i+1:]; break
        else: i += 1
    note = ' '.join(note_parts) or '盘中预案'
    for_date = opts.get('for_date') or datetime.date.today().strftime('%Y-%m-%d')
    try:
        px_now, prev, hi, lo, nm = tencent_px(full)
    except Exception as e:
        print(f'❌ 行情获取失败: {e}'); return
    acc = load(ACC_F)
    # 净值：现金 + 持仓按实时价（行情失败的持仓退回成本价）
    mv = 0.0
    for p in acc.get('positions', {}).values():
        try:
            cp = tencent_px(p.get('full') or p.get('code'))[0]
        except Exception:
            cp = p.get('cost', 0) or 0
        mv += cp * p.get('shares', 0)
    nav = acc['cash'] + mv
    if 'pct' in opts:
        shares = int(nav * opts['pct'] / px_now // 100) * 100
        if shares < 100:
            print(f'❌ 成数换算不足100股（净值{nav:,.0f} × {opts["pct"]} ÷ {px_now}）'); return
    elif 'shares' in opts:
        shares = int(opts['shares'] // 100) * 100
        if shares <= 0:
            print('❌ 股数须为 100 的正整数倍'); return
    else:
        print('❌ 必须指定 --pct 或 --shares'); return
    cond = {k: opts[k] for k in ('max', 'min', 'min_vol_ratio', 'main_net_min') if k in opts}
    if side == 'buy' and not cond:
        print('⚠️ 买单无条件（下一节点市价成交）——确认非追高行为')
    with ledger_lock():
        acc2, trades, orders = load(ACC_F), load(TRF_F), load(ODF_F)
        rec = orders if isinstance(orders, list) else orders.get('orders', [])
        same_side = [o for o in rec if o.get('status') == 'pending' and o.get('side') == side]
        if len(same_side) >= 4:
            print(f'❌ 同方向 pending 已有 {len(same_side)} 单（防滥挂上限4），先撤单再挂'); return
        ids = [int(o['id'][1:]) for o in rec
               if isinstance(o.get('id'), str) and o['id'].startswith('O') and o['id'][1:].isdigit()]
        next_id = f'O{max(ids, default=0) + 1}'
        order = {
            'id': next_id, 'for_date': for_date, 'code': code, 'full': full,
            'name': nm, 'side': side, 'shares': shares, 'cond': cond,
            'manual_confirm': bool(opts.get('manual')), 'stop': opts.get('stop'),
            'status': 'pending',
            'reason': (f'盘中预案({datetime.datetime.now():%m-%d %H:%M} addorder, '
                       f"NAV{nav:,.0f}×{opts.get('pct', '')}成→{shares}股): {note}"),
        }
        rec.append(order)
        save(ODF_F, rec)
    tag = '（次日单，明日节点执行）' if for_date != datetime.date.today().strftime('%Y-%m-%d') else '（今日节点自动执行）'
    print(f'✅ {next_id} 已挂单 pending: {side} {nm}({full}) {shares}股 cond={cond} '
          f'stop={opts.get("stop")} for={for_date} {tag}')


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'report'
    if cmd == 'scan':
        scan(int(sys.argv[2]) if len(sys.argv) > 2 else 15)
        return
    if cmd == 'report':
        report()
    elif cmd == 'snapshot':
        report()
        snapshot()
    elif cmd == 'evaluate':
        evaluate()
    elif cmd == 'addorder':
        addorder(sys.argv[2:])
    elif cmd == 'confirm':
        oid, px = sys.argv[2], float(sys.argv[3])
        note = ' '.join(sys.argv[4:]) or '人工核验通过'
        with ledger_lock():
            acc, trades, orders = load(ACC_F), load(TRF_F), load(ODF_F)
            o = next((x for x in orders if x['id'] == oid), None)
            if not o or o['status'] != 'awaiting_confirm':
                print(f'❌ 委托单 {oid} 状态不符（需 awaiting_confirm）')
                return
            # 确认时按当前实时价执行，传入核验时价格仅作参考留痕
            px_now, prev_c, hi_c, lo_c, _ = tencent_px(o['full'])
            # 架构核查修复(2026-09-22)：confirm 路径原无日期校验——9:25 前确认时
            # 腾讯行情时间戳仍是昨日，prev_c=前日昨收，涨跌停线可能算错（除权/高开）。
            # 复用腾讯涨停/跌停价字段（p[47]/p[48]，交易所口径）替代自行计算，
            # 消除"prev 取数时点"这个根源问题。
            raw = urllib.request.urlopen(f'https://web.sqt.gtimg.cn/q={o["full"]}', timeout=10).read().decode('gbk', errors='replace')
            _p = raw.split('~')
            if len(_p) > 48 and _p[47].strip():
                _lim_up, _lim_dn = float(_p[47]), float(_p[48])
            else:
                _lim_up = round(prev_c * (1 + LIMIT(o['code'])), 2)
                _lim_dn = round(prev_c * (1 - LIMIT(o['code'])), 2)
            if px_now >= _lim_up:
                print(f'❌ 已触涨停{_lim_up}无法买入')
                return
            if o['side'] == 'sell' and px_now <= _lim_dn:
                print(f'❌ 已触跌停{_lim_dn}无法卖出')
                return
            if not cond_met(o, px_now):
                print(f'❌ 当前价 {px_now} 已不满足条件 {o["cond"]}，委托单作废')
                o['status'] = 'expired'
                save(ODF_F, orders)
                return
            t, msg = do_trade(acc, trades, orders, o, px_now, note=f'人工核验@{px}: {note}', px_limit=(prev_c,))
            save(ACC_F, acc); save(TRF_F, trades); save(ODF_F, orders)
            print(('✅ ' if t else '⛔ ') + msg)
    elif cmd == 'reconcile':
        # 对账守门：--fix 以流水为准覆盖账本（默认审计模式只报差异）
        auto_fix = '--fix' in sys.argv[2:]
        diffs = reconcile(auto_fix=auto_fix)
        if not diffs:
            print('✅ 对账一致：账本与流水无差异')
        else:
            print(f"🚨 对账漂移（流水为真值）: {diffs}")
            if not auto_fix:
                print('   审计模式不改账本；确认流水无误后执行: python3 sim_report.py reconcile --fix')
    elif cmd == 'cancel':
        oid = sys.argv[2]
        note = ' '.join(sys.argv[3:]) or '手动撤单'
        with ledger_lock():
            orders = load(ODF_F)
            o = next((x for x in orders if x['id'] == oid), None)
            if o:
                o['status'] = 'cancelled'
                o['cancel_note'] = note
                save(ODF_F, orders)
                print(f'❌ {oid} 已撤单: {note}')
    else:
        print(__doc__)


if __name__ == '__main__':
    main()

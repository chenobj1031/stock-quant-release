#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sim_loop.py — 模拟盘盘中自主执行循环（launchd StartInterval=300 拉起）

设计（配合 sim_rules.md 全自主口径）：
- launchd 每 5 分钟调一次本脚本（单发模式，进程不常驻）
- 每次运行：① 机械评估今日委托单（evaluate）② 检查持仓止损/止盈线（机械执行）
  ③ 有动作则写入执行日志，无动作静默退出
- AI 的盘中角色：每 5 分钟节点快报之外，AI 可在会话内新增委托单（写入 sim_orders.json，
  次日或当日剩余时段生效），但**不能绕过本脚本直接改成交明细**
- 防重复：持仓止盈止损检查基于 cost 线，已触发的委托单状态机保证不重复成交
- 并发打架（2026-09-21 修复）：node_report（每日5节点）与 order_bridge（09:35）
  也读写同一批账本——check_stops / check_t_force_close 经 sr.locked 整函数
  临界区（fcntl 文件锁，锁对象 data/.sim.lock），消除 lost update 窗口
"""
import os, sys, json, re, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import sim_report as sr


def in_trading_hours():
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (915 <= hm <= 1132) or (1255 <= hm <= 1502)


def _check_stops():
    """持仓机械风控：跌破止损线 → 全部卖出（按当前价，模拟市价单）"""
    acc, trades, orders = sr.load(sr.ACC_F), sr.load(os.path.join(sr.DATA, 'sim_trades.json')), sr.load(sr.ODF_F)
    actions = []
    today = datetime.date.today().strftime('%Y-%m-%d')
    for code, pos in list(acc['positions'].items()):
        stop = pos.get('stop')
        if not stop:
            continue
        try:
            px, prev, hi, lo, nm, vr, ts = sr.tencent_full(pos['full'])
        except Exception:
            continue
        # 2026-09-13 修复：原式 `not ts... != today` 双重否定反转——当日数据反而跳过
        # 止损检查、陈旧(昨日)数据反而执行止损，与"当日数据才可用"纪律完全相反
        if ts.replace('-', '')[:8] != today.replace('-', ''):
            continue
        # 架构修复(2026-09-24)：原仅用瞬时价 px<=stop 判定——5分钟采样节点之间的
        # 盘中击穿会被漏掉（实例：莲花 09:5x 低点13.10<止损13.15，采样时已回升13.46，
        # 账本未执行）。止损是预先挂出的机械单，任何触碰都应成交：
        # ①隔夜止损（stop_date<今日）用当日低点 lo 判定触碰；
        # ②成交价按真实止损单语义：触碰后回升（px>stop）→ 按止损线价成交（触线即填）；
        #   仍在线下（px<=stop，含跳空低开）→ 按当前价成交（不允许假装在线上卖出）；
        # ③当日新设止损（stop_date==今日）仍用瞬时价——当日低点可能发生在设线之前。
        note_prefix = (pos.get('stop_note') or '')[:10]
        stop_date = pos.get('stop_date') or (note_prefix if note_prefix.count('-') == 2 else today)
        lo_eff = lo if (lo and lo > 0) else None
        touched_overnight = (stop_date < today and lo_eff is not None and lo_eff <= stop)
        if px <= stop or touched_overnight:
            exec_px = stop if px > stop else min(stop, px)
            sh_before = pos['shares']  # do_trade 会把 shares 减为 0，日志须用执行前的股数
            o = {'id': f"S{code}", 'for_date': today, 'code': code, 'full': pos['full'],
                 'name': pos['name'], 'side': 'sell', 'shares': pos['shares'],
                 'cond': {}, 'status': 'pending'}
            _why = f'止损线{stop}触发，机械卖出'
            if touched_overnight and px > stop:
                _why += f'（盘中击穿回溯：当日低点{lo_eff}触碰{stop}，采样时已回升{px}，按止损线价回溯成交）'
            t, msg = sr.do_trade(acc, trades, orders, o, exec_px, note=_why, px_limit=(prev,))
            if t:
                pos['stop'] = None
                actions.append(f"🛑 {pos['name']} 止损 {sh_before}股 @{t['px_exec']}（线{stop}）pnl {t.get('pnl', 0):+,.0f}")
                # P2-11b(2026-09-22体检)：清仓后同标的 pending 卖单（锁盈梯残余）持仓
                # 已不存在，留着会在 evaluate 里报"持仓不足"噪音——自动撤销留痕
                for oo in orders:
                    if (oo.get('status') == 'pending' and oo.get('side') == 'sell'
                            and oo.get('code') == code and oo.get('for_date') == today):
                        oo['status'] = 'cancelled'
                        oo['cancel_note'] = f"{datetime.datetime.now():%H:%M} 止损清仓后自动撤销（持仓已不存在）"
                        actions.append(f"🧹 {oo['id']} {oo.get('name','')} 随止损清仓自动撤销")
    sr.save(sr.ACC_F, acc)
    sr.save(os.path.join(sr.DATA, 'sim_trades.json'), trades)
    return actions


# 2026-09-21 并发打架修复：整函数=账本临界区（fcntl 文件锁，与 node_report/order_bridge 共享）
check_stops = sr.locked(_check_stops)


def _check_t_force_close():
    """14:45 做T单腿自动强平（铁律3，2026-09-18 修复缺失逻辑）：
    做T卖腿委托(reason含'14:45单腿未配对则市价强平'协议)当日14:45未成交 →
    按当前市价强平同代码可卖股数(走 do_trade 落流水：入cash+减持仓+写trades)。
    背景(9-15 事故根因)：O9 卖腿 14:45 未强平 → 会话人工补账绕过 do_trade →
    cash/positions 漂移 → 9-18 止损按错误持仓卖出 2400 股。
    防重复：强平成功后订单状态置 force_closed，后续节点跳过。"""
    acc, trades, orders = sr.load(sr.ACC_F), sr.load(os.path.join(sr.DATA, 'sim_trades.json')), sr.load(sr.ODF_F)
    actions = []
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return actions
    hm = now.hour * 100 + now.minute
    if not (1445 <= hm <= 1502):   # 仅 14:45~15:02 窗口执行，其余时段静默
        return actions
    today = now.strftime('%Y-%m-%d')
    for o in orders:
        if o['status'] != 'pending' or o['side'] != 'sell' or o['for_date'] != today:
            continue
        if not re.search(r'14:45单腿未配对则市价强平|单腿强平|失败回补协议', o.get('reason', '')):
            continue  # 仅处理带强平协议标记的做T卖腿
        code = o['code']
        pos = acc['positions'].get(code)
        # T+1：只强平可卖股(老股)
        today_bought = pos.get('today_bought', 0) if pos and pos.get('buy_date') == today else 0
        sellable = (pos['shares'] - today_bought) if pos else 0
        if sellable < 100:
            o['status'] = 'cancelled'
            o['cancel_note'] = f'{now:%H:%M} 14:45强平检查：可卖股<100(今买锁定{today_bought})，协议作废留痕'
            # P2-7 升级(2026-09-22体检)：原与普通留痕同级——9-15 事故的根因路径就是
            # "卖腿敞口无人收口"静默滑过；作废意味着买腿敞口未平，必须 ERROR 级提示
            actions.append(f"🚨 ERROR {o['id']} {o['name']} 14:45强平：可卖股不足(<100, 今买锁定{today_bought})，协议作废——买腿敞口未平，需人工核验")
            continue
        try:
            px, prev, hi, lo, nm, vr, ts = sr.tencent_full(o['full'])
        except Exception as e:
            actions.append(f"⚠️ {o['id']} 14:45强平：行情获取失败 {e}（下次节点重试）")
            continue
        if ts.replace('-', '')[:8] != today.replace('-', ''):
            actions.append(f"⚠️ {o['id']} 14:45强平：行情非今日({ts})，下次节点重试")
            continue
        o['shares'] = min(o['shares'], sellable)
        t, msg = sr.do_trade(acc, trades, orders, o, px, note='14:45做T单腿未配对，市价强平回补(铁律3)', px_limit=(prev,))
        if t:
            o['status'] = 'force_closed'
            o['force_closed_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
            actions.append(f"🔒 {o['name']} 14:45单腿强平 {t['shares']}股 @{t['px_exec']} pnl {t.get('pnl', 0):+,.0f}")
        else:
            actions.append(f"⛔ {o['id']} 14:45强平失败：{msg}（下次节点重试）")
    if actions:   # 有状态变更(强平成功/作废/失败重试留痕)才落盘
        sr.save(sr.ACC_F, acc)
        sr.save(os.path.join(sr.DATA, 'sim_trades.json'), trades)
        sr.save(sr.ODF_F, orders)
    return actions


# 2026-09-21 并发打架修复：整函数=账本临界区
check_t_force_close = sr.locked(_check_t_force_close)


def main():
    logf = os.path.join(BASE, '复盘', 'sim_log', 'exec.log')
    os.makedirs(os.path.dirname(logf), exist_ok=True)
    def log(line):
        stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(logf, 'a', encoding='utf-8') as f:
            f.write(f"[{stamp}] {line}\n")
    if not in_trading_hours():
        return  # 非交易时段静默
    # 1) 委托单机械评估
    try:
        results = sr.evaluate(quiet=True)
        for r in results:
            log(f"EVAL {r}")
    except Exception as e:
        log(f"EVAL ERROR {e}")
    # 2) 持仓止损机械检查
    try:
        for a in check_stops():
            log(a)
    except Exception as e:
        log(f"STOP ERROR {e}")
    # 2.5) 14:45 做T单腿自动强平（2026-09-18 修复：铁律3缺失导致9-15人工补账漂移）
    try:
        for a in check_t_force_close():
            log(a)
    except Exception as e:
        log(f"T_FORCE ERROR {e}")
    # 2.6) 对账守门（2026-09-18 新增：流水为唯一真值，漂移=有人绕过do_trade手工改账）
    try:
        diffs = sr.reconcile()
        if diffs:
            log(f"🚨 RECONCILE 账本漂移(流水为真值): {diffs} —— 立即人工介入")
    except Exception as e:
        log(f"RECONCILE ERROR {e}")
    # 3) 盘中动态纠偏检测（2026-09-14 立项，A 档草案模式）：
    #    exec_plan 突破/证伪检测 + ALERT 去重落盘 + 草案单生成 + 10:00/13:00 快照
    #    检测失败静默（不阻塞委托/止损主流程）
    try:
        import alert_engine
        alerts = alert_engine.run()
        for a in alerts:
            log(f"ALERT {a['kind']} {a['name']}({a['code']}) {a['detail']}")
    except Exception as e:
        log(f"ALERT ERROR {e}")


if __name__ == '__main__':
    main()

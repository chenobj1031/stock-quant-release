#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sim_trade.py — 轻量撮合引擎（模拟账户：成本/滑点/T+1/整百股/加权均价）
=====================================================================
借鉴 khQuant 的 KhTradeManager（khTrade.py）撮合逻辑，为 stock-quant 的
信号验证（--calibrate / score_expert_plan）提供"真实可交易性"约束：

1. 交易成本模型（全部可配置）：
   - 佣金：比例 + 最低佣金（默认万3、最低5元）
   - 印花税：卖出 0.1%（A股卖出单边）
   - 过户费：沪市 0.001%（万0.1），深市无
   - 流量费：默认 0.1 元/笔
2. 滑点双模式：
   - tick 模式：按最小变动价跳数（0.01元×N跳）
   - ratio 模式：按比例（默认 0.1%，买入上浮/卖出下调）
3. 资金/持仓校验：买入校验 实际价×数量+成本 总需求；卖出校验 can_use_volume
4. T+1/T+0：T+1 当日买入不可卖（can_use_volume=0），次日开盘前更新；T+0 可卖
5. 100 股整数倍：买入按整百、卖出 floor 到整百
6. 加权均价：加仓时按 (旧均价×旧量+成交价×新量)/总量 更新
7. 净值序列：逐日记录 total_asset，供 最大回撤/夏普/盈亏比 计算

用法（独立模块，供其他脚本 import）：
  from sim_trade import SimAccount, TRADE_COST_DEFAULT
  acc = SimAccount(init_capital=100000, trade_cost=TRADE_COST_DEFAULT)
  acc.new_day(date_str)                # 每日开盘前：T+1 可用量更新
  acc.execute(code, action, price, volume, date_str, reason='')
  acc.mark_to_market(prices)           # 每日收盘按最新价重估
  acc.net_value_series / acc.trades / acc.equity_curve

成交返回：{'filled': bool, 'reason': str, 'actual_price': float,
           'trade_cost': float, 'cash': float, 'position': float}
"""
import math
import logging
import os
import json

# 撮合引擎日志（DEBUG=每笔明细，INFO=汇总，WARNING=拒单/异常）；排查时设 level=logging.DEBUG
logger = logging.getLogger('sim_trade')

# ── 默认交易成本（与 khQuant 默认值对齐）─────────────────
TRADE_COST_DEFAULT = {
    'commission_rate': 0.0003,   # 佣金比例 万3
    'min_commission': 5.0,       # 最低佣金（元）
    'stamp_tax_rate': 0.001,     # 卖出印花税 0.1%
    'transfer_fee_rate': 0.00001,  # 过户费 沪市万0.1（深市0）
    'flow_fee': 0.1,             # 流量费（元/笔）
    'slippage': {'type': 'ratio', 'tick_size': 0.01, 'tick_count': 2,
                 'ratio': 0.001},  # 默认 0.1% 比例滑点
    'price_decimals': 2,         # 股票2位，ETF 3位
    't0_mode': False,            # T+1 默认
}

# 真实滑点校准结果（slippage_calib.py --apply 写回），加载后覆盖默认滑点
_CALIB_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'data', 'slippage_calib.json')


def _load_calibrated_slippage():
    """读取真实滑点校准结果；存在则覆盖 TRADE_COST_DEFAULT 的滑点 ratio"""
    try:
        if os.path.exists(_CALIB_JSON):
            with open(_CALIB_JSON, encoding='utf-8') as f:
                calib = json.load(f)
            ratio = calib.get('ratio')
            if isinstance(ratio, (int, float)) and ratio > 0:
                TRADE_COST_DEFAULT['slippage']['ratio'] = float(ratio)
                logger.info('已加载真实滑点校准: ratio=%.4f%%（%d笔样本）',
                            ratio * 100, calib.get('n', 0))
    except Exception as e:
        logger.debug(f'slippage 校准加载失败: {e}')


_load_calibrated_slippage()


class SimAccount:
    """模拟账户：现金/持仓/成交/净值曲线"""

    def __init__(self, init_capital=100000.0, trade_cost=None):
        self.init_capital = float(init_capital)
        self.c = dict(TRADE_COST_DEFAULT)
        if trade_cost:
            self.c.update(trade_cost)
        self.cash = self.init_capital
        self.positions = {}          # code -> {volume, can_use_volume, avg_price, market_value}
        self.trades = []             # 逐笔成交记录
        self.daily_assets = []       # 每日 {date, total_asset, cash, market_value}
        self.date = None             # 当前交易日
        self.equity_curve = []       # 净值序列（total_asset / init_capital）

    # ── 交易成本 ─────────────────────────────────────────
    def _tick_size(self):
        """最小变动价：tick 模式下按价位档位自动推导（A股 0.01，ETF 0.001）
        配置 slippage.tick_size 为正数时优先用配置值，否则按 price_decimals 推导"""
        s = self.c['slippage']
        cfg = s.get('tick_size')
        if isinstance(cfg, (int, float)) and cfg > 0:
            return float(cfg)
        # 按价格精度推导：2位小数→0.01，3位小数→0.001（ETF）
        return 0.01 if self.c['price_decimals'] <= 2 else 0.001

    def _limit_price(self, code, prev_close, name=''):
        """涨跌停价：(prev_close ± limit_ratio)，按板块/ST 判定
        返回 (涨停价, 跌停价)；prev_close 无效返回 (None, None)
        ST/*ST ±5%；创业板(300/301)/科创板(688/689) ±20%；主板 ±10%"""
        if not prev_close or prev_close <= 0:
            return None, None
        pure = ''.join(ch for ch in str(code) if ch.isdigit())
        nm = (name or '').upper()
        if 'ST' in nm:  # ST/*ST 股 ±5%
            ratio = 0.05
        elif pure.startswith(('300', '301', '688', '689')):  # 创业板/科创板 ±20%
            ratio = 0.20
        else:  # 主板 ±10%
            ratio = 0.10
        dec = self.c['price_decimals']
        return round(prev_close * (1 + ratio), dec), round(prev_close * (1 - ratio), dec)

    def _slippage_price(self, price, action):
        """滑点后价格：buy 上浮，sell 下调"""
        s = self.c['slippage']
        if s.get('type') == 'tick':
            delta = self._tick_size() * s.get('tick_count', 2)
            return price + delta if action == 'buy' else price - delta
        ratio = s.get('ratio', 0.001)
        return price * (1 + ratio) if action == 'buy' else price * (1 - ratio)

    def calculate_commission(self, amount):
        """佣金：max(金额×费率, 最低佣金)"""
        return max(amount * self.c['commission_rate'], self.c['min_commission'])

    def calculate_stamp_tax(self, amount, action):
        """印花税：仅卖出"""
        return amount * self.c['stamp_tax_rate'] if action == 'sell' else 0.0

    def calculate_transfer_fee(self, code, amount):
        """过户费：沪市A股(6开头,含科创板688) 万0.1；沪市B股(9开头)无过户费(走结算费另算)；深市 0"""
        pure = ''.join(ch for ch in str(code) if ch.isdigit())
        if pure.startswith('6'):  # 600/601/603/605/688 沪市A股
            return amount * self.c['transfer_fee_rate']
        return 0.0

    def calculate_trade_cost(self, price, volume, action, code):
        """总交易成本（元）= 佣金 + 印花税 + 过户费 + 流量费"""
        amount = price * volume
        comm = self.calculate_commission(amount)
        stamp = self.calculate_stamp_tax(amount, action)
        transfer = self.calculate_transfer_fee(code, amount)
        cost = comm + stamp + transfer + self.c['flow_fee']
        logger.debug('[cost] %s %s amount=%.2f comm=%.2f stamp=%.2f transfer=%.2f flow=%.2f total=%.2f',
                     code, action, amount, comm, stamp, transfer, self.c['flow_fee'], cost)
        return cost

    def calculate_slippage(self, price, action):
        """计算滑点后的实际成交价（保留价格精度）"""
        dec = self.c['price_decimals']
        return round(self._slippage_price(price, action), dec)

    # ── 每日流程 ─────────────────────────────────────────
    def new_day(self, date_str):
        """新交易日开盘前：T+1 模式下把 can_use_volume 更新为 volume"""
        self.date = date_str
        if not self.c.get('t0_mode'):
            for pos in self.positions.values():
                pos['can_use_volume'] = pos['volume']

    def mark_to_market(self, prices):
        """每日收盘：按最新价重估持仓市值，记录总资产/净值"""
        total_mv = 0.0
        for code, pos in self.positions.items():
            px = prices.get(code, pos.get('last_price', pos['avg_price']))
            if px is None or px <= 0:
                px = pos['avg_price']
            pos['market_value'] = round(px * pos['volume'], 2)
            pos['last_price'] = px
            total_mv += pos['market_value']
        total_asset = self.cash + total_mv
        self.daily_assets.append({'date': self.date, 'total_asset': round(total_asset, 2),
                                  'cash': round(self.cash, 2),
                                  'market_value': round(total_mv, 2)})
        self.equity_curve.append(total_asset / self.init_capital if self.init_capital else 1.0)
        logger.debug('[mtm] date=%s cash=%.2f mv=%.2f total=%.2f equity=%.4f positions=%d',
                     self.date, self.cash, total_mv, total_asset,
                     self.equity_curve[-1], len(self.positions))
        return round(total_asset, 2)

    # ── 下单 ─────────────────────────────────────────────
    def max_buy_volume(self, code, price, cash_ratio=1.0):
        """按可用资金比例计算最大可买股数（含成本、滑点、整百约束）
        2026-08-25 修复：改用滑点后的成交价计算——原用信号价估算，
        边界高价股（price*1000≈满仓）因滑点上浮超支被 execute 拒单（calibrate 3/73 跳过）
        """
        if price <= 0:
            return 0
        budget = self.cash * cash_ratio
        if budget <= 0:
            return 0
        # 滑点后的成交价（买入上浮），与 execute 口径一致
        slip_price = self.calculate_slippage(price, 'buy')
        if slip_price <= 0:
            return 0
        # 预留成本：佣金率 + 过户费率（沪市），印花税买入免；流量费按笔忽略（大金额可忽略）
        pure = ''.join(ch for ch in str(code) if ch.isdigit())
        transfer_rate = self.c['transfer_fee_rate'] if pure.startswith('6') else 0.0
        cost_adj = 1.0 + self.c['commission_rate'] + transfer_rate
        volume = int(budget / (slip_price * cost_adj) // 100 * 100)
        # 校验含成本后资金是否真的够（用滑点后价格）
        while volume > 0:
            cost = self.calculate_trade_cost(slip_price, volume, 'buy', code)
            if slip_price * volume + cost <= budget:
                break
            volume -= 100
        return max(volume, 0)

    def execute(self, code, action, price, volume, date_str=None, reason='',
                prev_close=None, name=''):
        """执行一笔交易（信号级）。返回成交结果 dict。
        action: 'buy'|'sell'；volume 为股数（内部强制整百）。
        prev_close: 昨收价，传入则校验涨跌停（涨停拒买/跌停拒卖，2026-08-25 新增）
        name: 股票名（ST 判定用，影响涨跌停比例）
        """
        dec = self.c['price_decimals']
        if date_str:
            if self.date != date_str:
                self.new_day(date_str)
        action = action.lower()
        result = {'filled': False, 'reason': '', 'actual_price': 0.0,
                  'trade_cost': 0.0, 'cash': self.cash, 'position': 0}
        logger.debug('[execute] %s %s %s vol=%d date=%s price=%s', code, action, name, volume, date_str, price)

        # 停牌/无效价格校验（2026-08-25 新增）
        if not price or price <= 0:
            result['reason'] = '停牌或无效价格'
            logger.warning('[execute] %s %s 拒单: %s', code, action, result['reason'])
            return result

        # 整百约束：买入必须整百，卖出允许零股清仓（A股规则）
        if action == 'buy':
            volume = int(volume // 100 * 100)
            if volume <= 0:
                result['reason'] = '买入股数不足100股'
                logger.warning('[execute] %s buy 拒单: %s', code, result['reason'])
                return result
        else:
            volume = int(volume)
            if volume <= 0:
                result['reason'] = '卖出股数不足1股'
                logger.warning('[execute] %s sell 拒单: %s', code, result['reason'])
                return result

        actual_price = self.calculate_slippage(price, action)
        # 涨跌停校验（2026-08-25 新增：避免回测买在涨停板/卖在跌停板）
        if prev_close and prev_close > 0:
            up, down = self._limit_price(code, prev_close, name)
            if up is not None and down is not None:
                if action == 'buy' and actual_price >= up:
                    result['reason'] = f'涨停拒买: 成交价{actual_price} >= 涨停价{up}'
                    logger.warning('[execute] %s buy 拒单: %s (prev_close=%s)', code, result['reason'], prev_close)
                    return result
                if action == 'sell' and actual_price <= down:
                    result['reason'] = f'跌停拒卖: 成交价{actual_price} <= 跌停价{down}'
                    logger.warning('[execute] %s sell 拒单: %s (prev_close=%s)', code, result['reason'], prev_close)
                    return result
        trade_cost = self.calculate_trade_cost(actual_price, volume, action, code)

        if action == 'buy':
            required = actual_price * volume + trade_cost
            if self.cash < required:
                result['reason'] = (f'资金不足: 需{required:.{dec}f}(含成本{trade_cost:.{dec}f}) '
                                    f'可用{self.cash:.{dec}f}')
                logger.warning('[execute] %s buy 拒单: %s', code, result['reason'])
                return result
            self.cash -= required
            pos = self.positions.get(code)
            if pos is None:
                self.positions[code] = {
                    'volume': volume,
                    'can_use_volume': volume if self.c.get('t0_mode') else 0,
                    'avg_price': actual_price,
                    'market_value': round(actual_price * volume, 2),
                    'last_price': actual_price,
                }
            else:
                old_v, old_avg = pos['volume'], pos['avg_price']
                pos['avg_price'] = round((old_avg * old_v + actual_price * volume) / (old_v + volume), dec)
                pos['volume'] += volume
                if self.c.get('t0_mode'):
                    pos['can_use_volume'] += volume
                pos['market_value'] = round(actual_price * pos['volume'], 2)
                pos['last_price'] = actual_price
        else:  # sell
            pos = self.positions.get(code)
            avail = pos.get('can_use_volume', 0) if pos else 0
            if not pos or avail < volume:
                result['reason'] = f'可用持仓不足: 需{volume} 可用{avail}'
                logger.warning('[execute] %s sell 拒单: %s', code, result['reason'])
                return result
            cash_in = actual_price * volume - trade_cost
            self.cash += cash_in
            pos['volume'] -= volume
            pos['can_use_volume'] -= volume
            if pos['volume'] <= 0:
                del self.positions[code]
            else:
                pos['market_value'] = round(actual_price * pos['volume'], 2)
                pos['last_price'] = actual_price

        self.trades.append({
            'date': self.date, 'code': code, 'action': action, 'price': round(actual_price, dec),
            'volume': volume, 'trade_cost': round(trade_cost, 2),
            'amount': round(actual_price * volume, 2), 'reason': reason,
        })
        result.update({'filled': True, 'actual_price': round(actual_price, dec),
                       'trade_cost': round(trade_cost, 2), 'cash': self.cash,
                       'position': self.positions.get(code, {}).get('volume', 0)})
        logger.debug('[execute] %s %s 成交 price=%s vol=%d cost=%.2f cash=%.2f pos=%d',
                     code, action, result['actual_price'], volume, trade_cost,
                     result['cash'], result['position'])
        return result

    # ── 统计口径（供绩效指标复用）────────────────────────
    def realized_pnl_by_code(self):
        """按股票结算已实现盈亏：卖出金额 - 加权成本×卖出量（khQuant 口径）"""
        summary = {}
        for t in self.trades:
            c = summary.setdefault(t['code'], {'buy_cost': 0.0, 'buy_vol': 0,
                                               'sell_amount': 0.0, 'sell_vol': 0})
            if t['action'] == 'buy':
                c['buy_cost'] += t['amount']
                c['buy_vol'] += t['volume']
            else:
                c['sell_amount'] += t['amount']
                c['sell_vol'] += t['volume']
        out = {}
        for code, c in summary.items():
            avg_cost = c['buy_cost'] / c['buy_vol'] if c['buy_vol'] else 0
            out[code] = round((c['sell_amount'] - avg_cost * c['sell_vol']), 2)
        return out

    def summary(self):
        """账户概览（文本）"""
        lines = [f'初始资金: {self.init_capital:.2f}',
                 f'当前现金: {self.cash:.2f}',
                 f'持仓数: {len(self.positions)}',
                 f'成交笔数: {len(self.trades)}',
                 f'净值末值: {self.equity_curve[-1]:.4f}' if self.equity_curve else '净值: 空']
        for code, pos in self.positions.items():
            lines.append(f'  {code}: {pos["volume"]}股 @均{pos["avg_price"]:.2f} '
                         f'(可用{pos["can_use_volume"]})')
        return '\n'.join(lines)


# ── 绩效指标公共工具（供 score_expert_plan / daily_review 复用，统一口径）──
def round_trip_cost_rate(trade_cost=None, code=''):
    """双向（买入+卖出）成本率（小数）：2×佣金率 + 印花税 + 滑点×2 + 过户费率(沪市A股)
    用于做空收益口径的对称成本扣减，替代硬编码 0.2%。
    返回小数（如 0.00361 = 0.361%）。"""
    c = dict(TRADE_COST_DEFAULT)
    if trade_cost:
        c.update(trade_cost)
    slip = c.get('slippage', {})
    slip_rate = slip.get('ratio', 0.0) if slip.get('type') == 'ratio' else 0.0
    pure = ''.join(ch for ch in str(code) if ch.isdigit())
    transfer = c['transfer_fee_rate'] if pure.startswith('6') else 0.0
    return 2 * c['commission_rate'] + c['stamp_tax_rate'] + 2 * slip_rate + transfer


def sharpe_ratio(returns, periods_per_year=250, rf=0.025):
    """年化夏普（khQuant 口径）：(mean - rf_per_period) / std × sqrt(periods_per_year)
    returns: 收益率序列，单位 %（如 1.5 表示 +1.5%）
    rf: 无风险年利率（默认 2.5%，十年期国债近似）
    n<3 或 std<=0 返回 None
    """
    n = len(returns)
    if n < 3:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std <= 0:
        return None
    # rf 是年利率（如 0.025），returns 单位是 %，把 rf 折成每期 % 再扣
    rf_per_period_pct = rf * 100 / periods_per_year
    return round((mean - rf_per_period_pct) / std * math.sqrt(periods_per_year), 2)


if __name__ == '__main__':
    # 自测：T+1 + 成本 + 滑点 + 整百 + 加权均价
    acc = SimAccount(init_capital=100000)
    acc.new_day('2026-08-24')
    r1 = acc.execute('sh600519', 'buy', 20.0, 2000, date_str='2026-08-24', reason='测试买入')
    assert r1['filled'], r1['reason']
    assert r1['actual_price'] > 20.0, '买入应有滑点上浮'
    assert r1['trade_cost'] > 0
    # T+1: 当日买入不可卖
    r_sell_same_day = acc.execute('sh600519', 'sell', 20.5, 1000, date_str='2026-08-24')
    assert not r_sell_same_day['filled'], 'T+1当日卖出应被拒'
    # 次日可卖
    r2 = acc.execute('sh600519', 'sell', 20.5, 1000, date_str='2026-08-25', reason='测试卖出')
    assert r2['filled'], r2['reason']
    # 加仓加权均价（零滑点配置下应为精确 11.0）
    acc0 = SimAccount(init_capital=100000,
                      trade_cost={'slippage': {'type': 'ratio', 'ratio': 0.0}})
    acc0.new_day('2026-08-25')
    acc0.execute('sz300750', 'buy', 10.0, 1000, date_str='2026-08-25')
    acc0.execute('sz300750', 'buy', 12.0, 1000, date_str='2026-08-25')
    assert abs(acc0.positions['sz300750']['avg_price'] - 11.0) < 1e-9, acc0.positions['sz300750']['avg_price']
    # 资金不足拒绝
    acc2 = SimAccount(init_capital=1000)
    r3 = acc2.execute('sz000001', 'buy', 50.0, 1000, date_str='2026-08-24')
    assert not r3['filled'] and '资金不足' in r3['reason']
    # 整百约束
    acc3 = SimAccount(init_capital=100000)
    r4 = acc3.execute('sz000001', 'buy', 10.0, 150, date_str='2026-08-24')
    assert r4['filled'] and r4['position'] == 100
    # 净值曲线
    acc3.mark_to_market({'sz000001': 11.0})
    assert len(acc3.equity_curve) == 1
    print('✅ sim_trade 自测通过')
    print(acc.summary())

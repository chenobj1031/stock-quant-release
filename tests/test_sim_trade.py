import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sim_trade import SimAccount, round_trip_cost_rate, sharpe_ratio


class TestSimTrade(unittest.TestCase):
    def setUp(self):
        self.acc = SimAccount(init_capital=100000)
        self.acc.new_day('2026-08-25')

    def test_buy_sell_roundtrip_with_cost(self):
        """看多含成本收益 < 价格收益率"""
        acc = SimAccount(init_capital=100000, trade_cost={'t0_mode': True})
        acc.new_day('2026-08-25')
        b = acc.execute('sh600519', 'buy', 10.0, 1000, date_str='2026-08-25')
        self.assertTrue(b['filled'])
        s = acc.execute('sh600519', 'sell', 10.5, 1000, date_str='2026-08-25')
        self.assertTrue(s['filled'])
        buy_total = b['actual_price'] * 1000 + b['trade_cost']
        sell_net = s['actual_price'] * 1000 - s['trade_cost']
        ret = (sell_net - buy_total) / buy_total * 100
        price_ret = (10.5 - 10.0) / 10.0 * 100
        self.assertLess(ret, price_ret, '含成本收益应<价格收益率')

    def test_limit_up_reject_buy_main(self):
        """主板涨停拒买 ±10%"""
        r = self.acc.execute('sh600519', 'buy', 11.0, 1000, date_str='2026-08-25', prev_close=10.0)
        self.assertFalse(r['filled'])
        self.assertIn('涨停', r['reason'])

    def test_limit_up_gem20(self):
        """创业板 ±20%"""
        r = self.acc.execute('sz300750', 'buy', 12.0, 1000, date_str='2026-08-25', prev_close=10.0)
        self.assertFalse(r['filled'])
        self.assertIn('涨停', r['reason'])

    def test_limit_up_st5(self):
        """ST ±5%"""
        r = self.acc.execute('sh600233', 'buy', 10.5, 1000, date_str='2026-08-25',
                            prev_close=10.0, name='ST华联')
        self.assertFalse(r['filled'])
        self.assertIn('涨停', r['reason'])

    def test_limit_down_reject_sell(self):
        """跌停拒卖"""
        acc = SimAccount(init_capital=100000, trade_cost={'t0_mode': True})
        acc.new_day('2026-08-25')
        acc.execute('sh600519', 'buy', 10.0, 1000, date_str='2026-08-25', prev_close=10.0)
        r = acc.execute('sh600519', 'sell', 9.0, 1000, date_str='2026-08-25', prev_close=10.0)
        self.assertFalse(r['filled'])
        self.assertIn('跌停', r['reason'])

    def test_normal_price_fills(self):
        """正常价成交"""
        r = self.acc.execute('sh600519', 'buy', 10.5, 1000, date_str='2026-08-25', prev_close=10.0)
        self.assertTrue(r['filled'])

    def test_suspended_reject(self):
        """停牌拒交易"""
        r = self.acc.execute('sh600519', 'buy', 0, 1000, date_str='2026-08-25')
        self.assertFalse(r['filled'])
        self.assertIn('停牌', r['reason'])

    def test_backward_compat_no_prev_close(self):
        """无 prev_close 向后兼容（不校验涨跌停）"""
        r = self.acc.execute('sh600519', 'buy', 10.5, 1000, date_str='2026-08-25')
        self.assertTrue(r['filled'])

    def test_fractional_share_sell(self):
        """零股清仓：卖出允许零股（原强制整百 bug 已修）"""
        acc = SimAccount(init_capital=100000, trade_cost={'t0_mode': True})
        acc.new_day('2026-08-25')
        acc.execute('sz300750', 'buy', 10.0, 200, date_str='2026-08-25')
        r = acc.execute('sz300750', 'sell', 10.5, 50, date_str='2026-08-25')
        self.assertTrue(r['filled'])
        self.assertEqual(r['position'], 150)

    def test_b_share_no_transfer_fee(self):
        """B股无过户费（原 9 开头误收 bug 已修）"""
        self.assertEqual(self.acc.calculate_transfer_fee('sh900927', 10000), 0)
        self.assertGreater(self.acc.calculate_transfer_fee('sh600519', 10000), 0)

    def test_tick_size_by_decimals(self):
        """tick_size 按精度推导（ETF 0.001 / 股票 0.01）"""
        acc_etf = SimAccount(trade_cost={'slippage': {'type': 'tick', 'tick_count': 2},
                                       'price_decimals': 3})
        self.assertEqual(acc_etf._tick_size(), 0.001)
        acc_stock = SimAccount(trade_cost={'slippage': {'type': 'tick', 'tick_count': 2},
                                           'price_decimals': 2})
        self.assertEqual(acc_stock._tick_size(), 0.01)

    def test_round_trip_cost_rate(self):
        """双向成本率 ~0.36%（替代硬编码 0.2%）
        显式传默认滑点配置（0.1%）：不受 data/slippage_calib.json 校准结果影响
        （校准后真实滑点更低属预期，见 slippage_calib.py）"""
        rt = round_trip_cost_rate(trade_cost={'slippage': {'type': 'ratio', 'ratio': 0.001}})
        self.assertGreater(rt, 0.003)
        self.assertLess(rt, 0.004)

    def test_sharpe_ratio(self):
        """夏普年化"""
        s = sharpe_ratio([1.0, 2.0, 1.5, 0.5, 2.5])
        self.assertIsNotNone(s)
        self.assertIsNone(sharpe_ratio([1.0]))  # n<3 返回 None


if __name__ == '__main__':
    unittest.main()

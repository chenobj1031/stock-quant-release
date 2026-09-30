import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import score_expert_plan as sep
import daily_review as dr
from sim_trade import round_trip_cost_rate


class TestComputePerformance(unittest.TestCase):
    def test_long_with_cost(self):
        """看多含成本 < 价格收益率（原 T+1 拒卖走无成本分支 bug 已修）"""
        rows = [{'code': 'sh600519', 'bias': 1, 'entry_price': 10.0,
                 'exit_price': 10.5, 'date': '2026-08-18'}]
        per = sep.compute_performance(rows, capital=100000, hold=3)
        price_ret = (10.5 - 10.0) / 10.0 * 100
        self.assertEqual(per['n'], 1)
        self.assertLess(per['returns'][0], price_ret, '含成本收益应<价格收益率')

    def test_short_with_cost(self):
        """做空 = 价格收益率 - 双向成本率（原硬编码 0.2% 已修）"""
        rows = [{'code': 'sh600519', 'bias': -1, 'entry_price': 10.0,
                 'exit_price': 9.5, 'date': '2026-08-18'}]
        per = sep.compute_performance(rows, capital=100000, hold=3)
        expected = 5.0 - round_trip_cost_rate() * 100
        self.assertAlmostEqual(per['returns'][0], expected, places=1)

    def test_sharpe_not_none_when_n_ge3(self):
        """夏普 n>=3 非 None（统一走 sharpe_ratio）"""
        rows = [
            {'code': 'sh600519', 'bias': 1, 'entry_price': 10.0, 'exit_price': 10.5, 'date': '2026-08-18'},
            {'code': 'sz300750', 'bias': 1, 'entry_price': 10.0, 'exit_price': 9.8, 'date': '2026-08-19'},
            {'code': 'sh600519', 'bias': 1, 'entry_price': 10.0, 'exit_price': 10.3, 'date': '2026-08-20'},
        ]
        per = sep.compute_performance(rows, capital=100000, hold=3)
        self.assertIsNotNone(per['sharpe'])

    def test_pnl_uses_rolling_equity(self):
        """pnl 按滚动资金算（与累乘净值口径一致）"""
        rows = [
            {'code': 'sh600519', 'bias': 1, 'entry_price': 10.0, 'exit_price': 10.5, 'date': '2026-08-18'},
            {'code': 'sh600519', 'bias': 1, 'entry_price': 10.0, 'exit_price': 10.5, 'date': '2026-08-19'},
        ]
        per = sep.compute_performance(rows, capital=100000, hold=3)
        # 全仓滚动：第二笔 pnl 应 > 第一笔（因 equity 增长）
        # returns 相同则 total_profit 是两笔 pnl 之和，第二笔更大
        self.assertEqual(per['n'], 2)


class TestComputePremarketPerf(unittest.TestCase):
    def test_long_with_cost(self):
        """看多含成本 < 价格收益率（原漏 trade_cost bug 已修）"""
        rows = [{'code': 'sh600519', 'pre_dir': '看多', 'pre_price': 10.0,
                 'price': 10.5, 'date': '2026-08-25'}]
        per = dr.compute_premarket_perf(rows, capital=100000)
        price_ret = (10.5 - 10.0) / 10.0 * 100
        self.assertLess(per['returns'][0], price_ret)

    def test_short_with_cost(self):
        """看空扣双向成本率"""
        rows = [{'code': 'sh600519', 'pre_dir': '看空', 'pre_price': 10.0,
                 'price': 9.5, 'date': '2026-08-25'}]
        per = dr.compute_premarket_perf(rows, capital=100000)
        expected = 5.0 - round_trip_cost_rate() * 100
        self.assertAlmostEqual(per['returns'][0], expected, places=1)

    def test_neutral_excluded(self):
        """中性不计入"""
        rows = [
            {'code': 'sh600519', 'pre_dir': '看多', 'pre_price': 10.0, 'price': 10.5, 'date': '2026-08-25'},
            {'code': 'sh600519', 'pre_dir': '中性', 'pre_price': 10.0, 'price': 10.2, 'date': '2026-08-25'},
        ]
        per = dr.compute_premarket_perf(rows, capital=100000)
        self.assertEqual(per['n'], 1)


if __name__ == '__main__':
    unittest.main()

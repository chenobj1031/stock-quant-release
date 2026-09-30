import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stock_quant as sq


class TestSignalStats(unittest.TestCase):
    def test_overall_fields_complete(self):
        """返回字段完整（含成本主字段 + price_* 对比）"""
        stats = sq.get_signal_stats()
        o = stats.get('overall', {})
        for k in ['total', 'avg_return', 'win_rate', 'pl_ratio', 'odds_net', 'illusion',
                  'price_win_rate', 'price_avg_return', 'price_pl_ratio', 'price_odds_net']:
            self.assertIn(k, o, f'缺字段 {k}')

    def test_cost_le_price_when_samples(self):
        """含成本均收益应 <= 价格口径（成本使收益更低）"""
        stats = sq.get_signal_stats()
        o = stats.get('overall', {})
        if o.get('total', 0) > 0:
            self.assertLessEqual(o['avg_return'], o['price_avg_return'] + 0.01,
                                 '含成本均收益应<=价格口径')

    def test_stats_list_structure(self):
        """stats[] 按策略分组，含必要字段"""
        stats = sq.get_signal_stats()
        for s in stats.get('stats', []):
            for k in ['strategy', 'total', 'win_rate', 'pl_ratio', 'odds_net']:
                self.assertIn(k, s, f'stats 缺字段 {k}')

    def test_workbuddy_collect_compat(self):
        """引用方 collect_signal_stats 字段兼容（自动含成本口径）"""
        import prepare_workbuddy_input as wb
        txt = wb.collect_signal_stats()
        self.assertIn('信号库统计', txt)


if __name__ == '__main__':
    unittest.main()

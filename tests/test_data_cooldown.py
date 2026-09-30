import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stock_quant as sq
from unittest.mock import patch, MagicMock


class TestDataCooldown(unittest.TestCase):
    """fetch_kline_sina 源级冷却机制（原 screener 1045 次重复失败 bug 已修）"""

    # ⚠️ 2026-08-25 修复：测试用 mock 数据（close=10.5）经 fetch_kline_sina 内部
    # cache_set 写入【真实缓存】data/*.cache，污染了 sh600519_240_60_1 缓存，
    # 导致后续所有 fetch_kline_sina('sh600519',240,60) 命中 1 条坏数据（K线数据不足）。
    # 修复：mock cache_set 为 no-op，测试数据绝不落盘。
    def setUp(self):
        self.cache_set_patch = patch.object(sq, 'cache_set', return_value=None)
        self.cache_set_patch.start()
        self.addCleanup(self.cache_set_patch.stop)

    def test_cooldown_daily_uses_eastmoney(self):
        """冷却期内日线直接走东财降级（不硬闯新浪）"""
        with patch.object(sq, '_em_cooldown_check', return_value=True), \
             patch.object(sq, 'fetch_kline_push2his',
                           return_value=[{'day': '2026-08-25', 'close': 10.5}]) as mock_em, \
             patch.object(sq, 'cache_get', return_value=None):
            r = sq.fetch_kline_sina('sh600519', 240, 60)
            self.assertTrue(mock_em.called, '冷却期内应调东财')
            self.assertEqual(len(r), 1)

    def test_cooldown_intraday_returns_empty(self):
        """冷却期内非日线返回空（无东财降级）"""
        with patch.object(sq, '_em_cooldown_check', return_value=True), \
             patch.object(sq, 'cache_get', return_value=None), \
             patch.object(sq, 'fetch_kline_push2his') as mock_em:
            r = sq.fetch_kline_sina('sh600519', 60, 60)
            self.assertFalse(mock_em.called, '非日线不应调东财')
            self.assertEqual(r, [])

    def test_empty_response_marks_cooldown(self):
        """空响应标记 sina 冷却"""
        with patch.object(sq, '_em_cooldown_check', return_value=False), \
             patch.object(sq, 'cache_get', return_value=None), \
             patch.object(sq.subprocess, 'run') as mock_run, \
             patch.object(sq, '_em_cooldown_mark') as mock_mark, \
             patch.object(sq, 'fetch_kline_push2his', return_value=[]):
            mock_run.return_value = MagicMock(stdout=b'')
            sq.fetch_kline_sina('sh600519', 240, 60)
            self.assertTrue(mock_mark.called, '空响应应标记冷却')


if __name__ == '__main__':
    unittest.main()

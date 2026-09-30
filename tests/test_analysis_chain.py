import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stock_quant as sq
import daily_review as dr


def make_tech(trend='多头', rsi=50, macd=0.5, duo_kong='做多', pct_5d=2.0,
              pct_20d=10.0, ma5=10, ma10=9.8, ma20=9.5, ma60=8.0,
              support=8.5, resistance=11.0, vol_ratio=1.2, kdj_j=50,
              qiang_shi=False, qiang_jin_qiang=False, top_overflow=False,
              rs_20=0.0):
    """构造最小可用的 tech dict（覆盖评分卡各因子读取的字段）"""
    return {'trend': trend, 'rsi': rsi, 'rsi_available': True, 'macd': macd,
            'duo_kong': duo_kong, 'pct_5d': pct_5d, 'pct_20d': pct_20d,
            'ma5': ma5, 'ma10': ma10, 'ma20': ma20, 'ma60': ma60,
            'support': support, 'resistance': resistance, 'vol_ratio': vol_ratio,
            'kdj_j': kdj_j, 'qiang_shi': qiang_shi,
            'qiang_jin_qiang': qiang_jin_qiang, 'top_overflow': top_overflow,
            'rs_20': rs_20}


class TestDirectionOf(unittest.TestCase):
    """评分 → 方向（写死可证伪：≥55看多、≤45看空、中间中性）"""

    def test_bull(self):
        self.assertEqual(dr.direction_of(55), '看多')
        self.assertEqual(dr.direction_of(80), '看多')

    def test_bear(self):
        self.assertEqual(dr.direction_of(45), '看空')
        self.assertEqual(dr.direction_of(20), '看空')

    def test_neutral(self):
        self.assertEqual(dr.direction_of(50), '中性')
        self.assertEqual(dr.direction_of(46), '中性')
        self.assertEqual(dr.direction_of(54), '中性')


class TestJudgeRecord(unittest.TestCase):
    """收盘判定：符合/方向符合/偏差/部分偏差"""

    def _pre(self, direction):
        return {'direction': direction, 'phase': 'premarket'}

    def test_bull_hit(self):
        """看多 & 实际>1% → 完全符合"""
        v, r = dr.judge_record(self._pre('看多'), {'pct': 2.5}, {})
        self.assertTrue(v.startswith('✅'))

    def test_bull_direction_only(self):
        """看多 & 实际0.5%（方向对幅度未达）→ 方向符合"""
        v, r = dr.judge_record(self._pre('看多'), {'pct': 0.5}, {})
        self.assertEqual(v, '✅ 方向符合')

    def test_bull_wrong(self):
        """看多 & 实际<0 → 方向偏差"""
        v, r = dr.judge_record(self._pre('看多'), {'pct': -1.5}, {})
        self.assertTrue(v.startswith('❌'))

    def test_bear_hit(self):
        v, r = dr.judge_record(self._pre('看空'), {'pct': -2.0}, {})
        self.assertTrue(v.startswith('✅'))

    def test_neutral_small(self):
        """中性 & |pct|<1% → 符合"""
        v, r = dr.judge_record(self._pre('中性'), {'pct': 0.3}, {})
        self.assertTrue(v.startswith('✅'))

    def test_neutral_big(self):
        """中性 & |pct|>=1% → 部分偏差"""
        v, r = dr.judge_record(self._pre('中性'), {'pct': 1.8}, {})
        self.assertTrue(v.startswith('⚠️'))

    def test_no_prev(self):
        v, r = dr.judge_record({'error': 'x'}, {'pct': 1.0}, {})
        self.assertEqual(v, '无法判定')


class TestJudgeExecPlan(unittest.TestCase):
    """预测可执行化判定：触发并达标/未触发/已失效"""

    def _pre(self, direction, tu=None, td=None, stop=None):
        return {'direction': direction,
                'exec_plan': {'trigger': 'x', 'target_up': tu, 'target_down': td, 'stop': stop}}

    def test_bull_hit_target(self):
        """看多 & 现价≥目标 → 触发并达标"""
        r = dr.judge_exec_plan(self._pre('看多', tu=74.0, td=70.0, stop=70.0), {'price': 75.0})
        self.assertEqual(r['verdict'], '触发并达标')
        self.assertTrue(r['triggered'] and r['hit_target'])

    def test_bull_not_triggered(self):
        """看多 & 现价未到目标 → 未触发"""
        r = dr.judge_exec_plan(self._pre('看多', tu=74.0, td=70.0, stop=70.0), {'price': 72.0})
        self.assertEqual(r['verdict'], '未触发')

    def test_bull_invalidated(self):
        """看多 & 现价≤止损 → 已失效"""
        r = dr.judge_exec_plan(self._pre('看多', tu=74.0, td=70.0, stop=70.0), {'price': 69.5})
        self.assertEqual(r['verdict'], '已失效')
        self.assertTrue(r['invalidated'])

    def test_bear_hit_target(self):
        """看空 & 现价≤目标 → 触发并达标"""
        r = dr.judge_exec_plan(self._pre('看空', tu=71.95, td=54.09, stop=71.95), {'price': 54.0})
        self.assertEqual(r['verdict'], '触发并达标')

    def test_neutral_break_up(self):
        """中性 & 突破上沿 → 触发并达标"""
        r = dr.judge_exec_plan(self._pre('中性', tu=74.0, td=70.0), {'price': 74.5})
        self.assertEqual(r['verdict'], '触发并达标')

    def test_neutral_in_zone(self):
        """中性 & 区间内 → 未触发"""
        r = dr.judge_exec_plan(self._pre('中性', tu=74.0, td=70.0), {'price': 72.0})
        self.assertEqual(r['verdict'], '未触发')

    def test_no_plan(self):
        """无 exec_plan → 返回 —"""
        r = dr.judge_exec_plan({'direction': '看多'}, {'price': 75.0})
        self.assertEqual(r['verdict'], '—')


class TestQuantScore(unittest.TestCase):
    """评分卡：因子聚合 + 方向分"""

    def _full_bull_mock(self):
        """构造全因子看多的完整 mock（9 因子全部为正，确保评分看多）"""
        tech = make_tech(trend='多头', duo_kong='做多', macd=0.5, pct_5d=6.0,
                         pct_20d=15.0, rs_20=20.0, qiang_shi=True, rsi=40)
        fund = {'pe_signal': '低估值', 'pb_signal': '低PB', 'roe': 20.0,
                'rev_growth': 25.0, 'profit_growth': 40.0}
        cap_flow = {'ratio': 1.2, 'main_net': 5.0, 'vol_ratio': 1.0,
                    'data_estimated': False, 'level2_available': True,
                    'level2_super_pct': 25.0}
        sentiment = {'score': 40}
        margin_data = {'available': True, 'net_flow': 1.0}
        inst_views = {'available': True, 'buy_count': 3, 'total_count': 5}
        patterns = [('测试', 'x', '看涨')]
        return tech, fund, cap_flow, sentiment, margin_data, inst_views, patterns

    def test_bullish_tech_scores_positive(self):
        """多头趋势 + 做多 + 正动量 + 正资金 → 评分应看多（≥55）"""
        tech, fund, cap, sent, marg, inst, pat = self._full_bull_mock()
        score = sq.calculate_quant_score(tech, fund, cap, sent, marg, inst, pat, 'sh600519')
        self.assertGreaterEqual(score['pct'], 55, f'强多头组合应看多，实际{score["pct"]}')

    def test_bearish_tech_scores_negative(self):
        """空头趋势 + 做空 + 负动量 → 评分应偏空"""
        tech = make_tech(trend='空头', duo_kong='做空', macd=-0.5, pct_5d=-6.0)
        score = sq.calculate_quant_score(tech, {}, {}, {}, {}, {}, [], 'sh600519')
        self.assertLessEqual(score['pct'], 45, f'空头组合应看空，实际{score["pct"]}')

    def test_scores_fields_complete(self):
        """scores 包含全部 9 因子"""
        tech = make_tech()
        score = sq.calculate_quant_score(tech, {}, {}, {}, {}, {}, [], 'sh600519')
        for k in ['动量', '技术', '基本面', '量能', '风险', '舆情', '资金流', 'Level2', '相对强弱']:
            self.assertIn(k, score['scores'], f'缺因子 {k}')

    def test_top_overflow_caps_bull(self):
        """乖离超阈值（top_overflow）→ 评分不应看多"""
        tech = make_tech(trend='多头', duo_kong='做多', top_overflow=True)
        score = sq.calculate_quant_score(tech, {}, {}, {}, {}, {}, [], 'sh600519')
        self.assertLess(score['pct'], 55, f'top_overflow 应禁止看多，实际{score["pct"]}')


class TestRoundtable(unittest.TestCase):
    """圆桌多视角：共识度 + 观察台 + 失效条件"""

    def test_consensus_full_bull(self):
        """全因子看多 → 全票一致偏多"""
        tech, fund, cap, sent, marg, inst, pat = TestQuantScore()._full_bull_mock()
        score = sq.calculate_quant_score(tech, fund, cap, sent, marg, inst, pat, 'sh600519')
        rt = sq.analyze_roundtable(score, tech, cap, 10.0)
        self.assertIn('偏多', rt['consensus'])
        self.assertGreaterEqual(rt['bull_count'], 3)

    def test_observation_has_ma20(self):
        """观察台应含 MA20 触发线"""
        tech = make_tech(ma20=9.5)
        score = sq.calculate_quant_score(tech, {}, {}, {}, {}, {}, [], 'sh600519')
        rt = sq.analyze_roundtable(score, tech, {}, 10.0)
        obs_vars = [o[0] for o in rt.get('observation', [])]
        self.assertIn('MA20', obs_vars)

    def test_invalidations_not_empty_when_bull(self):
        """偏多结论应有失效条件"""
        tech = make_tech(trend='多头', duo_kong='做多', macd=0.5, pct_5d=6.0)
        score = sq.calculate_quant_score(tech, {}, {}, {}, {}, {}, [], 'sh600519')
        rt = sq.analyze_roundtable(score, tech, {}, 10.0)
        self.assertTrue(rt.get('invalidations'), '偏多结论应有失效条件')


class TestBuildExecPlan(unittest.TestCase):
    """build_exec_plan：触发条件/目标位/失效条件生成"""

    def test_bull_plan(self):
        """看多 → 触发=突破压力，目标=压力位"""
        tech = make_tech(trend='多头', duo_kong='做多', resistance=11.0, support=8.5)
        plan = dr.build_exec_plan('看多', 60, tech, {})
        self.assertIn('11.00', plan['trigger'])
        self.assertEqual(plan['target_up'], 11.0)
        self.assertIn('失效', plan['invalidation'])

    def test_bear_plan(self):
        """看空 → 触发=跌破支撑，目标=支撑位"""
        tech = make_tech(trend='空头', duo_kong='做空', resistance=11.0, support=8.5)
        plan = dr.build_exec_plan('看空', 40, tech, {})
        self.assertIn('8.50', plan['trigger'])
        self.assertEqual(plan['target_down'], 8.5)

    def test_neutral_plan(self):
        """中性 → 等待方向选择"""
        tech = make_tech(trend='震荡', duo_kong='中性', resistance=11.0, support=8.5)
        plan = dr.build_exec_plan('中性', 50, tech, {})
        self.assertIn('方向选择', plan['trigger'])


if __name__ == '__main__':
    unittest.main()

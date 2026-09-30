"""信号策略阈值集中配置（2026-08-25 抽取，消除多文件重复定义）

改阈值只改这里一处，避免 signal_tracker / verify_chip_signal / mainline_backtest /
daily_review.chip_breakout_signal 不同步（原三处各自写死 40/1.5/5，易漏改）。
2026-08-25 补充：信号生命周期配置（signal_lifecycle.py 消费）——
每个信号类型的 启用开关/置信度乘数/淘汰阈值，支撑"无效信号自动淘汰"。
2026-08-26 架构优化补充：方向判定阈值（daily_review 消费，原散落 daily_review 顶部）
2026-08-28 架构优化补充：股票池单源 get_watch_pool()——data/pool.json 为唯一池定义，
daily_review（盘前/盘中/收盘）与 prepare_workbuddy_input 统一消费，
根治"池定义散落多处互不一致"。
"""
import json as _json
import os as _os

# ── 方向判定阈值（daily_review 消费，原散落 daily_review.py 顶部）──
DIR_BULL = 55   # score.pct >= 55 看多
DIR_BEAR = 45   # score.pct <= 45 看空
RET_BIG = 1.0   # 实际涨跌 |pct| >= 1% 视为涨/跌

# ── 股票池单源（2026-08-28 新增）──
# data/pool.json 为唯一池定义（{name, code, role} 列表），人工维护（模板见 data/pool.example.json）；
# 缺失/损坏时回退到内置示例池（仅首次运行体验用，请替换为自己的自选池）。
POOL_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'data', 'pool.json')
_POOL_FALLBACK = [
    ('贵州茅台', 'sh600519'), ('招商银行', 'sh600036'),
    ('宁德时代', 'sz300750'), ('长江电力', 'sh600900'),
    ('银行ETF', 'sh512800'),
]


def get_watch_pool():
    """股票池单源：优先读 data/pool.json，失败回退内置默认池
    返回 [(name, code), ...]，顺序即 pool.json 顺序
    """
    return [(n, c) for n, c, _ in get_watch_pool_with_role()]


def get_watch_pool_with_role():
    """股票池单源（含 role）：返回 [(name, code, role), ...]
    2026-09-01 新增：Workbuddy 提示词标的池动态注入需要区分持仓/观察归属
    """
    try:
        with open(POOL_FILE, encoding='utf-8') as f:
            data = _json.load(f)
        out = [(e['name'], e['code'], e.get('role', '观察'))
               for e in data.get('stocks', [])
               if isinstance(e, dict) and e.get('name') and e.get('code')]
        if out:
            return out
    except Exception:
        pass
    return [(n, c, '观察') for n, c in _POOL_FALLBACK]

# ── 筹码组合信号阈值（底部锁定+放量突破）──
LOCK_MIN = 40.0       # 底部锁定度 %
VR_MIN = 1.5          # 量比（新资金进入门槛）
HOLD_DAYS = 5         # 持有交易日（T+5 验证）

# ── 筹码分布计算参数（ChipTracker）──
DECAY = 0.90          # 筹码日衰减系数
BUCKETS = 120         # 价格分桶数
BOTTOM_ZONE = 0.20    # 底部锁定区（现价下方 20%）

# ── 信号生命周期配置（2026-08-25 新增，signal_lifecycle.py 消费）──
# 每个信号类型：
#   enabled              : 是否启用（False = 停发，record_signal 前检查）
#   confidence_multiplier: 置信度乘数（0 = 信号置信度置 0，不参与排序）
#   min_samples          : 淘汰判定的最小样本数（< 此值不做淘汰结论，继续积累）
#   max_loss_rate        : 最大允许亏损率（均收益 < -此值 触发淘汰）
#   min_win_rate         : 最低胜率（胜率 < 此值 触发淘汰；None = 不检查胜率）
#   hold_days            : 该信号默认持有交易日（T+N 验证窗口）
SIGNAL_LIFECYCLE = {
    '主升擒龙': {
        'enabled': True, 'confidence_multiplier': 1.0,
        'min_samples': 30, 'max_loss_rate': -1.5, 'min_win_rate': 40.0,
        'hold_days': 5,
    },
    '主线筹码信号': {
        'enabled': True, 'confidence_multiplier': 1.0,
        'min_samples': 10, 'max_loss_rate': -2.0, 'min_win_rate': 30.0,
        'hold_days': 5,
    },
    '波段启动': {
        'enabled': True, 'confidence_multiplier': 1.0,
        'min_samples': 30, 'max_loss_rate': -1.5, 'min_win_rate': 40.0,
        'hold_days': 5,
    },
}


def signal_config(strategy):
    """取某信号的生命周期配置；未配置返回默认启用"""
    return SIGNAL_LIFECYCLE.get(strategy, {
        'enabled': True, 'confidence_multiplier': 1.0,
        'min_samples': 30, 'max_loss_rate': -1.5, 'min_win_rate': None,
        'hold_days': 5,
    })

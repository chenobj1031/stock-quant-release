#!/bin/bash
# run_tests.sh — 全量测试入口（CI 守护）
# =====================================================================
# 用法：
#   ./run_tests.sh            # 跑全部单元测试 + 核心脚本语法检查
#   ./run_tests.sh --quick    # 只跑语法检查（pre-commit 用，20秒内）
# =====================================================================
set -e
cd "$(dirname "$0")"

QUICK=0
if [ "$1" = "--quick" ]; then QUICK=1; fi

echo "=========================================="
echo "  🧪 stock-quant 测试守护"
echo "=========================================="

# 1. 核心脚本语法检查（快，pre-commit 每次跑）
echo ""
echo "── 语法检查 ──────────────────────────────"
CORE_PY="config.py data_parser.py sim_trade.py decision_log.py signal_lifecycle.py \
strategy_replay.py tune_config.py pipeline.py data_health.py slippage_calib.py \
perf_trend.py score_expert_plan.py daily_review.py prepare_workbuddy_input.py \
signal_tracker.py verify_chip_signal.py mainline_backtest.py stock_quant.py"
FAIL=0
for f in $CORE_PY; do
    if [ -f "$f" ]; then
        if python3 -c "import ast; ast.parse(open('$f', encoding='utf-8').read())" 2>/dev/null; then
            echo "  ✅ $f"
        else
            echo "  ❌ $f 语法错误"
            FAIL=1
        fi
    fi
done

if [ $QUICK = 1 ]; then
    echo ""
    if [ $FAIL = 0 ]; then echo "✅ 语法全部通过（quick 模式）"; else echo "❌ 存在语法错误"; exit 1; fi
    exit $FAIL
fi

# 2. 单元测试（unittest discover）
echo ""
echo "── 单元测试 ──────────────────────────────"
if python3 -m unittest discover -s tests 2>&1 | tail -5; then
    echo "  ✅ 单元测试通过"
else
    echo "  ❌ 单元测试失败"
    FAIL=1
fi

# 3. 数据源健康度探针（轻量，网络可用时）
echo ""
echo "── 数据源健康度 ──────────────────────────"
python3 data_health.py --probe 2>&1 | tail -6

echo ""
if [ $FAIL = 0 ]; then
    echo "✅ 全部检查通过"
else
    echo "❌ 存在失败项，请修复后重跑"
fi
exit $FAIL

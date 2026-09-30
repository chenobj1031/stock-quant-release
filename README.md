# A股量化分析系统（每日闭环版）

一个跑在个人电脑上的 A 股分析流水线：**盘前给预案 → 盘中盯执行 → 收盘算总账**。数据全部来自免费公开接口（腾讯自选股 / 新浪财经 / 东方财富 / 财联社电报 RSS），无需 API key、无需 VIP 数据。

## 两大支柱

1. **决策信号**——六维分析（行情/技术/基本面/资金/舆情/宏观）+ 事件日历 + 财联社电报，浓缩成"触发线 + 动作 + 失效线"的可执行信号；
2. **模拟盘训练**——虚拟资金按信号真实"下单"（含滑点/手续费），收盘对账打分（触发率 + 方向命中率），偏差归因后沉淀为代码规则，让信号逐日变准。

核心哲学：**预测必须写成可证伪的条件，而不是模糊的方向。**

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置自选池（唯一入口，增删股票只改这一处）
cp data/pool.example.json data/pool.json
# 编辑 data/pool.json，填入你要跟踪的股票

# 3. 单只股票六维体检（中文名/6位代码/简称均可）
python3 stock_quant.py 600519

# 4. 每日闭环
python3 daily_review.py --premarket   # 盘前：今日预案（触发线/动作/失效线）
python3 daily_review.py --intraday    # 盘中：实时行情 vs 预案逐条对照
python3 daily_review.py --close       # 收盘：预测 vs 实际 + 归因 + 成绩单
```

## 主要入口

| 脚本 | 能力 |
|---|---|
| `stock_quant.py` | 单股十维报告（`--compare` 对比 / `--watchlist` 池批量 / `--hot` 强势股池 / `--html` 可视化 / `--chip-score` 筹码评分 / `--calibrate` 权重校准） |
| `daily_review.py` | 盘前/盘中/收盘三段闭环（`--pool` 临时换池 / `--date` 补跑 / `--no-cache` 强制实时） |
| `sim_loop.py` / `sim_report.py` / `sim_trade.py` | 模拟撮合账户与盯市 |
| `score_expert_plan.py` | 预案打分：触发率 + 方向命中率（成绩单） |
| `trend_screener.py` / `style_rotation.py` | 全市场趋势筛选 / 风格轮动 |
| `us_tech_watch.py` | 隔夜美股速览 |
| `holdings.py` | 实盘持仓台账（成本视角 + 止损止盈线） |
| `t_trade.py` | 做 T 明细与收益统计 |
| `prepare_workbuddy_input.py` | 专家研判预填材料 |

完整脚本清单见 `docs/脚本清单.md`。

## 目录结构

```
├── stock_quant.py          # 单股分析主入口
├── daily_review.py         # 每日闭环主入口
├── config.py               # 阈值/池单源（data/pool.json）
├── scoring_layer.py        # 评分层
├── signal_lifecycle.py     # 信号生命周期（自动淘汰）
├── data/                   # 运行数据（全部 gitignore，不入库）
│   ├── pool.example.json   # 自选池模板（唯一的池定义入口 data/pool.json 由它复制）
│   └── ...                 # 缓存/lastok/模拟账户/持仓台账（运行时生成）
├── 复盘/                   # 报告产物（运行时生成）
├── plans/                  # 预案文件（score_expert_plan 消费）
├── tests/                  # 单元测试（python3 -m unittest discover tests）
└── docs/                   # 脚本清单等文档
```

## 数据与隐私

- 所有运行数据（缓存、报告、持仓、模拟账户）都在本地 `data/` 与 `复盘/` 目录，**不上传、不入库**（已 gitignore）；
- 数据源为公开接口，有滞后/限流可能，系统内置多源交叉 + 日期校验 + 失败回退；每个数据块在报告中标注来源与时效。

## 免责声明

本项目为个人研究用途，不构成任何投资建议。数据来自公开渠道，可能存在误差。

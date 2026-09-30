#!/bin/zsh
# 盘后开盘后自动链：全市场扫描 → 委托转换（2026-09-14 架构建设）
# 调度：launchd com.stockquant.scan.bridge，交易日 09:35
cd /Users/chenjb1031/stock-quant
PY=/opt/miniconda3/bin/python3
[ -x "$PY" ] || PY=/usr/bin/python3
"$PY" scan_daily.py >> logs/scan_bridge.out.log 2>&1
"$PY" order_bridge.py  >> logs/scan_bridge.out.log 2>&1

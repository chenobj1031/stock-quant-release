#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
style_rotation.py — 风格动量择强（借鉴掘金示例策略"风格轮动.py"的核心理念）
=====================================================================
逻辑（与原示例一致，写死可证伪）：
  1. 以 上证50 / 沪深300 / 中证500 代表 大盘价值 / 中盘均衡 / 小盘成长 三种风格
  2. 计算各指数最近 N 个交易日（默认20）的区间收益率，择强 = 收益率最高者
  3. 结合系统已沉淀规则输出风格→仓位映射：
     - 指数分化提示（创业板 vs 上证 |差|>1pp）：高β风格降档
     - Beta 闸门（大盘|跌|>3%）：所有"加仓/追高"动作降级观望
     - 择强风格与大盘同向才可加仓（动量有效性前提）
输出：三指数动量对比表 + 最强风格 + 风格映射结论

用法：
  python3 style_rotation.py                 # 默认20日动量
  python3 style_rotation.py --days 10       # 改10日动量
  python3 style_rotation.py --index 3       # 看第3个指数详细
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_quant as sq

# ── 风格池（代码, 名称, 风格标签, β档）────────────────────────
STYLE_INDEXES = [
    ('sh000016', '上证50',   '大盘价值', '低β'),
    ('sh000300', '沪深300',  '中盘均衡', '中β'),
    ('sh000905', '中证500',  '小盘成长', '高β'),
]
DEFAULT_DAYS = 20


def fetch_momentum(code, days=DEFAULT_DAYS):
    """取指数最近 days 日收益率：区间收益率 = 最新收 / (days根前收) - 1"""
    k = sq.fetch_kline_sina(code, scale=240, datalen=days + 1)
    if not k or len(k) < days + 1:
        return None
    closes = [float(x['close']) for x in k]
    # 用最新收盘（可能是当日盘中/收盘）与 days 个交易日前收盘
    ret = closes[-1] / closes[0] - 1
    return {'ret': ret, 'last_close': closes[-1], 'prev_close': closes[0],
            'last_day': str(k[-1].get('day', ''))}


def fetch_index_pct(code):
    """取指数当日涨跌幅（腾讯实时）"""
    q = sq.fetch_quote_tencent(code)
    if q:
        return q.get('pct', 0)
    return None


def build_report(days=DEFAULT_DAYS):
    """核心逻辑（可复用）：返回风格动量择强报告文本段（markdown/控制台通用）
    供 daily_review.py 收盘复盘嵌入；单独运行走 main() 打印"""
    lines = []
    lines.append(f"### 风格动量择强（{days}日）")
    lines.append('')
    rows = []
    for code, name, label, beta in STYLE_INDEXES:
        mom = fetch_momentum(code, days)
        pct = fetch_index_pct(code)
        if not mom:
            lines.append(f"- ❌ {name}({code}) 数据获取失败")
            continue
        rows.append({'code': code, 'name': name, 'label': label, 'beta': beta,
                     'ret': mom['ret'], 'last_close': mom['last_close'],
                     'prev_close': mom['prev_close'], 'pct': pct,
                     'last_day': mom['last_day']})
        pct_txt = f"{pct:+.2f}%" if pct is not None else "当日N/A"
        lines.append(f"- {name}({label}) {days}日动量 {mom['ret']*100:+6.2f}%  {pct_txt}")
    if not rows:
        lines.append('- ❌ 全部指数数据获取失败')
        return '\n'.join(lines)

    best = max(rows, key=lambda r: r['ret'])
    lines.append('')
    lines.append(f"**最强风格**: {best['name']}({best['label']})  动量 {best['ret']*100:+.2f}%")

    # 环境约束（结合系统已沉淀规则）
    sh_pct = fetch_index_pct('sh000001')
    cyb_pct = fetch_index_pct('sz399006')
    notes = []
    if sh_pct is not None:
        if sh_pct < -3:
            notes.append(f"🔴 Beta闸门触发（上证{sh_pct:+.2f}%<-3%）：高β风格(中证500)降档，不新开仓")
        elif sh_pct < 0:
            notes.append(f"🟡 大盘下跌（上证{sh_pct:+.2f}%）：动量择强只作防守参考，不追高")
        else:
            notes.append(f"🟢 大盘上涨（上证{sh_pct:+.2f}%）：动量择强有效性较高")
    if sh_pct is not None and cyb_pct is not None and abs(cyb_pct - sh_pct) > 1.0:
        stronger = '创业板' if cyb_pct > sh_pct else '上证'
        notes.append(f"⚠️ 指数分化（创业板{cyb_pct:+.2f}% vs 上证{sh_pct:+.2f}%）："
                     f"{stronger}显著更强——风格分化加剧，跟随最强风格需谨慎")
    for n in notes:
        lines.append(f"- {n}")

    # 风格映射结论
    lines.append('')
    lines.append(f"**结论**: 当前应配风格 = {best['name']}({best['label']}, {best['beta']})")
    if best['beta'] == '高β' and sh_pct is not None and sh_pct < -3:
        lines.append("  - ⚠️ 高β风格在Beta闸门日降档——若持有以防守/减仓为主，不追高")
    elif sh_pct is not None and sh_pct < 0:
        lines.append("  - ⚠️ 大盘走弱时动量风格持续性存疑，仅作观察/防守参考")
    else:
        lines.append("  - 与大盘同向，可作配置方向参考")
    lines.append('')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='风格动量择强')
    parser.add_argument('--days', type=int, default=DEFAULT_DAYS, help='动量窗口（交易日）')
    parser.add_argument('--index', type=int, default=0, help='仅展示第N个指数明细(1-3)')
    args = parser.parse_args()

    print(f"📊 风格动量择强（{args.days}日）")
    print(f"{'='*64}")
    print(build_report(args.days))

    # 明细展示
    if 1 <= args.index <= len(STYLE_INDEXES):
        code, name, label, beta = STYLE_INDEXES[args.index - 1]
        mom = fetch_momentum(code, args.days)
        if mom:
            print(f"\n  📈 {name} 明细：{args.days}日前收 {mom['prev_close']:.2f} → "
                  f"最新收 {mom['last_close']:.2f}（{mom['last_day']}）")


if __name__ == '__main__':
    main()

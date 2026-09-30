#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股多维度分析工具 v3.0
数据源：新浪财经API（实时行情 + 历史K线）+ 腾讯自选股（资金流向）
技术指标：MA5/10/20/60, RSI, MACD, BOLL, 量价分析 + 资金流分析
"""

import json, subprocess, sys, re, math
from datetime import datetime
from collections import OrderedDict

# ============================================================
# 配置区：关注的标的
# ============================================================
INDEXES = {
    'sh000001': '上证指数',
    'sz399001': '深证成指',
    'sz399006': '创业板指',
    'sh000688': '科创50',
    'sh000300': '沪深300',
}

STOCKS = OrderedDict([
    ('sh600519', '贵州茅台'),
    ('sh600036', '招商银行'),
    ('sz300750', '宁德时代'),
    ('sh600900', '长江电力'),
    ('sh512800', '银行ETF'),
])

ALL_SYMBOLS = list(INDEXES.keys()) + list(STOCKS.keys())

# ============================================================
# 数据获取层
# ============================================================

def fetch_quote(symbol):
    """获取单个标的实时行情"""
    cmd = f'curl -s --connect-timeout 8 "https://hq.sinajs.cn/list={symbol}" -H "Referer: https://finance.sina.com.cn"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    for line in result.stdout.strip().split('\n'):
        if symbol in line:
            raw = line.split('"')[1]
            parts = raw.split(',')
            return {
                'open': float(parts[1]) if parts[1] else 0,
                'pre_close': float(parts[2]) if parts[2] else 0,
                'price': float(parts[3]) if parts[3] else 0,
                'high': float(parts[4]) if parts[4] else 0,
                'low': float(parts[5]) if parts[5] else 0,
                'volume': int(parts[8]) if parts[8] else 0,
                'amount': float(parts[9]) if parts[9] else 0,
            }
    return None

def fetch_quotes(symbols):
    """批量获取实时行情"""
    lst = ','.join(symbols)
    cmd = f'curl -s --connect-timeout 10 "https://hq.sinajs.cn/list={lst}" -H "Referer: https://finance.sina.com.cn"'
    result = subprocess.run(cmd, shell=True, capture_output=True, timeout=15)
    raw_stdout = result.stdout.decode('gbk', errors='replace')
    data = {}
    for line in raw_stdout.strip().split('\n'):
        m = re.search(r'hq_str_(\w+)="(.+)"', line)
        if m:
            code = m.group(1)
            raw = m.group(2)
            parts = raw.split(',')
            data[code] = {
                'open': float(parts[1]) if parts[1] else 0,
                'pre_close': float(parts[2]) if parts[2] else 0,
                'price': float(parts[3]) if parts[3] else 0,
                'high': float(parts[4]) if parts[4] else 0,
                'low': float(parts[5]) if parts[5] else 0,
                'volume': int(parts[8]) if parts[8] else 0,
                'amount': float(parts[9]) if parts[9] else 0,
            }
    return data

def fetch_kline(symbol, days=60):
    """获取历史K线数据"""
    prefix = 'sz' if symbol.startswith('sz') else 'sh'
    cmd = f'curl -s --connect-timeout 8 "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{symbol[2:]}&scale=240&datalen={days}" -H "Referer: https://finance.sina.com.cn"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    try:
        return json.loads(result.stdout)
    except:
        return []

# ============================================================
# 资金流向数据获取（腾讯自选股API）
# ============================================================

def fetch_capital_flow(stock_codes):
    """从腾讯自选股获取资金流向数据
    返回: { 'sh600519': {'outer': 外盘, 'inner': 内盘, 'ratio': 外内比, 'main_in': 主力流入, 'main_out': 主力流出, 'main_net': 主力净额}, ... }
    """
    tencent_map = {}
    for c in stock_codes:
        if c.startswith('sh'):
            tencent_map[c] = f"sh{c[2:]}"
        elif c.startswith('sz'):
            tencent_map[c] = f"sz{c[2:]}"
    
    codes_str = ','.join(tencent_map.values())
    cmd = f'curl -s --connect-timeout 6 "https://web.sqt.gtimg.cn/q={codes_str}"'
    r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
    raw = r.stdout.decode('gbk', errors='replace')
    
    result = {}
    for line in raw.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split('~')
        if len(parts) < 50:
            continue
        code = parts[2]  # 纯数字代码
        # 映射回原始code
        orig_code = None
        for k, v in tencent_map.items():
            if v.endswith(code):
                orig_code = k
                break
        if not orig_code:
            continue
        
        outer_str = parts[7].strip() if len(parts) > 7 else '0'
        inner_str = parts[8].strip() if len(parts) > 8 else '0'
        outer = float(outer_str) if outer_str.replace('.','',1).replace('-','',1).isdigit() else 0
        inner = float(inner_str) if inner_str.replace('.','',1).replace('-','',1).isdigit() else 0
        
        # 资金流向
        main_in = float(parts[47]) if len(parts) > 47 and parts[47].strip() else 0
        main_out = float(parts[48]) if len(parts) > 48 and parts[48].strip() else 0
        
        result[orig_code] = {
            'outer': outer,
            'inner': inner,
            'ratio': round(outer / inner, 2) if inner > 0 else 0,
            'main_in': main_in,
            'main_out': main_out,
            'main_net': round(main_in - main_out, 2),
        }
    return result


# ============================================================
# 技术指标计算层
# ============================================================

def calc_ma(data, period):
    """计算移动平均线"""
    if len(data) < period:
        return None
    return sum(data[-period:]) / period

def calc_rsi(data, period=14):
    """计算RSI"""
    if len(data) < period + 1:
        return 50
    deltas = [data[i] - data[i-1] for i in range(-period, 0)]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_macd(data, fast=12, slow=26, signal=9):
    """计算MACD"""
    if len(data) < slow + signal:
        return None, None, None
    # 简单EMA计算
    def ema(data, period):
        result = [data[0]]
        k = 2 / (period + 1)
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result
    ema_fast = ema(data, fast)
    ema_slow = ema(data, slow)
    dif = [ema_fast[i] - ema_slow[i] for i in range(len(data))]
    dea = ema(dif, signal)
    macd = [(dif[i] - dea[i]) * 2 for i in range(len(data))]
    return dif[-1], dea[-1], macd[-1]

def calc_boll(data, period=20, k=2):
    """计算布林带"""
    if len(data) < period:
        return None, None, None
    segment = data[-period:]
    ma = sum(segment) / period
    variance = sum((x - ma) ** 2 for x in segment) / period
    std = math.sqrt(variance)
    return ma + k * std, ma, ma - k * std

def calc_vol_ratio(volumes, period=5):
    """计算量比（当前量/均量）"""
    if len(volumes) < period + 1:
        return 1.0
    avg_vol = sum(volumes[-(period+1):-1]) / period
    return volumes[-1] / avg_vol if avg_vol > 0 else 1.0

# ============================================================
# 分析引擎
# ============================================================

def analyze_stock(code, name, quote, kline_data, capital_flow=None):
    """对单个标的进行完整分析（含资金流）"""
    result = {'name': name, 'code': code, 'quote': quote}
    
    if not quote or quote['pre_close'] == 0:
        result['error'] = '数据异常'
        return result
    
    # 基础行情
    result['pct'] = (quote['price'] - quote['pre_close']) / quote['pre_close'] * 100
    result['amplitude'] = (quote['high'] - quote['low']) / quote['pre_close'] * 100 if quote['pre_close'] else 0
    
    # 技术指标
    if kline_data and len(kline_data) > 5:
        closes = [float(d['close']) for d in kline_data]
        highs = [float(d['high']) for d in kline_data]
        lows = [float(d['low']) for d in kline_data]
        volumes = [float(d['volume']) for d in kline_data]
        
        result['ma5'] = calc_ma(closes, 5)
        result['ma10'] = calc_ma(closes, 10)
        result['ma20'] = calc_ma(closes, 20)
        result['ma60'] = calc_ma(closes, 60)
        result['rsi'] = calc_rsi(closes, 14)
        result['dif'], result['dea'], result['macd'] = calc_macd(closes)
        result['boll_up'], result['boll_mid'], result['boll_dn'] = calc_boll(closes)
        result['vol_ratio'] = calc_vol_ratio(volumes)
        result['high_60d'] = max(highs)
        result['low_60d'] = min(lows)
        result['pct_5d'] = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0
        result['pct_20d'] = (closes[-1] - closes[-21]) / closes[-21] * 100 if len(closes) >= 21 else 0
        
        # 趋势判断
        price = quote['price']
        bull_count = 0
        for ma_val in [result.get('ma5'), result.get('ma10'), result.get('ma20'), result.get('ma60')]:
            if ma_val and price > ma_val:
                bull_count += 1
        result['ma_bull_count'] = bull_count  # 4条均线中站上几条
        result['trend'] = '多头' if bull_count >= 3 else ('震荡' if bull_count >= 1 else '空头')
        
        # 超买超卖判断
        rsi = result.get('rsi', 50)
        if rsi < 25:
            result['rsi_signal'] = '⚠️⚠️ 深度超卖，超跌反弹概率大'
        elif rsi < 35:
            result['rsi_signal'] = '⚠️ 接近超卖区，关注反弹机会'
        elif rsi > 75:
            result['rsi_signal'] = '⚠️⚠️ 深度超买，注意回调风险'
        elif rsi > 65:
            result['rsi_signal'] = '⚠️ 接近超买区，注意获利了结'
        else:
            result['rsi_signal'] = '中性区间'
    
    # 资金流分析
    if capital_flow:
        cf = capital_flow.get(code)
        if cf:
            result['cf'] = cf
            ratio = cf['ratio']
            if ratio > 1.3:
                result['cf_signal'] = '🟢🟢 买盘强势（外盘>内盘30%+）'
            elif ratio > 1.05:
                result['cf_signal'] = '🟢 买盘略强'
            elif ratio < 0.7:
                result['cf_signal'] = '🔴🔴 卖盘碾压（外盘<内盘30%+）'
            elif ratio < 0.95:
                result['cf_signal'] = '🔴 卖盘略强'
            else:
                result['cf_signal'] = '⚪ 买卖均衡'
    
    return result

def print_report(quote_data, kline_data_map, capital_flow_data=None):
    """打印完整分析报告（含资金流）"""
    
    print("=" * 72)
    print(f"  📊 A股多维度分析报告 v3.0（含资金流）")
    print(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  数据来源: 新浪财经API + 腾讯自选股（资金流）")
    print("=" * 72)
    
    # 大盘指数
    print("\n📌 一、大盘指数")
    print("-" * 72)
    print(f"  {'指数':<10} {'收盘价':>8} {'涨跌幅':>8} {'今开':>8} {'最高':>8} {'最低':>8} {'成交额(亿)':>10}")
    print("-" * 72)
    for code, name in INDEXES.items():
        q = quote_data.get(code)
        if q:
            pct = (q['price'] - q['pre_close']) / q['pre_close'] * 100
            amt = q['amount'] / 1e8
            arrow = '🟢' if pct >= 0 else '🔴'
            print(f"  {arrow} {name:<8} {q['price']:>8.2f} {pct:>+7.2f}% {q['open']:>8.2f} {q['high']:>8.2f} {q['low']:>8.2f} {amt:>10.0f}")
    
    # 个股分析
    print("\n\n📌 二、个股多维分析")
    print("=" * 72)
    
    results = []
    for code, name in STOCKS.items():
        q = quote_data.get(code)
        kline = kline_data_map.get(code, [])
        result = analyze_stock(code, name, q, kline, capital_flow_data)
        results.append(result)
        
        if result.get('error'):
            print(f"\n⚠️  {name} ({code}): 数据异常")
            continue
        
        r = result
        arrow = '🟢' if r['pct'] >= 0 else '🔴'
        
        print(f"\n{arrow} 【{r['name']}】({r['code']})")
        print(f"  {'='*60}")
        print(f"  📋 基础行情: 收盘 {r['quote']['price']:.2f}  {r['pct']:+6.2f}%  "
              f"开{r['quote']['open']:.2f} 高{r['quote']['high']:.2f} 低{r['quote']['low']:.2f}  "
              f"振幅{r['amplitude']:.2f}%")
        if 'pct_5d' in r:
            print(f"  📅 阶段表现: 近5日 {r['pct_5d']:+6.2f}%  |  近20日 {r['pct_20d']:+6.2f}%  |  "
                  f"60日最高 {r['high_60d']:.2f}  |  60日最低 {r['low_60d']:.2f}")
        
        if 'ma5' in r:
            price = r['quote']['price']
            ma5_str = f"{r['ma5']:.2f}{'✅' if price > r['ma5'] else '❌'}"
            ma10_str = f"{r['ma10']:.2f}{'✅' if price > r['ma10'] else '❌'}"
            ma20_str = f"{r['ma20']:.2f}{'✅' if price > r['ma20'] else '❌'}"
            ma60_str = f"{r['ma60']:.2f}{'✅' if price > r['ma60'] else '❌'}"
            print(f"  📐 均线系统: MA5={ma5_str}  MA10={ma10_str}  MA20={ma20_str}  MA60={ma60_str}")
            print(f"  🏷️  趋势判断: 【{r['trend']}】 站上均线数: {r['ma_bull_count']}/4")
        
        if 'rsi' in r:
            print(f"  📊 技术指标: RSI(14)={r['rsi']:.1f}  {r['rsi_signal']}")
            if r.get('dif') is not None:
                macd_signal = '🔴空头' if r['macd'] < 0 else '🟢多头'
                print(f"  📊 MACD: DIF={r['dif']:.3f}  DEA={r['dea']:.3f}  MACD柱={r['macd']:.3f} ({macd_signal})")
            if r.get('boll_mid'):
                pos = '上轨附近' if price > r['boll_up'] else ('下轨附近' if price < r['boll_dn'] else '中轨附近')
                print(f"  📊 BOLL: 上轨{r['boll_up']:.2f}  中轨{r['boll_mid']:.2f}  下轨{r['boll_dn']:.2f}  ({pos})")
            if r.get('vol_ratio'):
                vr = r['vol_ratio']
                vol_signal = '放量' if vr > 1.5 else ('缩量' if vr < 0.7 else '平量')
                print(f"  📊 量价分析: 量比={vr:.2f} ({vol_signal})")
        
        # 资金流分析
        if 'cf' in r:
            cf = r['cf']
            print(f"  💰 资金流向: 外盘{cf['outer']:.0f}手 / 内盘{cf['inner']:.0f}手  |  "
                  f"外内比={cf['ratio']}  {r.get('cf_signal','')}")
            if cf['main_net'] > 0:
                print(f"  💰 主力资金: 流入{cf['main_in']:.2f}  流出{cf['main_out']:.2f}  "
                      f"净流入🟢{cf['main_net']:.2f}")
            else:
                print(f"  💰 主力资金: 流入{cf['main_in']:.2f}  流出{cf['main_out']:.2f}  "
                      f"净流出🔴{abs(cf['main_net']):.2f}")
    
    # 综合排序
    print("\n\n📌 三、综合强度排序（按今日涨跌幅+RSI+趋势综合评分）")
    print("-" * 72)
    sorted_results = sorted(results, key=lambda r: r.get('pct', 0) if not r.get('error') else -999, reverse=True)
    for i, r in enumerate(sorted_results):
        if r.get('error'): continue
        score = r.get('pct', 0) * 0.4 + (50 - abs(r.get('rsi', 50) - 50)) * 0.3 + r.get('ma_bull_count', 0) * 5
        print(f"  {i+1}. {r['name']:10s}  {r.get('pct',0):+6.2f}%  RSI={r.get('rsi',0):.0f}  "
              f"趋势={r.get('trend','?')}  评分={score:.0f}")
    
    # 后市展望
    print("\n\n📌 四、本周后市展望")
    print("-" * 72)
    for r in results:
        if r.get('error'): continue
        print(f"\n  【{r['name']}】")
        price = r['quote']['price']
        pct = r['pct']
        trend = r.get('trend', '未知')
        rsi = r.get('rsi', 50)
        ma_bull = r.get('ma_bull_count', 0)
        
        # 生成个性化建议
        if rsi < 30 and price < r.get('ma5', 999):
            outlook = '🔴 深度超卖中，但尚未企稳，等待放量站上MA5再考虑介入'
        elif rsi < 35 and ma_bull >= 1:
            outlook = '🟢 接近超卖区且有反弹迹象，可以逢低分批布局'
        elif pct > 3 and ma_bull >= 2:
            outlook = '🟢 强势反弹，短线多头占优，可持有观察'
        elif ma_bull == 0:
            outlook = '🔴 全周期空头排列，建议观望，等待技术面修复'
        elif trend == '震荡':
            outlook = '🟡 震荡格局，高抛低吸为主，等待方向选择'
        else:
            outlook = '🟡 中性，等待更多信号确认'
        
        print(f"  {outlook}")

# ============================================================
# 主程序
# ============================================================

if __name__ == '__main__':
    print("📡 正在获取实时行情数据...")
    quotes = fetch_quotes(ALL_SYMBOLS)
    print(f"  获取到 {len(quotes)} 个标的行情")
    
    print("📡 正在获取资金流向数据（腾讯自选股）...")
    capital_flow = fetch_capital_flow(list(STOCKS.keys()))
    print(f"  获取到 {len(capital_flow)} 个标的资金流向")
    for code, cf in capital_flow.items():
        name = STOCKS.get(code, code)
        print(f"  {name}: 外盘{cf['outer']:.0f} 内盘{cf['inner']:.0f} 比={cf['ratio']} 主力净={cf['main_net']:+.2f}")
    
    print("📡 正在获取历史K线数据...")
    kline_map = {}
    for code in STOCKS.keys():
        kline = fetch_kline(code, 60)
        kline_map[code] = kline
        print(f"  {STOCKS[code]}: {len(kline)} 条K线数据", end='  ')
        if kline:
            print(f"({kline[0]['day'][:10]} ~ {kline[-1]['day'][:10]})")
        else:
            print()
    
    print("\n" + "=" * 72)
    print_report(quotes, kline_map, capital_flow)
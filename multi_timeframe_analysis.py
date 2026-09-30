#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多周期量化分析脚本 v2.5
功能：对指定股票进行5分钟/15分钟/30分钟/60分钟/日线五周期共振分析 + 新闻情绪
数据源：新浪财经API（行情 + K线 + 新闻）
"""

import json, subprocess, re, sys, math
import numpy as np
import pandas as pd
from datetime import datetime
from collections import OrderedDict

# ============================================================
# 配置（示例池：请替换为自己的分析标的）
# ============================================================
STOCK_MAP = OrderedDict([
    ('sh600519', '贵州茅台'),
    ('sh600036', '招商银行'),
    ('sz300750', '宁德时代'),
    ('sh600900', '长江电力'),
    ('sh512800', '银行ETF'),
])

# 多周期配置： (名称, scale参数, 用于计算的K线条数, 判断说明)
TIMEFRAMES = [
    ('5分钟',   5,   48,   '超短线，精确买卖点'),
    ('15分钟',  15,  48,   '短线，日内波段方向'),
    ('30分钟',  30,  48,   '中短线，上午/下午盘方向'),
    ('60分钟',  60,  48,   '中线，日内趋势'),
    ('日线',    240, 60,   '长线，中期趋势'),
]

# ETF列表（无个股新闻）
ETF_CODES = {'sh512800'}

# 新闻情绪关键词
POSITIVE_KEYWORDS = [
    '业绩预告', '业绩增长', '净利润', '预增', '扭亏', '涨停', '大涨',
    '中标', '合同', '订单', '突破', '放量', '创新高', '增持', '回购',
    '利好', '获批', '投产', '量产', '合作', '战略', '投资', '扩产',
    '盈喜', '高增长', '景气', '上调', '买入', '推荐', '增持评级',
    '国产替代', '政策', '补贴', '减税', '降息', '降准',
]

NEGATIVE_KEYWORDS = [
    '减持', '亏损', '预亏', '跌停', '大跌', '下调', '利空', '监管',
    '问询', '警示', '调查', '立案', '处罚', 'ST', '退市', '风险提示',
    '违约', '逾期', '债务', '冻结', '质押', '平仓', '减持公告',
    '诉讼', '仲裁', '赔偿', '计提', '减值', '商誉', '暴雷', '崩盘',
    '裁员', '停工', '停产', '下滑', '下降', '低迷', '疲软', '衰退',
    '加息', '收紧', '制裁', '管制', '限制',
]

# ============================================================
# 数据获取
# ============================================================

def fetch_quote(symbol):
    """获取实时行情"""
    cmd = f'curl -s --connect-timeout 8 "https://hq.sinajs.cn/list={symbol}" -H "Referer: https://finance.sina.com.cn"'
    r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
    raw = r.stdout.decode('gbk', errors='replace')
    m = re.search(r'hq_str_\w+="(.+)"', raw)
    if not m: return None
    p = m.group(1).split(',')
    return {
        'price': float(p[3]), 'pre_close': float(p[2]),
        'open': float(p[1]), 'high': float(p[4]), 'low': float(p[5]),
        'vol': int(p[8]), 'amt': float(p[9])
    }

def fetch_kline(symbol, scale, datalen):
    """获取指定周期的K线数据"""
    prefix = 'sz' if symbol.startswith('sz') else 'sh'
    code = symbol[2:]
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{code}&scale={scale}&datalen={datalen}"
    cmd = f'curl -s --connect-timeout 8 "{url}" -H "Referer: https://finance.sina.com.cn"'
    r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
    try:
        return json.loads(r.stdout)
    except:
        return []

def fetch_all_timeframes(symbol, stock_name):
    """获取所有周期的K线数据"""
    result = {}
    for tf_name, scale, count, _ in TIMEFRAMES:
        data = fetch_kline(symbol, scale, count)
        if data and len(data) > 5:
            closes = np.array([float(d['close']) for d in data])
            highs = np.array([float(d['high']) for d in data])
            lows = np.array([float(d['low']) for d in data])
            volumes = np.array([float(d['volume']) for d in data])
            result[tf_name] = {
                'closes': closes, 'highs': highs, 'lows': lows, 'volumes': volumes,
                'count': len(data),
                'latest_time': data[-1].get('day', '')[:19]
            }
    return result

# ============================================================
# 新闻情绪模块（新增）
# ============================================================

def fetch_news(symbol, stock_name):
    """从新浪财经抓取个股新闻"""
    # ETF没有个股新闻
    if symbol in ETF_CODES:
        return []
    
    code = symbol[2:]
    cmd = f'curl -s --connect-timeout 8 "https://finance.sina.com.cn/realstock/company/{symbol}/nc.shtml" -H "Referer: https://finance.sina.com.cn"'
    r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
    html = r.stdout.decode('gbk', errors='replace')
    
    # 提取新闻标题
    titles = re.findall(r'title=\"([^\"]{10,80})\"', html)
    
    # 过滤：只保留含股票名称或关键字的新闻标题
    news = []
    for t in titles:
        t = t.strip()
        # 排除导航栏等非新闻内容
        if len(t) < 15: continue
        if any(k in t for k in ['随身自选', '免费股价', '融资融券', '模拟交易', '跟高手', '自选股']):
            continue
        # 保留含股票名或关键字的
        if stock_name[:2] in t or any(k in t for k in ['公告', '业绩', '涨', '跌', '投资', '研报', '利好', '利空', '减持', '增持', '合同', '中标', '投产', '扩产', '合作', '订单', '突破']):
            news.append(t)
    
    # 去重
    seen = set()
    unique_news = []
    for n in news:
        if n not in seen:
            seen.add(n)
            unique_news.append(n)
    
    return unique_news[:10]  # 最多返回10条

def analyze_news_sentiment(news_titles):
    """分析新闻情绪：利好/利空/中性"""
    if not news_titles:
        return {'score': 0, 'positive': 0, 'negative': 0, 'neutral': 0, 'details': []}
    
    positive = 0
    negative = 0
    neutral = 0
    details = []
    
    for title in news_titles:
        pos_count = sum(1 for k in POSITIVE_KEYWORDS if k in title)
        neg_count = sum(1 for k in NEGATIVE_KEYWORDS if k in title)
        
        if pos_count > neg_count:
            sentiment = '利好'
            positive += 1
        elif neg_count > pos_count:
            sentiment = '利空'
            negative += 1
        else:
            sentiment = '中性'
            neutral += 1
        
        details.append({'title': title, 'sentiment': sentiment})
    
    total = positive + negative + neutral
    # 情绪得分：-100 ~ +100
    score = ((positive - negative) / total) * 100 if total > 0 else 0
    
    return {
        'score': score,
        'positive': positive,
        'negative': negative,
        'neutral': neutral,
        'total': total,
        'details': details
    }

def extract_key_events(news_titles):
    """提取关键事件（业绩预告、减持、合同中标等）"""
    events = []
    
    # 业绩类
    for t in news_titles:
        if '业绩预告' in t or '净利润' in t or '预增' in t or '业绩' in t:
            events.append(('业绩', t))
        elif '减持' in t or '增持' in t:
            events.append(('股东变动', t))
        elif '中标' in t or '合同' in t or '订单' in t:
            events.append(('订单/合同', t))
        elif '涨停' in t or '跌停' in t or '大涨' in t:
            events.append(('行情异动', t))
        elif '投产' in t or '量产' in t or '扩产' in t:
            events.append(('产能', t))
        elif '监管' in t or '问询' in t or '立案' in t or '处罚' in t:
            events.append(('监管风险', t))
        elif '买入' in t or '推荐' in t or '增持评级' in t or '研报' in t:
            events.append(('机构评级', t))
    
    return events

# ============================================================
# 技术指标计算
# ============================================================

def calc_ma(data, n):
    if len(data) < n: return None
    return np.mean(data[-n:])

def calc_rsi(data, n=14):
    if len(data) < n+1: return 50
    deltas = np.diff(data)
    gains = np.where(deltas > 0, deltas, 0)[-n:]
    losses = np.where(deltas < 0, -deltas, 0)[-n:]
    avg_g = np.mean(gains) if gains.sum() > 0 else 0
    avg_l = np.mean(losses) if losses.sum() > 0 else 0.001
    return 100 - 100/(1+avg_g/avg_l)

def calc_macd(data, fast=12, slow=26, sig=9):
    if len(data) < slow+sig: return 0, 0, 0
    def ema(d, p):
        r = [d[0]]; k = 2/(p+1)
        for i in range(1, len(d)): r.append(d[i]*k + r[-1]*(1-k))
        return np.array(r)
    ef = ema(data, fast); es = ema(data, slow)
    dif = ef - es; dea = ema(dif, sig)
    return dif[-1], dea[-1], (dif[-1] - dea[-1]) * 2

def calc_boll(data, n=20, k=2):
    if len(data) < n: return 0, 0, 0
    seg = data[-n:]; ma = np.mean(seg); std = np.std(seg, ddof=0)
    return ma + k*std, ma, ma - k*std

def calc_kdj(data, n=9):
    if len(data) < n: return 50, 50, 50
    recent = data[-n:]
    lows = np.min(recent); highs = np.max(recent)
    if highs - lows == 0: return 50, 50, 50
    rsv = (data[-1] - lows) / (highs - lows) * 100
    k = rsv * 1/3 + 50 * 2/3
    d = k * 1/3 + 50 * 2/3
    j = 3*k - 2*d
    return k, d, j

# ============================================================
# 单周期分析
# ============================================================

def analyze_timeframe(tf_name, data, price):
    """对单个周期进行分析，返回多空信号"""
    result = {'timeframe': tf_name, 'signals': [], 'bullish': 0, 'bearish': 0, 'score': 0}
    closes = data['closes']
    volumes = data['volumes']
    
    # 1. 均线系统
    ma5 = calc_ma(closes, 5); ma10 = calc_ma(closes, 10); ma20 = calc_ma(closes, 20)
    
    if ma5 and price > ma5: result['bullish'] += 1; result['signals'].append(f'MA5多头')
    elif ma5: result['bearish'] += 1; result['signals'].append(f'MA5空头')
    if ma10 and price > ma10: result['bullish'] += 1; result['signals'].append(f'MA10多头')
    elif ma10: result['bearish'] += 1; result['signals'].append(f'MA10空头')
    if ma20 and price > ma20: result['bullish'] += 1; result['signals'].append(f'MA20多头')
    elif ma20: result['bearish'] += 1; result['signals'].append(f'MA20空头')
    
    # 2. RSI
    rsi = calc_rsi(closes, 14)
    result['rsi'] = rsi
    if rsi < 25: result['bullish'] += 2; result['signals'].append(f'RSI={rsi:.0f}深度超卖')
    elif rsi < 35: result['bullish'] += 1; result['signals'].append(f'RSI={rsi:.0f}接近超卖')
    elif rsi > 75: result['bearish'] += 2; result['signals'].append(f'RSI={rsi:.0f}深度超买')
    elif rsi > 65: result['bearish'] += 1; result['signals'].append(f'RSI={rsi:.0f}接近超买')
    else: result['signals'].append(f'RSI={rsi:.0f}中性')
    
    # 3. MACD
    dif, dea, macd = calc_macd(closes)
    result['macd'] = macd
    if macd > 0: result['bullish'] += 1; result['signals'].append(f'MACD多头({macd:.3f})')
    else: result['bearish'] += 1; result['signals'].append(f'MACD空头({macd:.3f})')
    if dif > dea: result['bullish'] += 1; result['signals'].append('DIF上穿DEA')
    else: result['bearish'] += 1; result['signals'].append('DIF下穿DEA')
    
    # 4. BOLL
    boll_up, boll_mid, boll_dn = calc_boll(closes)
    result['boll_pos'] = '上轨' if price > boll_up else ('下轨' if price < boll_dn else '中轨')
    if price < boll_dn: result['bullish'] += 1; result['signals'].append(f'跌破BOLL下轨，超跌')
    elif price > boll_up: result['bearish'] += 1; result['signals'].append(f'突破BOLL上轨，超买')
    
    # 5. 量价关系
    vol_ratio = volumes[-1] / np.mean(volumes[-6:-1]) if np.mean(volumes[-6:-1]) > 0 else 1
    result['vol_ratio'] = vol_ratio
    if vol_ratio > 1.5 and closes[-1] > closes[-2]:
        result['bullish'] += 1; result['signals'].append('放量上涨')
    elif vol_ratio > 1.5 and closes[-1] < closes[-2]:
        result['bearish'] += 1; result['signals'].append('放量下跌')
    elif vol_ratio < 0.7:
        result['signals'].append('缩量')
    
    # 6. KDJ
    k, d, j = calc_kdj(closes)
    result['kdj_j'] = j
    if j < 20: result['bullish'] += 1; result['signals'].append(f'KDJ超卖(J={j:.0f})')
    elif j > 80: result['bearish'] += 1; result['signals'].append(f'KDJ超买(J={j:.0f})')
    
    result['score'] = result['bullish'] - result['bearish']
    
    if result['score'] >= 3: result['verdict'] = '🟢 多头'
    elif result['score'] <= -3: result['verdict'] = '🔴 空头'
    elif result['score'] >= 1: result['verdict'] = '🟡 偏多'
    elif result['score'] <= -1: result['verdict'] = '🟠 偏空'
    else: result['verdict'] = '⚪ 震荡'
    
    if ma5 and ma10 and ma20:
        result['support'] = min(ma5, ma10, ma20, boll_dn)
        result['resistance'] = max(ma5, ma10, ma20, boll_up)
    else:
        result['support'] = boll_dn
        result['resistance'] = boll_up
    
    return result

# ============================================================
# 多周期共振分析（含新闻情绪）
# ============================================================

def multi_timeframe_analysis(quote, all_tf_data, stock_name, news_sentiment):
    """多周期共振分析 + 新闻情绪"""
    price = quote['price']
    pct = (price - quote['pre_close']) / quote['pre_close'] * 100
    
    print(f"\n{'='*72}")
    print(f"  📊 多周期量化分析 —— {stock_name}")
    print(f"  当前价: {price:.2f}  ({pct:+.2f}%)  |  开盘: {quote['open']:.2f}  "
          f"最高: {quote['high']:.2f} 最低: {quote['low']:.2f}")
    print(f"{'='*72}")
    
    # 逐周期分析
    tf_results = []
    for tf_name, _, _, _ in TIMEFRAMES:
        data = all_tf_data.get(tf_name)
        if data is None:
            continue
        result = analyze_timeframe(tf_name, data, price)
        tf_results.append(result)
    
    # 打印各周期分析
    print(f"\n  {'周期':<8} {'多空评分':>8} {'RSI':>6} {'MACD':>8} {'布林':>6} {'量比':>6} {'判断':>10}")
    print(f"  {'-'*60}")
    for r in tf_results:
        print(f"  {r['timeframe']:<8} {r['score']:>+8d} {r['rsi']:>6.1f} {r['macd']:>8.3f} "
              f"{r['boll_pos']:>6} {r['vol_ratio']:>6.2f} {r['verdict']:>10}")
    
    # 多周期一致性评分
    total_score = sum(r['score'] for r in tf_results)
    max_possible = len(tf_results) * 10
    consistency = total_score / max_possible * 100 if max_possible > 0 else 0
    
    bull_tfs = sum(1 for r in tf_results if '多头' in r['verdict'] or '偏多' in r['verdict'])
    bear_tfs = sum(1 for r in tf_results if '空头' in r['verdict'] or '偏空' in r['verdict'])
    neutral_tfs = len(tf_results) - bull_tfs - bear_tfs
    
    # 各周期详细信号
    print(f"\n  📋 各周期详细信号:")
    for r in tf_results:
        key_signals = [s for s in r['signals'] if any(k in s for k in ['超卖','超买','放量','MACD多头','MACD空头','KDJ'])]
        if not key_signals:
            key_signals = r['signals'][:3]
        print(f"  {r['timeframe']:<6}: {' | '.join(key_signals)}")
    
    # ============================================================
    # 📰 新闻情绪输出（新增）
    # ============================================================
    print(f"\n  📰 新闻情绪分析")
    print(f"  {'='*60}")
    
    if news_sentiment['total'] == 0:
        print(f"  ⚪ 无近期相关新闻")
    else:
        s = news_sentiment
        score_str = f"{s['score']:+.0f}"
        if s['score'] > 30:
            score_icon = '🟢🟢 强烈利好'
        elif s['score'] > 10:
            score_icon = '🟢 偏利好'
        elif s['score'] < -30:
            score_icon = '🔴🔴 强烈利空'
        elif s['score'] < -10:
            score_icon = '🔴 偏利空'
        else:
            score_icon = '⚪ 中性'
        
        print(f"  情绪得分: {score_str}  {score_icon}")
        print(f"  利好: {s['positive']}条  利空: {s['negative']}条  中性: {s['neutral']}条")
        
        # 关键事件
        events = extract_key_events([d['title'] for d in s['details']])
        if events:
            print(f"\n  关键事件:")
            for event_type, title in events[:5]:
                event_icon = {'业绩':'📊','股东变动':'👤','订单/合同':'📋','行情异动':'📈','产能':'🏭','监管风险':'⚠️','机构评级':'🏦'}.get(event_type, '📌')
                print(f"  {event_icon} [{event_type}] {title[:60]}")
        
        # 新闻标题
        print(f"\n  近期新闻:")
        for d in s['details'][:6]:
            icon = '🟢' if d['sentiment'] == '利好' else ('🔴' if d['sentiment'] == '利空' else '⚪')
            print(f"  {icon} {d['title'][:60]}")
    
    # ============================================================
    # 综合判断（技术面 + 新闻情绪）
    # ============================================================
    print(f"\n  📌 综合研判")
    print(f"  {'='*60}")
    print(f"  📐 技术面: 多头{bull_tfs}个 | 空头{bear_tfs}个 | 评分{total_score:+d}")
    print(f"  📰 新闻面: 情绪得分{news_sentiment['score']:+.0f} | 利好{news_sentiment['positive']}条 | 利空{news_sentiment['negative']}条")
    
    # 技术面判断
    if bull_tfs >= 4:
        tech_verdict = '🟢🟢 强烈看多'
        tech_reason = '所有周期共振向上'
    elif bull_tfs >= 3 and total_score > 0:
        tech_verdict = '🟢 看多'
        tech_reason = '多数周期多头'
    elif bear_tfs >= 4:
        tech_verdict = '🔴🔴 强烈看空'
        tech_reason = '所有周期共振向下'
    elif bear_tfs >= 3 and total_score < 0:
        tech_verdict = '🔴 看空'
        tech_reason = '多数周期空头'
    elif bull_tfs >= bear_tfs:
        tech_verdict = '🟡 偏多震荡'
        tech_reason = '多空分歧，多头略优'
    else:
        tech_verdict = '🟠 偏空震荡'
        tech_reason = '多空分歧，空头略优'
    
    # 新闻修正
    news_adj = ''
    if news_sentiment['score'] > 30:
        news_adj = '（新闻情绪强烈利好，加分）'
    elif news_sentiment['score'] < -30:
        news_adj = '（新闻情绪强烈利空，减分）'
    elif news_sentiment['score'] > 10:
        news_adj = '（新闻情绪偏利好，微加分）'
    elif news_sentiment['score'] < -10:
        news_adj = '（新闻情绪偏利空，微减分）'
    
    print(f"  🏷️  技术结论: {tech_verdict}（{tech_reason}）")
    print(f"  📰 新闻修正: {news_adj if news_adj else '⚪ 新闻情绪中性，无修正'}")
    
    # 最终建议
    if '强烈看多' in tech_verdict and news_sentiment['score'] > 0:
        print(f"\n  ✅ 最终建议: 🟢🟢 强烈看多，技术面与新闻面共振向上，可积极布局")
    elif '看多' in tech_verdict and news_sentiment['score'] > -10:
        print(f"\n  ✅ 最终建议: 🟢 看多，技术面偏多，新闻面无重大利空，可逢低布局")
    elif '强烈看空' in tech_verdict:
        print(f"\n  ❌ 最终建议: 🔴🔴 强烈看空，技术面与新闻面共振向下，建议回避")
    elif '看空' in tech_verdict:
        print(f"\n  ❌ 最终建议: 🔴 看空，技术面偏空，建议观望")
    else:
        print(f"\n  ⚠️ 最终建议: 多空分歧，控制仓位，等待方向明确")
    
    # 关键价位
    supports = [r['support'] for r in tf_results if r['support'] > 0]
    resistances = [r['resistance'] for r in tf_results if r['resistance'] > 0]
    if supports and resistances:
        key_support = min(supports)
        key_resistance = max(resistances)
        print(f"  关键支撑: {key_support:.2f}  |  关键压力: {key_resistance:.2f}")
    
    return total_score, consistency, bull_tfs, bear_tfs

# ============================================================
# 主程序
# ============================================================

if __name__ == '__main__':
    targets = list(STOCK_MAP.items())
    
    if len(sys.argv) > 1:
        target_codes = sys.argv[1:]
        targets = [(c, STOCK_MAP.get(c, c)) for c in target_codes if c in STOCK_MAP or c.startswith('sz') or c.startswith('sh')]
        if not targets:
            targets = [(c, n) for c, n in STOCK_MAP.items() if any(a in n for a in sys.argv[1:])]
        if not targets:
            targets = list(STOCK_MAP.items())
    
    print(f"📡 多周期量化分析引擎 v2.5（含新闻情绪）启动...")
    print(f"  分析标的: {', '.join(n for _, n in targets)}")
    print(f"  分析周期: 5分钟/15分钟/30分钟/60分钟/日线 + 新闻情绪")
    print(f"  数据源: 新浪财经API（行情 + K线 + 新闻）")
    print(f"  {'='*72}")
    
    for symbol, name in targets:
        print(f"\n📡 正在获取 {name}({symbol}) 数据...")
        
        # 获取实时行情
        quote = fetch_quote(symbol)
        if not quote or quote['price'] == 0:
            print(f"  ❌ {name}: 行情数据获取失败")
            continue
        
        # 获取多周期K线数据
        all_tf_data = fetch_all_timeframes(symbol, name)
        if not all_tf_data:
            print(f"  ❌ {name}: K线数据获取失败")
            continue
        
        # 获取新闻情绪（新增）
        news_titles = fetch_news(symbol, name)
        news_sentiment = analyze_news_sentiment(news_titles)
        print(f"  📰 新闻: {news_sentiment['total']}条  "
              f"利好{news_sentiment['positive']}条  "
              f"利空{news_sentiment['negative']}条  "
              f"情绪{news_sentiment['score']:+.0f}")
        
        # 运行多周期共振分析 + 新闻情绪
        multi_timeframe_analysis(quote, all_tf_data, name, news_sentiment)
    
    print(f"\n{'='*72}")
    print(f"  分析完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*72}")
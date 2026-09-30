#!/usr/bin/env python3
"""scan_daily.py — 全市场扫描 → 绝对评分 → candidates.json（2026-09-14 建设）

架构定位（09-14 复盘沉淀）：pool.json 曾是所有分析管线的单点入口，导致模拟盘
09-04~09-14 全部操作困在 12 只池内（用户 09-03 已授权全市场自主选股）。
本组件把 clist/push2ex 通道的输出从"环境感知"升级为"标的候选"：
每日盘前对全市场 A 股做 硬筛 → 绝对评分 → 候选精查(K线确认) → 统一候选清单
（池内 + 池外同场竞技，20cm 标的显式标注），供 order_bridge 转委托。

数据源：东财 clist 全市场快照（HTTP 数字镜像优先，参考 memory 限流经验）
        + 腾讯 fqkline 个股K线（候选精查：MA20/60、量比、RSI、ATR）
纪律：本组件只产生候选与评分，不直接下单（下单权在 order_bridge + sim_loop）
"""
import json, os, sys, time, datetime, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')
CAND_F = os.path.join(DATA, 'candidates.json')
POOL_F = os.path.join(DATA, 'pool.json')
SCAN_LOG = os.path.join(BASE, '复盘', 'scan_log')

# ---- 硬筛参数（全市场第一道闸，宁缺毋滥）----
MIN_AMOUNT = 2.0e8        # 成交额 ≥ 2 亿（流动性）
MIN_CAP, MAX_CAP = 5.0e9, 5.0e11   # 总市值 50亿 ~ 5000亿（避开壳股与巨象）
MIN_PCT = 1.5             # 当日涨幅 ≥ 1.5%（只看强者）
FLOW_RATIO_MIN = 0.0      # 主力净额 > 0（f62，当日资金为正）
TOP_N_REFINE = 12         # 进入K线精查的名额
CAND_KEEP = 15            # 候选清单保留数量
REFRESH_SECS = 0.35       # 翻页/精查间隔（限流保护）

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
      'Referer': 'https://quote.eastmoney.com/'}

CLIST_HOSTS = ['https://push2delay.eastmoney.com',  # delay 镜像（2026-09-14 实测：数字镜像 92/83/48/13 全部 RemoteDisconnected 时唯一存活）
               'http://92.push2.eastmoney.com',     # push2 实时数字镜像（与 stock_quant._EM_PUSH2_HOSTS 同源，HTTP 优先）
               'http://83.push2.eastmoney.com',
               'https://push2.eastmoney.com']


def http_get(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'ignore')


def is_20cm(code6):
    """20cm 涨跌停：创业板(30) / 科创板(68)；memory 涨停幅度口径"""
    return code6[:2] in ('30', '68')


def fetch_universe():
    """全市场 A 股快照（分页拉取）。返回 list[dict]：
    code6/name/price/pct/amount/turnover/cap/main_net"""
    fields = 'f12,f14,f2,f3,f6,f8,f20,f62'
    fs = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'   # 沪深A股（含科创/创业）
    rows, last_err = [], None
    for host in CLIST_HOSTS:
        rows, last_err = [], None
        try:
            for pn in range(1, 30):            # 上限 29 页 × 1000 ≈ 2.9万，覆盖全A
                url = (f'{host}/api/qt/clist/get?pn={pn}&pz=1000&po=1&np=1'
                       f'&fltt=2&invt=2&fid=f6&fs={fs}&fields={fields}')
                d = json.loads(http_get(url))
                diff = (d.get('data') or {}).get('diff') or []
                if not diff:
                    break
                for x in diff:
                    rows.append({
                        'code6': str(x.get('f12', '')), 'name': x.get('f14', ''),
                        'price': x.get('f2'), 'pct': x.get('f3'),
                        'amount': x.get('f6'), 'turnover': x.get('f8'),
                        'cap': x.get('f20'), 'main_net': x.get('f62')})
                time.sleep(REFRESH_SECS)
            if rows:
                print(f'  全市场快照: {len(rows)} 只（源 {host}）')
                return rows
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = e
            continue
    raise RuntimeError(f'clist 全市场快照全部源失败: {last_err}')


def hard_filter(rows):
    """第一道闸：流动性/市值/强度/资金 硬条件 + 剔除 ST/新股"""
    out, why = [], {'ST新股': 0, '流动性': 0, '市值': 0, '强度': 0, '资金': 0, '脏数据': 0}
    for r in rows:
        nm = r['name'] or ''
        if ('ST' in nm.upper() or nm.startswith(('N', 'C')) or len(r['code6']) != 6):
            why['ST新股'] += 1
            continue
        try:
            if not (r['amount'] and r['amount'] >= MIN_AMOUNT):
                why['流动性'] += 1
                continue
            if not (r['cap'] and MIN_CAP <= r['cap'] <= MAX_CAP):
                why['市值'] += 1
                continue
            if r['pct'] is None or r['pct'] < MIN_PCT:
                why['强度'] += 1
                continue
            if r['main_net'] is None or r['main_net'] <= FLOW_RATIO_MIN:
                why['资金'] += 1
                continue
        except TypeError:
            why['脏数据'] += 1
            continue
        r['is_20cm'] = is_20cm(r['code6'])
        out.append(r)
    return out, why


def score_universe(rows):
    """绝对评分 0-100（跨市场可比，不依赖池内相对排序）：
    强度30 + 资金浓度30 + 换手20 + 流动性10 + 市值适中10"""
    if not rows:
        return []
    for r in rows:
        s = 0.0
        s += min(max((r['pct'] - MIN_PCT) / 8.0, 0), 1) * 30
        flow_ratio = (r['main_net'] or 0) / max(r['amount'] or 1, 1)
        s += min(max(flow_ratio / 0.15, 0), 1) * 30
        s += min(max(((r['turnover'] or 0) - 3) / 20.0, 0), 1) * 20
        s += min(max((r['amount'] - MIN_AMOUNT) / 1.5e9, 0), 1) * 10
        cap_mid = (MIN_CAP + MAX_CAP) / 2
        s += min(max(1 - abs(r['cap'] - cap_mid) / cap_mid, 0), 1) * 10
        r['score'] = round(s, 1)
    return sorted(rows, key=lambda x: -x['score'])


def secid(code6):
    """腾讯/东财 secid 规则：沪 1.x，深 0.x"""
    return ('1.' if code6[0] in '569' else '0.') + code6


def full_code(code6):
    return ('sh' if code6[0] in '569' else 'sz') + code6


def fetch_kline(code6, n=70):
    """腾讯 fqkline 日K（前复权）→ [{'day','open','close','volume'}, ...]"""
    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?'
           f'param={full_code(code6)},day,,,{n},qfq')
    d = json.loads(http_get(url))
    node = (d.get('data') or {}).get(full_code(code6)) or {}
    k = node.get('qfqday') or node.get('day') or []
    return [{'day': x[0], 'open': float(x[1]), 'close': float(x[2]),
             'high': float(x[3]), 'low': float(x[4]),
             'volume': float(x[5])} for x in k]


def refine_candidate(r):
    """K线精查：MA20/60 多头、量比≥1.5、ATR 止损建议。
    通过 → 返回 enrich 后 dict；不通过 → 返回 None（原因写进 r['reject']）"""
    try:
        k = fetch_kline(r['code6'])
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        r['reject'] = 'K线获取失败'
        return None
    if len(k) < 61:
        r['reject'] = '上市不足60日'
        return None
    closes = [x['close'] for x in k]
    px = closes[-1]
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    if not (px >= ma20 >= ma60):
        r['reject'] = f'MA排列不多头(px{px:.2f} ma20 {ma20:.2f} ma60 {ma60:.2f})'
        return None
    pre_vol = sum(x['volume'] for x in k[-11:-1]) / 10 or 1
    vol_ratio = k[-1]['volume'] / pre_vol
    if vol_ratio < 1.5:
        r['reject'] = f'量比{vol_ratio:.2f}<1.5'
        return None
    if k[-1]['close'] > k[-1]['open'] * 1.099 and r['pct'] >= 9.5 and not r['is_20cm']:
        pass  # 主板涨停也放行（涨停是强确认，由 order_bridge 决定是否追高禁入）
    trs = []
    for i in range(-14, 0):
        h, l, pc = k[i]['high'], k[i]['low'], k[i - 1]['close']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / len(trs)
    r.update({'full': full_code(r['code6']), 'px': px, 'ma20': round(ma20, 2),
              'ma60': round(ma60, 2), 'vol_ratio': round(vol_ratio, 2),
              'atr_pct': round(atr / px * 100, 2),
              'stop_sug': round(px - 2 * atr, 2),   # 2×ATR 止损（与 calc_position_size 同口径）
              'kline_date': k[-1]['day'][:10]})
    return r


def load_pool_codes():
    """观察池 code6 集合（pool.json 兼容 list / dict 包装）"""
    try:
        pool = json.load(open(POOL_F, encoding='utf-8'))
        stocks = pool if isinstance(pool, list) else (pool.get('stocks') or pool.get('pool') or [])
        return {str(s.get('code', '')).replace('sh', '').replace('sz', '').zfill(6)
                for s in stocks if isinstance(s, dict) and s.get('code')}
    except (OSError, ValueError, AttributeError):
        return set()


def run(scan_date=None):
    """主流程：拉取→硬筛→评分→精查→候选清单落盘。返回 candidates list"""
    scan_date = scan_date or datetime.date.today().strftime('%Y-%m-%d')
    print(f'=== 全市场扫描 {scan_date} ===')
    uni = fetch_universe()
    passed, why = hard_filter(uni)
    print(f'  硬筛通过: {len(passed)} 只 | 淘汰: ' +
          ' '.join(f'{k}{v}' for k, v in why.items()))
    if not passed:
        raise RuntimeError('硬筛后无候选，检查数据源')
    ranked = score_universe(passed)
    pool_codes = load_pool_codes()
    candidates = []
    for r in ranked[:TOP_N_REFINE]:
        got = refine_candidate(r)
        time.sleep(REFRESH_SECS)
        if got:
            got['in_pool'] = got['code6'] in pool_codes
            candidates.append(got)
        print(f"  精查 {r['name']}({r['code6']}): " +
              (f"✓ score={r['score']}" if got else f"✗ {r['reject']}"))
    cand = {
        'date': scan_date, 'generated_at': datetime.datetime.now().strftime('%H:%M:%S'),
        'universe_size': len(uni), 'filtered': len(passed),
        'candidates': candidates[:CAND_KEEP],
        'out_pool_count': sum(1 for c in candidates if not c['in_pool']),
        'in_pool_count': sum(1 for c in candidates if c['in_pool'])}
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(SCAN_LOG, exist_ok=True)
    json.dump(cand, open(CAND_F, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(cand, open(os.path.join(SCAN_LOG, f'candidates_{scan_date}.json'),
                         'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'候选 {len(cand["candidates"])} 只（池内{cand["in_pool_count"]}/池外{cand["out_pool_count"]}）→ {CAND_F}')
    return cand


if __name__ == '__main__':
    run(scan_date=sys.argv[1] if len(sys.argv) > 1 else None)

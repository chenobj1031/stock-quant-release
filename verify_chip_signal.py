# -*- coding: utf-8 -*-
"""
筹码组合信号回测验证：底部锁定 + 放量突破 是否携带超额收益
============================================================
研究问题：daily_review 的"底部锁定+放量突破"组合信号（阈值是拍脑袋定的）
是否真有预测力？若有 → 信号可进入决策层；若无 → 应砍掉或改阈值。

方法：
  - 数据：data/*.cache 全部日线缓存序列（与分歧度实验同源）
  - 信号（T日收盘后判定，防未来函数）：
      底部锁定度 >= 40%（现价下方20%内筹码占比）
      量比 vol_ratio >= 1.5（当日量 / 前5日均量）
      现价 >= 成本峰（筹码最密集价格带）
  - 目标：T+1 日收益、T+5 持有收益（收益>0 为方向正确）
  - 对照组：① 全样本基准 ② 仅底部锁定(未放量/未突破) ③ 仅放量(底部锁定不足)
  - 统计纪律：分档(信号) 与目标(未来收益) 相互独立；触发组样本 >=30 才采信；
              报告样本量与分档明细，可核验。
增量筹码：滚动维护价格分桶筹码数组（与 stock_quant.analyze_chip_distribution
          的三角分配+衰减逻辑一致），避免每个 T 全量重算（O(n²) 太慢）。
"""
import glob
import math
import os
import pickle
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (LOCK_MIN, VR_MIN, DECAY, BUCKETS,
                    BOTTOM_ZONE, HOLD_DAYS as HOLD)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')


# ── 统计显著性辅助（纯 Python，无 scipy 依赖；verify_chip_score.py 复用）──
def _regularized_beta(x, a, b):
    """正则化不完全 beta 函数 I_x(a,b)——用连分数（Lentz 算法）实现"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                  + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        # 连分数（适用于小 x）
        qab = a + b
        qap = a + 1.0
        qam = a - 1.0
        c = 1.0
        d = 1.0 - qab * x / qap
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        h = d
        for m in range(1, 200):
            m2 = 2 * m
            aa = m * (b - m) * x / ((qam + m2) * (a + m2))
            d = 1.0 + aa * d
            if abs(d) < 1e-30:
                d = 1e-30
            c = 1.0 + aa / c
            if abs(c) < 1e-30:
                c = 1e-30
            d = 1.0 / d
            h *= d * c
            aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
            d = 1.0 + aa * d
            if abs(d) < 1e-30:
                d = 1e-30
            c = 1.0 + aa / c
            if abs(c) < 1e-30:
                c = 1e-30
            d = 1.0 / d
            delta = d * c
            h *= delta
            if abs(delta - 1.0) < 1e-10:
                break
        return bt * h
    # 对称变换：I_x(a,b) = 1 - I_{1-x}(b,a)
    return 1.0 - _regularized_beta(1.0 - x, b, a)


def _t_sf(t, df):
    """t 分布生存函数 P(T > t)，df 为自由度
    ⚠️ 2026-08-18 修复：df 很大（>100）时不完全 beta 连分数数值不稳定
    （曾输出 p>1），改用正态近似（df→∞ t 分布→标准正态，误差<0.1%）"""
    if math.isnan(t) or math.isinf(t):
        return 0.0
    if df > 100:
        # 正态近似：P(Z > t) = 0.5 * erfc(t / sqrt(2))
        return 0.5 * math.erfc(t / math.sqrt(2.0))
    x = df / (df + t * t)
    return 0.5 * _regularized_beta(x, df / 2.0, 0.5)


def welch_ttest(a, b):
    """两独立样本 Welch t-test（不等方差）。
    返回 (t, df, p_双尾) 或 None（样本不足/方差为0无法计算）。"""
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return None
    m1, m2 = sum(a) / n1, sum(b) / n2
    v1 = sum((x - m1) ** 2 for x in a) / (n1 - 1)
    v2 = sum((x - m2) ** 2 for x in b) / (n2 - 1)
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se == 0 or math.isnan(se):
        return None
    t = (m1 - m2) / se
    df = (v1 / n1 + v2 / n2) ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    p = 2.0 * _t_sf(abs(t), df)  # 双尾
    return t, df, p


def mean_ci(rets, conf=0.95):
    """均值 + (1-conf) 置信区间（t 分布临界值，大样本近似 z 亦可）"""
    n = len(rets)
    if n < 2:
        return None
    m = sum(rets) / n
    var = sum((x - m) ** 2 for x in rets) / (n - 1)
    se = math.sqrt(var / n)
    if se == 0:
        return (m, m, m)
    # 双尾临界值 t_{1-alpha/2, n-1}（用正态近似 1.96 起步，n<100 时细化）
    alpha = 1.0 - conf
    crit = 1.96
    if n < 100:
        # 二分求解：找 t 使 P(|T|>t)=alpha
        lo, hi = 0.0, 12.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if 2.0 * _t_sf(mid, n - 1) > alpha:
                lo = mid
            else:
                hi = mid
        crit = (lo + hi) / 2
    half = crit * se
    return (m - half, m, m + half)

# ── 信号阈值集中管理于 config.py（2026-08-25 抽取，与 signal_tracker/mainline_backtest/daily_review 同步）──


def load_daily_sequences(pool='all'):
    """加载日线序列缓存。
    ⚠️ 2026-08-18 新增 pool 参数：
      - 'all'（默认）：data/*.cache（含 u_ 跨行业池 + 既有缓存）
      - 'mainline'：仅 data/m_*.cache（主线题材池：AI算力/光模块/PCB/半导体/固态电池等）
    """
    seqs = []
    if pool == 'mainline':
        pattern = os.path.join(DATA_DIR, 'm_*.cache')
    else:
        pattern = os.path.join(DATA_DIR, '*.cache')
    for p in glob.glob(pattern):
        try:
            with open(p, 'rb') as f:
                obj = pickle.load(f)
            if (isinstance(obj, list) and obj and isinstance(obj[0], dict)
                    and 'day' in obj[0] and 'close' in obj[0]):
                days = [b['day'] for b in obj]
                if not any(':' in str(d) for d in days):
                    seqs.append(obj)
        except Exception:
            pass
    return seqs


class ChipTracker:
    """增量筹码分布（滚动维护，逻辑与 analyze_chip_distribution 一致）"""

    def __init__(self, buckets=BUCKETS, decay=DECAY, bottom_zone=BOTTOM_ZONE):
        self.buckets = buckets
        self.decay = decay
        self.bottom_zone = bottom_zone
        self.chip = None          # 价格分桶筹码数组
        self.price_at = None      # 桶中心价
        self.lo = self.hi = 0.0
        self.step = 0.0
        self.seen = 0

    def _init_buckets(self, kline):
        lows = [float(d['low']) for d in kline]
        highs = [float(d['high']) for d in kline]
        p_min, p_max = min(lows), max(highs)
        span = max(p_max - p_min, float(kline[-1]['close']) * 0.01)
        self.lo = p_min - span * 0.05
        self.hi = p_max + span * 0.05
        self.step = (self.hi - self.lo) / self.buckets
        self.chip = [0.0] * self.buckets
        self.price_at = [self.lo + self.step * (i + 0.5) for i in range(self.buckets)]

    def _alloc_day(self, o, h, l, c, v):
        """把当日成交量按三角形分布分配到分桶（收盘价处权重最大）"""
        tri_lo, tri_hi = min(l, o), max(h, o)
        if tri_hi - tri_lo < self.step:
            tri_lo, tri_hi = c - self.step, c + self.step
        tri_span = tri_hi - tri_lo
        if tri_span <= 0:
            return
        for j in range(self.buckets):
            p = self.price_at[j]
            if p < tri_lo or p > tri_hi:
                continue
            if p <= c:
                w = (p - tri_lo) / (c - tri_lo) if c > tri_lo else 1.0
            else:
                w = (tri_hi - p) / (tri_hi - c) if tri_hi > c else 1.0
            w = max(0.0, min(1.0, w))
            self.chip[j] += v * w

    def add_day(self, kline, i, is_last=False):
        """加入第 i 根K线（更新到 i 日收盘）。is_last=True 表示这是窗口终点（不衰减当日）"""
        if self.chip is None:
            self._init_buckets(kline)
        d = kline[i]
        o = float(d['open']); h = float(d['high']); l = float(d['low'])
        c = float(d['close']); v = float(d.get('volume', 0))
        if v > 0 and c > 0:
            self._alloc_day(o, h, l, c, v)
        # 历史筹码衰减（当日筹码不衰减；只有非窗口终点才衰减）
        if not is_last:
            self.chip = [x * self.decay for x in self.chip]
        self.seen = i + 1

    def metrics(self, price):
        """按当前筹码分布计算指标（获利盘/成本峰/平均成本/集中度/底部锁定）"""
        total = sum(self.chip)
        if total <= 0:
            return None
        win = sum(self.chip[j] for j in range(self.buckets) if self.price_at[j] <= price)
        winner_pct = round(win / total * 100, 1)
        peak_j = max(range(self.buckets), key=lambda j: self.chip[j])
        cost_peak = self.price_at[peak_j]
        cost_avg = sum(self.chip[j] * self.price_at[j] for j in range(self.buckets)) / total
        order = sorted(range(self.buckets), key=lambda j: -self.chip[j])
        acc, p90_lo, p90_hi = 0.0, self.price_at[order[0]], self.price_at[order[0]]
        for j in order:
            acc += self.chip[j]
            p90_lo = min(p90_lo, self.price_at[j])
            p90_hi = max(p90_hi, self.price_at[j])
            if acc >= total * 0.90:
                break
        concentration = round((p90_hi - p90_lo) / cost_avg * 100, 1) if cost_avg > 0 else 0
        floor = price * (1 - self.bottom_zone)
        bot = sum(self.chip[j] for j in range(self.buckets)
                  if floor <= self.price_at[j] <= price)
        bottom_lock = round(bot / total * 100, 1)
        return {'winner_pct': winner_pct, 'cost_peak': cost_peak, 'cost_avg': cost_avg,
                'concentration': concentration, 'bottom_lock': bottom_lock}


def vol_ratio(volumes, i):
    """量比：当日量 / 前5日均量"""
    if i < 5:
        return 0.0
    avg5 = sum(volumes[i - 5:i]) / 5
    return volumes[i] / avg5 if avg5 > 0 else 0.0


def main(pool='all'):
    seqs = load_daily_sequences(pool=pool)
    pool_label = '主线题材池(m_*.cache)' if pool == 'mainline' else '全样本(*.cache)'
    print(f'加载日线序列 [{pool_label}]: {len(seqs)} 个')

    # 收集观测：T 日信号档 → T+1 / T+5 收益
    # 档位：full=完整触发 / lock_only=仅底部锁定 / vr_only=仅放量 / none=未触发
    groups = defaultdict(lambda: {'n': 0, 't1_hits': 0, 't1_ret': 0.0, 't1_rets': [],
                                  't5_hits': 0, 't5_ret': 0.0, 't5_n': 0, 't5_rets': []})
    # ⚠️ 2026-08-18：回调子集（T日收盘<MA20）独立收集，用于"回调状态下的信号有效性"对比
    groups_pb = defaultdict(lambda: {'n': 0, 't5_hits': 0, 't5_ret': 0.0, 't5_n': 0, 't5_rets': []})
    n_skip = 0
    for seq in seqs:
        closes = [float(d['close']) for d in seq]
        volumes = [float(d.get('volume', 0)) for d in seq]
        n = len(seq)
        if n < 40:
            continue
        tracker = ChipTracker()
        # 预热：前 30 根只算筹码，不做信号
        for i in range(30):
            tracker.add_day(seq, i, is_last=False)
        for i in range(30, n - 1):
            tracker.add_day(seq, i, is_last=True)  # 更新到 i 日收盘（作为窗口终点）
            price = closes[i]
            m = tracker.metrics(price)
            if m is None:
                n_skip += 1
                tracker.add_day(seq, i, is_last=False)
                continue
            vr = vol_ratio(volumes, i)
            lock = m['bottom_lock']
            peak = m['cost_peak']
            # 信号判定（写死）
            full = lock >= LOCK_MIN and vr >= VR_MIN and price >= peak
            lock_only = lock >= LOCK_MIN and not (vr >= VR_MIN and price >= peak)
            vr_only = vr >= VR_MIN and lock < LOCK_MIN
            if full:
                grp = 'full'
            elif lock_only:
                grp = 'lock_only'
            elif vr_only:
                grp = 'vr_only'
            else:
                grp = 'none'

            t1 = (closes[i + 1] / price - 1) * 100
            g = groups[grp]
            g['n'] += 1
            g['t1_hits'] += 1 if t1 > 0 else 0
            g['t1_ret'] += t1
            g['t1_rets'].append(t1)
            # T+5（需窗口内有足够后续K线）
            if i + HOLD < n:
                t5 = (closes[i + HOLD] / price - 1) * 100
                g['t5_hits'] += 1 if t5 > 0 else 0
                g['t5_ret'] += t5
                g['t5_n'] += 1
                g['t5_rets'].append(t5)
                # 回调子集（T日收盘<MA20，仅 full/none 两档对比）
                ma20 = sum(closes[i - 19:i + 1]) / 20
                if price < ma20:
                    gp = groups_pb[grp]
                    gp['n'] += 1
                    gp['t5_hits'] += 1 if t5 > 0 else 0
                    gp['t5_ret'] += t5
                    gp['t5_n'] += 1
                    gp['t5_rets'].append(t5)
            # 恢复：i 日已在窗口内，作为普通日继续（衰减）
            tracker.add_day(seq, i, is_last=False)

    print(f'有效观测（含各档）: {sum(v["n"] for v in groups.values())} ｜ 跳过 {n_skip}\n')

    # ── 汇总表 ──
    print(f'{"档位":<12} | {"n":>6} | {"T+1胜率":>8} | {"T+1均收益":>9} | {"T+5胜率":>8} | {"T+5均收益":>9}')
    print('-' * 66)
    label = {'full': '完整触发(锁定+放量+突破)',
             'lock_only': '仅底部锁定(未放量/未突破)',
             'vr_only': '仅放量(底部锁定不足)',
             'none': '未触发'}
    order = ['full', 'lock_only', 'vr_only', 'none']
    for g in order:
        d = groups[g]
        if d['n'] == 0:
            print(f'{label[g]:<12} | {"0":>6} | {"-":>8} | {"-":>9} | {"-":>8} | {"-":>9}')
            continue
        t1_wr = d['t1_hits'] / d['n'] * 100
        t5_wr = d['t5_hits'] / d['t5_n'] * 100 if d['t5_n'] else 0
        print(f'{label[g]:<12} | {d["n"]:>6} | {t1_wr:>7.1f}% | {d["t1_ret"]/d["n"]:>+8.2f}% | '
              f'{t5_wr:>7.1f}% | {d["t5_ret"]/d["t5_n"]:>+8.2f}%')

    # ── 对比：完整触发 vs 全样本基准 + 显著性检验 ──
    full = groups['full']
    none = groups['none']
    base_n = sum(v['n'] for v in groups.values())
    base_t1 = sum(v['t1_ret'] for v in groups.values()) / base_n
    base_t5_n = sum(v['t5_n'] for v in groups.values())
    base_t5 = sum(v['t5_ret'] for v in groups.values()) / base_t5_n if base_t5_n else 0
    print('\n═══ 核心对比：完整触发 vs 全样本基准 ═══')
    print(f'全样本基准:    T+1均收益 {base_t1:+.2f}%  |  T+5均收益 {base_t5:+.2f}%  (n={base_n})')
    if full['n'] >= 30:
        print(f'完整触发({full["n"]}个): T+1均收益 {full["t1_ret"]/full["n"]:+.2f}%  |  '
              f'T+5均收益 {full["t5_ret"]/full["t5_n"]:+.2f}%')
        diff1 = full['t1_ret'] / full['n'] - base_t1
        diff5 = full['t5_ret'] / full['t5_n'] - base_t5
        print(f'超额: T+1 {diff1:+.2f}%  |  T+5 {diff5:+.2f}%')

        # 显著性检验（2026-08-18 升级）：full vs none 的 Welch t-test + 95%CI
        if none['t5_n'] >= 30 and full['t5_n'] >= 30:
            wt = welch_ttest(full['t5_rets'], none['t5_rets'])
            fci = mean_ci(full['t5_rets'])
            nci = mean_ci(none['t5_rets'])
            print('\n── 统计显著性（T+5 收益，Welch t-test，双尾）──')
            print(f'完整触发  95%CI: [{fci[0]:+.2f}%, {fci[2]:+.2f}%]  (n={full["t5_n"]})')
            print(f'未触发    95%CI: [{nci[0]:+.2f}%, {nci[2]:+.2f}%]  (n={none["t5_n"]})')
            if wt:
                t, df, p = wt
                sig = '✅ 差异显著' if p < 0.05 else ('🟡 边缘显著' if p < 0.10 else '❌ 差异不显著')
                print(f't={t:.2f}  df={df:.0f}  p={p:.4f}  → {sig}')
                print(f'（p<0.05 认为信号有真实超额收益；p≥0.10 说明差异可能是随机波动）')
            else:
                print('⚠️ 方差过小无法计算 t 检验')
        else:
            print('⚠️ full 或 none 样本 <30，无法做显著性检验')
    else:
        print(f'⚠️ 完整触发样本仅 {full["n"]} 个（<30），结论不可靠，需积累更多数据')

    # ── 触发分布：各序列触发次数（了解信号在哪些股票上出现）──
    print('\n═══ 回调子集对比（T日收盘<MA20，2026-08-18 新增）═══')
    pb_full = groups_pb.get('full', {})
    pb_none = groups_pb.get('none', {})
    print(f'回调子集观测: full={pb_full.get("n", 0)}  none={pb_none.get("n", 0)}')
    if pb_full.get('t5_n', 0) >= 30 and pb_none.get('t5_n', 0) >= 30:
        pbf = pb_full['t5_rets']
        pbn = pb_none['t5_rets']
        wt2 = welch_ttest(pbf, pbn)
        fci2 = mean_ci(pbf)
        nci2 = mean_ci(pbn)
        print(f'回调+完整触发  T+5均收 {pb_full["t5_ret"]/pb_full["t5_n"]:+.2f}%  95%CI: [{fci2[0]:+.2f}%, {fci2[2]:+.2f}%]  (n={pb_full["t5_n"]})')
        print(f'回调+未触发    T+5均收 {pb_none["t5_ret"]/pb_none["t5_n"]:+.2f}%  95%CI: [{nci2[0]:+.2f}%, {nci2[2]:+.2f}%]  (n={pb_none["t5_n"]})')
        if wt2:
            t2, df2, p2 = wt2
            sig2 = '✅ 差异显著' if p2 < 0.05 else ('🟡 边缘显著' if p2 < 0.10 else '❌ 差异不显著')
            print(f't={t2:.2f}  df={df2:.0f}  p={p2:.4f}  → {sig2}')
            print(f'（回调状态下信号是否有效：p<0.05 → 回调中该信号仍有超额收益）')
        else:
            print('⚠️ 方差过小无法计算')
    else:
        print('⚠️ 回调子集内 full/none 样本 <30，无法做显著性检验')

    print('\n═══ 说明 ═══')
    print('· 分档(信号) 与目标(T+1/T+5收益) 相互独立，无同源筛选')
    print('· T+1胜率 = 收益>0 占比；均收益为算数平均（未做无风险收益扣除）')
    print('· 触发样本 <30 时结论仅供参考；阈值(40/1.5/站峰)为拍脑袋值，可扫描调优')
    print('· 2026-08-18 样本已扩充：跨行业79只缓存 + 关注股，板块偏差大幅消除')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='筹码组合信号回测验证（底部锁定+放量+突破）')
    parser.add_argument('--pool', choices=['all', 'mainline'], default='all',
                        help='股票池: all=全样本(*.cache，含跨行业池) / mainline=主线题材池(m_*.cache)')
    args = parser.parse_args()
    main(pool=args.pool)

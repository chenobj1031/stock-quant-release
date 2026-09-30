#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""us_tech_watch.py — 美股科技/存储链盘中一键快照（供 A 股盘前对照）

用途：A 股收盘后盯美股科技表现，快速拉「指数期货 + 半导体/存储链个股 + A50 夜盘」，
输出涨跌幅与数据时间戳（区分盘前/盘中/陈旧收盘），供周二 A 股 O11/O12 触发决策。

用法：python3 us_tech_watch.py
数据源：新浪 hf 指数期货（实时） + 腾讯 qt.gtimg.cn 美股（免费延时约15分钟）
"""
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}


def fetch(url, headers):
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=12).read()


def sina_futures(symbols):
    raw = fetch("https://hq.sinajs.cn/list=" + ",".join(symbols), UA)
    out = {}
    for line in raw.decode("gbk", "ignore").splitlines():
        if '="' not in line:
            continue
        key = line.split("=")[0].replace("var hq_str_", "").strip()
        f = line.split('="')[1].rstrip('";').split(",")
        if len(f) < 9 or not f[0]:
            continue
        try:
            cur, hi, lo, prev = float(f[0]), float(f[4]), float(f[5]), float(f[7])
            opn = float(f[8])
        except Exception:
            continue
        out[key] = {
            "name": f[13] if len(f) > 13 else key,
            "cur": cur, "hi": hi, "lo": lo, "open": opn, "prev": prev,
            "chg": (cur - prev) / prev * 100 if prev else 0.0,
            "time": f[6],
        }
    return out


def tencent_us(codes):
    raw = fetch("https://qt.gtimg.cn/q=" + ",".join(codes),
                {"User-Agent": "Mozilla/5.0"})
    out = {}
    for line in raw.decode("gbk", "ignore").splitlines():
        if '="' not in line:
            continue
        key = line.split("=")[0].replace("v_", "").strip()
        f = line.split('="')[1].rstrip('";').split("~")
        if len(f) < 33:
            continue
        try:
            cur = float(f[3])
            chg = float(f[32])
        except Exception:
            continue
        out[key] = {"name": f[1], "code": f[2], "cur": cur, "chg": chg, "time": f[30]}
    return out


def main():
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print("=" * 70)
    print(f"  🇺🇸 美股科技/存储链快照   {now} (北京时间)")
    print("=" * 70)

    # 1) 指数期货（实时盘前/盘中）
    fut = sina_futures(["hf_NQ", "hf_ES", "hf_YM", "hf_SOX", "hf_VIX", "hf_CL"])
    print("\n【指数/宏观期货】")
    label = {"hf_NQ": "纳指期货", "hf_ES": "标普期货", "hf_YM": "道指期货",
             "hf_SOX": "费半期货", "hf_VIX": "恐慌指数", "hf_CL": "纽约原油"}
    for k in ["hf_NQ", "hf_ES", "hf_YM", "hf_SOX", "hf_VIX", "hf_CL"]:
        if k in fut:
            d = fut[k]
            flag = "⬆" if d["chg"] > 0.15 else ("⬇" if d["chg"] < -0.15 else "→")
            print(f"  {flag} {label[k]:<6} {d['cur']:<10.2f} {d['chg']:+.2f}%   "
                  f"高{d['hi']:.2f} 低{d['lo']:.2f}  前{d['prev']:.2f}  @{d['time']}")

    # 2) 美股科技/半导体/存储个股（延时约15分钟）
    codes = ["usNVDA", "usAMD", "usAVGO", "usMRVL", "usINTC", "usTSM",
             "usMU", "usSNDK", "usSTX", "usWDC", "usARM", "usQCOM",
             "usMSFT", "usGOOGL", "usAMZN", "usSMH"]
    st = tencent_us(codes)
    print("\n【半导体/存储/AI 个股】")
    groups = {
        "AI算力": ["usNVDA", "usAMD", "usAVGO", "usMRVL", "usINTC"],
        "存储链": ["usMU", "usSNDK", "usSTX", "usWDC"],
        "代工/设备": ["usTSM", "usARM"],
        "AI巨头": ["usMSFT", "usGOOGL", "usAMZN"],
        "半导体ETF": ["usSMH"],
    }
    latest_time = ""
    for g, cs in groups.items():
        row = []
        for c in cs:
            if c in st:
                d = st[c]
                latest_time = d["time"] if d["time"] > latest_time else latest_time
                row.append(f"{d['name']}{d['chg']:+.1f}%")
        if row:
            print(f"  {g:<8} " + "  ".join(row))
    print(f"  （个股报价时间戳: {latest_time or 'n/a'} —— 若为 9-18 16:00 即陈旧收盘，开盘后转实时）")

    # 3) A50 夜盘（对 A 股最直接的映射）
    a50 = sina_futures(["hf_CHA50"])
    if "hf_CHA50" in a50 and a50["hf_CHA50"]["cur"]:
        d = a50["hf_CHA50"]
        print(f"\n【A50 期货(夜盘)】 {d['cur']:.2f} {d['chg']:+.2f}%  @{d['time']}")
    else:
        print("\n【A50 期货(夜盘)】 数据未取到（接口限流或夜盘未开）")


if __name__ == "__main__":
    main()

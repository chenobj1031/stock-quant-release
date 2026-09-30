#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
科技线主力建仓追踪器（2026-09-09 建立）
目标: 量化"价面磨底/震荡 + 主力悄悄净流入"的背离信号，跟踪窗口 2026-08-03 → 2026-09-16 收盘
      (9-11 美国CPI / 9-17 FOMC 决议为终点事件；FOMC 后框架翻转，本表停止增量解读)
数据源: 东财 push2his fflow 历史资金流（5镜像轮询兜底）
  字段: f51日期 f52主力净 f53小单 f54中单 f55大单 f56超大单 f57主力净占比 f62收盘价 f63涨跌幅%
指标(自 8/3 起):
  累计主力净流入(亿) / 累计超大单(亿) / 净流入天数 / 逢跌买入天数(收跌且主力净>0) /
  区间涨跌幅(磨底判定: -20%~+15%) / 建仓评分(0-9)
用法: python3 accumulation_tracker.py [--start 2026-08-03] [--out 复盘/accumulation]
输出: 复盘/accumulation/YYYY-MM-DD_主力建仓跟踪.md + snapshots/YYYY-MM-DD.json(供次日diff)
"""
import json, os, sys, time, urllib.request, datetime

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
HOSTS = ["92.push2his.eastmoney.com", "83.push2his.eastmoney.com",
         "48.push2his.eastmoney.com", "13.push2his.eastmoney.com",
         "push2his.eastmoney.com"]
BASE = "data/accumulation_universe.json"


def secid(c):
    # sh/sz/bj → 东财 secid 前缀: 1=沪, 0=深, 北交所试 0.
    return ("1." if c[:2] == "sh" else "0.") + c[2:]


def fetch_fflow(code, lmt=80):
    sid = secid(code)
    url = (f"/api/qt/stock/fflow/daykline/get?lmt={lmt}&klt=101&secid={sid}"
           f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f62,f63")
    last_err = None
    for h in HOSTS:
        try:
            r = urllib.request.urlopen(urllib.request.Request("http://" + h + url, headers=UA), timeout=8).read().decode("utf-8", errors="replace")
            d = json.loads(r)
            ks = d.get("data", {}).get("klines")
            if ks:
                rows = []
                for line in ks:
                    f = line.split(",")
                    rows.append({"date": f[0], "main": float(f[1]), "xl": float(f[5]),
                                 "close": float(f[7]), "pct": float(f[8])})
                return rows
        except Exception as e:
            last_err = e
            time.sleep(0.2)
    return None


def analyze(rows, start):
    rows = [r for r in rows if r["date"] >= start]
    if len(rows) < 5:
        return None
    cum_main = sum(r["main"] for r in rows)
    cum_xl = sum(r["xl"] for r in rows)
    pos_days = sum(1 for r in rows if r["main"] > 0)
    buy_on_down = sum(1 for r in rows if r["pct"] < 0 and r["main"] > 0)
    pchg = (rows[-1]["close"] / rows[0]["close"] - 1) * 100
    in_range = -20 <= pchg <= 15
    return {"n": len(rows), "cum_main_yi": round(cum_main / 1e8, 2), "cum_xiada_yi": round(cum_xl / 1e8, 2),
            "pos_days": pos_days, "buy_on_down": buy_on_down, "pchg": round(pchg, 1),
            "in_range": in_range, "first": rows[0]["date"], "last": rows[-1]["date"],
            "close": rows[-1]["close"]}


def main():
    args = sys.argv[1:]
    start = args[args.index("--start") + 1] if "--start" in args else "2026-08-03"
    outdir = args[args.index("--out") + 1] if "--out" in args else "复盘/accumulation"
    os.makedirs(outdir + "/snapshots", exist_ok=True)
    universe = json.load(open(BASE))["stocks"]
    today = datetime.date.today().isoformat()

    results, fails = [], []
    for i, s in enumerate(universe):
        rows = fetch_fflow(s["code"])
        if not rows:
            fails.append(f'{s["name"]}({s["code"]})')
            continue
        m = analyze(rows, start)
        if m is None:
            fails.append(f'{s["name"]}({s["code"]}) 数据不足')
            continue
        m.update({"name": s["name"], "code": s["code"], "group": s["group"]})
        results.append(m)
        time.sleep(0.12)

    if not results:
        print("全部抓取失败:", fails)
        sys.exit(1)

    # 评分(0-9): 累计主力净流入分位(0-4) + 超大单占比(主力净为正且超大单>60% →+1) + 逢跌买入(≥3天+1, ≥5天+2) + 磨底区间(+1)
    cums = sorted(r["cum_main_yi"] for r in results)
    def pctile(v):
        if v <= 0:
            return 0
        below = sum(1 for c in cums if c < v)
        return min(4, int(below / len(results) * 5))
    for r in results:
        sc = pctile(r["cum_main_yi"])
        if r["cum_main_yi"] > 0 and r["cum_xiada_yi"] > 0.6 * r["cum_main_yi"]:
            sc += 1
        sc += 2 if r["buy_on_down"] >= 5 else (1 if r["buy_on_down"] >= 3 else 0)
        if r["in_range"]:
            sc += 1
        r["score"] = sc
    results.sort(key=lambda r: (-r["score"], -r["cum_main_yi"]))

    # 与昨日快照 diff（新增信号/退坡）
    snap_dir = outdir + "/snapshots"
    snaps = sorted(f for f in os.listdir(snap_dir) if f.endswith(".json"))
    prev = None
    for f in reversed(snaps):
        if f < today + ".json":
            prev = json.load(open(snap_dir + "/" + f))
            prev_name = f
            break
    if prev:
        prev_map = {p["name"]: p for p in prev}
        for r in results:
            p = prev_map.get(r["name"])
            if p:
                r["diff_cum"] = round(r["cum_main_yi"] - p["cum_main_yi"], 2)
                r["diff_score"] = r["score"] - p["score"]
            else:
                r["diff_cum"], r["diff_score"] = None, None
    else:
        prev_name = None
        for r in results:
            r["diff_cum"], r["diff_score"] = None, None

    # markdown
    L = [f"# 科技线主力建仓跟踪 {today}（窗口 {start} 起）", ""]
    L.append(f"样本 {len(results)}/{len(universe)} 只 | 数据截至 {results[0]['last'] if results else 'NA'} | 对比基准: {prev_name or '首日基线(无diff)'}")
    L.append("")
    L.append("**信号语义**: 主力净累计=8/3起主力净流入总和(亿)；逢跌买入=收跌且主力净流入>0的天数；磨底区间=区间涨幅-20%~+15%(>15%视为已启动/突破,单列)；评分=净流入分位(0-4)+超大单主导(+1)+逢跌买入(0-2)+磨底(+1)")
    L.append("")
    hdr = "| 排名 | 标的 | 子线 | 主力累计(亿) | 超大单(亿) | 净流入天数 | 逢跌买入 | 区间涨跌% | 磨底 | 评分 | 较昨日累计 | 评分Δ |"
    L.append(hdr)
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        flag = "✅" if r["in_range"] else ("🚀已启动" if r["pchg"] > 15 else "🔻")
        d_cum = f"{r['diff_cum']:+.1f}" if r["diff_cum"] is not None else "—"
        d_sc = f"{r['diff_score']:+d}" if r["diff_score"] is not None else "—"
        L.append(f"| {i} | {r['name']} {r['code']} | {r['group']} | {r['cum_main_yi']:.1f} | {r['cum_xiada_yi']:.1f} | {r['pos_days']}/{r['n']} | {r['buy_on_down']} | {r['pchg']:+.1f} | {flag} | **{r['score']}** | {d_cum} | {d_sc} |")
    if fails:
        L.append("")
        L.append(f"⚠️ 抓取失败 {len(fails)}: " + ", ".join(fails))
    L.append("")
    L.append(f"*生成 {datetime.datetime.now():%H:%M} · 数据源: 东财push2his fflow · 窗口止 2026-09-16 收盘(FOMC 9-17 凌晨决议后本表停止增量解读)*")
    md_path = f"{outdir}/{today}_主力建仓跟踪.md"
    open(md_path, "w").write("\n".join(L))
    json.dump([{k: r[k] for k in ("name", "code", "group", "cum_main_yi", "cum_xiada_yi", "pos_days", "buy_on_down", "pchg", "in_range", "score", "close")} for r in results],
              open(f"{snap_dir}/{today}.json", "w"), ensure_ascii=False, indent=1)

    # 控制台 Top15
    print(f"✅ {md_path}  ({len(results)}只, 失败{len(fails)})")
    print(f"窗口 {results[0]['first']}→{results[0]['last']}")
    print("Top15:")
    for i, r in enumerate(results[:15], 1):
        flag = "磨底" if r["in_range"] else ("已启动" if r["pchg"] > 15 else "区间外")
        d = f" Δcum{r['diff_cum']:+.1f}/Δsc{r['diff_score']:+d}" if r["diff_cum"] is not None else ""
        print(f"  {i:2d}. {r['name']}[{r['group']}] 累计{r['cum_main_yi']:+.1f}亿 超大{r['cum_xiada_yi']:+.1f}亿 逢跌买{r['buy_on_down']} 区间{r['pchg']:+.1f}% {flag} 评分{r['score']}{d}")


if __name__ == "__main__":
    main()

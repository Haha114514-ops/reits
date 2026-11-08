# -*- coding: utf-8 -*-
"""全市场 REITs 一键取数 + 粗筛指标。

数据源（2026-11 实测可用）：
  深交所  reits.szse.cn  基金列表：代码/名称/上市日/总份额/流通份额
  上交所  query.sse.com.cn  REITS_JZ：每半年评估净值；REITS_FXYKM：发售信息
  新浪    日线（不复权）：价格、成交量 → 成交额、价格分位、上市首日
  东方财富 fundf10 静态页：历次分红（每份派现、除息日）
  （东方财富行情接口和集思录本机连不上，不用）

用法：
  python fetch_all.py --out <工作目录>\\2026-11-07
输出：
  <out>/raw/            每只的日线、分红原始数据（缓存，重跑跳过）
  <out>/universe.csv    全部指标一张表
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pandas as pd
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

UA = {"User-Agent": "Mozilla/5.0"}

# 经营权类关键词（到期资产交还）。拿不准的在 TYPE_OVERRIDE 里手工指定。
OPS_WORDS = ("高速", "公路", "交控", "交建", "铁建", "隧道", "路桥", "能源", "电力", "电投", "华电", "风电",
             "光伏", "水电", "清洁", "环保", "水务", "供热", "热电", "垃圾", "水利", "燃气", "储能", "电建", "绿电")
# 名称认不出的手工指定（代码: (类型, 板块)）。新上市只有简称的，查上交所 REITS_FXYKM 全称后补这里。
OVERRIDE = {
    "508001": ("经营权", "高速公路"), "180201": ("经营权", "高速公路"), "508036": ("经营权", "高速公路"),
    "508093": ("经营权", "高速公路"),  # 易方达广西北投高速
    "508030": ("经营权", "能源"), "508059": ("经营权", "能源"), "508081": ("经营权", "能源"),  # 中核汇能/三峡新能源/华能燃煤
    "180801": ("经营权", "环保水利"),  # 首钢绿能 垃圾焚烧
    "180103": ("产权", "园区"), "508010": ("产权", "园区"), "508088": ("产权", "园区"), "508092": ("产权", "园区"),
    "508099": ("产权", "园区"), "508039": ("产权", "园区"), "508097": ("产权", "园区"),  # 首农/南京建邺 待核
    "508048": ("产权", "仓储物流"), "508084": ("产权", "仓储物流"),
    "508068": ("产权", "保租房"), "508095": ("产权", "保租房"),
    "180605": ("产权", "消费"), "508600": ("产权", "消费"), "508601": ("产权", "消费"), "508602": ("产权", "消费"),
    "508603": ("产权", "消费"), "508607": ("产权", "消费"), "508609": ("产权", "消费"),
}
TYPE_OVERRIDE = {}
SECTOR_RULES = [
    ("高速公路", ("高速", "公路", "交控", "交建", "铁建", "隧道", "路桥")),
    ("能源", ("能源", "电力", "电投", "华电", "风电", "光伏", "水电", "清洁", "储能", "电建", "绿电", "燃气", "供热", "热电")),
    ("环保水利", ("环保", "水务", "垃圾", "水利")),
    ("保租房", ("保租", "租赁住房", "安居", "宽庭", "住房", "公寓")),
    ("仓储物流", ("物流", "仓储", "盐田港", "普洛斯", "菜鸟", "安博", "宝湾", "深国际", "顺丰", "京东", "嘉里")),
    ("数据中心", ("数据", "算力", "IDC")),
    ("消费", ("消费", "奥莱", "商业", "百联", "印力", "万达", "大悦城", "物美", "华润", "天虹", "砂之船", "金茂", "绿发")),
    ("园区", ("产业园", "产园", "园区", "科创", "高新", "软件", "光谷", "临港", "张江", "创新", "产业", "科技")),
    ("商办", ("办公", "写字楼", "商办", "国资")),
]


def get(url, **kw):
    for i in range(3):
        try:
            r = requests.get(url, headers={**UA, **kw.pop("headers", {})}, timeout=20, **kw)
            r.raise_for_status()
            return r
        except Exception:
            if i == 2:
                raise
            time.sleep(1 + i)


# ---------- 名单 ----------
def sz_list():
    rows, page = [], 1
    while True:
        d = get("https://reits.szse.cn/api/report/ShowReport/data",
                params={"SHOWTYPE": "JSON", "CATALOGID": "reits_fund_list", "PAGENO": page}).json()[0]
        for x in d["data"]:
            name = re.sub("<[^>]+>", "", x["jjjcurl"]).strip()
            rows.append({"code": x["sys_key"], "market": "sz", "name": name, "list_date": x["ssrq"],
                         "shares_wan": float(x["dqgm"].replace(",", "")),
                         "float_wan": float(x["ltgm"].replace(",", ""))})
        if page >= int(d["metadata"]["pagecount"]):
            return rows
        page += 1


def sina_quotes(symbols):
    out = {}
    for i in range(0, len(symbols), 200):
        t = get("https://hq.sinajs.cn/list=" + ",".join(symbols[i:i + 200]),
                headers={"Referer": "https://finance.sina.com.cn"}).content.decode("gbk", "replace")
        for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', t):
            f = m.group(2).split(",")
            if len(f) > 30 and f[0]:
                out[m.group(1)] = {"name": f[0], "price": float(f[3]) or float(f[2]), "date": f[30]}
    return out


def sh_list():
    q = sina_quotes([f"sh{c}" for c in range(508000, 509000)])
    return [{"code": s[2:], "market": "sh", "name": v["name"]} for s, v in q.items()]


def sse_query(sql):
    res, page = [], 1
    while True:
        d = get("https://query.sse.com.cn/commonSoaQuery.do",
                params={"sqlId": sql, "isPagination": "true", "pageHelp.pageSize": 200,
                        "pageHelp.pageNo": page, "pageHelp.beginPage": page},
                headers={"Referer": "https://www.sse.com.cn/"}).json()
        part = d.get("result") or []
        res += part
        total = int(d.get("pageHelp", {}).get("total") or len(res))
        if not part or len(res) >= total:
            return res
        page += 1


# ---------- 单只 ----------
def kline(sym, raw):
    p = os.path.join(raw, f"{sym}_k.csv")
    if not os.path.exists(p):
        js = get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                 params={"symbol": sym, "scale": 240, "ma": "no", "datalen": 3000}).json() or []
        pd.DataFrame(js).to_csv(p, index=False)
    df = pd.read_csv(p)
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["day"] = pd.to_datetime(df["day"])
    return df


def dividends(code, raw):
    p = os.path.join(raw, f"{code}_fh.csv")
    if not os.path.exists(p):
        t = get(f"https://fundf10.eastmoney.com/fhsp_{code}.html").content.decode("utf-8", "replace")
        rows = []
        for df in pd.read_html(io.StringIO(t)):
            if "除息日" in df.columns:
                for _, r in df.iterrows():
                    m = re.search(r"([\d.]+)元", str(r.get("每10份分红", "")))
                    if m:
                        rows.append({"ex_date": r["除息日"], "per_unit": float(m.group(1)) / 10})
        pd.DataFrame(rows, columns=["ex_date", "per_unit"]).to_csv(p, index=False)
    df = pd.read_csv(p)
    df["ex_date"] = pd.to_datetime(df["ex_date"], errors="coerce")
    return df.dropna()


def classify(code, name):
    if code in OVERRIDE:
        return OVERRIDE[code]
    if "有巢" in name:
        return "产权", "保租房"
    sector = next((s for s, ws in SECTOR_RULES if any(w in name for w in ws)), "其他")
    if code in TYPE_OVERRIDE:
        kind = TYPE_OVERRIDE[code]
    elif code.startswith(("1802", "1804")) or sector in ("高速公路", "能源", "环保水利"):
        kind = "经营权"
    elif any(w in name for w in OPS_WORDS):
        kind = "经营权"
    else:
        kind = "产权"
    return kind, sector


def metrics(item, raw, today, nav_hist):
    code, mk = item["code"], item["market"]
    k = kline(mk + code, raw)
    fh = dividends(code, raw)
    out = dict(item)
    out["kind"], out["sector"] = classify(code, item["name"])
    if k.empty:
        out["note"] = "无行情（未上市？）"
        return out
    last = k.iloc[-1]
    px = float(last["close"])
    out["price"] = px
    out["last_day"] = last["day"].date().isoformat()
    out.setdefault("list_date", k.iloc[0]["day"].date().isoformat())
    if not out.get("list_date"):
        out["list_date"] = k.iloc[0]["day"].date().isoformat()
    out["days_listed"] = (today - datetime.strptime(out["list_date"], "%Y-%m-%d").date()).days
    tail = k.tail(20)
    out["turnover20_wan"] = round(float((tail["volume"] * tail["close"]).mean()) / 1e4, 1)
    out["chg_1y_pct"] = round((px / float(k[k["day"] <= pd.Timestamp(today - timedelta(days=365))]["close"].iloc[-1]) - 1) * 100, 1) \
        if (k["day"] <= pd.Timestamp(today - timedelta(days=365))).any() else None
    out["price_pctile"] = round(float((k["close"] <= px).mean()) * 100, 1)
    out["price_vs_ipo"] = round(px / float(k.iloc[0]["open"]), 2)
    ttm = fh[fh["ex_date"] > pd.Timestamp(today - timedelta(days=365))]["per_unit"].sum()
    out["dist_ttm"] = round(float(ttm), 4)
    out["dist_yield_ttm"] = round(float(ttm) / px * 100, 2) if px else None
    out["dist_count_ttm"] = int((fh["ex_date"] > pd.Timestamp(today - timedelta(days=365))).sum())
    out["dist_total"] = round(float(fh["per_unit"].sum()), 4)
    if out["days_listed"] < 400:
        out["note"] = "上市不足 13 个月，TTM 分派不完整"
    if "shares_wan" in out and out.get("shares_wan"):
        out["float_ratio"] = round(out["float_wan"] / out["shares_wan"] * 100, 1)
    navs = nav_hist.get(code)
    if navs:
        navs = sorted(navs)
        out["nav_date"], out["nav"] = navs[-1]
        out["p_nav"] = round(px / out["nav"], 2)
        yr = [v for d, v in navs if d <= (datetime.strptime(navs[-1][0], "%Y-%m-%d") - timedelta(days=360)).date().isoformat()]
        if yr:
            out["nav_chg_1y_pct"] = round((out["nav"] / yr[-1] - 1) * 100, 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--today")
    a = ap.parse_args()
    today = datetime.strptime(a.today, "%Y-%m-%d").date() if a.today else date.today()
    raw = os.path.join(a.out, "raw")
    os.makedirs(raw, exist_ok=True)

    print("名单：深交所 …", end=" ", flush=True)
    sz = sz_list()
    print(len(sz), "只；上交所（新浪扫码段）…", end=" ", flush=True)
    sh = sh_list()
    print(len(sh), "只")
    nav_hist = {}
    try:
        jz = sse_query("REITS_JZ")
        for x in jz:
            nav_hist.setdefault(x["fundCode"], []).append((x["appraiseDate"], float(x["fundUnitnetWorth"])))
        full = {x["fundCode"]: x["secNameFull"] for x in jz}
    except Exception as e:
        print("上交所净值失败：", e)
        full = {}
    for x in sh:
        x["name"] = full.get(x["code"], x["name"])

    items = sh + sz
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda it: _safe(metrics, it, raw, today, nav_hist), items))
    res.sort(key=lambda r: (r.get("kind", ""), r.get("sector", ""), r["code"]))
    cols = ["code", "market", "name", "kind", "sector", "list_date", "days_listed", "price", "last_day",
            "turnover20_wan", "dist_ttm", "dist_count_ttm", "dist_yield_ttm", "dist_total", "price_vs_ipo",
            "price_pctile", "chg_1y_pct", "nav", "nav_date", "p_nav", "nav_chg_1y_pct",
            "shares_wan", "float_wan", "float_ratio", "note"]
    p = os.path.join(a.out, "universe.csv")
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(res)
    n_ok = sum(1 for r in res if r.get("price"))
    print(f"完成：{len(res)} 只（有行情 {n_ok}），写入 {p}")
    bad = [r for r in res if r.get("note", "").startswith("出错")]
    for r in bad:
        print("  ", r["code"], r["note"])


def _safe(fn, it, *args):
    try:
        return fn(it, *args)
    except Exception as e:
        out = dict(it)
        out["note"] = f"出错：{type(e).__name__} {e}"[:120]
        return out


if __name__ == "__main__":
    main()

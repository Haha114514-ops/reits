# -*- coding: utf-8 -*-
"""下载沪市 REITs 定期报告 PDF 并转成文本（深市待补）。

用法：python fetch_reports.py --codes 508089,508097 --out D:\\AC\\reits\\2026-11-07\\reports
      [--kinds 季度报告,年度报告] [--start 2025-10-01] [--end 2026-11-07]
每只每类只取最新一份（季度报告不含"更正"之外的同期多份时取最新）。输出 <code>_<类>.pdf / .txt。
数据源：上交所 query.sse.com.cn sqlId=REITS_BULLETIN（参数 fundCode/title/startDate/endDate）。
"""
import argparse
import os
import sys
import time

import fitz  # pymupdf
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/"}


def bulletins(code, title, start, end):
    r = requests.get("https://query.sse.com.cn/commonSoaQuery.do", headers=H, timeout=20, params={
        "sqlId": "REITS_BULLETIN", "isPagination": "true", "fundCode": code, "title": title,
        "reitsBulletinType": "", "startDate": start, "endDate": end,
        "pageHelp.pageSize": 50, "pageHelp.pageNo": 1})
    return r.json().get("result") or []


def sz_bulletins(code, title, start, end):
    import json as _j
    d = requests.post("https://reits.szse.cn/api/disc/announcement/annList", timeout=20,
                      headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
                               "Referer": "https://reits.szse.cn/"},
                      data=_j.dumps({"seDate": [start, end], "stock": [code], "channelCode": ["reits-xxpl"],
                                     "pageSize": 100, "pageNum": 1})).json()
    return [{"title": x["title"], "sseDate": x["publishTime"][:10],
             "url": "https://disc.static.szse.cn/download" + x["attachPath"]}
            for x in d.get("data") or [] if title in x["title"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--kinds", default="季度报告,年度报告")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-12-31")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    codes = a.codes.split(",")
    if a.codes.endswith(".csv"):
        import csv as _c
        codes = [r["code"] for r in _c.DictReader(open(a.codes, encoding="utf-8-sig"))]
    for code in codes:
        for kind in a.kinds.split(","):
            if os.path.exists(os.path.join(a.out, f"{code}_{kind}.txt")):
                continue
            try:
                src = sz_bulletins if code.startswith("18") else bulletins
                res = [x for x in src(code, kind, a.start, a.end)
                       if kind in (x.get("title") or "") and "摘要" not in x["title"] and "英文" not in x["title"]]
            except Exception as e:
                print(f"{code} {kind}: 查询失败 {type(e).__name__}")
                continue
            if not res:
                print(f"{code} {kind}: 未找到")
                continue
            res.sort(key=lambda x: x.get("sseDate") or x.get("publishDate") or "", reverse=True)
            x = res[0]
            url = x.get("url") or x.get("URL") or ""
            if url.startswith("/"):
                url = "https://www.sse.com.cn" + url
            base = os.path.join(a.out, f"{code}_{kind}")
            if not os.path.exists(base + ".txt"):
                pdf = None
                for i in range(3):
                    try:
                        pdf = requests.get(url, headers=H, timeout=120).content
                        break
                    except Exception:
                        time.sleep(3 * (i + 1))
                if not pdf:
                    print(f"{code} {kind}: 下载失败 {url}")
                    continue
                open(base + ".pdf", "wb").write(pdf)
                doc = fitz.open(base + ".pdf")
                open(base + ".txt", "w", encoding="utf-8").write("\n".join(p.get_text() for p in doc))
            print(f"{code} {kind}: {x['title'][:60]} ({x.get('sseDate') or x.get('publishDate')})")


if __name__ == "__main__":
    main()

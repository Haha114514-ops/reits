# -*- coding: utf-8 -*-
"""从 fetch_reports.py 下载的报告文本里抽"五格"关键数（省 token，别整篇读）。

两种用法：
  看片段：python report_facts.py --dir <reports> --codes 508089,508097
  出表格：python report_facts.py --dir <reports> --codes <universe.csv> --csv <out.csv>
表格字段：
  irr_mgr        年报 3.1 表"内部收益率"（管理人按 2025 年末收盘价测算，%）
  remain_years   剩余运营/经营/收费年限（季报优先）
  occ_now/occ_prev   报告期末出租率（多资产取平均，%）
  rent_now/rent_prev 租金单价（元/平方米/月，多资产取平均）
  traffic_yoy / power_yoy / water_yoy  车流量、发电量、污水处理量同比 %
  ann_dist_yield 季报"年化现金流分派率"（%，按报告期末市值）
  dist_note      可供分配金额同比变动超 10% 的原因（前 80 字）
  —— 重估 NAV（产权类，取年报，亿元/元每份）——
  book_nav_ps    账面份额净值（年报 3.2）
  fair_nav_ps    年报"公允价值参考净值"：资产换成评估值但**留着商誉**，只作对照
  appraisal_yi   评估价值合计（年报评估章节）
  goodwill_yi / dtl_yi  商誉 / 递延所得税负债（合并资产负债表）
  rnav_ps        重估 NAV 每份 =（评估价值 + 其他资产 − 负债）÷ 份额
                 其他资产 = 总资产 − 固定资产 − 在建工程 − 无形资产 − 投资性房地产 − 商誉
  rnav_dtl_ps    同上但加回递延所得税负债（乐观口径）
  rnav_src       评估值来源：年报评估值 / 公允参考净值−商誉 / 待核
"""
import argparse
import csv
import os
import re
import sys
from statistics import mean

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SNIPPETS = [
    ("管理人IRR", r"内部收益率(（%）|\(%\))?\d", 70, "年度报告"),
    ("剩余年限", r"剩余(运营|经营|收费)?(期限|年限)约为?[\d.]+年|收费期届满|特许经营权期限自", 90, "季度报告"),
    ("出租率", r"报告期末出租率", 70, "季度报告"),
    ("租金单价", r"租金单价(水平)?.{0,60}?元/平方米/月", 60, "季度报告"),
    ("车流量", r"日均(自然|收费)?车流量", 80, "季度报告"),
    ("发电量", r"发电量.{0,40}?(万千瓦时|亿千瓦时)", 80, "季度报告"),
    ("污水处理量", r"污水处理量实际处理", 60, "季度报告"),
    ("分配变化说明", r"较上年同期变化超过10%的情况说明", 120, "季度报告"),
    ("扩募", r"扩募时间扩募方式", 120, "季度报告"),
]
NUM = r"(-?\d[\d,]*\.?\d*)"


def load(path, keep_space):
    if not os.path.exists(path):
        return ""
    t = open(path, encoding="utf-8").read()
    if not keep_space:
        return re.sub(r"\s+", "", t)
    t = re.sub(r"\s+", " ", t)
    # PDF 转文字会在中文之间插空格（"剩 余运营年限"），去掉非数字之间的空格，保留数字之间的
    return re.sub(r"(?<=[^\d.,\-])\s(?=[^\d.,\-])", "", t)


def f(x):
    try:
        return float(x.replace(",", ""))
    except Exception:
        return None


def pairs(t, pat):
    out = []
    for m in re.finditer(pat, t):
        a, b = f(m.group(1)), f(m.group(2))
        if a is not None and b is not None:
            out.append((a, b))
    return out


def window_pairs(t, label_pat, width):
    """找到标签后，在其后 width 字内取第一组"本期 上期 [同比]"两个带小数的数（表格行）。"""
    out = []
    for m in re.finditer(label_pat, t):
        w = t[m.end():m.end() + width]
        n = re.search(r"(\d{1,6}(?:,\d{3})*\.\d+)\s?%?\s+(\d{1,6}(?:,\d{3})*\.\d+)\s?%?", w)
        if n:
            out.append((f(n.group(1)), f(n.group(2))))
    return out


def yoy(t, label_pat):
    # 表格行：<指标> ... 单位 本期 上年同期 同比%
    m = re.search(label_pat + r"[^0-9]{0,120}?" + NUM + r"\s+" + NUM + r"\s+" + NUM, t)
    if m:
        a, b, c = f(m.group(1)), f(m.group(2)), f(m.group(3))
        if a and b:
            return round((a / b - 1) * 100, 1)
        return c
    m = re.search(label_pat + r".{0,80}?较\d{4}\s?年同期(增长|上升|下降|减少)\s?(\d+\.?\d*)%", t)
    if m:
        v = float(m.group(2))
        return v if m.group(1) in ("增长", "上升") else -v
    return None


ASSET_ITEMS = ("固定资产", "在建工程", "无形资产", "投资性房地产")  # 被评估值替代的资产科目（另加商誉）
UNIT = {"元": 1, "万元": 1e4, "亿元": 1e8}


def bs_item(bs, label):
    """合并资产负债表里某科目的本期末金额（元）；'-' 或没有 → 0。"""
    m = re.search(label + r"\s?(?:\d+(?:\.\d+){2,}\s)?\s?(-?\d[\d,]*\.\d+|-)(?=\s|$)", bs)
    if not m:
        return 0.0
    return 0.0 if m.group(1) == "-" else f(m.group(1))


def appraisal_total(y):
    """年报末尾评估章节里的评估值合计（元）。排除'交易案例比较法/校验'等辅助结果。"""
    tail_start = int(len(y) * 0.75)
    t = y[tail_start:]
    pat = r"(?:评估值|评估价值|市场价值|估值结果)(?:合计|总计)?(?:为|是)?(?:人民币)?\s?(\d[\d,]*\.?\d*)\s?(亿元|万元|元)(?!\s?/)"
    totals, by_sentence = [], {}
    for m in re.finditer(pat, t):
        pre = t[max(0, m.start() - 200):m.start()]
        if re.search(r"比较法|校验", pre):
            continue
        v = f(m.group(1))
        if not v:
            continue
        v = round(v * UNIT[m.group(2)])
        if re.search(r"合计|总计|共计|整体", t[max(0, m.start() - 40):m.end()]):
            totals.append(v)
        # 同一句（到句号/分号为止）里出现多个数，取最后一个（如"评估值82,000万元…扣除增值税后的评估值78,140万元"）
        s0 = max(t.rfind("。", 0, m.start()), t.rfind("；", 0, m.start()))
        s0 = max(s0, m.start() - 150)
        # 同一资产的结论常在"摘要"和"正文"各出现一次，按数值去重
        by_sentence[s0] = v
    vals = []
    for v in by_sentence.values():
        if v not in vals:
            vals.append(v)
    if not vals:
        return None, ""
    if len(vals) == 1:
        return vals[0], "年报评估值"
    # 常见写法：先写"全部项目市场价值 X"，再逐项列出 —— 某个数 ≈ 其余之和，它就是合计
    for v in sorted(vals, reverse=True):
        rest = sum(x for x in vals if x != v)
        if rest and abs(v / rest - 1) < 0.01:
            return v, "年报评估值合计"
    if totals:
        return max(totals), "年报评估值合计"
    return sum(vals), "年报评估值逐项相加"


def reval(y):
    """重估 NAV。y 为年报文本（keep_space）。返回 dict，取不到的不填。"""
    r = {}
    m = re.search(r"期末(?:不动产)?基金净资产\s?" + NUM, y)
    na = f(m.group(1)) if m else None
    m = re.search(r"期末(?:不动产)?基金份额净值\s?" + NUM, y)
    bk = f(m.group(1)) if m else None
    m = re.search(r"期末(?:不动产)?基金份额公允价值参考净值\s?" + NUM, y)
    fair = f(m.group(1)) if m else None
    if not (na and bk):
        return r
    shares = na / bk
    r["book_nav_ps"], r["fair_nav_ps"] = bk, fair
    m = re.search(r"合并资产负债表\s?会计主体", y) or re.search(r"资产负债表\s?会计主体", y)
    if not m:
        return r
    bs = y[m.start():m.start() + 9000]
    gw, dtl = bs_item(bs, "商誉"), bs_item(bs, "递延所得税负债")
    asset_book = sum(bs_item(bs, k) for k in ASSET_ITEMS)
    assets = asset_book + gw
    r["goodwill_yi"], r["dtl_yi"] = round(gw / 1e8, 2), round(dtl / 1e8, 2)
    ap, src = appraisal_total(y)
    # 合理性校验：REIT 按收购价入账，评估值一般在账上资产（含商誉）的 0.5–1.6 倍；超出多为抓漏/重复
    if ap and (asset_book <= 0 or not (0.5 < ap / assets < 1.6)):
        r["rnav_src"] = f"待核：评估值{ap / 1e8:.2f}亿 vs 账上资产{assets / 1e8:.2f}亿"
        ap = None
    rnav_a = na - assets + ap if ap else None
    rnav_b = fair * shares - gw if fair else None
    if rnav_a and rnav_b and abs(rnav_a / rnav_b - 1) > 0.1:
        rnav, src = rnav_b, "公允参考净值−商誉（年报评估值抓取不一致）"
        ap = None
    elif rnav_a:
        rnav = rnav_a
    elif rnav_b:
        rnav, src = rnav_b, "公允参考净值−商誉"
    else:
        return r  # 取不到就留空（rnav_src 里可能写了"待核"原因）
    r["appraisal_yi"] = round(ap / 1e8, 2) if ap else None
    r["rnav_ps"] = round(rnav / shares, 4)
    r["rnav_dtl_ps"] = round((rnav + dtl) / shares, 4)
    r["rnav_src"] = src
    return r


def extract(dirpath, code):
    q = load(os.path.join(dirpath, f"{code}_季度报告.txt"), True)
    y = load(os.path.join(dirpath, f"{code}_年度报告.txt"), True)
    r = {"code": code, "has_q3": bool(q), "has_annual": bool(y)}
    m = re.search(r"内部收益率\s?(?:（%）|\(%\))?\s*" + NUM + r"\s?%?", y)
    r["irr_mgr"] = f(m.group(1)) if m else None
    m = re.search(r"剩余\s?(?:运营|经营|收费)?\s?(?:期限|年限)\s?约为?\s?" + NUM + r"\s?年", q) or \
        re.search(r"剩余\s?(?:运营|经营|收费)?\s?(?:期限|年限)\s?约为?\s?" + NUM + r"\s?年", y)
    r["remain_years"] = f(m.group(1)) if m else None
    if r["remain_years"] is None:
        # 兜底：从"收费期/特许经营期 ... 至 YYYY年M月" 或 "YYYY年M月D日（...收费期届满）" 推算，基准 2026-09-30
        m = re.search(r"(?:收费期限|收费期|特许经营权?期限?|经营期限|运营期限)[^。；]{0,60}?至\s?(\d{4})\s?年\s?(\d{1,2})\s?月", q + y) or \
            re.search(r"(\d{4})\s?年\s?(\d{1,2})\s?月\s?\d{1,2}\s?日\s?[（(][^）)]{0,20}(?:收费期|经营期)届满", q + y)
        if m:
            yrs = int(m.group(1)) + int(m.group(2)) / 12 - (2026 + 9 / 12)
            if 0 < yrs < 60:
                r["remain_years"] = round(yrs, 2)
                r["remain_src"] = "到期日推算"
    occ = [p for p in window_pairs(q, r"(?:报告期末|期末)[^。；\d]{0,12}?出租率", 260) if 0 < p[0] <= 100 and 0 < p[1] <= 100]
    if occ:
        r["occ_now"] = round(mean(p[0] for p in occ), 2)
        r["occ_prev"] = round(mean(p[1] for p in occ), 2)
        r["occ_min"] = min(p[0] for p in occ)
        r["occ_worst_chg"] = round(min(p[0] - p[1] for p in occ), 2)
    rent = [p for p in window_pairs(q, r"(?:租金单价|平均月租金|月均租金|平均租金)[^。；]{0,160}?元\s?/\s?(?:平方米|㎡|m2|间)", 120)
            if p[0] > 0 and p[1] > 0 and 0.5 < p[0] / p[1] < 2]
    if rent:
        r["rent_now"] = round(mean(p[0] for p in rent), 2)
        r["rent_prev"] = round(mean(p[1] for p in rent), 2)
        r["rent_worst_chg_pct"] = round(min((p[0] / p[1] - 1) * 100 for p in rent), 1)
    r["traffic_yoy"] = yoy(q, r"日均\s?(?:自然|收费)?\s?车流\s?量")
    r["power_yoy"] = yoy(q, r"(?:上网电量|发电量)")
    r["water_yoy"] = yoy(q, r"污水\s?处理量")
    m = re.search(r"年化现金流分派率\s?(?:（%）|\(%\))\s*" + NUM, q)
    r["ann_dist_yield"] = f(m.group(1)) if m else None
    m = re.search(r"较上年同期变化超过\s?10%\s?的情况说明\s*(.{0,80})", q)
    r["dist_note"] = m.group(1).strip() if m else ""
    if y:
        r.update(reval(y))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--codes", required=True)
    ap.add_argument("--csv")
    ap.add_argument("--max", type=int, default=2)
    a = ap.parse_args()
    codes = a.codes.split(",")
    if a.codes.endswith(".csv"):
        codes = [r["code"] for r in csv.DictReader(open(a.codes, encoding="utf-8-sig"))]
    if a.csv:
        rows = [extract(a.dir, c) for c in codes]
        cols = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.DictWriter(fp, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        for k in cols[1:]:
            n = sum(1 for r in rows if r.get(k) not in (None, "", False))
            print(f"{k:<20}{n}/{len(rows)}")
        return
    for code in codes:
        print(f"\n===== {code}")
        cache = {}
        for label, pat, width, kind in SNIPPETS:
            t = cache.setdefault(kind, load(os.path.join(a.dir, f"{code}_{kind}.txt"), False))
            for h in [t[m.start():m.start() + width] for m in re.finditer(pat, t)][:a.max]:
                print(f"  [{label}] {h}")


if __name__ == "__main__":
    main()

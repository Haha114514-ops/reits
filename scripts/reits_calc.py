# -*- coding: utf-8 -*-
"""REITs 研究计算器（纯标准库）。

子命令：
  screen   粗筛：读行情 CSV，分桶、剔流动性/次新/解禁，桶内排序
  irr      经营权类：按买入价 + 逐年每份现金流算 XIRR，对比国债利差
  cap      产权类：隐含 cap rate、P/NAV
  track    兑现率：逐期 实际/预测，连续不达标报警

示例见 SKILL.md。
"""
import argparse
import csv
import sys
from datetime import date, datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OPS_WORDS = ("高速", "公路", "能源", "光伏", "风电", "水电", "电厂", "环保", "水利", "供热", "垃圾", "污水", "经营权")


def parse_date(s):
    s = s.strip().replace("/", "-")
    return datetime.strptime(s, "%Y-%m-%d").date()


def fnum(s, default=None):
    try:
        s = str(s).strip().replace("%", "").replace(",", "")
        return float(s) if s not in ("", "-", "--") else default
    except ValueError:
        return default


# ---------- XIRR ----------
def xnpv(rate, flows):
    d0 = flows[0][0]
    return sum(cf / (1 + rate) ** ((d - d0).days / 365.0) for d, cf in flows)


def xirr(flows):
    lo, hi = -0.99, 1.0
    f_lo, f_hi = xnpv(lo, flows), xnpv(hi, flows)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = xnpv(mid, flows)
        if abs(f_mid) < 1e-10:
            break
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def cmd_irr(a):
    buy_day = parse_date(a.buy_date) if a.buy_date else date.today()
    flows = [(buy_day, -(a.price + a.fee * a.price))]
    with open(a.cashflows, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            d = parse_date(row["date"])
            if d <= buy_day:
                continue
            cf = fnum(row["amount"], 0.0)
            capex = fnum(row.get("capex", ""), 0.0) or 0.0
            flows.append((d, cf * a.haircut - capex))
    if len(flows) < 2:
        sys.exit("买入日之后没有现金流，检查 CSV 日期")
    r = xirr(flows)
    if r is None:
        sys.exit("现金流不收敛（总回收低于买入价太多？）")
    total = sum(cf for _, cf in flows[1:])
    years = (flows[-1][0] - buy_day).days / 365.0
    line = a.gov10y + a.spread / 100
    print(f"买入成本      {-flows[0][1]:.4f} 元/份（{buy_day}）")
    print(f"剩余现金流    {len(flows)-1} 笔，合计 {total:.4f} 元/份，覆盖 {years:.1f} 年")
    print(f"折扣系数      {a.haircut:.0%}（兑现率/压力情景）")
    print(f"隐含 IRR      {r:.2%}")
    print(f"红线          10年国债 {a.gov10y:.2f}% + {a.spread:.0f}bp = {line:.2f}%")
    verdict = "通过" if r * 100 >= line else "不通过（不碰）"
    print(f"结论          {verdict}，利差 {(r*100 - a.gov10y)*100:.0f}bp")
    if years < 5:
        print("提示          剩余不足 5 年：按短债看，比的是同期限信用债，不是 10 年国债")


# ---------- cap rate ----------
def cmd_cap(a):
    ev = a.mktcap + a.net_debt
    cap = a.noi / ev
    print(f"企业价值 EV   {ev:,.2f}（市值 {a.mktcap:,.2f} + 净负债 {a.net_debt:,.2f}）")
    print(f"年化 NOI      {a.noi:,.2f}")
    print(f"隐含 cap rate {cap:.2%}")
    if a.nav:
        print(f"P/NAV         {a.mktcap / a.nav:.2f}")
    if a.appraisal_cap:
        gap = (cap - a.appraisal_cap / 100) * 10000
        print(f"评估报告折现率/cap {a.appraisal_cap:.2f}%，市场比评估高 {gap:.0f}bp" if gap >= 0
              else f"评估报告折现率/cap {a.appraisal_cap:.2f}%，市场比评估低 {-gap:.0f}bp（市场比评估师还乐观）")
    if a.peer_median:
        ok = cap * 100 >= a.peer_median
        print(f"同类中位数    {a.peer_median:.2f}% → {'不低于中位数，可继续看' if ok else '低于中位数，不动'}")


# ---------- track ----------
def cmd_track(a):
    bad_streak, worst = 0, 0
    print(f"{'期间':<12}{'实际':>12}{'预测':>12}{'兑现率':>9}")
    with open(a.file, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            act, fc = fnum(row["actual"]), fnum(row["forecast"])
            if not act or not fc:
                continue
            rate = act / fc
            flag = ""
            if rate < a.threshold:
                bad_streak += 1
                flag = " ←不达标"
            else:
                bad_streak = 0
            worst = max(worst, bad_streak)
            print(f"{row['period']:<12}{act:>12,.2f}{fc:>12,.2f}{rate:>9.1%}{flag}")
    print()
    if bad_streak >= 2:
        print(f"结论：最近连续 {bad_streak} 期低于 {a.threshold:.0%} → 出局/回模板按实际数重算")
    elif worst >= 2:
        print(f"结论：历史上出现过连续 {worst} 期不达标，但最近已恢复，继续观察")
    else:
        print("结论：兑现正常，拿着")


# ---------- screen ----------
def cmd_screen(a):
    today = parse_date(a.today) if a.today else date.today()
    rows = []
    with open(a.file, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    kept, dropped = {"经营权": [], "产权": []}, []
    for r in rows:
        name = r.get("name", "")
        kind = (r.get("type") or "").strip()
        if kind not in ("经营权", "产权"):
            kind = "经营权" if any(w in name for w in OPS_WORDS) else "产权"
        amt = fnum(r.get("turnover_wan"), 0)
        if amt < a.min_turnover:
            dropped.append((name, f"成交额 {amt:.0f} 万 < {a.min_turnover:.0f} 万"))
            continue
        ld = r.get("list_date", "").strip()
        if ld and (today - parse_date(ld)).days < a.min_days:
            dropped.append((name, f"上市 {(today - parse_date(ld)).days} 天，不满 {a.min_days} 天"))
            continue
        ul = r.get("unlock_date", "").strip()
        if ul:
            gap = (parse_date(ul) - today).days
            if 0 <= gap <= 90:
                dropped.append((name, f"{gap} 天后有解禁"))
                continue
        r["_kind"] = kind
        kept[kind].append(r)

    def show(kind, key, label, reverse=True):
        lst = [r for r in kept[kind] if fnum(r.get(key)) is not None]
        lst.sort(key=lambda r: fnum(r[key]), reverse=reverse)
        print(f"\n== {kind}类（{len(kept[kind])} 只留下）按「{label}」排序，前 {a.top} ==")
        for r in lst[:a.top]:
            extra = "  ".join(f"{k}={r[k]}" for k in ("dist_yield", "p_nav", "remain_years", "price_pct") if r.get(k))
            print(f"  {r.get('code','')} {r.get('name','')}  {extra}")
        missing = len(kept[kind]) - len(lst)
        if missing:
            print(f"  （{missing} 只缺「{key}」列，未参与排序）")

    # 经营权类：分派率含本金返还，不能直接比；有剩余年限就用 分派率/剩余年限 粗调，更好的是逐只算 IRR
    for r in kept["经营权"]:
        dy, yrs = fnum(r.get("dist_yield")), fnum(r.get("remain_years"))
        if dy is not None and yrs:
            # 粗略：扣掉直线摊销的本金返还，剩下的近似"真收益"
            r["_adj"] = f"{dy - 100.0 / yrs:.2f}"
    show("经营权", "_adj" if any(r.get("_adj") for r in kept["经营权"]) else "dist_yield",
         "分派率−100/剩余年限（粗估真收益）" if any(r.get("_adj") for r in kept["经营权"]) else "分派率（缺剩余年限，仅参考）")
    show("产权", "p_nav", "P/NAV 从低到高", reverse=False)

    if dropped:
        print(f"\n剔除 {len(dropped)} 只：")
        for n, why in dropped:
            print(f"  {n}：{why}")


def main():
    p = argparse.ArgumentParser(description="REITs 研究计算器")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen")
    s.add_argument("--file", required=True)
    s.add_argument("--min-turnover", type=float, default=500, help="日均成交额下限，万元")
    s.add_argument("--min-days", type=int, default=180)
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--today")
    s.set_defaults(func=cmd_screen)

    s = sub.add_parser("irr")
    s.add_argument("--price", type=float, required=True, help="买入价 元/份")
    s.add_argument("--buy-date")
    s.add_argument("--fee", type=float, default=0.0, help="交易费率，如 0.0005")
    s.add_argument("--cashflows", required=True, help="CSV: date,amount[,capex] 每份")
    s.add_argument("--gov10y", type=float, required=True, help="10年国债收益率 %%")
    s.add_argument("--spread", type=float, default=250, help="要求利差 bp")
    s.add_argument("--haircut", type=float, default=1.0, help="现金流打折系数，如兑现率 0.9")
    s.set_defaults(func=cmd_irr)

    s = sub.add_parser("cap")
    s.add_argument("--mktcap", type=float, required=True)
    s.add_argument("--net-debt", type=float, default=0.0)
    s.add_argument("--noi", type=float, required=True, help="年化 NOI（同单位）")
    s.add_argument("--nav", type=float)
    s.add_argument("--appraisal-cap", type=float, help="评估报告折现率或资本化率 %%")
    s.add_argument("--peer-median", type=float, help="同类隐含 cap rate 中位数 %%")
    s.set_defaults(func=cmd_cap)

    s = sub.add_parser("track")
    s.add_argument("--file", required=True, help="CSV: period,actual,forecast")
    s.add_argument("--threshold", type=float, default=0.85)
    s.set_defaults(func=cmd_track)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()

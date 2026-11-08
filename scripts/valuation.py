# -*- coding: utf-8 -*-
"""全市场估值分档：好不好 × 贵不贵（读 fetch_all.py 的输出）。

用法：python valuation.py --dir <工作目录>\\2026-11-07 [--today 2026-11-07]
输出：<dir>/估值分档_<日期>.xlsx、<dir>/valuation.csv

口径（同板块内比较，板块不足 4 只的并入同类型比较）：
  贵不贵 = 现金收益率在板块内的排名（经营权类用"分红+净值变化"的真实收益，深市缺净值时用板块中位净值变化估算）
          + P/NAV（沪市才有）+ 上市以来价格分位
  好不好 = 分红趋势（近 12 个月 vs 前 12 个月）+ 评估净值一年变化
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def dist_trend(code, raw, today):
    p = os.path.join(raw, f"{code}_fh.csv")
    if not os.path.exists(p):
        return None
    fh = pd.read_csv(p)
    fh["ex_date"] = pd.to_datetime(fh["ex_date"], errors="coerce")
    t = pd.Timestamp(today)
    w1 = fh[(fh.ex_date > t - pd.Timedelta(days=365))]
    w2 = fh[(fh.ex_date > t - pd.Timedelta(days=730)) & (fh.ex_date <= t - pd.Timedelta(days=365))]
    avg2 = (w1.per_unit.sum() + w2.per_unit.sum()) / 2
    # 发放次数不同 = 时点错位（如一年发1次、下一年发3次），同比失真，不判趋势
    if w2.per_unit.sum() <= 0 or len(w1) != len(w2):
        return None, avg2
    return round((w1.per_unit.sum() / w2.per_unit.sum() - 1) * 100, 1), avg2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--today")
    ap.add_argument("--gov10y", type=float, default=1.59, help="10年国债收益率%%，每次现查")
    a = ap.parse_args()
    today = datetime.strptime(a.today, "%Y-%m-%d").date() if a.today else date.today()
    d = pd.read_csv(os.path.join(a.dir, "universe.csv"), dtype={"code": str})
    raw = os.path.join(a.dir, "raw")

    tr = [dist_trend(c, raw, today) if n >= 730 else (None, None) for c, n in zip(d.code, d.days_listed)]
    d["dist_yoy_pct"] = [x[0] for x in tr]
    d["dist_avg2y"] = [x[1] for x in tr]
    # 收益率：满两年用两年平均（抹平发放时点），否则用近一年
    d["dist_yield_ttm"] = d.apply(lambda r: round(r.dist_avg2y / r.price * 100, 2) if pd.notna(r.dist_avg2y) and r.dist_avg2y > 0
                                  else r.dist_yield_ttm, axis=1)
    d["peer"] = d.sector
    small = d.sector.map(d.sector.value_counts()) < 4
    d.loc[small, "peer"] = d.loc[small, "kind"]

    # 现金收益率：经营权扣净值变化（没有就用同板块中位数估算），产权只看现金分红
    nav_abs_chg = d.nav - d.nav / (1 + d.nav_chg_1y_pct / 100)
    d["nav_drag_pct"] = (nav_abs_chg / d.price * 100).round(2)
    med_drag = d[d.kind == "经营权"].groupby("sector").nav_drag_pct.median()
    est = d.kind.eq("经营权") & d.nav_drag_pct.isna()
    d.loc[est, "nav_drag_pct"] = d.loc[est, "sector"].map(med_drag)
    d["yield_basis"] = d.dist_yield_ttm
    ops = d.kind == "经营权"
    d.loc[ops, "yield_basis"] = (d.dist_yield_ttm + d.nav_drag_pct.clip(upper=0))[ops].round(2)
    d["yield_note"] = ""
    d.loc[ops, "yield_note"] = "分红扣净值下降"
    d.loc[est, "yield_note"] = "分红扣净值下降（深市无净值，按同板块中位估算）"
    d.loc[~ops, "yield_note"] = "现金分派率"

    full = d.days_listed >= 400
    d["yield_rank"] = None
    for peer, g in d[full].groupby("peer"):
        d.loc[g.index, "yield_rank"] = g.yield_basis.rank(pct=True)  # 越高越便宜
        d.loc[g.index, "peer_median_yield"] = g.yield_basis.median()
    d["peer_median_yield"] = d["peer_median_yield"].round(2)

    def value(r):
        if not (r.days_listed >= 400):
            return None, ""
        s, why = 0, []
        yr = float(r.yield_rank)
        if yr >= 2 / 3:
            s += 2; why.append(f"收益率{r.yield_basis:.1f}%高于同类中位{r.peer_median_yield:.1f}%")
        elif yr <= 1 / 3:
            s -= 2; why.append(f"收益率{r.yield_basis:.1f}%低于同类中位{r.peer_median_yield:.1f}%")
        if pd.notna(r.p_nav):
            if r.p_nav < 0.9:
                s += 1; why.append(f"股价只有净值{r.p_nav:.2f}倍")
            elif r.p_nav > 1.3:
                s -= 1; why.append(f"股价是净值{r.p_nav:.2f}倍")
        if r.price_pctile <= 20:
            s += 1; why.append(f"价格在上市以来低位（{r.price_pctile:.0f}%分位）")
        elif r.price_pctile >= 80:
            s -= 1; why.append(f"价格在上市以来高位（{r.price_pctile:.0f}%分位）")
        lab = "便宜" if s >= 2 else ("贵" if s <= -2 else "合理")
        return lab, "；".join(why)

    def quality(r):
        s, why, n = 0, [], 0
        if pd.notna(r.dist_yoy_pct):
            n += 1
            if r.dist_yoy_pct >= -5:
                s += 1; why.append(f"分红{r.dist_yoy_pct:+.0f}%")
            elif r.dist_yoy_pct < -15:
                s -= 2; why.append(f"分红缩水{r.dist_yoy_pct:.0f}%")
            else:
                why.append(f"分红{r.dist_yoy_pct:+.0f}%")
        if pd.notna(r.nav_chg_1y_pct):
            n += 1
            lim = -12 if r.kind == "经营权" else -3   # 经营权每年净值本来就会摊销下降
            bad = -18 if r.kind == "经营权" else -8
            if r.nav_chg_1y_pct >= lim:
                s += 1; why.append(f"净值{r.nav_chg_1y_pct:+.0f}%")
            elif r.nav_chg_1y_pct < bad:
                s -= 2; why.append(f"净值下调{r.nav_chg_1y_pct:.0f}%")
            else:
                why.append(f"净值{r.nav_chg_1y_pct:+.0f}%")
        if n == 0:
            return "数据不足", "上市不满两年、也无净值历史"
        lab = "好" if s >= 1 and s == n else ("差" if s <= -2 else "一般")
        return lab, "；".join(why)

    # ---- 第二轮：并入定期报告抽出的数（report_facts.py --csv <dir>/facts.csv）----
    fp = os.path.join(a.dir, "facts.csv")
    has_facts = os.path.exists(fp)
    if has_facts:
        fx = pd.read_csv(fp, dtype={"code": str})
        d = d.merge(fx, on="code", how="left")
        kp = os.path.join(raw, "{}_k.csv")

        def px_at(r, day):
            try:
                k = pd.read_csv(kp.format(r.market + r.code))
                k = k[k.day <= day]
                return float(k.close.iloc[-1])
            except Exception:
                return None

        def annuity_irr(P, CF, N):
            lo, hi = -0.5, 0.5
            for _ in range(80):
                r_ = (lo + hi) / 2
                n = int(N)
                v_ = sum(CF / (1 + r_) ** t for t in range(1, n + 1)) + CF * (N - n) / (1 + r_) ** N
                lo, hi = (r_, hi) if v_ > P else (lo, r_)
            return r_

        def irr_now(r):
            if pd.isna(r.get("irr_mgr")) or r.kind != "经营权":
                return None, ""
            p0, p1 = px_at(r, "2025-12-31"), r.price
            if not p0:
                return None, ""
            r0 = r.irr_mgr / 100
            if pd.notna(r.get("remain_years")) and r.remain_years > 1:
                N0, N1 = r.remain_years + 0.75, r.remain_years - 0.1
                CF = p0 * r0 / (1 - (1 + r0) ** -N0) if r0 else p0 / N0
                return round(annuity_irr(p1, CF, N1) * 100, 2), "管理人IRR按现价、剩余年限重算"
            # 无年限：用久期近似，D≈6
            return round((r0 + (p0 / p1 - 1) / 6) * 100, 2), "管理人IRR按价格变动近似（缺剩余年限）"

        t = d.apply(irr_now, axis=1, result_type="expand")
        d["irr_now"], d["irr_note"] = t[0], t[1]
    # ---- 第三轮：兑现率核验（人工从评估报告抽，<dir>/realization.csv）----
    rp = os.path.join(a.dir, "realization.csv")
    if os.path.exists(rp):
        rz = pd.read_csv(rp, dtype={"code": str})
        d = d.merge(rz[["code", "assumed_2026_pct", "actual_yoy_pct", "irr_flat_pct"]], on="code", how="left")
    for c in ("assumed_2026_pct", "actual_yoy_pct", "irr_flat_pct"):
        if c not in d:
            d[c] = None
    GOV10Y = a.gov10y
    LINE = GOV10Y + 2.5

    base_value, base_quality = value, quality

    def value(r):  # noqa: F811
        if has_facts and r.kind == "经营权" and pd.notna(r.get("irr_now")):
            i = r.irr_now
            why = f"现价持有到期IRR约{i:.1f}%（红线{LINE:.1f}%）"
            if pd.notna(r.get("irr_flat_pct")):
                i = r.irr_flat_pct
                why += f"；但评估假设落空，车流持平时仅约{i:.1f}%"
            return ("便宜" if i >= LINE + 1 else "合理" if i >= LINE else "贵"), why
        return base_value(r)

    def quality(r):  # noqa: F811
        if not has_facts:
            return base_quality(r)
        s, why, n = 0, [], 0
        if r.kind == "经营权":
            for col, name in (("traffic_yoy", "车流"), ("power_yoy", "发电量"), ("water_yoy", "处理量")):
                v_ = r.get(col)
                if pd.notna(v_) and abs(v_) < 200:  # >200% 多为扩募装入新资产，不可比
                    n += 1
                    if v_ >= -3:
                        s += 1; why.append(f"{name}{v_:+.0f}%")
                    elif v_ < -10:
                        s -= 2; why.append(f"{name}{v_:.0f}%")
                    else:
                        why.append(f"{name}{v_:+.0f}%")
                    break
        else:
            oc, rc = r.get("occ_worst_chg"), r.get("rent_worst_chg_pct")
            if pd.notna(oc):
                n += 1
                if oc >= -2 and (pd.isna(r.get("occ_min")) or r.occ_min >= 80):
                    s += 1; why.append(f"出租率{r.occ_now:.0f}%（最差资产{oc:+.1f}点）")
                elif oc < -5 or r.get("occ_min", 100) < 75:
                    s -= 2; why.append(f"出租率下滑（最差资产{oc:+.1f}点，最低{r.occ_min:.0f}%）")
                else:
                    why.append(f"出租率{r.occ_now:.0f}%（最差资产{oc:+.1f}点）")
            if pd.notna(rc):
                n += 1
                if rc >= -3:
                    s += 1; why.append(f"租金{rc:+.0f}%")
                elif rc < -8 or (rc < -5 and pd.notna(oc) and oc > 0):
                    s -= 2; why.append(f"租金{rc:.0f}%" + ("（降价换出租率）" if pd.notna(oc) and oc > 0 else ""))
                else:
                    why.append(f"租金{rc:+.0f}%")
        if pd.notna(r.get("assumed_2026_pct")) and pd.notna(r.get("actual_yoy_pct")):
            gap = r.actual_yoy_pct - r.assumed_2026_pct
            n += 1
            if gap >= -1:
                s += 1; why.append(f"兑现：实际{r.actual_yoy_pct:+.1f}% vs 评估假设{r.assumed_2026_pct:+.1f}%")
            elif gap < -3:
                s -= 3; why.append(f"兑现落空：实际{r.actual_yoy_pct:+.1f}% vs 评估假设{r.assumed_2026_pct:+.1f}%")
            else:
                why.append(f"兑现略差：实际{r.actual_yoy_pct:+.1f}% vs 假设{r.assumed_2026_pct:+.1f}%")
        note = str(r.get("dist_note") or "")
        if any(k in note for k in ("保理", "一次性", "处置", "退税", "追溯")):
            why.append("分配含一次性因素：" + note[:30])
            s -= 1
        if n == 0:
            lab, w = base_quality(r)
            return lab, w + ("；" + "；".join(why) if why else "")
        lab = "好" if s >= n else ("差" if s <= -2 else "一般")
        return lab, "；".join(why)

    v = d.apply(value, axis=1, result_type="expand")
    q = d.apply(quality, axis=1, result_type="expand")
    d["估值"], d["估值依据"] = v[0], v[1]

    # ---- 重估 NAV（产权类，只作参考列，不参与打分）----
    # 账面 P/NAV 会被折旧压低（显得贵）或被收购溢价/商誉抬高（显得便宜），重估口径看价格比资产现值贵多少
    for c in ("rnav_ps", "rnav_dtl_ps", "book_nav_ps", "rnav_src"):
        if c not in d:
            d[c] = None
    prop = d.kind == "产权"
    d["p_book_ann"] = (d.price / d.book_nav_ps).where(prop).round(2)
    d["p_rnav"] = (d.price / d.rnav_ps).where(prop).round(2)
    d["p_rnav_dtl"] = (d.price / d.rnav_dtl_ps).where(prop).round(2)
    d["rnav_vs_book"] = (d.rnav_ps / d.book_nav_ps).where(prop).round(2)

    def rnav_note(r):
        if pd.isna(r.p_rnav):
            return ""
        s = f"按评估值重估后股价是净值{r.p_rnav:.2f}倍（账面口径{r.p_book_ann:.2f}）"
        if r.rnav_vs_book < 0.9:
            s += "，账面含收购溢价/商誉偏高"
        elif r.rnav_vs_book > 1.1:
            s += "，账面被折旧压低"
        return s

    d["估值依据"] = [("；".join(x for x in (str(a or ""), rnav_note(r)) if x)) for a, (_, r) in zip(d["估值依据"], d.iterrows())]
    d["质地"], d["质地依据"] = q[0], q[1]

    def bucket(r):
        if r["估值"] is None or pd.isna(r["估值"]):
            return "6 新上市·数据不足"
        if r["质地"] == "差":
            return "4 便宜但有毛病（可能是陷阱）" if r["估值"] == "便宜" else "5 一般或差、不便宜（回避）"
        if r["质地"] == "好":
            return {"便宜": "1 好又便宜（不差的低估品）", "合理": "3 一般不贵", "贵": "2 好但贵（贵的好东西）"}[r["估值"]]
        # 一般 / 数据不足
        return {"便宜": "3 一般不贵", "合理": "3 一般不贵", "贵": "5 一般或差、不便宜（回避）"}[r["估值"]]

    d["分档"] = d.apply(bucket, axis=1)
    d["流动性"] = d.turnover20_wan.apply(lambda x: "偏冷(<300万)" if x < 300 else ("一般" if x < 500 else "好"))
    d.to_csv(os.path.join(a.dir, "valuation.csv"), index=False, encoding="utf-8-sig")

    show = {"分档": "分档", "code": "代码", "name": "名称", "kind": "类型", "sector": "板块", "price": "收盘价",
            "yield_basis": "收益率%(估值用)", "peer_median_yield": "同类中位%", "yield_note": "收益率口径",
            "p_nav": "P/NAV", "p_rnav": "P/重估NAV(产权)", "p_rnav_dtl": "P/重估NAV(加回递延税)",
            "rnav_vs_book": "重估÷账面", "rnav_src": "评估值来源", "price_pctile": "价格分位%", "dist_yoy_pct": "分红同比%", "nav_chg_1y_pct": "净值一年变化%",
            "irr_now": "现价持有到期IRR%(经营权)", "irr_mgr": "管理人年底IRR%", "remain_years": "剩余年限",
            "occ_now": "出租率%", "occ_worst_chg": "出租率最差变化(点)", "rent_worst_chg_pct": "租金最差变化%",
            "traffic_yoy": "车流同比%", "power_yoy": "发电量同比%", "water_yoy": "处理量同比%", "dist_note": "分配变动原因",
            "估值": "估值", "估值依据": "估值依据", "质地": "质地", "质地依据": "质地依据",
            "turnover20_wan": "20日均成交(万)", "流动性": "流动性", "days_listed": "上市天数"}
    out = d.sort_values(["分档", "kind", "yield_basis"], ascending=[True, True, False]).reindex(columns=list(show)).rename(columns=show)
    xp = os.path.join(a.dir, f"估值分档_{today.isoformat()}.xlsx")
    with pd.ExcelWriter(xp) as w:
        out.to_excel(w, sheet_name="全部96只", index=False)
        for b, g in out.groupby("分档"):
            g.to_excel(w, sheet_name=b.split(" ")[1][:12], index=False)
        pd.DataFrame({"说明": [
            "同板块内比较；板块不足4只的并入同类型（经营权/产权）比较。",
            "估值：收益率在同类前1/3 +2、后1/3 −2；P/NAV<0.9 +1、>1.3 −1；价格分位≤20% +1、≥80% −1。≥2便宜，≤−2贵。",
            "经营权收益率 = 近一年分派率 − 一年净值下降（÷现价）；深市无净值的按同板块中位估算。",
            "产权收益率 = 近一年现金分派率（净值上调不算现金）。",
            "质地：分红同比（≥−5%好，<−15%差）+ 评估净值一年变化（产权 ≥−3%好、<−8%差；经营权 ≥−12%好、<−18%差，因本来就在摊销）。",
            "上市不满400天的不估值，归入'新上市·数据不足'。",
            "P/重估NAV（产权，参考列不打分）：重估NAV =（评估价值 + 其他资产 − 负债）÷ 份额，评估值取最近年报；其他资产要剔掉固定资产、在建工程、无形资产、投资性房地产和商誉（评估值已含资产全部价值，留着商誉等于算两遍）。年报'公允价值参考净值'没剔商誉，不能直接用。",
            "重估÷账面 <0.9：账面含收购溢价/商誉，账面 P/NAV 偏小；>1.1：账面被折旧压低，账面 P/NAV 偏大。加回递延税一列是乐观口径（认为那笔税不会交）。",
            "这是按公开数据的机械分档，用来缩小研究范围，不是买卖建议；入围的仍要按 SKILL 第二节五格逐只核实。",
        ]}).to_excel(w, sheet_name="口径说明", index=False)
    print(xp)
    for b, g in d.groupby("分档"):
        print(f"\n{b}（{len(g)}只）")
        for _, r in g.sort_values("yield_basis", ascending=False).iterrows():
            print(f"  {r.code} {str(r['name']).replace('REIT','')[:12]} | {r.sector} | 收益{r.yield_basis}% | {r['估值依据'] or '-'} | {r['质地依据']} | {r['流动性']}")


if __name__ == "__main__":
    main()

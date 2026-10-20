#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""家务事件判定器（2026-10-20，DeepSeek 定稿）：每晚 22:30 运行。
只读 hidden_files/家务记录.md，按口径逐徽章聚合，产出候选清单
hidden_files/家务判定候选_YYYY-MM-DD.md，交每日总结汇总 -> DeepSeek 批量
终审（首周逐笔）-> 通过才写台账 badge_ 条目并回写存档 [颁发:徽章名]。
本脚本永不写台账、永不写存档、永不产生积分。
未达也列（带进度/阈值）；存档无任何行时输出"无数据"而非 0。
回补行只用于展示与对照：候选证据若全部为 [回补] 行，显式标注供终审裁定。
周期清空机制（2026-10-20，规则手册《徽章进度口径·徽章周期清空》子节）：
每条候选标注 cycle_week（取自达成时间戳所在周，非判定器运行周）与达成
日期；连续/累计类已达候选附"颁发后计数归零"提示；清单头部分年级标注
本周已发累计（仅统计带 cycle_week 落账字段的新条目，历史行不计入）。
单周 +50 上限的截断/顺延在每日总结终审落账环节执行，本脚本不做截断。
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync_sections as ss  # noqa: E402

BASIC_ITEMS = ("洗碗", "扫地", "拖地", "晾衣服", "晾袜子", "整理桌面")
PSEUDO_ITEMS = ("其他（连做链）",)
GNAME = {"g5": "五年级", "g6": "六年级"}


def ref(r):
    return "%s|%s|%s|%s" % (r["date"], GNAME.get(r["gk"], r["gk"]),
                            r["item"], r["key"] or "-")


def all_backfill(rows):
    return bool(rows) and all(r["backfill"] for r in rows)


def cyc_tag(date_str, note=""):
    """候选周期标注：cycle_week 取自达成时间戳所在周；候选产出后跨零点
    不变更归属（归属在产出时按达成日期写死）。"""
    return "｜达成日期 %s｜cycle_week=%s%s" % (
        date_str, ss.cycle_week_of(date_str), note)


def streak_anchor(date_set, today):
    """连续类达成日期：与 _streak_from_dates 同锚（today 或 yesterday）。"""
    if today.isoformat() in date_set:
        return today
    return today - timedelta(days=1)


def judge(now):
    arch = ss._parse_chore_archive()
    rows = arch["rows"]
    ws, we = ss.week_bounds(now)
    ws_s, we_s = ws.strftime("%Y-%m-%d"), we.strftime("%Y-%m-%d")
    today = now.date()
    cur_cycle = ss.cycle_week_of(ws_s)
    # 台账已颁发徽章（只读，用于候选标注防重复颁发）
    awarded = {}
    raw_events = {}
    try:
        raw_events = ss.extract_badge_events()
        for gk in ("g5", "g6"):
            awarded[gk] = {}
            for e in raw_events.get(gk, []):
                awarded[gk].setdefault(e["key"], []).append(e["date"])
    except Exception:
        awarded = {"g5": {}, "g6": {}}
        raw_events = {}
    # 本周已发累计（分年级）：仅统计带 cycle_week 落账字段的新条目；
    # 无 cycle_week 的历史行不计入（DeepSeek 二次终审执行解释，留痕口径）。
    week_issued = {}
    for gk in ("g5", "g6"):
        # 按归属周计：落账日期在当周、但 cycle_week 属上一周期的跨周/迟到
        # 补发（late_cycle）不占当周上限，故按 recorded cycle_week 计数。
        n = sum(1 for e in raw_events.get(gk, [])
                if e.get("cycle_week_recorded")
                and e.get("cycle_week") == cur_cycle)
        week_issued[gk] = (n, n * 5)

    lines = []
    if not rows:
        lines.append("（家务记录存档无任何记录行——全部徽章记为「无数据」，非 0。）")
        return lines

    def award_note(gk, bk):
        ds = awarded.get(gk, {}).get(bk)
        return "；台账已颁发：%s" % ("、".join(sorted(ds)) if ds else "无")

    for gk in ("g5", "g6"):
        gn = GNAME[gk]
        grows = [r for r in rows if r["gk"] == gk]
        lines.append("## %s" % gn)
        _wn, _wp = week_issued[gk]
        lines.append("- 本周已发累计（cycle_week=%s）：%d 笔 / +%d 分"
                     "（单周上限 +50，截断/顺延在落账环节；仅含带 cycle_week"
                     " 的新条目，历史行不计入）" % (cur_cycle, _wn, _wp))
        # 家务小帮手：任一基础家务行即达成
        basic = [r for r in grows if r["item"] in BASIC_ITEMS]
        if basic:
            # 引用优先取最早的照片证据行（弱证据口头行仅在无照片行时引用）
            photo = [r for r in basic if r["evidence"] in ("照片", "照片+口头")]
            ev = sorted(photo or basic, key=lambda r: r["date"])[0]
            lines.append("- 【家务小帮手】候选（已达）依据：%s%s%s%s" % (
                ref(ev), "（回补证据，终审请裁定）" if ev["backfill"] else "",
                award_note(gk, "jiawuxiaobangshou"), cyc_tag(ev["date"])))
        else:
            lines.append("- 【家务小帮手】未达：暂无基础家务记录")
        # 三合一快手：同日独自、项目去重>=3（口头行不计；连做链伪行不计）
        by_day = {}
        for r in grows:
            if r["mode"] == "独自" and r["evidence"] != "口头" \
                    and r["item"] not in PSEUDO_ITEMS:
                by_day.setdefault(r["date"], {})[r["item"]] = r
        hit = [(d, v) for d, v in sorted(by_day.items()) if len(v) >= 3]
        if hit:
            d, v = hit[-1]
            lines.append("- 【三合一快手】候选（已达 %s）依据：%s%s%s%s" % (
                d, "；".join(ref(x) for x in v.values()),
                "（证据全部为回补行，终审请裁定）" if all_backfill(list(v.values())) else "",
                award_note(gk, "sanheyi"), cyc_tag(d)))
        else:
            best = max((len(v) for v in by_day.values()), default=0)
            lines.append("- 【三合一快手】未达：单日最多 %d 项（需 3 项不同）" % best)
        # 家务之星：自然周有记录天数 / 5
        wk_dates = sorted({r["date"] for r in grows if ws_s <= r["date"] <= we_s})
        wk_rows = [r for r in grows if r["date"] in wk_dates]
        st = "候选（已达）" if len(wk_dates) >= 5 else "未达"
        lines.append("- 【家务之星】%s：本周 %d/5 天（%s）%s%s" % (
            st, min(len(wk_dates), 5), "、".join(wk_dates) or "无",
            award_note(gk, "jiawuzhixing") if len(wk_dates) >= 5 else "",
            cyc_tag(wk_dates[4], "｜周类：下周一进度归零")
            if len(wk_dates) >= 5 else ""))
        if len(wk_dates) >= 5 and all_backfill(wk_rows):
            lines[-1] += "（证据含回补行，终审请裁定）"
        # 整理之星：整理桌面累计天数 / 5（按日去重）
        tidy_all = sorted({r["date"] for r in grows if r["item"] == "整理桌面"})
        st = "候选（已达）" if len(tidy_all) >= 5 else "未达"
        lines.append("- 【整理之星】%s：累计 %d/5 天（%s）%s" % (
            st, min(len(tidy_all), 5), "、".join(tidy_all) or "无",
            cyc_tag(tidy_all[4], "｜累计类：颁发后累计计数归零重新累计")
            if len(tidy_all) >= 5 else ""))
        # 整理小达人：整理桌面连续 / 5（不计回补行）
        tidy_live = {r["date"] for r in grows
                     if r["item"] == "整理桌面" and not r["backfill"]}
        n = ss._streak_from_dates(tidy_live, today)
        st = "候选（已达）" if n >= 5 else "未达"
        bf_days = len({r["date"] for r in grows
                       if r["item"] == "整理桌面" and r["backfill"]})
        lines.append("- 【整理小达人】%s：连续 %d/5 天%s%s" % (
            st, min(n, 5),
            "（另有回补整理行 %d 天，按拍板不计入连续、仅展示对照，不代表未做）" % bf_days
            if bf_days else "",
            cyc_tag(streak_anchor(tidy_live, today).isoformat(),
                    "｜连续类：颁发后连续计数归零重新起算")
            if n >= 5 else ""))
        # 坚持小勇士·家务通道：有记录日期连续 / 14（仅家务通道）
        n = ss._streak_from_dates(arch["dates"].get(gk, set()), today)
        st = "候选（家务通道已达）" if n >= 14 else "未达"
        lines.append("- 【坚持小勇士·家务通道】%s：连续 %d/14 天%s" % (
            st, min(n, 14),
            cyc_tag(streak_anchor(arch["dates"].get(gk, set()), today).isoformat(),
                    "｜通道类：颁发后该通道计数归零；每通道每 cycle_week ≤1 次")
            if n >= 14 else ""))
        lines.append("")
    # 合作之星：兄弟同日同项目合作成对日连续 / 3
    n = ss._streak_from_dates(arch["coop_dates"], today)
    st = "候选（已达）" if n >= 3 else "未达"
    lines.append("## 合作之星（双人）")
    lines.append("- 【合作之星】%s：成对日连续 %d/3 天（成对日：%s）%s" % (
        st, min(n, 3), "、".join(sorted(arch["coop_dates"])) or "无",
        cyc_tag(streak_anchor(arch["coop_dates"], today).isoformat(),
                "｜连续类：颁发后连续计数归零重新起算") if n >= 3 else ""))
    # 家务接力手（现行口径 a24c506：同周两兄弟各>=2项 + 至少1次接力动作）
    def items(gk):
        return [(r["date"], r["item"]) for r in rows
                if r["gk"] == gk and ws_s <= r["date"] <= we_s
                and r["item"] not in PSEUDO_ITEMS]
    ci5, ci6 = items("g5"), items("g6")
    relay = False
    for i in {x for _, x in ci5} & {x for _, x in ci6}:
        d5 = {d for d, x in ci5 if x == i}
        d6 = {d for d, x in ci6 if x == i}
        if any(x != y for x in d5 for y in d6):
            relay = True
    for d in {d for d, _ in ci5} & {d for d, _ in ci6}:
        if {x for dd, x in ci5 if dd == d} != {x for dd, x in ci6 if dd == d}:
            relay = True
    ok = len(ci5) >= 2 and len(ci6) >= 2 and relay
    lines.append("## 家务接力手（双人）")
    lines.append("- 【家务接力手】%s：本周五年级 %d 项、六年级 %d 项（各需≥2），接力动作：%s%s" % (
        "候选（已达）" if ok else "未达", len(ci5), len(ci6),
        "有" if relay else "无",
        cyc_tag(max(d for d, _ in ci5 + ci6), "｜周类：下周一进度归零")
        if ok else ""))
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="演练模式：文件头标注演练，候选不得写台账")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    now = datetime.now(ss.BEIJING)
    date_s = now.strftime("%Y-%m-%d")
    out = a.out or str(ss.REPO.parent / ("家务判定候选_%s.md" % date_s))
    body = judge(now)
    head = ["# 家务判定候选清单 %s（判定器 22:30 自动产出）" % date_s, ""]
    if a.dry_run:
        head.append("> **演练（dry-run）：本清单仅供核对，候选不得写台账。**")
        head.append("")
    head.append("流程：每日总结汇总本清单 -> DeepSeek 批量终审（首周逐笔）"
                "-> 通过后写台账 badge_ 条目（key 注释带 cycle_week=YYYY-Www，"
                "由落账环节写入）并回写存档 [颁发:徽章名]；"
                "不通过保留 [驳回:原因]。颁发后基准按规则手册《徽章周期清空》"
                "子节分类口径（周类跨周重置、连续/累计类达成后计数归零重新累计、"
                "坚持小勇士废止一人一次）。单周总上限 +50 的截断/顺延在落账"
                "环节执行，判定器不做截断；候选归属周以达成时间戳所在周为准，"
                "产出后跨零点不变更。")
    head.append("回补证据口径（DeepSeek 终审 2026-10-20）：[回补] 行可作候选证据，"
                "但首周（10-20~10-26）含回补证据的候选一律逐笔终审、不得批量自动放行；"
                "仅依赖 [回补] 行且证据为口头/时刻未知的候选不予颁发（弱证据单独不成立，"
                "与同批强证据并存时以强证据为准）；连做链行不计入任何项目类计数。")
    head.append("")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(head + body) + "\n")
    print("WROTE", out)
    print("\n".join(body))


if __name__ == "__main__":
    main()

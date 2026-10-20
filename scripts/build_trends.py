#!/usr/bin/env python3
"""学习走势数据生成（DeepSeek 方案 2026-10-20；2026-10-20 英语0%修复后口径）：
读题目库.db（只读），按日聚合正确率，生成 kanban-deploy/data/trends.json。
结构：{updated_at, dates[30], grades:{年级:{overall[], subjects:{学科:{days[], weak[]}}}},
       isolation_reasons, manual_review_queue, paper_batch_alerts}
weak: 每科错误次数前3知识点，带补练表单预填链接。

正确率口径（DeepSeek 2026-10-20 两次裁定：调查终报 B+C+D 批准 + 紧急裁决 Q1乙/Q2乙/Q3）：
- 已判行：是否正确 IS NOT NULL 且 状态 != '待确认'（待确认题不计分母，规则手册硬规则）。
  分母全局单算此一定义：学科值与 overall 共用（overall = 未隔离学科已判行汇总）。
- 回填行（来源='错题库回填'）计入分母（Q1 乙：不行级剔除，回填是错题的唯一记录）。
- 隔离（该组输出 null）判定顺序：
  1) 裁定组级隔离清单 ADJUDICATED_ISOLATED（7 组，两次裁定确认）；
  2) 全错组（已判>=1 且 correct=0）→ null（Q3 规则一，优先于占比规则）；
  3) 待确认占比 >= 25% → null（Q2 乙的动态化）。占比公式（固化，审计可复现）：
     待确认占比 = 该组全部 状态='待确认' 行数 / (已判行数 + 待确认行数)。
     裁决两案代入：10-19 五语 3/7=42.9%、10-17 六英 14/24=58.3%，均触发；
     若按裁决原文仅计"带判定被回写NULL的待确认行"口径，两案为 3/7、4/14=28.6%，亦均触发。
     本实现取全待确认行口径：未决题占全部记录之比更能反映样本代表性，且更保守。
     （此执行口径经 DeepSeek 2026-10-20 终审追认，见《英语0pct修复_终审验收包_2026-10-20.md》裁决2。）
  4) 回填占比 >= 50% → null（Q3 规则二）。占比 = 已判行中回填行数 / 已判行数；
     已判<5 行档同判 null 并标 small_sample，进人工复核队列。
- 隔离原因仅写入数据层 isolation_reasons（供家长版/审计追溯），不得进入孩子版输出。
- 本脚本不读 M1 溯源打标列（裁决 C1-Q1a：该标记仅作溯源元数据，聚合零消费，过滤条件不含该列）。
"""
import json
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DB_PATH = REPO.parent / "题目库.db"
OUT = REPO / "data" / "trends.json"
FORM = "https://docs.google.com/forms/d/e/1FAIpQLSfe_5U2ObcmxXle8XG0xtvLVkuJ2JIx9f6qG4w5-vKNug1ePw/viewform"
DAYS = 30
GRADES = ["五年级", "六年级"]
SUBJECTS = ["语文", "数学", "英语"]

# 裁定组级隔离清单（DeepSeek 调查终报 D 方案 + 紧急裁决 Q1 乙确认，共 7 组 → null）
ADJUDICATED_ISOLATED = {
    ("2026-10-19", "五年级", "英语"): "M2",        # 本案：批改卷只录错题 11 行
    ("2026-10-06", "五年级", "语文"): "M4",        # 只错行混排失真
    ("2026-10-16", "五年级", "英语"): "M4",        # 只错行混排失真
    ("2026-10-06", "六年级", "语文"): "M3",        # 整组隔离
    ("2026-10-13", "五年级", "语文"): "M3",        # 整组隔离
    ("2026-10-16", "六年级", "数学"): "M3",        # 整组隔离
    ("2026-10-06", "六年级", "英语"): "M1-pure",   # 纯回填组（4 行全回填全错）
}
PENDING_RATIO_THRESHOLD = 0.25   # Q2 乙动态化执行参数（裁决两案 42.9% / 28.6% 均触发）
BACKFILL_RATIO_THRESHOLD = 0.50  # Q3 规则二阈值（裁决原文）
SMALL_SAMPLE_JUDGED = 5          # Q3：已判<5 为小样本档


def q_count(err):
    if err >= 10:
        return 5
    if err >= 5:
        return 4
    return 3


def form_link(grade, subject, kp, n, mode):
    q = urllib.parse.urlencode({
        "usp": "pp_url",
        "entry.1016062372": grade,
        "entry.1067840043": subject,
        "entry.472993468": kp,
        "entry.1078806938": str(n),
        "entry.2028236352": mode,
    })
    return f"{FORM}?{q}"


def group_stats(db, date, grade, subject):
    """单组统计 + 隔离判定。返回 (value_or_None, info dict)。
    info 含 judged/correct/pending/两占比/small_sample/rules/reason。"""
    rows = db.execute(
        "SELECT 来源, 是否正确, 状态, 来源邮件ID FROM question_bank "
        "WHERE 日期=? AND 年级=? AND 学科=?", (date, grade, subject)).fetchall()
    judged_rows = [(src, ok) for src, ok, st, _ in rows
                   if ok is not None and st != "待确认"]
    pending = sum(1 for _, _, st, _ in rows if st == "待确认")
    judged = len(judged_rows)
    correct = sum(ok for _, ok in judged_rows)
    bf = sum(1 for src, _ in judged_rows if src == "错题库回填")
    bf_share = (bf / judged) if judged else 0.0
    pend_share = (pending / (judged + pending)) if (judged + pending) else 0.0
    info = {"judged": judged, "correct": correct, "pending": pending,
            "pending_share": round(pend_share, 3),
            "backfill_share": round(bf_share, 3),
            "small_sample": False, "rules": [], "reason": None}
    key = (date, grade, subject)
    if key in ADJUDICATED_ISOLATED:
        info["rules"].append("adjudicated_" + ADJUDICATED_ISOLATED[key])
        info["reason"] = "adjudicated_group_isolation"
        return None, info
    if judged == 0:
        return None, info
    if correct == 0:
        info["rules"].append("all_wrong")
        info["reason"] = "all_wrong"
        return None, info
    if pend_share >= PENDING_RATIO_THRESHOLD:
        info["rules"].append(f"pending_ratio={pend_share:.2f}")
        info["reason"] = info["rules"][-1]
        return None, info
    if bf_share >= BACKFILL_RATIO_THRESHOLD:
        info["rules"].append(f"backfill_ratio={bf_share:.2f}")
        if judged < SMALL_SAMPLE_JUDGED:
            info["small_sample"] = True
            info["rules"].append("small_sample")
        info["reason"] = info["rules"][0]
        return None, info
    return round(correct / judged * 100, 1), info


def paper_batch_alerts(db, dates):
    """paper_batches 拦截（数据层）：单个来源邮件批次已判>=3 且全错
    → 疑似'只录错题'批次，输出告警供 watcher/家长侧复核。"""
    alerts = []
    for d in dates:
        for mail_id, grade, subject, cnt in db.execute(
                "SELECT 来源邮件ID, 年级, 学科, COUNT(*) FROM question_bank "
                "WHERE 日期=? AND 来源邮件ID != '' AND 是否正确 = 0 "
                "AND 状态 != '待确认' GROUP BY 来源邮件ID, 年级, 学科",
                (d,)):
            judged_total = db.execute(
                "SELECT COUNT(是否正确) FROM question_bank WHERE 日期=? "
                "AND 年级=? AND 学科=? AND 来源邮件ID=? AND 状态 != '待确认'",
                (d, grade, subject, mail_id)).fetchone()[0]
            if judged_total >= 3 and judged_total == cnt:
                alerts.append({"date": d, "grade": grade, "subject": subject,
                               "mail_id": mail_id, "judged": judged_total,
                               "alert": "batch_all_wrong_suspected_partial_ingest"})
    return alerts


def main():
    today = datetime.now(BEIJING).date()
    dates = [(today - timedelta(days=DAYS - 1 - i)).isoformat() for i in range(DAYS)]
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    data = {"updated_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
            "dates": dates, "grades": {},
            "isolation_reasons": {}, "manual_review_queue": [],
            "paper_batch_alerts": paper_batch_alerts(db, dates)}
    for g in GRADES:
        gdata = {"overall": [], "empty": True, "sparse": True, "subjects": {}}
        subj_stats = {}
        for s in SUBJECTS:
            series = []
            for d in dates:
                val, info = group_stats(db, d, g, s)
                series.append(val)
                if info["reason"] is not None:
                    data["isolation_reasons"].setdefault(g, {}).setdefault(s, {})[d] = {
                        "reason": info["reason"], "rules": info["rules"],
                        "judged": info["judged"], "correct": info["correct"],
                        "pending": info["pending"],
                        "pending_share": info["pending_share"],
                        "backfill_share": info["backfill_share"],
                        "small_sample": info["small_sample"]}
                    if info["judged"] < SMALL_SAMPLE_JUDGED:
                        data["manual_review_queue"].append({
                            "date": d, "grade": g, "subject": s,
                            "reason": info["reason"], "rules": info["rules"],
                            "judged": info["judged"],
                            "small_sample_flag": True})
                subj_stats[(d, s)] = (val, info)
            weak = []
            for kp, err, last in db.execute(
                    "SELECT 知识点名, 错误次数, 最近错误日期 FROM knowledge_points "
                    "WHERE 年级=? AND 学科=? AND 错误次数>0 "
                    "ORDER BY 错误次数 DESC LIMIT 3", (g, s)):
                n = q_count(err)
                weak.append({"kp": kp, "err": err, "last": last, "n": n,
                             "link_tonight": form_link(g, s, kp, n, "今晚随练习发"),
                             "link_now": form_link(g, s, kp, n, "现在单独发")})
            npts_s = sum(1 for v in series if v is not None)
            gdata["subjects"][s] = {"days": series, "weak": weak,
                                    "empty": npts_s == 0, "sparse": npts_s < 2}
        overall = []
        for d in dates:
            tot_j = tot_c = 0
            for s in SUBJECTS:
                val, info = subj_stats[(d, s)]
                if val is not None:
                    tot_j += info["judged"]
                    tot_c += info["correct"]
            overall.append(round(tot_c / tot_j * 100, 1) if tot_j else None)
        npts = sum(1 for v in overall if v is not None)
        gdata["overall"] = overall
        gdata["empty"] = npts == 0
        gdata["sparse"] = npts < 2
        data["grades"][g] = gdata
    db.close()
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"trends.json 已生成：{OUT} ({OUT.stat().st_size} bytes)；"
          f"隔离组 {sum(len(ss) for gg in data['isolation_reasons'].values() for ss in gg.values())}；"
          f"复核队列 {len(data['manual_review_queue'])}；批次告警 {len(data['paper_batch_alerts'])}")


if __name__ == "__main__":
    main()

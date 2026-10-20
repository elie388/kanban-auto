#!/usr/bin/env python3
"""学习走势数据生成（DeepSeek 方案 2026-10-20）：读题目库.db（只读），
按日聚合正确率（未作答不计分母），生成 kanban-deploy/data/trends.json。
结构：{updated_at, dates[30], grades:{年级:{overall[], subjects:{学科:{days[], weak[]}}}}}
weak: 每科错误次数前3知识点，带补练表单预填链接。"""
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


def daily_acc(db, dates, grade, subject=None):
    out = []
    for d in dates:
        if subject:
            row = db.execute(
                "SELECT SUM(是否正确), COUNT(是否正确) FROM question_bank "
                "WHERE 日期=? AND 年级=? AND 学科=? AND 是否正确 IS NOT NULL",
                (d, grade, subject)).fetchone()
        else:
            row = db.execute(
                "SELECT SUM(是否正确), COUNT(是否正确) FROM question_bank "
                "WHERE 日期=? AND 年级=? AND 是否正确 IS NOT NULL",
                (d, grade)).fetchone()
        s, c = row
        out.append(round(s / c * 100, 1) if c else None)
    return out


def main():
    today = datetime.now(BEIJING).date()
    dates = [(today - timedelta(days=DAYS - 1 - i)).isoformat() for i in range(DAYS)]
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    data = {"updated_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
            "dates": dates, "grades": {}}
    for g in GRADES:
        overall = daily_acc(db, dates, g)
        npts = sum(1 for v in overall if v is not None)
        gdata = {"overall": overall, "empty": npts == 0, "sparse": npts < 2,
                 "subjects": {}}
        for s in SUBJECTS:
            weak = []
            for kp, err, last in db.execute(
                    "SELECT 知识点名, 错误次数, 最近错误日期 FROM knowledge_points "
                    "WHERE 年级=? AND 学科=? AND 错误次数>0 "
                    "ORDER BY 错误次数 DESC LIMIT 3", (g, s)):
                n = q_count(err)
                weak.append({"kp": kp, "err": err, "last": last, "n": n,
                             "link_tonight": form_link(g, s, kp, n, "今晚随练习发"),
                             "link_now": form_link(g, s, kp, n, "现在单独发")})
            days_s = daily_acc(db, dates, g, s)
            npts_s = sum(1 for v in days_s if v is not None)
            gdata["subjects"][s] = {"days": days_s, "weak": weak,
                                    "empty": npts_s == 0, "sparse": npts_s < 2}
        data["grades"][g] = gdata
    db.close()
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"trends.json 已生成：{OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

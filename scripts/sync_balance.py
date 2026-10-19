#!/usr/bin/env python3
"""台账(积分记录.md) → data/balance.json 同步器。

KB-BALANCE-AUTO-2026-10-19，DeepSeek 条文级方案落地。
只读台账，唯一写出 data/balance.json；不碰积分台账、point_ledger、broker。

退出码：0=无变更或推送成功 / 2=解析失败 / 3=推送失败 / 4=锁冲突
"""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = Path(os.environ.get("LEDGER_PATH", str(REPO.parent / "积分记录.md")))
DATA_FILE = REPO / "data" / "balance.json"
LOG_FILE = REPO / "logs" / "sync_balance.log"
FAIL_LOG = REPO / "logs" / "sync_balance.fail"
LOCK_FILE = Path("/tmp/sync_balance.lock")

BEIJING = timezone(timedelta(hours=8))
ROW_PAT = re.compile(
    r"^\|\s*(\d{1,2}-\d{1,2}|\d{4}-\d{2}-\d{2})\s*\|\s*(.*?)\s*\|\s*([+-]?\d+)\s*\|\s*([+-]?\d+)\s*\|"
)
HTML_COMMENT = re.compile(r"<!--.*?-->")
PAREN = re.compile(r"[（(][^（）()【】]*[）)]")
NAME_GUARD = re.compile(r"[一-鿿]{2,3}(同学|小朋友)")
BALANCE_MIN, BALANCE_MAX = -999, 9999
RECENT_N = 20
LOG_KEEP_LINES = 2000


def log(status, reason="", extra=None):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "status": status,
        "reason": reason,
    }
    if extra:
        rec.update(extra)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > LOG_KEEP_LINES:
            LOG_FILE.write_text("\n".join(lines[-LOG_KEEP_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def clean_desc(raw):
    d = HTML_COMMENT.sub("", raw)
    d = PAREN.sub("", d)
    d = re.sub(r"\s+", " ", d).strip()
    d = NAME_GUARD.sub("[已脱敏]", d)
    if len(d) > 60:
        d = d[:60] + "…"
    if not d:
        d = re.sub(r"\s+", " ", raw).strip()[:60] or "（无描述）"
    return d


def norm_date(s):
    s = s.strip()
    if re.fullmatch(r"\d{1,2}-\d{1,2}", s):
        year = datetime.now(BEIJING).year
        mm, dd = s.split("-")
        return "%d-%02d-%02d" % (year, int(mm), int(dd))
    return s


def parse_ledger(path):
    content = path.read_text(encoding="utf-8")
    grades = {}
    for key, grade in (("g5", "五年级"), ("g6", "六年级")):
        m = re.search(r"## " + grade + r"\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
        if not m:
            raise ValueError("section 缺失：" + grade)
        rows = []
        for line in m.group(1).split("\n"):
            mm = ROW_PAT.match(line)
            if mm:
                rows.append({
                    "date": norm_date(mm.group(1)),
                    "desc": mm.group(2),
                    "delta": int(mm.group(3)),
                    "balance_after": int(mm.group(4)),
                })
        if not rows:
            raise ValueError("无有效记账行：" + grade)
        if rows[0]["delta"] != rows[0]["balance_after"]:
            raise ValueError("期初行不一致：" + grade)
        for i in range(1, len(rows)):
            if rows[i]["balance_after"] != rows[i - 1]["balance_after"] + rows[i]["delta"]:
                raise ValueError(
                    "链式复算失败：%s 第%d行 %d+%d!=%d" % (
                        grade, i + 1, rows[i - 1]["balance_after"],
                        rows[i]["delta"], rows[i]["balance_after"]))
        balance = rows[-1]["balance_after"]
        if not (BALANCE_MIN <= balance <= BALANCE_MAX):
            raise ValueError("余额越界：%s %d" % (grade, balance))
        recent = [{
            "date": r["date"],
            "desc": clean_desc(r["desc"]),
            "delta": r["delta"],
            "balance_after": r["balance_after"],
        } for r in rows[-RECENT_N:]]
        recent.reverse()
        grades[key] = {"balance": balance, "recent_logs": recent}
    return grades


def run_git(*args):
    # 注意：绝不打印 remote URL（含 PAT）
    return subprocess.run(
        ["git", "-C", str(REPO)] + list(args),
        capture_output=True, text=True, timeout=60)


def main():
    try:
        lock = open(LOCK_FILE, "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        log("lock_conflict", "flock 未获取到锁")
        return 4

    if not LEDGER.exists():
        log("parse_error", "台账不存在：" + str(LEDGER))
        return 2
    raw = LEDGER.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    mtime = datetime.fromtimestamp(LEDGER.stat().st_mtime, tz=BEIJING).isoformat(timespec="seconds")

    try:
        grades = parse_ledger(LEDGER)
    except Exception as e:
        with open(FAIL_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now(BEIJING).isoformat(timespec="seconds"), str(e)[:200]))
        try:
            tail = LOG_FILE.read_text(encoding="utf-8").splitlines()[-2:]
            prev = [json.loads(line).get("status") for line in tail]
        except Exception:
            prev = []
        alert = prev.count("parse_error") >= 2
        log("parse_error", str(e)[:200], {"sha256": sha[:12], "alert": alert})
        if alert:
            with open(FAIL_LOG, "a", encoding="utf-8") as f:
                f.write("ALERT: 连续 3 次解析失败，需人工介入\n")
        return 2

    new_doc = {"schema_version": 1, "grades": grades}
    allowed_grade = {"balance", "recent_logs"}
    allowed_log = {"date", "desc", "delta", "balance_after"}
    for gk, gv in grades.items():
        if set(gv.keys()) != allowed_grade:
            log("parse_error", "字段白名单拦截：" + gk)
            return 2
        for entry in gv["recent_logs"]:
            if set(entry.keys()) != allowed_log or "<!--" in entry["desc"]:
                log("parse_error", "日志条目校验失败：" + gk)
                return 2

    def strip_volatile(d):
        d = dict(d)
        d.pop("generated_at", None)
        d.pop("source_mtime", None)
        d.pop("source_sha256", None)
        return d

    old_doc = None
    if DATA_FILE.exists():
        try:
            old_doc = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            old_doc = None

    changed = (old_doc is None) or (strip_volatile(old_doc) != strip_volatile(new_doc))
    git_dirty = bool(run_git("status", "--porcelain", "data/balance.json").stdout.strip())

    if not changed and not git_dirty:
        log("no_change", "内容无变化且工作区干净", {"sha256": sha[:12]})
        return 0

    if changed:
        new_doc["generated_at"] = datetime.now(BEIJING).isoformat(timespec="seconds")
        new_doc["source_mtime"] = mtime
        new_doc["source_sha256"] = sha
        tmp = DATA_FILE.with_suffix(".json.tmp")
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(new_doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, DATA_FILE)

    head_before = run_git("rev-parse", "HEAD").stdout.strip()
    r = run_git("add", "data/balance.json")
    if r.returncode != 0:
        log("push_failed", "git add 失败 rc=%d" % r.returncode)
        return 3
    run_git("commit", "-m",
            "chore(balance): sync from ledger @ " + datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"))
    head_after = run_git("rev-parse", "HEAD").stdout.strip()

    push_ok = False
    last_rc = None
    for _ in range(3):
        r = run_git("push")
        last_rc = r.returncode
        if r.returncode == 0:
            push_ok = True
            break
        time.sleep(30)
    if push_ok:
        log("pushed", "已推送", {"sha256": sha[:12], "head": head_after[:12]})
        return 0
    log("push_failed", "push 3 次失败 rc=%s，本地文件保留待下次重试" % last_rc,
        {"sha256": sha[:12], "head_before": head_before[:12], "head_after": head_after[:12]})
    return 3


if __name__ == "__main__":
    sys.exit(main())

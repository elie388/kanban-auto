#!/usr/bin/env python3
"""stash orphan 扫描（2026-10-20 DeepSeek 双根因定案 T+1 块2）。

数据源：logs/build_stats_stash.log（build_stats 的 stash 台账，T+0 已上线）。
规则：push 后超过阈值（默认 10 分钟）仍无终态（popped / CONFLICT /
stash_missing）的 ref，若仍滞留在 git stash list → orphan 告警；若已不在
stash list → 视为人工 pop 已处置（resolved_external，首次留 info 行）。

触发：daemon_guard 每 5 分钟 import 调用 scan()（定案问题③）；也可独立运行
做手工复核：python3 scripts/stash_orphan_scan.py [--selftest]
告警面：logs/stash_orphan_alert.log（JSON 行）；去重状态
logs/stash_orphan_state.json（同一 ref 持续滞留每 60 分钟复告警一次）。
红线：只读台账与 stash list；除告警/状态文件外不写任何东西；异常不外抛。
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = os.path.dirname(SCRIPT_DIR)
TZ = timezone(timedelta(hours=8), "CST")
TERMINAL_EVENTS = ("popped", "CONFLICT", "stash_missing")
DEFAULT_THRESHOLD_MIN = 10
REALERT_MIN = 60


def _now():
    return datetime.now(TZ)


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _git_stash_refs(repo_dir):
    r = subprocess.run(["git", "-C", repo_dir, "stash", "list", "--format=%H"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path, state):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError:
        pass


def _append_alert(path, rec):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def scan(repo_dir=None, now=None, threshold_min=DEFAULT_THRESHOLD_MIN):
    """扫描一轮，返回本轮新发出的告警记录列表（orphan / resolved_external）。

    任何异常都吞掉并返回已收集部分——调用方（daemon_guard）不得被扫描拖垮。
    """
    repo_dir = repo_dir or os.environ.get("KANBAN_REPO_DIR", DEFAULT_REPO)
    now = now or _now()
    logs_dir = os.path.join(repo_dir, "logs")
    ledger_path = os.path.join(logs_dir, "build_stats_stash.log")
    alert_path = os.path.join(logs_dir, "stash_orphan_alert.log")
    state_path = os.path.join(logs_dir, "stash_orphan_state.json")
    alerts = []
    try:
        pushes = {}   # ref -> push ts（同一 ref 只记首次 push）
        terminal = set()
        try:
            with open(ledger_path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        for ln in lines:
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            ref = rec.get("ref")
            ev = rec.get("event")
            if not ref:
                continue
            if ev == "push":
                ts = _parse_ts(rec.get("ts"))
                if ts is not None and ref not in pushes:
                    pushes[ref] = ts
            elif ev in TERMINAL_EVENTS:
                terminal.add(ref)
        if not pushes:
            return []
        live_refs = _git_stash_refs(repo_dir)
        if live_refs is None:
            return []
        state = _load_state(state_path)
        dirty_state = False
        for ref, push_ts in pushes.items():
            if ref in terminal:
                continue
            age_min = (now - push_ts).total_seconds() / 60.0
            if age_min < threshold_min:
                continue
            entry = state.get(ref, {})
            if ref in live_refs:
                last = _parse_ts(entry.get("last_alert_ts"))
                if last is not None and (now - last).total_seconds() < REALERT_MIN * 60:
                    continue
                rec = {"ts": now.isoformat(timespec="seconds"),
                       "event": "stash_orphan", "ref": ref,
                       "push_ts": push_ts.isoformat(timespec="seconds"),
                       "age_min": round(age_min, 1),
                       "source": "stash_orphan_scan"}
                _append_alert(alert_path, rec)
                alerts.append(rec)
                entry = {"first_alert_ts": entry.get("first_alert_ts")
                         or now.isoformat(timespec="seconds"),
                         "last_alert_ts": now.isoformat(timespec="seconds")}
                state[ref] = entry
                dirty_state = True
            else:
                if entry.get("resolved_external"):
                    continue
                rec = {"ts": now.isoformat(timespec="seconds"),
                       "event": "resolved_external", "ref": ref,
                       "push_ts": push_ts.isoformat(timespec="seconds"),
                       "level": "info",
                       "detail": "ref 已不在 stash list 且无 popped 记录，"
                                 "视为人工 pop 已处置",
                       "source": "stash_orphan_scan"}
                _append_alert(alert_path, rec)
                alerts.append(rec)
                entry["resolved_external"] = True
                state[ref] = entry
                dirty_state = True
        if dirty_state:
            _save_state(state_path, state)
    except Exception:
        pass
    return alerts


def selftest():
    """五类样本各一例（定案验收 6）：临时仓库 + 合成台账实测解析配对。"""
    import tempfile
    results = []

    def rec(name, ok, detail):
        results.append(ok)
        print("[%s] %s :: %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)

    td = tempfile.mkdtemp()
    repo = os.path.join(td, "repo")
    os.makedirs(os.path.join(repo, "logs"))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    with open(os.path.join(repo, "f.txt"), "w") as f:
        f.write("base\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=repo, check=True)
    # 造一个真实滞留 stash（orphan 本体）
    with open(os.path.join(repo, "f.txt"), "a") as f:
        f.write("orphan edit\n")
    subprocess.run(["git", "stash", "push", "-qm", "build_stats-auto"], cwd=repo, check=True)
    orphan_ref = subprocess.run(["git", "-C", repo, "rev-parse", "stash@{0}"],
                                capture_output=True, text=True).stdout.strip()
    now = _now()
    old = (now - timedelta(minutes=30)).isoformat(timespec="seconds")

    def line(ev, ref, ts=old):
        return json.dumps({"ts": ts, "event": ev, "ref": ref,
                           "pid": 1, "boot_id": "x"}, ensure_ascii=False)
    fake = lambda ch: ch * 40
    ledger = "\n".join([
        line("push", fake("a")), line("popped", fake("a")),          # 已 pop
        line("push", fake("b")), line("CONFLICT", fake("b")),        # 冲突终态
        line("push", fake("c")), line("stash_missing", fake("c")),   # 缺失终态
        line("push", orphan_ref),                                    # 真 orphan
        line("push", fake("d")),                                     # 人工 pop
        line("push", fake("e"), now.isoformat(timespec="seconds")),  # 未超时
    ]) + "\n"
    with open(os.path.join(repo, "logs", "build_stats_stash.log"), "w", encoding="utf-8") as f:
        f.write(ledger)
    alerts = scan(repo, now=now)
    by_event = {}
    for a in alerts:
        by_event.setdefault(a["event"], []).append(a)
    try:
        assert len(by_event.get("stash_orphan", [])) == 1 \
            and by_event["stash_orphan"][0]["ref"] == orphan_ref, by_event
        rec("S1 orphan检出", True, "仅真滞留 ref 告警，push/popped/CONFLICT/stash_missing/未超时均不误报")
    except Exception as e:
        rec("S1 orphan检出", False, str(e)[:200])
    try:
        assert len(by_event.get("resolved_external", [])) == 1 \
            and by_event["resolved_external"][0]["ref"] == fake("d"), by_event
        rec("S2 resolved_external", True, "人工 pop 的 ref 首次留 info 行")
    except Exception as e:
        rec("S2 resolved_external", False, str(e)[:200])
    try:
        again = scan(repo, now=now + timedelta(minutes=5))
        assert again == [], "5 分钟内应被去重：%r" % (again,)
        later = scan(repo, now=now + timedelta(minutes=61))
        orphans = [a for a in later if a["event"] == "stash_orphan"]
        assert len(orphans) == 1 and orphans[0]["ref"] == orphan_ref, later
        rec("S3 去重与复告警", True, "同 ref 60 分钟内不重复告警，61 分钟后复告警")
    except Exception as e:
        rec("S3 去重与复告警", False, str(e)[:200])
    n_fail = sum(1 for x in results if not x)
    print("自测完成：%d 通过 / %d 失败" % (len(results) - n_fail, n_fail), flush=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    found = scan()
    for a in found:
        print(json.dumps(a, ensure_ascii=False), flush=True)
    sys.exit(0)

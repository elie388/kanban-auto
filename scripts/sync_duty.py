#!/usr/bin/env python3
"""看板同步脚本值班包装器（Token降耗事项一，DeepSeek 2026-10-20 定案）。

依次运行 build_stats.py 与 sync_sections.py，逐字复刻两个原 cron 的报告规则，
输出首行 DECISION silent|report、次行 JSON {"events":[...]}，供 hook 脚本翻译为
silent/wake。状态（连续失败计数/已报标记/已报 event_ts）存 logs/sync_duty_state.json。

报告规则复刻来源：
- kanban-stats-sync：exit0 静默 / exit2 立即报 / exit3 连续2次报一次；
  同步对账：sync_state.last_event_ts 存在且 last_build_ts<last_event_ts 且滞后>600s 报一次，
  以 last_event_ts 去重。
- kanban-balance-sync：exit0/4 静默 / exit2 连续3次报一次 / exit3 连续3次报一次。
混合退出码语义（原 cron 未明示，本包装器口径，C1 回放覆盖）：某码连续计数被异码打断
（exit0 重置全部；exit4 为锁冲突跳过，计数中性不增不减）。

用法：
  python3 sync_duty.py            # 生产：真跑两脚本并判定
  python3 sync_duty.py --replay F  # 回放：F 为 JSON 场景文件，纯判定不跑脚本不写状态
  python3 sync_duty.py --selftest  # 内置场景自测（C1 证据），全过 exit 0
"""
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # kanban-deploy/
LOGS = os.path.join(BASE, "logs")
STATE_PATH = os.path.join(LOGS, "sync_duty_state.json")
SYNC_STATE_PATH = os.path.join(LOGS, "sync_state.json")

MSG_STATS_2 = "看板数据生成熔断（台账解析失败或模板异常变更），线上数据停留在上次成功值"
MSG_STATS_3 = "看板数据推送连续失败（本地文件已保留，下次自动重试），需检查网络/PAT"
MSG_STATS_STALE = "看板同步对账异常：watchd 有未构建事件超10分钟"
MSG_SEC_2 = "看板栏目数据解析失败，线上数据将停留在上次成功值，需人工检查规则手册或数据源格式"
MSG_SEC_3 = "看板栏目数据 JSON 推送失败，本地文件已保留，需检查网络/PAT"


def decide_stats(st, code, sync, now):
    ev = []
    if code == 2:
        ev.append({"script": "build_stats", "kind": "exit2", "msg": MSG_STATS_2})
        st["stats3"] = 0
        st["stats3_reported"] = False
    elif code == 3:
        st["stats3"] = st.get("stats3", 0) + 1
        if st["stats3"] >= 2 and not st.get("stats3_reported"):
            ev.append({"script": "build_stats", "kind": "exit3x2", "msg": MSG_STATS_3})
            st["stats3_reported"] = True
    elif code == 0:
        st["stats3"] = 0
        st["stats3_reported"] = False
    elif code is not None:
        ev.append({"script": "build_stats", "kind": "exit_unknown_%s" % code,
                   "msg": "看板数据生成脚本未知退出码 %s，需人工检查" % code})
    if sync and sync.get("last_event_ts"):
        et = sync["last_event_ts"]
        bt = sync.get("last_build_ts") or 0
        if bt < et and (now - et) > 600 and st.get("reported_event_ts") != et:
            ev.append({"script": "build_stats", "kind": "sync_stale", "msg": MSG_STATS_STALE})
            st["reported_event_ts"] = et
    return ev


def _streak(st, key, repkey, code, target, threshold, script, kind, msg):
    ev = []
    if code == target:
        st[key] = st.get(key, 0) + 1
        if st[key] >= threshold and not st.get(repkey):
            ev.append({"script": script, "kind": kind, "msg": msg})
            st[repkey] = True
    elif code == 4:
        pass  # 锁冲突：中性
    elif code == 0:
        st[key] = 0
        st[repkey] = False
    elif code is not None:
        st[key] = 0  # 异码打断连续
        st[repkey] = False
    return ev


def decide_sections(st, code):
    ev = []
    ev += _streak(st, "sec2", "sec2_reported", code, 2, 3,
                  "sync_sections", "exit2x3", MSG_SEC_2)
    ev += _streak(st, "sec3", "sec3_reported", code, 3, 3,
                  "sync_sections", "exit3x3", MSG_SEC_3)
    if code is not None and code not in (0, 2, 3, 4):
        ev.append({"script": "sync_sections", "kind": "exit_unknown_%s" % code,
                   "msg": "看板栏目同步脚本未知退出码 %s，需人工检查" % code})
    return ev


def decide(st, stats_code, sec_code, sync, now):
    return (decide_stats(st, stats_code, sync, now)
            + decide_sections(st, sec_code))


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def run_script(name):
    try:
        r = subprocess.run([sys.executable, os.path.join(BASE, "scripts", name)],
                           cwd=BASE, capture_output=True, text=True, timeout=560)
        return r.returncode
    except subprocess.TimeoutExpired:
        return -9


def read_sync_state():
    try:
        with open(SYNC_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def grading_stay_event(st):
    """H6 批改停留检查段（2026-10-23）：只读调用 hidden_files/scripts/
    grading_stay_check.py --json，有 grading 卡停留超 20 分钟才产事件（2026-10-23 由 60 分钟下调：当晚 4 张卡 grading 长挂 16–96 分钟、60 分钟阈值下仅 1 张达线且事件无人回收，见 hidden_files/程序异常记录.md）；
    以 task_id@updated_at 签名去重（同一批卡不重复报，成员/时刻变化
    才再报）。子脚本缺失/失败/超时一律静默跳过，不影响主决策。"""
    script = os.path.join(BASE, "..", "scripts", "grading_stay_check.py")
    try:
        r = subprocess.run([sys.executable, script, "--json", "--threshold-min", "20"],
                           capture_output=True, text=True, timeout=60)
        rows = json.loads(r.stdout or "[]")
    except Exception:
        return []
    if not rows:
        st.pop("grading_stay_sig", None)
        return []
    sig = "|".join(sorted("%s@%s" % (x.get("task_id"), x.get("updated_at"))
                          for x in rows))
    if st.get("grading_stay_sig") == sig:
        return []
    st["grading_stay_sig"] = sig
    desc = "；".join("%s %s「%s」（停留%s分钟）"
                     % (x.get("grade"), x.get("task_id"),
                        (x.get("title") or "")[:16], x.get("stay_minutes"))
                     for x in rows[:5])
    return [{"script": "grading_stay_check", "kind": "grading_stay",
             "msg": "批改停留超20分钟 %d 张：%s" % (len(rows), desc)}]


INGEST_AUDIT_PATH = os.path.join(BASE, "..", "logs", "task_ingest_audit.jsonl")
INGEST_AUDIT_MAX_BYTES = 50 * 1024 * 1024  # 50MB
INGEST_AUDIT_MAX_LINES = 200_000           # 20 万行（与大小阈值先到为准）


def ingest_audit_capacity_event(st, path=None):
    """C2 检查段（2026-10-23 DeepSeek 复核条件）：摄取审计日志
    logs/task_ingest_audit.jsonl（H2 落点）容量监控——大小超 50MB 或
    行数超 20 万行（先到为准）产事件；超限期间只报一次
    （st.ingest_audit_over 闩锁），回落阈值下复位可再报。
    文件缺失/读取失败一律静默跳过，不影响主决策。"""
    p = path or INGEST_AUDIT_PATH
    try:
        size = os.path.getsize(p)
        lines = 0
        with open(p, "rb") as f:
            for _ in f:
                lines += 1
    except Exception:
        return []
    over = []
    if size > INGEST_AUDIT_MAX_BYTES:
        over.append("大小 %.1fMB>50MB" % (size / 1048576.0))
    if lines > INGEST_AUDIT_MAX_LINES:
        over.append("行数 %d>200000" % lines)
    if not over:
        st.pop("ingest_audit_over", None)
        return []
    if st.get("ingest_audit_over"):
        return []
    st["ingest_audit_over"] = True
    return [{"script": "sync_duty", "kind": "ingest_audit_capacity",
             "msg": "摄取审计日志容量超限（%s）：logs/task_ingest_audit.jsonl"
                    " 需轮转/归档处理" % "、".join(over)}]


GROQ_SEND_LOG_PATH = os.path.join(BASE, "..", "logs", "groq_send_log.jsonl")
GROQ_SEND_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50MB（与 C2 摄取审计同口径）
GROQ_SEND_LOG_MAX_LINES = 200_000           # 20 万行（先到为准）


def groq_send_log_capacity_event(st, path=None):
    """Groq 送数日志容量监控（2026-10-23 G0 通过后 DeepSeek 数据纪律 3.2：
    logs/groq_send_log.jsonl 纳入 sync_duty 容量监控，口径同 C2——
    大小超 50MB 或行数超 20 万行产事件；超限期间只报一次
    （st.groq_send_log_over 闩锁），回落后复位可再报。
    文件缺失/读取失败一律静默跳过，不影响主决策。"""
    p = path or GROQ_SEND_LOG_PATH
    try:
        size = os.path.getsize(p)
        lines = 0
        with open(p, "rb") as f:
            for _ in f:
                lines += 1
    except Exception:
        return []
    over = []
    if size > GROQ_SEND_LOG_MAX_BYTES:
        over.append("大小 %.1fMB>50MB" % (size / 1048576.0))
    if lines > GROQ_SEND_LOG_MAX_LINES:
        over.append("行数 %d>200000" % lines)
    if not over:
        st.pop("groq_send_log_over", None)
        return []
    if st.get("groq_send_log_over"):
        return []
    st["groq_send_log_over"] = True
    return [{"script": "sync_duty", "kind": "groq_send_log_capacity",
             "msg": "Groq送数日志容量超限（%s）：logs/groq_send_log.jsonl"
                    " 需轮转/归档处理" % "、".join(over)}]


def main():
    st = load_state()
    stats_code = run_script("build_stats.py")
    sync = read_sync_state()
    sec_code = run_script("sync_sections.py")
    events = decide(st, stats_code, sec_code, sync, time.time())
    events += grading_stay_event(st)
    events += ingest_audit_capacity_event(st)
    events += groq_send_log_capacity_event(st)
    save_state(st)
    print("DECISION " + ("report" if events else "silent"))
    print(json.dumps({"events": events,
                      "codes": {"stats": stats_code, "sections": sec_code}},
                     ensure_ascii=False))
    return 0


# ---------------- C1 回放 / 自测 ----------------

def _scenarios():
    now = 1_800_000_000.0
    stale = {"last_event_ts": now - 700, "last_build_ts": now - 800}
    return [
        # (名称, 步骤列表[(stats, sections, sync, now)], 期望每步事件 kind 列表)
        ("stats_exit2_immediate", [(2, 0, None, now)], [["exit2"]]),
        ("stats_exit3_single_silent", [(3, 0, None, now)], [[]]),
        ("stats_exit3_twice_report", [(3, 0, None, now), (3, 0, None, now)],
         [[], ["exit3x2"]]),
        ("stats_exit3_thrice_no_repeat",
         [(3, 0, None, now), (3, 0, None, now), (3, 0, None, now)],
         [[], ["exit3x2"], []]),
        ("stats_stale_report", [(0, 0, stale, now)], [["sync_stale"]]),
        ("stats_stale_dedupe", [(0, 0, stale, now), (0, 0, stale, now)],
         [["sync_stale"], []]),
        ("stats_stale_not_yet", [(0, 0, {"last_event_ts": now - 300,
                                          "last_build_ts": now - 400}, now)], [[]]),
        ("sections_exit2_x3", [(0, 2, None, now), (0, 2, None, now), (0, 2, None, now)],
         [[], [], ["exit2x3"]]),
        ("sections_exit3_x3", [(0, 3, None, now), (0, 3, None, now), (0, 3, None, now)],
         [[], [], ["exit3x3"]]),
        ("sections_exit4_silent", [(0, 4, None, now)], [[]]),
        ("sections_streak_broken_by_0",
         [(0, 2, None, now), (0, 2, None, now), (0, 0, None, now), (0, 2, None, now)],
         [[], [], [], []]),
    ]


def run_scenarios(scenarios):
    results = []
    all_ok = True
    for name, steps, expected in scenarios:
        st = {}
        got = []
        for (sc, se, sync, now) in steps:
            ev = decide(st, sc, se, sync, now)
            got.append([e["kind"] for e in ev])
        ok = (got == expected)
        all_ok = all_ok and ok
        results.append({"case": name, "ok": ok, "expected": expected, "got": got})
    return all_ok, results


def _selftest_capacity():
    """C2 容量检查段自测：临时小阈值 + 临时文件，验证触发/闩锁/复位。"""
    import tempfile
    global INGEST_AUDIT_MAX_BYTES, INGEST_AUDIT_MAX_LINES
    saved = (INGEST_AUDIT_MAX_BYTES, INGEST_AUDIT_MAX_LINES)
    results = []
    try:
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        st = {}
        with open(path, "w") as f:
            f.write("{}\n" * 5)
        INGEST_AUDIT_MAX_LINES = 3
        INGEST_AUDIT_MAX_BYTES = 50 * 1024 * 1024
        ev1 = ingest_audit_capacity_event(st, path)
        results.append(("capacity_lines_over_fires",
                        len(ev1) == 1 and ev1[0]["kind"] == "ingest_audit_capacity"))
        results.append(("capacity_latched_no_repeat",
                        ingest_audit_capacity_event(st, path) == []))
        with open(path, "w") as f:
            f.write("{}\n")
        results.append(("capacity_reset_under_threshold",
                        ingest_audit_capacity_event(st, path) == []
                        and "ingest_audit_over" not in st))
        with open(path, "w") as f:
            f.write("{}\n" * 5)
        results.append(("capacity_refires_after_reset",
                        len(ingest_audit_capacity_event(st, path)) == 1))
        INGEST_AUDIT_MAX_LINES = 200_000
        INGEST_AUDIT_MAX_BYTES = 4
        st2 = {}
        results.append(("capacity_bytes_over_fires",
                        len(ingest_audit_capacity_event(st2, path)) == 1))
        results.append(("capacity_missing_file_silent",
                        ingest_audit_capacity_event({}, path + ".missing") == []))
        os.unlink(path)
    finally:
        INGEST_AUDIT_MAX_BYTES, INGEST_AUDIT_MAX_LINES = saved
    return results


def _selftest_groq_capacity():
    """Groq 送数日志容量检查段自测：同 C2 套路（触发/闩锁/复位/再触发/缺失静默）。"""
    import tempfile
    global GROQ_SEND_LOG_MAX_BYTES, GROQ_SEND_LOG_MAX_LINES
    saved = (GROQ_SEND_LOG_MAX_BYTES, GROQ_SEND_LOG_MAX_LINES)
    results = []
    try:
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        st = {}
        with open(path, "w") as f:
            f.write("{}\n" * 5)
        GROQ_SEND_LOG_MAX_LINES = 3
        GROQ_SEND_LOG_MAX_BYTES = 50 * 1024 * 1024
        ev1 = groq_send_log_capacity_event(st, path)
        results.append(("groq_capacity_lines_over_fires",
                        len(ev1) == 1 and ev1[0]["kind"] == "groq_send_log_capacity"))
        results.append(("groq_capacity_latched_no_repeat",
                        groq_send_log_capacity_event(st, path) == []))
        with open(path, "w") as f:
            f.write("{}\n")
        results.append(("groq_capacity_reset_under_threshold",
                        groq_send_log_capacity_event(st, path) == []
                        and "groq_send_log_over" not in st))
        with open(path, "w") as f:
            f.write("{}\n" * 5)
        results.append(("groq_capacity_refires_after_reset",
                        len(groq_send_log_capacity_event(st, path)) == 1))
        results.append(("groq_capacity_missing_file_silent",
                        groq_send_log_capacity_event({}, path + ".missing") == []))
        os.unlink(path)
    finally:
        GROQ_SEND_LOG_MAX_BYTES, GROQ_SEND_LOG_MAX_LINES = saved
    return results


def selftest():
    ok, results = run_scenarios(_scenarios())
    for r in results:
        print(("PASS " if r["ok"] else "FAIL ") + r["case"]
              + ("" if r["ok"] else " expected=%s got=%s" % (r["expected"], r["got"])))
    for name, cok in _selftest_capacity():
        print(("PASS " if cok else "FAIL ") + name)
        ok = ok and cok
    for name, cok in _selftest_groq_capacity():
        print(("PASS " if cok else "FAIL ") + name)
        ok = ok and cok
    print("SELFTEST " + ("ALL_PASS" if ok else "FAILED"))
    return 0 if ok else 1


def replay(path):
    with open(path) as f:
        scenarios = json.load(f)
    parsed = []
    for s in scenarios:
        steps = [(step.get("stats"), step.get("sections"),
                  step.get("sync"), step.get("now", 1_800_000_000.0))
                 for step in s["steps"]]
        parsed.append((s["name"], steps, s["expected"]))
    ok, results = run_scenarios(parsed)
    print(json.dumps({"all_ok": ok, "results": results}, ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    if len(sys.argv) > 2 and sys.argv[1] == "--replay":
        sys.exit(replay(sys.argv[2]))
    sys.exit(main())

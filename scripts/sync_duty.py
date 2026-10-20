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


def main():
    st = load_state()
    stats_code = run_script("build_stats.py")
    sync = read_sync_state()
    sec_code = run_script("sync_sections.py")
    events = decide(st, stats_code, sec_code, sync, time.time())
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


def selftest():
    ok, results = run_scenarios(_scenarios())
    for r in results:
        print(("PASS " if r["ok"] else "FAIL ") + r["case"]
              + ("" if r["ok"] else " expected=%s got=%s" % (r["expected"], r["got"])))
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

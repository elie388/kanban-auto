#!/usr/bin/env python3
"""看板守护进程存活检查（DeepSeek 终审 2026-10-20《看板全栏目实时同步》Q3 落地）。

由 cron 每 5 分钟调用一次：
  1. 顺带跑 stash orphan 扫描（双根因定案问题③：并入 guard，零新增调度面；
     扫描异常只留痕、绝不影响主流程）
  2. 读 logs/kanban_watchd.pid 判活：boot_id 比对 → kill -0 → /proc cmdline
     复核（防 pid 复用误判）；boot_id 不符判"换环境"直接拉起不走静默窗
  3. 死亡且在 git 静默窗（最近 commit <300s）内 → 推迟拉起，但推迟有上限：
     同一 dead_pid 累计推迟达 GIT_SILENT_MAX_S=900s 强制拉起（定案事项二：
     原逻辑可无限期推迟，曾致 20 分钟不拉起）；skip 行补记 dead_pid /
     dead_reason / first_dead_ts / skip_count，连续 >=2 次 WARN、>=4 次写
     daemon_guard_alert.log
  4. 拉起守护进程（start_new_session 后台），轮询 10s 复核存活并日志
  5. 拉起失败 → 写 logs/daemon_guard.fail 告警（不碰 docs/ 程序异常台账）

幂等：重复调用不会拉起第二个实例（守护进程自身有 PID+flock 单例）。

用法：python3 scripts/daemon_guard.py
环境变量：KANBAN_REPO_DIR（默认脚本所在仓库）
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = Path(os.environ.get("KANBAN_REPO_DIR", str(SCRIPT_DIR.parent))).resolve()
LOGS_DIR = REPO_DIR / "logs"
PID_FILE = LOGS_DIR / "kanban_watchd.pid"
GUARD_LOG = LOGS_DIR / "daemon_guard.log"
FAIL_LOG = LOGS_DIR / "daemon_guard.fail"
ALERT_LOG = LOGS_DIR / "daemon_guard_alert.log"
STATE_FILE = LOGS_DIR / "daemon_guard_state.json"
WATCHD_LOG = LOGS_DIR / "kanban_watchd.log"
WATCHD = SCRIPT_DIR / "kanban_watchd.py"
BEIJING = timezone(timedelta(hours=8))
GIT_SILENT_S = 300
GIT_SILENT_MAX_S = 900  # 定案事项二：单次死亡事件的推迟封顶（秒）
LOG_KEEP_LINES = 500


def log(status, reason="", extra=None):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(BEIJING).isoformat(timespec="seconds"),
           "status": status, "reason": reason}
    if extra:
        rec.update(extra)
    with open(GUARD_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        lines = GUARD_LOG.read_text(encoding="utf-8").splitlines()
        if len(lines) > LOG_KEEP_LINES:
            GUARD_LOG.write_text("\n".join(lines[-LOG_KEEP_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def fail(msg):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(FAIL_LOG, "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (datetime.now(BEIJING).isoformat(timespec="seconds"), msg[:200]))


def alert(msg):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(ALERT_LOG, "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (datetime.now(BEIJING).isoformat(timespec="seconds"), msg[:300]))


def _current_boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def read_pid_info():
    """pid 文件解析：JSON {pid, boot_id, start_ts} 或历史纯整数。
    返回 dict 或 None（文件缺失/不可解析）。旧版纯整数 boot_id 为 None，
    按定案降级口径跳过 boot 比对。"""
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    if not text:
        return None
    try:
        d = json.loads(text)
        if isinstance(d, dict) and isinstance(d.get("pid"), int):
            return {"pid": d["pid"], "boot_id": d.get("boot_id"),
                    "start_ts": d.get("start_ts")}
    except ValueError:
        pass
    try:
        return {"pid": int(text), "boot_id": None, "start_ts": None}
    except ValueError:
        return None


def _infer_dead_reason(pid):
    """从 watchd 日志尾部找同 pid 的 exit 记录取退出原因；无 → unknown_kill
    （无 exit 记录而进程消失，与 SIGKILL 类外力终止一致，定案分级口径）。"""
    try:
        lines = WATCHD_LOG.read_text(encoding="utf-8").splitlines()[-100:]
    except OSError:
        return "unknown_kill"
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("pid") == pid and rec.get("status") == "exit":
            return rec.get("exit_reason") or "exit_recorded"
    return "unknown_kill"


def daemon_alive():
    """返回 (status, info)：status ∈ alive / dead / env_changed。

    判定顺序（定案 3d 修正）：boot_id 比对 → kill -0 存活 → cmdline 复核。
    """
    info = read_pid_info()
    if info is None:
        return "dead", {"pid": None, "dead_reason": "no_pid_file",
                        "detail": "no pid file"}
    pid = info["pid"]
    cur_boot = _current_boot_id()
    if info.get("boot_id") and cur_boot != "unknown" \
            and info["boot_id"] != cur_boot:
        return "env_changed", {
            "pid": pid,
            "detail": "boot_id 不符（pid文件=%s 当前=%s），判换环境"
                      % (info["boot_id"], cur_boot)}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", {"pid": pid, "dead_reason": _infer_dead_reason(pid),
                        "detail": "pid=%d dead" % pid}
    except PermissionError:
        # P2（DeepSeek 审查 2026-10-20）：无权限探测 = 进程存在（属其他用户），
        # 判存活防误判死亡后重复拉起双实例
        return "alive", {"pid": pid,
                         "detail": "pid=%d alive (no probe permission)" % pid}
    except OSError as e:
        return "dead", {"pid": pid, "dead_reason": "unknown_kill",
                        "detail": "kill -0 err: %s" % e}
    # pid 复用防护（定案 3d）：kill -0 命中后再验 cmdline 是否真是 watchd
    try:
        cmdline = Path("/proc/%d/cmdline" % pid).read_bytes() \
            .replace(b"\x00", b" ").decode("utf-8", "replace")
        if "kanban_watchd.py" not in cmdline:
            return "dead", {"pid": pid, "dead_reason": "pid_reused",
                            "detail": "pid=%d cmdline 不含 kanban_watchd.py" % pid}
    except OSError:
        pass  # /proc 不可读 → 按存活（沿用 PermissionError 口径）
    return "alive", {"pid": pid, "detail": "pid=%d" % pid}


def git_last_commit_ts():
    r = subprocess.run(["git", "-C", str(REPO_DIR), "log", "-1", "--format=%ct"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _load_state():
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False),
                              encoding="utf-8")
    except OSError:
        pass


def _normalize_dead_state(st, pid, now):
    """存活保障（2026-10-21 guard 崩溃修复）：旧状态文件可能缺字段或类型异常
    （KeyError 'first_dead_ts' 曾致 guard 崩溃退出码 1、无人拉起守护），
    缺失/异常时降级补齐默认值，绝不崩溃。"""
    if not isinstance(st, dict):
        st = {}
    if st.get("dead_pid") != pid:
        return {"dead_pid": pid, "first_dead_ts": now, "skip_count": 0}
    try:
        first = float(st.get("first_dead_ts"))
    except (TypeError, ValueError):
        first = now
    try:
        skip = int(st.get("skip_count", 0))
    except (TypeError, ValueError):
        skip = 0
    return {"dead_pid": pid, "first_dead_ts": first, "skip_count": skip}


def _clear_state():
    try:
        STATE_FILE.unlink()
    except OSError:
        pass


def _run_orphan_scan():
    """块2 集成（定案问题③）：guard 每轮顺带跑 stash orphan 扫描；
    扫描只读+告警落盘，异常只留痕，绝不影响 guard 主流程。"""
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        import stash_orphan_scan
        for a in stash_orphan_scan.scan(str(REPO_DIR)):
            log("stash_orphan", a.get("event", ""),
                {"ref": a.get("ref"), "event": a.get("event"),
                 "age_min": a.get("age_min")})
    except Exception as e:
        log("orphan_scan_error", str(e)[:200])


def _relaunch():
    try:
        out = open(LOGS_DIR / "kanban_watchd.out", "a", encoding="utf-8")
    except OSError as e:
        fail("无法打开 kanban_watchd.out: %s" % e)
        return 1
    env = dict(os.environ)
    env.setdefault("KANBAN_REPO_DIR", str(REPO_DIR))
    try:
        subprocess.Popen([sys.executable, str(WATCHD)], cwd=str(SCRIPT_DIR),
                         env=env, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)
    except Exception as e:
        fail("拉起守护进程失败: %s" % e)
        log("relaunch_failed", str(e)[:200])
        return 1
    # P2（DeepSeek 审查 2026-10-20）：复核由固定等 2s 改为轮询至多 10s——
    # 守护进程冷启动（解释器+导入+单例+状态恢复）偶尔超过 2s，固定等待会误报
    # 拉起失败并留下 fail 告警噪音。
    alive2, why2 = False, {}
    for _ in range(10):
        time.sleep(1)
        st2, why2 = daemon_alive()
        alive2 = (st2 == "alive")
        if alive2:
            break
    if alive2:
        log("relaunched", "守护进程已拉起", {"detail": why2.get("detail", "")})
        return 0
    fail("守护进程拉起后 10s 仍未存活")
    log("relaunch_failed", "拉起后 10s 未存活", {"detail": why2.get("detail", "")})
    return 1


def main():
    _run_orphan_scan()
    status, info = daemon_alive()
    if status == "alive":
        _clear_state()
        log("alive", info.get("detail", ""))
        return 0
    if status == "env_changed":
        # 换环境（系统重启后旧 pid 文件残留）：直接拉起，不走静默窗、不判死亡
        _clear_state()
        log("env_changed", info.get("detail", ""), {"dead_pid": info.get("pid")})
        return _relaunch()
    # status == dead：静默窗内推迟，但同一 dead_pid 推迟封顶 900s 强制拉起
    last = git_last_commit_ts()
    now = time.time()
    if last is not None and now - last < GIT_SILENT_S:
        # 2026-10-21 崩溃修复：状态归一化（缺字段/类型异常降级，不崩溃）
        st = _normalize_dead_state(_load_state(), info.get("pid"), now)
        st["skip_count"] += 1
        _save_state(st)
        extra = {"dead_pid": info.get("pid"),
                 "dead_reason": info.get("dead_reason"),
                 "first_dead_ts": st["first_dead_ts"],
                 "skip_count": st["skip_count"], "last_commit": last}
        if now - st["first_dead_ts"] >= GIT_SILENT_MAX_S:
            log("force_relaunch",
                "静默窗推迟达上限 %ds，强制拉起" % GIT_SILENT_MAX_S, extra)
            _clear_state()
            return _relaunch()
        if st["skip_count"] >= 2:
            extra["level"] = "WARN"
        if st["skip_count"] >= 4:
            alert("守护进程死亡后连续 skip %d 次未拉起（dead_pid=%s, "
                  "dead_reason=%s）" % (st["skip_count"], info.get("pid"),
                                       info.get("dead_reason")))
        log("skip", "git 静默窗内（最近 commit <300s），暂不拉起", extra)
        return 0
    log("dead", "守护进程死亡/缺失，准备拉起",
        {"detail": info.get("detail", ""),
         "dead_reason": info.get("dead_reason")})
    _clear_state()
    return _relaunch()


def selftest():
    """--selftest：_normalize_dead_state 降级健壮性（2026-10-21 崩溃回归）。"""
    results = []

    def rec(name, ok, detail):
        results.append(ok)
        print("[%s] %s :: %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)

    try:
        now = 1000.0
        # G1：缺 first_dead_ts（崩溃现场）→ 降级补齐，不抛 KeyError
        s = _normalize_dead_state({"dead_pid": 9, "skip_count": 2}, 9, now)
        assert s == {"dead_pid": 9, "first_dead_ts": now, "skip_count": 2}, s
        # G2：非 dict / 全空 → 初始化
        s = _normalize_dead_state(None, 9, now)
        assert s == {"dead_pid": 9, "first_dead_ts": now, "skip_count": 0}, s
        s = _normalize_dead_state({}, 9, now)
        assert s["first_dead_ts"] == now and s["skip_count"] == 0, s
        # G3：类型异常 → 降级
        s = _normalize_dead_state(
            {"dead_pid": 9, "first_dead_ts": "garbage", "skip_count": "x"}, 9, now)
        assert s["first_dead_ts"] == now and s["skip_count"] == 0, s
        # G4：pid 变化 → 重置为新事件
        s = _normalize_dead_state(
            {"dead_pid": 7, "first_dead_ts": 100.0, "skip_count": 5}, 9, now)
        assert s == {"dead_pid": 9, "first_dead_ts": now, "skip_count": 0}, s
        # G5：正常透传
        s = _normalize_dead_state(
            {"dead_pid": 9, "first_dead_ts": 900.0, "skip_count": 3}, 9, now)
        assert s == {"dead_pid": 9, "first_dead_ts": 900.0, "skip_count": 3}, s
        rec("G1-G5 状态归一化", True,
            "缺字段/非dict/类型异常/pid变化/正常透传 全部降级不崩溃")
    except Exception as e:
        rec("G1-G5 状态归一化", False, str(e)[:200])
    n_fail = sum(1 for x in results if not x)
    print("自测完成：%d 通过 / %d 失败" % (len(results) - n_fail, n_fail), flush=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())

#!/usr/bin/env python3
"""看板守护进程存活检查（DeepSeek 终审 2026-10-20《看板全栏目实时同步》Q3 落地）。

由 cron 每 5 分钟调用一次：
  1. 读 logs/kanban_watchd.pid，kill -0 判活 → 存活则退出 0
  2. 死亡/缺失 → 检查 git 静默窗（仓库最近 push/commit <300s 则不拉起，fail-safe）
  3. 拉起守护进程（start_new_session 后台），2s 后复核存活并日志
  4. 拉起失败 → 写 logs/daemon_guard.fail 告警（不碰 docs/ 程序异常台账，避免卷入他流）

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
WATCHD = SCRIPT_DIR / "kanban_watchd.py"
BEIJING = timezone(timedelta(hours=8))
GIT_SILENT_S = 300
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


def daemon_alive():
    try:
        pid = int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False, "no pid file"
    try:
        os.kill(pid, 0)
        return True, "pid=%d" % pid
    except (ProcessLookupError, PermissionError):
        return False, "pid=%d dead" % pid
    except OSError as e:
        return False, "kill -0 err: %s" % e


def git_last_commit_ts():
    r = subprocess.run(["git", "-C", str(REPO_DIR), "log", "-1", "--format=%ct"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def main():
    alive, why = daemon_alive()
    if alive:
        log("alive", why)
        return 0
    # Q3/R2：静默窗内不拉起（fail-safe），避免与刚完成的 push 产生竞态
    last = git_last_commit_ts()
    if last is not None and time.time() - last < GIT_SILENT_S:
        log("skip", "git 静默窗内（最近 commit <300s），暂不拉起", {"last_commit": last})
        return 0
    log("dead", "守护进程死亡/缺失，准备拉起", {"detail": why})
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
    time.sleep(2)
    alive2, why2 = daemon_alive()
    if alive2:
        log("relaunched", "守护进程已拉起", {"detail": why2})
        return 0
    fail("守护进程拉起后 2s 仍未存活")
    log("relaunch_failed", "拉起后未存活", {"detail": why2})
    return 1


if __name__ == "__main__":
    sys.exit(main())

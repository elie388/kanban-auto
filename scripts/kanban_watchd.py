#!/usr/bin/env python3
"""看板实时同步守护进程：事件驱动触发 build/sync（DeepSeek 终审 2026-10-20《看板全栏目实时同步》方案 A 落地）。

监听（Q4 裁决澄清：徽章颁发台账即积分记录.md，无独立文件，不得虚构路径）：
  hidden_files/积分记录.md -> [build_stats.py, sync_sections.py]
  hidden_files/规则手册.md  -> [sync_sections.py]
明确不监听 kanban-deploy/data/*.json 产物（防自触发循环）。

触发策略（取与关系，Q5）：
  - 防抖 45s：同源变更聚合，防抖窗内多次变更只触发一轮
  - 软静默 [00:00,06:00) Asia/Shanghai：事件不触发 build，记录 skip，06:00 后首轮收敛
  - push 限流：连续 push 间隔 >=60s；滑动 5min 内 push <=2 次（以 git 实际 commit 为准，跨进程精确）
  - 限流计数持久化：logs/push_rate.log（审计用），重启不清零

存活保障（Q3）：logs/kanban_watchd.pid + /tmp/kanban_watchd.lock 单例；
  daemon_guard.py（5m cron）负责判活拉起。

只读源文件；只写 kanban-deploy/data/（经消费者脚本）、logs/ 与守护进程自身 pid/log。
红线：不改台账内容、不碰 broker/point_ledger/题目库；STRONG_SIGNAL_ENABLED 不动。

用法：
  python3 scripts/kanban_watchd.py              前台运行（systemd/守护方式自选）
  python3 scripts/kanban_watchd.py --selftest  自测后退出（0=通过）
环境变量（默认生产路径，仅测试覆盖）：
  KANBAN_REPO_DIR / LEDGER_PATH / RULEBOOK_PATH /
  KANBAN_WATCHD_POLL_S / KANBAN_WATCHD_DEBOUNCE_S / KANBAN_WATCHD_RUN_ONCE=1
"""
import atexit
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = SCRIPT_DIR.parent
BEIJING = timezone(timedelta(hours=8))

REPO_DIR = Path(os.environ.get("KANBAN_REPO_DIR", str(DEFAULT_REPO))).resolve()
LEDGER = Path(os.environ.get("LEDGER_PATH", str(REPO_DIR.parent / "积分记录.md")))
RULEBOOK = Path(os.environ.get("RULEBOOK_PATH", str(REPO_DIR.parent / "规则手册.md")))
LOGS_DIR = REPO_DIR / "logs"
PID_FILE = LOGS_DIR / "kanban_watchd.pid"
LOCK_FILE = Path("/tmp/kanban_watchd.lock")
LOG_FILE = LOGS_DIR / "kanban_watchd.log"
RATE_LOG = LOGS_DIR / "push_rate.log"
STATE_FILE = LOGS_DIR / "sync_state.json"  # F3（2026-10-20 审计修复3）：事件/构建对账与已消费指纹持久化
GIT_LOCK = REPO_DIR.parent / "logs" / "git_push.lock"  # F3：与 task_ingest.do_push 共用同一把锁，串行化 git 区段
LOG_KEEP_LINES = 2000

POLL_S = float(os.environ.get("KANBAN_WATCHD_POLL_S", "20"))
DEBOUNCE_S = float(os.environ.get("KANBAN_WATCHD_DEBOUNCE_S", "45"))
PUSH_MIN_INTERVAL_S = 60
PUSH_WINDOW_S = 300
PUSH_WINDOW_MAX = 2
GIT_SILENT_S = 300  # 供 guard 参考：push 后静默窗

try:
    sys.path.insert(0, str(SCRIPT_DIR))
    import sync_balance as _sb
    SILENT_WINDOW = tuple(_sb.BUILD_SILENT_WINDOW)  # 单点定义复用（R6）
except Exception:
    SILENT_WINDOW = (0, 6)  # 回退字面量（sync_balance 不可导入时）

QBANK = Path(os.environ.get("QBANK_PATH", str(REPO_DIR.parent / "题目库.db")))
WATCH_TARGETS = {
    "ledger": (LEDGER, ["build_stats.py", "sync_sections.py"]),
    "rulebook": (RULEBOOK, ["sync_sections.py"]),
    # 学习走势（2026-10-20 DeepSeek 方案）：题目库只读聚合 → data/trends.json
    "qbank": (QBANK, ["build_trends.py"]),
}


def now_iso():
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def log(status, reason="", extra=None):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now_iso(), "status": status, "reason": reason}
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


def signature(path):
    """文件指纹：(mtime_ns, size)。仅 stat，不读全文（轮询 20s 低开销）。"""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def in_silent_window(now=None):
    now = now or datetime.now(BEIJING)
    start, end = SILENT_WINDOW
    return start <= now.hour < end


def git(*args):
    return subprocess.run(["git", "-C", str(REPO_DIR)] + list(args),
                          capture_output=True, text=True, timeout=60)


def last_commit_ts():
    """仓库最近一次 commit 的 epoch 秒；失败返回 None。"""
    r = git("log", "-1", "--format=%ct")
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def commits_in_window(window_s):
    """近 window_s 秒内的 commit 数（跨进程精确的限流计数）。"""
    r = git("rev-list", "--count", "--since=%d seconds ago" % int(window_s), "HEAD")
    if r.returncode != 0:
        return 999  # fail-closed：查不到就当超限，宁可 defer
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 999


def push_allowed(now_ts):
    """限流判定（取与关系）：返回 (ok, reason)。"""
    last = last_commit_ts()
    if last is not None and now_ts - last < PUSH_MIN_INTERVAL_S:
        return False, "push_interval<%ds" % PUSH_MIN_INTERVAL_S
    if commits_in_window(PUSH_WINDOW_S) >= PUSH_WINDOW_MAX:
        return False, "push_window>=%d/%ds" % (PUSH_WINDOW_MAX, PUSH_WINDOW_S)
    return True, ""


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state):
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _acquire_git_lock(timeout_s=30):
    """F3：与 task_ingest 共用 git_push.lock。成功返回打开的锁文件句柄；超时返回 None。
    watchd 消费者（build_stats/sync_sections）在内部做 git 操作，整段持锁可防与 task_ingest 推送交错。"""
    GIT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    lf = open(GIT_LOCK, "w")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lf
        except BlockingIOError:
            if time.monotonic() >= deadline:
                lf.close()
                return None
            time.sleep(1)


def record_push():
    """守护进程触发的某轮产生了 commit → 持久化记一笔（审计用）。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(RATE_LOG, "a", encoding="utf-8") as f:
        f.write("%s pushed\n" % now_iso())


class Watcher:
    def __init__(self):
        self.sigs = {}      # key -> 上次已消费指纹
        self.pending = {}   # key -> 防抖到期时间戳（monotonic）
        self._silent_logged = False
        # F3：恢复已消费指纹。重启后若源文件与已消费指纹不一致，说明停机期间有变更未构建，
        # poll 首轮会按 restored 逻辑重新武装 pending，避免事件丢失。
        self._state = _load_state()
        consumed = self._state.get("consumed", {}) if isinstance(self._state, dict) else {}
        for key, saved in consumed.items():
            if isinstance(saved, list) and len(saved) == 2:
                self.sigs[key] = (saved[0], saved[1])

    def poll_once(self):
        """单轮：检测变更 -> 防抖到期且非静默且限流通过 -> 运行消费者。"""
        for key, (path, consumers) in WATCH_TARGETS.items():
            sig = signature(str(path))
            if sig is None:
                if self.sigs.get(key) is not None:
                    log("source_missing", str(path), {"target": key})
                    self.sigs[key] = None
                continue
            if key not in self.sigs:
                self.sigs[key] = sig  # 首轮只建基线，不触发
                continue
            if sig != self.sigs[key] and key not in self.pending:
                self.pending[key] = time.monotonic() + DEBOUNCE_S
                self._state["last_event_ts"] = time.time()
                _save_state(self._state)
                log("change_detected", str(path),
                    {"target": key, "debounce_s": DEBOUNCE_S})
            if key in self.pending and time.monotonic() >= self.pending[key]:
                self._fire(key, consumers)

    def _fire(self, key, consumers):
        path = str(WATCH_TARGETS[key][0])
        if in_silent_window():
            if not self._silent_logged:
                log("skip", "silent window [00:00,06:00)（软静默，事件暂不触发）",
                    {"target": key})
                self._silent_logged = True
            return  # 保持 pending，06:00 后收敛
        self._silent_logged = False
        ok, why = push_allowed(time.time())
        if not ok:
            log("rate_limited", why, {"target": key})
            return  # 保持 pending，下一轮再试
        lock = _acquire_git_lock()
        if lock is None:
            log("git_lock_busy", "git_push.lock 30s 未获得，保持 pending 下轮重试",
                {"target": key})
            return  # 不消费，下轮再试
        try:
            for script in consumers:
                self._run_consumer(script, key, path)
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
            except OSError:
                pass
        # 全部消费者跑完 → 消费本次变更，并写对账状态（F3）
        new_sig = signature(path)
        self.sigs[key] = new_sig
        self.pending.pop(key, None)
        self._state["last_build_ts"] = time.time()
        if new_sig is not None:
            self._state.setdefault("consumed", {})[key] = [new_sig[0], new_sig[1]]
        _save_state(self._state)

    def _run_consumer(self, script, key, path):
        script_path = SCRIPT_DIR / script
        log("consumer_start", str(script_path), {"target": key})
        head_before = git("rev-parse", "HEAD").stdout.strip()
        env = dict(os.environ)
        # LEDGER_PATH 等透传（build_stats/sync_balance 均支持 LEDGER_PATH）
        env.setdefault("LEDGER_PATH", str(LEDGER))
        try:
            r = subprocess.run([sys.executable, str(script_path)],
                               cwd=str(SCRIPT_DIR), env=env,
                               capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            log("consumer_timeout", script, {"target": key})
            return
        tail = (r.stdout or "")[-500:]
        log("consumer_done", script,
            {"target": key, "rc": r.returncode,
             "stderr_tail": (r.stderr or "")[-300:], "stdout_tail": tail})
        head_after = git("rev-parse", "HEAD").stdout.strip()
        if head_after and head_after != head_before:
            record_push()
            log("pushed", script, {"target": key, "commit": head_after[:12]})


def acquire_singleton():
    """PID 文件 + flock 单例。返回 (ok, reason)。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        lock_fp = open(LOCK_FILE, "w")
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False, "another instance holds the lock"
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return False, "pid file alive (pid=%d)" % pid
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass
    PID_FILE.write_text(str(os.getpid()))
    def _cleanup():
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass
    atexit.register(_cleanup)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: sys.exit(0))
        except (OSError, ValueError):
            pass
    return True, ""


def run_forever():
    ok, why = acquire_singleton()
    if not ok:
        log("singleton_conflict", why)
        print("singleton_conflict: " + why)
        return 1
    log("start", "kanban_watchd 启动",
        {"poll_s": POLL_S, "debounce_s": DEBOUNCE_S,
         "targets": sorted(WATCH_TARGETS.keys()),
         "silent_window": "%02d:00-%02d:00" % SILENT_WINDOW})
    print("kanban_watchd started, pid=%d" % os.getpid(), flush=True)
    w = Watcher()
    try:
        while True:
            try:
                w.poll_once()
            except Exception as e:  # 单轮异常不杀进程
                log("poll_error", str(e)[:200])
            if os.environ.get("KANBAN_WATCHD_RUN_ONCE") == "1":
                break
            time.sleep(POLL_S)
    finally:
        log("stop", "kanban_watchd 退出")
    return 0


def selftest():
    results = []

    def rec(name, ok, detail):
        results.append(ok)
        print("[%s] %s :: %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)

    # T1 指纹变更检测
    try:
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write("a")
            tp = tf.name
        s1 = signature(tp)
        assert s1 is not None
        os.utime(tp, (time.time() + 5, time.time() + 5))
        s2 = signature(tp)
        assert s2 != s1, "mtime 变化应改变指纹"
        with open(tp, "w") as f:
            f.write("ab")
        s3 = signature(tp)
        assert s3 != s2, "size 变化应改变指纹"
        assert signature(tp + ".missing") is None
        os.unlink(tp)
        rec("T1 指纹变更检测", True, "mtime/size 变化均可检出，缺失文件返回 None")
    except Exception as e:
        rec("T1 指纹变更检测", False, str(e)[:200])

    # T2 软静默边界（常量单点来自 sync_balance）
    try:
        assert SILENT_WINDOW == (0, 6), "SILENT_WINDOW=%r" % (SILENT_WINDOW,)
        assert in_silent_window(datetime(2026, 10, 20, 0, 0, tzinfo=BEIJING)) is True
        assert in_silent_window(datetime(2026, 10, 20, 5, 59, tzinfo=BEIJING)) is True
        assert in_silent_window(datetime(2026, 10, 20, 6, 0, tzinfo=BEIJING)) is False
        assert in_silent_window(datetime(2026, 10, 20, 12, 0, tzinfo=BEIJING)) is False
        rec("T2 软静默边界", True, "SILENT_WINDOW=(0,6)，边界 [00:00,06:00) 正确")
    except Exception as e:
        rec("T2 软静默边界", False, str(e)[:200])

    # T3 限流判定（临时 git 仓库实测）
    try:
        import tempfile
        td = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=td, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=td, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=td, check=True)
        Path(td, "f").write_text("1")
        subprocess.run(["git", "add", "."], cwd=td, check=True)
        subprocess.run(["git", "commit", "-qm", "c1"], cwd=td, check=True)
        now_ts = time.time()
        global REPO_DIR
        old_repo = REPO_DIR
        REPO_DIR = Path(td)
        try:
            ok, _ = push_allowed(now_ts)
            assert ok is False, "刚 commit（<60s）应被拦截"
            # 把 c1 改为 10 分钟前，再补一个 4 分钟前的 commit → 窗口内 1 个 → 放行
            subprocess.run(["git", "commit", "-q", "--amend", "--no-edit",
                            "--date=@%d" % int(now_ts - 600)],
                           cwd=td, check=True,
                           env=dict(os.environ, GIT_COMMITTER_DATE="@%d" % int(now_ts - 600)))
            Path(td, "f").write_text("2")
            subprocess.run(["git", "add", "."], cwd=td, check=True)
            subprocess.run(["git", "commit", "-qm", "c2", "--date=@%d" % int(now_ts - 240)],
                           cwd=td, check=True,
                           env=dict(os.environ, GIT_COMMITTER_DATE="@%d" % int(now_ts - 240)))
            ok, _ = push_allowed(now_ts)
            assert ok is True, "窗口内仅 1 commit 且间隔>60s 应放行"
            Path(td, "f").write_text("3")
            subprocess.run(["git", "add", "."], cwd=td, check=True)
            subprocess.run(["git", "commit", "-qm", "c3", "--date=@%d" % int(now_ts - 120)],
                           cwd=td, check=True,
                           env=dict(os.environ, GIT_COMMITTER_DATE="@%d" % int(now_ts - 120)))
            ok, why = push_allowed(now_ts)
            assert ok is False and "push_window" in why, "窗口内 2 commit 应被拦截"
        finally:
            REPO_DIR = old_repo
        rec("T3 限流判定", True, "间隔<60s 拦截 / 窗口内>=2 拦截 / 正常放行")
    except Exception as e:
        rec("T3 限流判定", False, str(e)[:200])

    # T4 防抖合并：快速连续变更只留一个 pending（不提前触发）
    try:
        w = Watcher()
        w.sigs["ledger"] = ("old", 1)
        real_sig = signature
        calls = {"n": 0}
        def fake_sig(p):
            calls["n"] += 1
            return ("new", 2)
        globals()["signature"] = fake_sig
        try:
            w.poll_once()
            assert "ledger" in w.pending, "首次变更应进入防抖"
            first_deadline = w.pending["ledger"]
            time.sleep(0.05)
            w.poll_once()
            assert w.pending["ledger"] == first_deadline, "防抖窗内重复变更不应重置计时"
            assert time.monotonic() < w.pending["ledger"], "45s 防抖不应立即触发"
        finally:
            globals()["signature"] = real_sig
        rec("T4 防抖合并", True, "连续变更聚合为一次 pending，计时不重置")
    except Exception as e:
        rec("T4 防抖合并", False, str(e)[:200])

    n_fail = sum(1 for x in results if not x)
    print("自测完成：%d 通过 / %d 失败" % (len(results) - n_fail, n_fail), flush=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run_forever())

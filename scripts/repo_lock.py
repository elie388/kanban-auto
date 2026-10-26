# -*- coding: utf-8 -*-
"""看板仓库共享写锁（治本c项，2026-10-26）：同一时间只一路写 kanban-deploy。

背景：do_push / build_stats / build_trends / watchd 已共用 hidden_files/logs/git_push.lock，
但 sync_sections（/tmp 锁）、feedback_publish / summary_publish / summary_status（无锁）
各行其是，git add/commit/push 会并行撞车。本模块统一收口：所有写库方在 git 区段
前持同一把 git_push.lock，超时 fail-safe 跳过（不挂死）。

语义（与既有实现一致）：
- KANBAN_GIT_LOCK_HELD=1（watchd 外层已持锁）时返回哑句柄：不重复取锁、不解锁。
- 否则 flock EX 阻塞等待至 timeout（默认 60s，每 5s 轮询），超时返回 None（调用方跳过推送并告警）。
- 释放仅对真实句柄做 flock UN + close；哑句柄 no-op。
"""
import fcntl
import os
import time

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(os.path.dirname(REPO_DIR), "logs", "git_push.lock")

_HELD_ENV = "KANBAN_GIT_LOCK_HELD"


class _DummyHandle:
    """外层已持锁时的占位句柄：释放 no-op。"""
    dummy = True


def acquire_repo_lock(timeout=60, poll=5):
    """取共享写锁。成功返回句柄（含哑句柄）；超时返回 None。"""
    if os.environ.get(_HELD_ENV) == "1":
        return _DummyHandle()
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fp = open(LOCK_PATH, "w")
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fp
        except BlockingIOError:
            if time.time() >= deadline:
                fp.close()
                return None
            time.sleep(min(poll, max(0, deadline - time.time())))


def release_repo_lock(handle):
    """释放。哑句柄 no-op；真实句柄 UN + close。"""
    if handle is None or getattr(handle, "dummy", False):
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


class repo_lock:
    """with repo_lock(timeout) as ok: ok=True 已持锁，False=超时未持锁（调用方跳过）。"""
    def __init__(self, timeout=60):
        self.timeout = timeout
        self.handle = None
        self.ok = False

    def __enter__(self):
        self.handle = acquire_repo_lock(timeout=self.timeout)
        self.ok = self.handle is not None
        return self.ok

    def __exit__(self, *exc):
        release_repo_lock(self.handle)
        self.handle = None
        return False

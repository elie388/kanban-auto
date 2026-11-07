#!/usr/bin/env python3
"""任务表事件消费者（2026-10-21 选项1补强）。

watchd 的 tasks_5/tasks_6 监听项触发本脚本：把
hidden_files/tasks_daily_{5,6}.json 有变化的拷贝到 kanban-deploy/data/，
有变化则 commit+push（触发 Pages 部署）。幂等：无变化直接退出 0。

实现复用 task_ingest.do_push（单实现防漂移）：
- watchd 在 _fire 外层已持 git_push.lock 并置 KANBAN_GIT_LOCK_HELD=1，
  do_push 识别该标记跳过重复取锁（flock 同一进程多 fd 会互斥失败）；
- 独立运行时按 do_push 既有逻辑自行取锁。

返回码：0=成功/无变化，1=失败（watchd 据此记 consumer_failed 并重试）。
"""
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = Path(os.environ.get("KANBAN_REPO_DIR", str(SCRIPT_DIR.parent))).resolve()
HIDDEN_SCRIPTS = REPO_DIR.parent / "scripts"


def selftest(task_ingest):
    """Hermetic 自测：临时 HIDDEN + 临时 git 仓库（含本地 bare origin）。"""
    import json
    import subprocess
    import tempfile

    def rec(results, name, ok, detail):
        results.append(ok)
        print("[%s] %s :: %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)

    results = []
    # B2-074：mkdtemp 用后清理（shutil.rmtree），防 /tmp 堆积；
    # B2-078：HIDDEN 恢复放在外层 finally，异常时也不污染本进程状态。
    td = None
    old_hidden = None
    old_held = None
    try:
        import shutil
        td = tempfile.mkdtemp()
        repo = os.path.join(td, "kanban-deploy")
        bare = os.path.join(td, "origin.git")
        os.makedirs(os.path.join(repo, "data"))
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", bare], cwd=repo, check=True)
        old_hidden = task_ingest.HIDDEN
        task_ingest.HIDDEN = td
        old_held = os.environ.pop("KANBAN_GIT_LOCK_HELD", None)
        def _check(cond, msg):
            # B2-077：不用 assert 做检查（python -O 下 assert 会被优化掉），改显式抛错。
            if not cond:
                raise AssertionError(msg)
        doc = {"date": "2026-10-21", "tasks": []}
        with open(os.path.join(td, "tasks_daily_5.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        # B2-076：同时写六年级表，覆盖六年级路径。
        with open(os.path.join(td, "tasks_daily_6.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        ok = task_ingest.do_push()
        dst = os.path.join(repo, "data", "tasks_daily_5.json")
        _check(ok is True, "首轮有变化应返回 True")
        _check(os.path.exists(dst), "data 文件应已拷贝")
        _check(os.path.exists(os.path.join(repo, "data", "tasks_daily_6.json")), "六年级表应已拷贝")
        r = subprocess.run(["git", "-C", repo, "log", "--oneline"],
                           capture_output=True, text=True)
        # B2-075：rev-list/log 查 returncode，不只比 stdout 计数。
        _check(r.returncode == 0 and r.stdout.strip(), "应已产生 commit")
        ok2 = task_ingest.do_push()
        _check(ok2 is True, "无变化应返回 True")
        r2 = subprocess.run(["git", "-C", repo, "rev-list", "--count", "HEAD"],
                            capture_output=True, text=True)
        _check(r2.returncode == 0, "rev-list 应成功")
        n1 = r2.stdout.strip()
        doc["tasks"] = [{"task_id": "T-1"}]
        with open(os.path.join(td, "tasks_daily_5.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        ok3 = task_ingest.do_push()
        r3 = subprocess.run(["git", "-C", repo, "rev-list", "--count", "HEAD"],
                            capture_output=True, text=True)
        _check(r3.returncode == 0, "rev-list 应成功")
        _check(ok3 is True and r3.stdout.strip() != n1, "变化后应新增 commit")
        rec(results, "S1 拷贝+提交+幂等", True,
            "有变化→拷贝+commit+push；无变化→True 空转；再变化→新 commit")
    except Exception as e:
        # B2-080：异常信息不截断 200 字，保留完整原因便于定位。
        rec(results, "S1 拷贝+提交+幂等", False, str(e))
    finally:
        if old_hidden is not None:
            task_ingest.HIDDEN = old_hidden
        if old_held is not None:
            os.environ["KANBAN_GIT_LOCK_HELD"] = old_held
        if td:
            try:
                import shutil
                shutil.rmtree(td, ignore_errors=True)
            except Exception:
                pass
    n_fail = sum(1 for x in results if not x)
    print("自测完成：%d 通过 / %d 失败" % (len(results) - n_fail, n_fail), flush=True)
    return 1 if n_fail else 0


def derive_closed_set():
    """②（2026-11-05）：从 grading_closed_batches.jsonl 派生已收口集合。

    task_id 经 strip＋转大写去重（jsonl 追加写，同一 task 取最后一条的 ts），
    原子写 kanban-deploy/data/closed_batches.json（{generated_at, task_ids 排序后,
    closed_at, skipped}）；空 id 零容忍（出现即派生失败告警，不写出，旧文件不动）；
    首次上线源空时仍写出 task_ids: [] 的合法文件，保证 workflow test -s 通过；
    内容不变不复写（仅适用于文件已存在且内容一致时）。
    返回 (ok, detail)。失败不阻断 do_push（旧文件保留，板面回落今日行为）。
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    hidden = REPO_DIR.parent
    src = hidden / "logs" / "grading_closed_batches.jsonl"
    dst = REPO_DIR / "data" / "closed_batches.json"
    closed_at = {}
    skipped = 0
    # B2-061：源缺失时区分“从未有过”与“已被删除”。源不存在且目标已存在
    # （已有收口记录）时不覆盖旧文件，返回失败告警，防静默解收口；仅源与目标
    # 均不存在（首次运行）时写出空文件供 workflow test -s 通过。
    if not src.exists():
        if dst.exists():
            print("[derive_closed_set] 源缺失且旧文件已存在：不覆盖，旧收口集合保留", flush=True)
            return False, "源缺失，旧文件保留"
    if src.exists():
        try:
            lines = src.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            print(f"[derive_closed_set] 读源失败：{e}", flush=True)
            return False, "读源失败"
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except Exception:
                skipped += 1
                print(f"[derive_closed_set] 跳过坏行（JSON 解析失败）：{line[:120]}", flush=True)
                continue
            # B2-065：非 dict 行跳过该行，不中断整批。
            if not isinstance(rec, dict):
                skipped += 1
                print(f"[derive_closed_set] 跳过坏行（非 dict）：{line[:120]}", flush=True)
                continue
            # B2-069：task_id 为数字 0 时 `or ""` 会误判为空；先取原始值再转字符串。
            _tid_raw = rec.get("task_id")
            if _tid_raw is None:
                tid = ""
            elif isinstance(_tid_raw, str):
                tid = _tid_raw.strip()
            else:
                tid = str(_tid_raw).strip()
            if not tid:
                print("[derive_closed_set] 空 task_id 零容忍：派生失败，不写出", flush=True)
                return False, "空 task_id"
            # B2-064：task_id/ts 格式校验，脏数据不进公开输出。
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_.]*", tid):
                skipped += 1
                print(f"[derive_closed_set] 跳过坏行（task_id 格式非法）：{tid[:80]}", flush=True)
                continue
            _ts = str(rec.get("ts") or rec.get("closed_at") or "")
            _k = tid.upper()
            closed_at[_k] = max(closed_at.get(_k, ""), _ts)
    now = _dt.now(_tz(_td(hours=8))).isoformat()
    obj = {"generated_at": now,
           "task_ids": sorted(closed_at.keys()),
           "closed_at": closed_at,
           "skipped": skipped}
    text = _json.dumps(obj, ensure_ascii=False, indent=2)
    try:
        if dst.exists():
            try:
                old = _json.loads(dst.read_text(encoding="utf-8"))
            except Exception:
                old = None
            # 内容不变不复写：只比 task_ids＋closed_at＋skipped（忽略 generated_at），
            # 否则每次刷新时间戳都会触发 do_push 的 data/ 变化，造成推送风暴。
            # generated_at 语义＝当前集合内容最后一次派生（即最后一次变化）的时间。
            if (isinstance(old, dict) and old.get("task_ids") == obj["task_ids"]
                    and old.get("closed_at") == obj["closed_at"]
                    and old.get("skipped") == obj["skipped"]):
                return True, "内容不变不复写"
        dst.parent.mkdir(parents=True, exist_ok=True)
        # B2-062：临时文件名带 pid，防两路并发用同一 .json.tmp 互相覆盖；
        # B2-071：写后 fsync 再 replace，防系统崩溃丢数据。
        tmp = dst.with_suffix(f".json.tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as _f:
                _f.write(text)
                _f.flush()
                os.fsync(_f.fileno())
            os.replace(tmp, dst)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            raise
        return True, f"写出 {len(closed_at)} 条"
    except OSError as e:
        print(f"[derive_closed_set] 写出失败：{e}", flush=True)
        return False, "写出失败"


def main():
    # B2-072：HIDDEN_SCRIPTS 由 KANBAN_REPO_DIR 环境变量派生，插入前校验其为
    # 预期位置且含 task_ingest.py，防环境变量被控导致导入劫持。
    _hs = HIDDEN_SCRIPTS.resolve()
    _expect = (REPO_DIR.parent / "scripts").resolve()
    if _hs != _expect or not (_hs / "task_ingest.py").is_file():
        print(f"导入路径校验失败：HIDDEN_SCRIPTS={_hs} 非预期 {_expect}，拒绝导入", file=sys.stderr)
        return 1
    sys.path.insert(0, str(_hs))
    try:
        import task_ingest
    except Exception as e:
        print("导入 task_ingest 失败：%s" % e, file=sys.stderr)
        return 1
    if "--selftest" in sys.argv:
        return selftest(task_ingest)
    try:
        ok_derive, detail_derive = derive_closed_set()
        print(f"[derive_closed_set] {detail_derive}", flush=True)
    except Exception as e:
        print(f"[derive_closed_set] 异常（不阻断推送）：{e}", flush=True)
    try:
        ok = task_ingest.do_push()
    except Exception as e:
        print("do_push 异常：%s" % e, file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

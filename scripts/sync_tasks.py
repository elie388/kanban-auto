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
    try:
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
        try:
            doc = {"date": "2026-10-21", "tasks": []}
            with open(os.path.join(td, "tasks_daily_5.json"), "w", encoding="utf-8") as f:
                json.dump(doc, f)
            ok = task_ingest.do_push()
            dst = os.path.join(repo, "data", "tasks_daily_5.json")
            assert ok is True, "首轮有变化应返回 True"
            assert os.path.exists(dst), "data 文件应已拷贝"
            r = subprocess.run(["git", "-C", repo, "log", "--oneline"],
                               capture_output=True, text=True)
            assert r.stdout.strip(), "应已产生 commit"
            ok2 = task_ingest.do_push()
            assert ok2 is True, "无变化应返回 True"
            r2 = subprocess.run(["git", "-C", repo, "rev-list", "--count", "HEAD"],
                                capture_output=True, text=True)
            n1 = r2.stdout.strip()
            doc["tasks"] = [{"task_id": "T-1"}]
            with open(os.path.join(td, "tasks_daily_5.json"), "w", encoding="utf-8") as f:
                json.dump(doc, f)
            ok3 = task_ingest.do_push()
            r3 = subprocess.run(["git", "-C", repo, "rev-list", "--count", "HEAD"],
                                capture_output=True, text=True)
            assert ok3 is True and r3.stdout.strip() != n1, "变化后应新增 commit"
            rec(results, "S1 拷贝+提交+幂等", True,
                "有变化→拷贝+commit+push；无变化→True 空转；再变化→新 commit")
        finally:
            task_ingest.HIDDEN = old_hidden
            if old_held is not None:
                os.environ["KANBAN_GIT_LOCK_HELD"] = old_held
    except Exception as e:
        rec(results, "S1 拷贝+提交+幂等", False, str(e)[:200])
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
                continue
            tid = rec.get("task_id") or ""
            if not isinstance(tid, str):
                tid = str(tid)
            tid = tid.strip()
            if not tid:
                print("[derive_closed_set] 空 task_id 零容忍：派生失败，不写出", flush=True)
                return False, "空 task_id"
            closed_at[tid.upper()] = str(rec.get("ts") or rec.get("closed_at") or "")
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
        tmp = dst.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dst)
        return True, f"写出 {len(closed_at)} 条"
    except OSError as e:
        print(f"[derive_closed_set] 写出失败：{e}", flush=True)
        return False, "写出失败"


def main():
    sys.path.insert(0, str(HIDDEN_SCRIPTS))
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

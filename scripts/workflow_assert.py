#!/usr/bin/env python3
"""Workflow 侧硬断言（DeepSeek 终审 B1 §2.3-2 / §7.4 修订 / B5 双写 / R3）。

由 .github/workflows/kanban-update.yml 在复制 data/*.json 到 public/data/ 后调用。
与生成端共用 scripts/_assert_lib.py 的同一份词表 + schema 定义。

任一失败 → exit 1 → workflow 步骤 fail，保留旧 public/（不部署坏数据）。
用法：cd kanban-deploy && python3 scripts/workflow_assert.py
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import _assert_lib

REPO_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(REPO_DIR, "data")

CHECKS = [
    ("data/stats_grade_5.json", _assert_lib.assert_stats_schema),
    ("data/stats_grade_6.json", _assert_lib.assert_stats_schema),
    ("data/points_log_5.json", _assert_lib.assert_points_log_schema),
    ("data/points_log_6.json", _assert_lib.assert_points_log_schema),
    ("data/shop_grade_5.json", _assert_lib.assert_shop_schema),
    ("data/shop_grade_6.json", _assert_lib.assert_shop_schema),
]


def main():
    failed = 0
    for rel, checker in CHECKS:
        path = os.path.join(REPO_DIR, rel)
        try:
            assert os.path.getsize(path) > 0, "文件缺失或为空"
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            checker(d, os.path.basename(path))
            print("OK " + rel)
        except AssertionError as e:
            print("::error::kanban data assertion failed: %s: %s" % (rel, e))
            failed += 1
        except Exception as e:
            print("::error::kanban data assertion failed: %s: %s(%s)"
                  % (rel, type(e).__name__, e))
            failed += 1
    if failed:
        print("::error::kanban data assertion failed: %d/6 文件未通过" % failed)
        return 1
    print("看板真实数据 6 文件断言全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

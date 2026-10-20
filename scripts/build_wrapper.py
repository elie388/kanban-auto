#!/usr/bin/env python3
"""GitHub Actions 包装器：适配 build_kanban.py 的路径约定。

流程：
  1. ledger/积分记录.md（fetch_ledger.py 拉取）→ 复制到 scripts/ 下供 build 用
  2. 运行原版 build_kanban.py（不修改原逻辑）
  3. 产物复制为 repo 根目录 index.html（供 FTP 上传）

原 build_kanban.py 的 OUTPUT_PATH 指向 ~/workspace，在 Actions 里不可写，
此处通过 sitecustomize 技巧重定向：直接调用其 main 逻辑前 monkey-patch 路径。
更稳妥：复制脚本并替换路径常量（保持原逻辑不变）。
"""
import os
import re
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
LEDGER_SRC = os.path.join(REPO_ROOT, "ledger", "积分记录.md")

# 原版脚本与模板由部署流程同步进 scripts/（见 README）
ORIG_BUILD = os.path.join(SCRIPTS_DIR, "build_kanban.py")
TEMPLATE_SRC = os.path.join(SCRIPTS_DIR, "积分看板.template.html")


def main():
    if not os.path.exists(LEDGER_SRC):
        print(f"ERROR: 台账不存在 {LEDGER_SRC}", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(ORIG_BUILD):
        print(f"ERROR: 生成器不存在 {ORIG_BUILD}", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(TEMPLATE_SRC):
        print(f"ERROR: 模板不存在 {TEMPLATE_SRC}", file=sys.stderr)
        sys.exit(2)

    # 1. 台账放到脚本同目录（build_kanban.py 的 BASE 约定）
    shutil.copy2(LEDGER_SRC, os.path.join(SCRIPTS_DIR, "积分记录.md"))

    # 2. 读取原脚本，替换 OUTPUT_PATH 为 repo 根 index.html
    with open(ORIG_BUILD, encoding="utf-8") as f:
        src = f.read()
    patched = src.replace(
        'OUTPUT_PATH = os.path.expanduser("~/workspace/积分看板.html")',
        'OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html")',
    )
    if patched == src:
        print("WARN: OUTPUT_PATH 替换未命中，检查原脚本路径常量", file=sys.stderr)
    patched_path = os.path.join(SCRIPTS_DIR, "_build_patched.py")
    with open(patched_path, "w", encoding="utf-8") as f:
        f.write(patched)

    # 3. 运行（cwd=scripts，保证相对路径行为一致）
    import subprocess
    r = subprocess.run(
        [sys.executable, patched_path],
        cwd=SCRIPTS_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(r.stdout)
    if r.stderr:
        print(r.stderr, file=sys.stderr)
    if r.returncode != 0:
        print(f"ERROR: 生成器退出码 {r.returncode}", file=sys.stderr)
        sys.exit(r.returncode)

    out = os.path.normpath(os.path.join(SCRIPTS_DIR, "..", "index.html"))
    if not os.path.exists(out):
        print(f"ERROR: 产物未生成 {out}", file=sys.stderr)
        sys.exit(1)

    # 完整性校验（DeepSeek 安全要求 2026-10-18）：部署前必须确认看板完整，
    # 防止截断/残缺的 HTML 被发布到 GitHub Pages
    _verify_output(out)

    print(f"OK: {out} ({os.path.getsize(out)} bytes)")


def _verify_output(path: str) -> None:
    """校验生成的 index.html 完整可用，任一失败即退出非零（阻断部署）。"""
    size = os.path.getsize(path)
    # 1. 体积下限：正常看板约 57KB，低于 10KB 视为异常
    if size < 10_000:
        print(f"ERROR: 完整性校验失败：文件过小 ({size} bytes)，疑似生成异常", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    # 2. 文档闭合：必须以 </html> 结尾，防止截断
    if not content.rstrip().endswith("</html>"):
        print("ERROR: 完整性校验失败：文档未以 </html> 闭合，疑似截断", file=sys.stderr)
        sys.exit(1)
    # 3. 关键标记：标题必须存在，防止模板渲染失败出空白页
    if "实时积分" not in content:
        print("ERROR: 完整性校验失败：缺少'实时积分'标题标记，疑似渲染失败", file=sys.stderr)
        sys.exit(1)
    print(f"完整性校验通过：{size} bytes，文档闭合，标题标记正常")


if __name__ == "__main__":
    # DEPRECATED 守卫（DeepSeek 审查 P2，2026-10-20）：旧生成链已停用
    # （workflow 注释明示保留为 fallback、不再调用），直接执行拒绝运行。
    raise SystemExit("DEPRECATED: build_wrapper.py 已停用（fallback 保留），禁止直接执行")
    main()

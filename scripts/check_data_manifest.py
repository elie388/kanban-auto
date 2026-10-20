#!/usr/bin/env python3
"""清单对账（2026-10-20 DeepSeek 方案，审计修复1）：
前端模板实际 fetch 的 data/*.json 集合 vs workflow 部署拷贝集合。
- 前端需要但 workflow 没拷贝 → exit 1（防 trends.json 式静默 404）
- workflow 拷贝但前端不用 → 仅警告（历史遗留，如 shop_grade_*.json 已停拉）
提取规则：
  前端：模板中 'data/<name>.json' 字面量 + 引号字面量 '<name>.json'
        + '<prefix>' + grade 动态拼名（展开为 _5/_6）
  部署：workflow yml 中出现的全部 <name>.json token
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TEMPLATE = REPO / "templates" / "积分看板_全新版式样板.html"
WORKFLOW = REPO / ".github" / "workflows" / "kanban-update.yml"


def frontend_set(tpl: str) -> set:
    out = set()
    out |= set(re.findall(r"data/([a-z0-9_]+\.json)", tpl))
    out |= set(re.findall(r"'([a-z][a-z0-9_]*\.json)'", tpl))
    # 动态拼名：'stats_grade_' + grade / 'points_log_' + grade
    for prefix in re.findall(r"'([a-z][a-z0-9_]*_)' \+ [a-zA-Z_]+", tpl):
        for g in ("5", "6"):
            out.add(f"{prefix}{g}.json")
    return out


def workflow_set(yml: str) -> set:
    """只认拷贝语义出现的 token（注释/说明里提到的不算，防假覆盖）：
    ① shell `cp ... data/<name>.json` 行  ② python 里 'data/<name>.json' 字面量
    （sections 通道的 files 字典键、trends 通道的 p 赋值均为此形态）。"""
    out = set()
    for line in yml.splitlines():
        if "cp " in line:
            out |= set(re.findall(r"data/([a-z0-9_]+\.json)", line))
    out |= set(re.findall(r"'data/([a-z0-9_]+\.json)'", yml))
    return out


def main() -> int:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    yml = WORKFLOW.read_text(encoding="utf-8")
    fe, wf = frontend_set(tpl), workflow_set(yml)
    missing = sorted(fe - wf)
    surplus = sorted(wf - fe)
    print(f"前端 fetch 集合（{len(fe)}）: {sorted(fe)}")
    print(f"workflow 部署集合（{len(wf)}）: {sorted(wf)}")
    for s in surplus:
        print(f"::warning:: workflow 拷贝了前端未使用的 {s}（遗留不拦截）")
    if missing:
        for m in missing:
            print(f"::error:: 前端 fetch {m} 但 workflow 未拷贝（线上将 404）")
        return 1
    print("清单对账通过：前端所需文件全部在部署清单内。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

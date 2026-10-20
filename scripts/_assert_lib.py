"""双端共用断言库（DeepSeek 终审 B5 / §7.4-3：生成端与 workflow 端双写同一份词表+schema）。

使用方：
  - 生成端：scripts/build_stats.py  (import _assert_lib)
  - 部署端：scripts/workflow_assert.py (import _assert_lib)，由 .github/workflows/kanban-update.yml 调用

词表扩充必须走规则手册修订流程，双写两端后才生效，任何人不得私自加词。
"""

import unicodedata

SCHEMA_VERSION = "1.0"

# points_log 专用 schema（KB-TIME-2026-10-19：time/time_source，1.0→1.1）
POINTS_LOG_SCHEMA_VERSION = "1.1"
TIME_SOURCE_ENUM = ("submit", "parent_confirm", "entry", "deadline")

# ---------------------------------------------------------------------------
# B5 禁用词表（大小写不敏感扫描，全角半角等价：NFKC 归一化后比对）
# ---------------------------------------------------------------------------
FORBIDDEN_TERMS = [
    # 结构类
    "<!--", "-->", "<<", ">>", "<", ">",
    # 键值类（含全角冒号）
    "key:", "key：", "注释",
    # 内部术语类
    "仲裁", "补录", "家长特批", "deepseek", "规则归类", "【", "】",
    # 审核代号类（B5 新增：孩子页不应出现审核黑话）
    "ge初审", "ge 初审", "ds终审", "ds 终审", "gemini",
]

# 预归一化词表（模块加载时一次算好）
_NORM_TERMS = []
for _t in FORBIDDEN_TERMS:
    _nt = unicodedata.normalize("NFKC", _t).lower()
    if _nt:
        _NORM_TERMS.append((_t, _nt))


def normalize_text(s):
    """NFKC 归一化 + 小写；失败返回 None（调用方按命中处理，保守策略）。"""
    try:
        return unicodedata.normalize("NFKC", s).lower()
    except Exception:
        return None


def find_forbidden_terms(reason):
    """扫描单条 reason，返回命中的词表条目原文列表（去重保序）。

    归一化失败视为命中 ["<normalize-failed>"]（保守策略，B5）。
    """
    if not isinstance(reason, str):
        return ["<non-string>"]
    norm = normalize_text(reason)
    if norm is None:
        return ["<normalize-failed>"]
    hits = []
    for orig, nt in _NORM_TERMS:
        if nt in norm and orig not in hits:
            hits.append(orig)
    return hits


# ---------------------------------------------------------------------------
# R3 / B1 schema 断言（生成端与 workflow 端共用同一份定义）
# ---------------------------------------------------------------------------
def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _check_grade(d, filename):
    import re as _re
    grade = d.get("grade")
    assert grade in (5, 6), "grade 须为 5 或 6，实际=%r" % (grade,)
    m = _re.search(r"_(\d)\.json$", filename)
    assert m and int(m.group(1)) == grade, \
        "文件名 %s 与 grade 字段 %r 不一致" % (filename, grade)


def assert_stats_schema(d, filename="stats_grade_?.json"):
    """stats_grade_{5,6}.json：§1.1 + B1 §2.3-2"""
    assert isinstance(d, dict), "顶层须为 object"
    assert d.get("schema_version") == SCHEMA_VERSION, "schema_version 须为 1.0"
    _check_grade(d, filename)
    assert _is_int(d.get("balance")), "balance 须为整数"
    assert isinstance(d.get("ledger_date"), str) and d["ledger_date"], "ledger_date 须为非空字符串"
    assert isinstance(d.get("generated_at"), str) and d["generated_at"], "generated_at 须为非空字符串"
    assert isinstance(d.get("source"), str) and d["source"], "source 须为非空字符串"
    cs = d.get("checksum")
    assert isinstance(cs, str) and len(cs) == 16, "checksum 须为 16 位 hex"


import re as _re

def assert_points_log_schema(d, filename="points_log_?.json"):
    """points_log_{5,6}.json：§1.2 + R3（含 filtered_count/filtered）+ B5 零命中 + KB-TIME 1.1"""
    assert isinstance(d, dict), "顶层须为 object"
    assert d.get("schema_version") == POINTS_LOG_SCHEMA_VERSION, "points_log schema_version 须为 1.1"
    _check_grade(d, filename)
    assert _is_int(d.get("filtered_count")), "filtered_count 须为整数"
    assert isinstance(d.get("generated_at"), str) and d["generated_at"], "generated_at 须为非空字符串"
    w = d.get("window")
    assert isinstance(w, dict), "window 须为 object"
    assert w.get("days") == 7 and w.get("max_items") == 20, "window 须为 {days:7, max_items:20}"  # P1#2（裁定2 甲 2026-10-20）：日志窗口统一近7天，与 build_stats 生成端同步
    items = d.get("items")
    assert isinstance(items, list), "items 须为数组"
    assert len(items) <= 20, "items 不得超过 20 条"
    for i, it in enumerate(items):
        assert isinstance(it, dict), "items[%d] 须为 object" % i
        assert isinstance(it.get("date"), str) and it["date"], "items[%d].date 异常" % i
        # KB-TIME 1.1：time/time_source（G3 双向一致）
        tm = it.get("time")
        tsrc = it.get("time_source")
        assert (tm is None) == (tsrc is None), "items[%d] time/time_source 不一致" % i
        if tm is not None:
            assert _re.match(r"^([01]\d|2[0-3]):[0-5]\d$", tm), "items[%d].time 格式非法" % i
            assert tsrc in TIME_SOURCE_ENUM, "items[%d].time_source 枚举外" % i
        reason = it.get("reason")
        assert isinstance(reason, str) and reason, "items[%d].reason 异常" % i
        assert len(reason) <= 41, "items[%d].reason 超长（>41）" % i  # 40 + "…"
        assert _is_int(it.get("pts")), "items[%d].pts 须为整数" % i
        assert it.get("sign") in ("plus", "minus"), "items[%d].sign 异常" % i
        assert isinstance(it.get("filtered"), bool), "items[%d].filtered 须为 bool" % i
        if it["pts"] > 0:
            assert it["sign"] == "plus", "items[%d] sign 与 pts 符号不一致" % i
        elif it["pts"] < 0:
            assert it["sign"] == "minus", "items[%d] sign 与 pts 符号不一致" % i
        hits = find_forbidden_terms(reason)
        assert not hits, "items[%d].reason 命中禁用词 %r" % (i, hits)


def assert_shop_schema(d, filename="shop_grade_?.json"):
    """shop_grade_{5,6}.json：R1 实现约束（§1.5 修订）"""
    assert isinstance(d, dict), "顶层须为 object"
    assert d.get("schema_version") == SCHEMA_VERSION, "schema_version 须为 1.0"
    _check_grade(d, filename)
    tiers = d.get("tiers")
    assert isinstance(tiers, list) and len(tiers) > 0, "tiers 须为非空数组"
    for i, t in enumerate(tiers):
        assert isinstance(t, dict), "tiers[%d] 须为 object" % i
        assert _is_int(t.get("pts")), "tiers[%d].pts 须为整数" % i
        assert isinstance(t.get("name"), str) and t["name"], "tiers[%d].name 异常" % i
        assert t.get("kind") in ("Minecraft", "和平精英", "愿望"), "tiers[%d].kind 异常" % i
        hits = find_forbidden_terms(t["name"])
        assert not hits, "tiers[%d].name 命中禁用词 %r" % (i, hits)
    assert _is_int(d.get("current_balance")), "current_balance 须为整数"
    assert d.get("rule_source") == "规则手册第85行", "rule_source 须为规则手册第85行"
    assert isinstance(d.get("generated_at"), str) and d["generated_at"], "generated_at 须为非空字符串"

#!/usr/bin/env python3
"""sync_sections.py 跨周边界定值单测（B-2）。
固定时钟验证：周日 23:59:59 的徽章属本周；周一 00:00:00 起属下周；
跨周切换瞬间不产生半周数据（上周徽章不混入本周）。
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_sections as ss

BEIJING = ss.BEIJING
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# 固定时钟：2026-10-25 周日 23:59:59（Asia/Shanghai）
sunday_night = datetime(2026, 10, 25, 23, 59, 59, tzinfo=BEIJING)
ws, we = ss.week_bounds(sunday_night)
check("周日23:59:59 week_start=2026-10-19",
      ws.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-19 00:00:00")
check("周日23:59:59 week_end=2026-10-25",
      we.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-25 23:59:59")

# 固定时钟：2026-10-26 周一 00:00:00
monday_morning = datetime(2026, 10, 26, 0, 0, 0, tzinfo=BEIJING)
ws2, we2 = ss.week_bounds(monday_morning)
check("周一00:00:00 week_start=2026-10-26",
      ws2.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-26 00:00:00")
check("周一00:00:00 week_end=2026-11-01",
      we2.strftime("%Y-%m-%d %H:%M:%S") == "2026-11-01 23:59:59")

# 边界归属：上周日徽章在周日夜属本周、在周一晨属上周（不混入）
events = {"g5": [{"key": "xueba", "name": "学霸", "icon": "👑", "date": "2026-10-25"}],
          "g6": []}
d1 = ss.build_badges(events, now=sunday_night)
check("周日夜：10-25徽章在本周",
      len(d1["by_grade"]["g5"]) == 1)
check("周日夜：week元数据含tz与起止",
      d1["week"]["tz"] == "Asia/Shanghai"
      and d1["week"]["week_start"].startswith("2026-10-19T00:00:00")
      and d1["week"]["week_end"].startswith("2026-10-25T23:59:59"))
d2 = ss.build_badges(events, now=monday_morning)
check("周一晨：10-25徽章已不在本周（无半周数据）",
      len(d2["by_grade"]["g5"]) == 0)
check("周一晨：week起止已切换",
      d2["week"]["start"] == "2026-10-26" and d2["week"]["end"] == "2026-11-01")

# N-1（2026-10-20 改真断言，原用例仅断言 callable、注释自承假绿）：
# 合成台账实测 extract_badge_events 的冲正语义——
# ① 颁发→冲正 抵消为 0；② 重复冲正幂等（不异常、仍为 0）；
# ③ 冲正先于原始事件 → 冲正被跳过（no-op），其后颁发正常计 1；
# ④ 同 key 同日同描述、完整 key 不同 → 不误杀（满分之星同日两场考试计 2），
#    完整 key 完全相同 → 去重为 1 条；
# ⑤ cycle_week：带落账字段 → recorded=True 且用记录值；无字段 → recorded=False
#    且按日期推算（展示侧推算口径本身保留，仅计数侧口径另行对齐）。
import tempfile
_N1_LEDGER = """## 五年级

| 日期 | 描述 | 积分 | 余额 |
| --- | --- | --- | --- |
| 10-19 | ⚡三合一快手徽章 | +5 | 105 | <!-- key:五年级_2026-10-19_badge_sanheyi_三合一 -->
| 10-19 | 冲正三合一快手 | -5 | 100 | <!-- key:五年级_2026-10-19_reverse_badge_sanheyi_冲正 -->
| 10-19 | 📝默写之星徽章 | +5 | 105 | <!-- key:五年级_2026-10-19_badge_moxiezhixing_默写 -->
| 10-19 | 冲正默写之星 | -5 | 100 | <!-- key:五年级_2026-10-19_reverse_badge_moxiezhixing_冲正 -->
| 10-19 | 冲正默写之星（重复） | -5 | 95 | <!-- key:五年级_2026-10-19_reverse_badge_moxiezhixing_冲正二 -->
| 10-19 | 冲正单词之星（先于原始） | -5 | 90 | <!-- key:五年级_2026-10-19_reverse_badge_dancizhixing_冲正 -->
| 10-20 | 🔤单词之星徽章 | +5 | 95 | <!-- key:五年级_2026-10-20_badge_dancizhixing_单词 -->

## 六年级

| 日期 | 描述 | 积分 | 余额 |
| --- | --- | --- | --- |
| 10-19 | 💯满分之星徽章 | +5 | 105 | <!-- key:六年级_2026-10-19_badge_baifentupo_满分甲 -->
| 10-19 | 💯满分之星徽章 | +5 | 110 | <!-- key:六年级_2026-10-19_badge_baifentupo_满分乙 -->
| 10-19 | 💯满分之星徽章 | +5 | 115 | <!-- key:六年级_2026-10-19_badge_baifentupo_满分甲 -->
| 10-20 | 📆三日不断徽章 | +5 | 120 | <!-- key:六年级_2026-10-20_badge_sanribuduan_三日不断 | cycle_week=2026-W43 -->
| 10-16 | 👑学霸徽章 | +10 | 130 | <!-- key:六年级_2026-10-16_badge_xueba_ -->
"""
_fd, _tp = tempfile.mkstemp(suffix=".md")
with open(_fd, "w", encoding="utf-8") as _f:
    _f.write(_N1_LEDGER)
_orig_ledger = ss.LEDGER
try:
    ss.LEDGER = Path(_tp)
    _ev = ss.extract_badge_events()
finally:
    ss.LEDGER = _orig_ledger
    os.unlink(_tp)
_g5 = {}
for _e in _ev["g5"]:
    _g5[_e["key"]] = _g5.get(_e["key"], 0) + 1
check("N-1 冲正抵消：颁发→冲正 = 0", _g5.get("sanheyi", 0) == 0)
check("N-1 重复冲正幂等：颁发→冲正→冲正 = 0 不异常", _g5.get("moxiezhixing", 0) == 0)
check("N-1 冲正先行被跳过：冲正→颁发 = 1", _g5.get("dancizhixing", 0) == 1)
_g6 = {}
for _e in _ev["g6"]:
    _g6.setdefault(_e["key"], []).append(_e)
check("N-1 同日同描述不同完整key不误杀+同key去重：满分之星 = 2",
      len(_g6.get("baifentupo", [])) == 2)
_sr = _g6.get("sanribuduan", [{}])[0]
check("N-1 cycle_week 落账字段：recorded=True 且用记录值",
      _sr.get("cycle_week_recorded") is True and _sr.get("cycle_week") == "2026-W43")
_xb = _g6.get("xueba", [{}])[0]
check("N-1 cycle_week 历史行：recorded=False 且按日期推算",
      _xb.get("cycle_week_recorded") is False
      and _xb.get("cycle_week") == ss.cycle_week_of("2026-10-16"))

# B方案（2026-10-20 用户拍板+DeepSeek 终审）：补发徽章同带展示+「补发」角标。
# 构造事件复刻 2026-10-20 今晚实况：W43 三日不断（本周期归属）+ W42 补发
# 两枚（late_cycle=W42、落账日期 10-20 属 W43）。
_LATE_EVENTS = {
    "g5": [
        {"key": "sanribuduan", "name": "三日不断", "icon": "📆",
         "date": "2026-10-20", "cycle_week": "2026-W43",
         "cycle_week_recorded": True, "late_cycle": None,
         "late_cycle_recorded": False},
        {"key": "jiawuxiaobangshou", "name": "家务小帮手", "icon": "🏅",
         "date": "2026-10-20", "cycle_week": "2026-W42",
         "cycle_week_recorded": True, "late_cycle": "2026-W42",
         "late_cycle_recorded": True},
        {"key": "sanheyikuaishou", "name": "三合一快手", "icon": "🏅",
         "date": "2026-10-20", "cycle_week": "2026-W42",
         "cycle_week_recorded": True, "late_cycle": "2026-W42",
         "late_cycle_recorded": True},
    ],
    "g6": [
        {"key": "sanribuduan", "name": "三日不断", "icon": "📆",
         "date": "2026-10-20", "cycle_week": "2026-W43",
         "cycle_week_recorded": True, "late_cycle": None,
         "late_cycle_recorded": False},
        {"key": "jiawuxiaobangshou", "name": "家务小帮手", "icon": "🏅",
         "date": "2026-10-20", "cycle_week": "2026-W42",
         "cycle_week_recorded": True, "late_cycle": "2026-W42",
         "late_cycle_recorded": True},
    ],
}
_w43_now = datetime(2026, 10, 20, 23, 30, 0, tzinfo=BEIJING)
_b43 = ss.build_badges(_LATE_EVENTS, now=_w43_now)
_g5_items = {it["key"]: it for it in _b43["by_grade"]["g5"]}
_g6_items = {it["key"]: it for it in _b43["by_grade"]["g6"]}
check("B补发 W43视角：五年级带内 3 枚（1本周+2补发）",
      len(_b43["by_grade"]["g5"]) == 3)
check("B补发 W43视角：六年级带内 2 枚（1本周+1补发）",
      len(_b43["by_grade"]["g6"]) == 2)
check("B补发 W43视角：本周归属徽章无 late 标记",
      "late" not in _g5_items.get("sanribuduan", {}))
check("B补发 W43视角：补发带 late=True 且 origin_cycle=W42",
      _g5_items.get("jiawuxiaobangshou", {}).get("late") is True
      and _g5_items.get("jiawuxiaobangshou", {}).get("origin_cycle") == "2026-W42"
      and _g5_items.get("sanheyikuaishou", {}).get("late") is True
      and _g6_items.get("jiawuxiaobangshou", {}).get("late") is True)
check("B补发 W43视角：补发不并入本周同 key 的 count",
      _g5_items.get("sanribuduan", {}).get("count") == 1)

# 过渡周防误标：W44 周一视角下，W42 补发与 W43 归属徽章都不得出现在
# W44 带内（补发只在其颁发所在周期 W43 展示）。
_w44_now = datetime(2026, 10, 26, 0, 5, 0, tzinfo=BEIJING)
_b44 = ss.build_badges(_LATE_EVENTS, now=_w44_now)
check("B补发 W44视角：上周补发不出现、不误标为本周归属",
      _b44["by_grade"]["g5"] == [] and _b44["by_grade"]["g6"] == []
      and _b44["week"]["cycle_week"] == "2026-W44")

# W44 内新发生的补发（归属 W43、W44 颁发）应在 W44 带内带角标展示。
_LATE_W44 = {
    "g5": [{"key": "moxiezhixing", "name": "默写之星", "icon": "📝",
            "date": "2026-10-27", "cycle_week": "2026-W43",
            "cycle_week_recorded": True, "late_cycle": "2026-W43",
            "late_cycle_recorded": True}],
    "g6": [],
}
_b44b = ss.build_badges(_LATE_W44, now=datetime(2026, 10, 27, 12, 0, 0,
                                               tzinfo=BEIJING))
_it = (_b44b["by_grade"]["g5"] or [{}])[0]
check("B补发 W44视角：本周期内颁发的补发同带展示带角标",
      len(_b44b["by_grade"]["g5"]) == 1 and _it.get("late") is True
      and _it.get("origin_cycle") == "2026-W43")

# P3（DeepSeek 终审条件）：同 key 同归属周多笔补发 -> 带内合并为一条目、
# count 累加（×N 与「补发」角标并存），不产生两个独立 late 条目。
_LATE_MULTI = {
    "g5": [
        {"key": "jiawuxiaobangshou", "name": "家务小帮手", "icon": "🏅",
         "date": "2026-10-20", "cycle_week": "2026-W42",
         "cycle_week_recorded": True, "late_cycle": "2026-W42",
         "late_cycle_recorded": True},
        {"key": "jiawuxiaobangshou", "name": "家务小帮手", "icon": "🏅",
         "date": "2026-10-21", "cycle_week": "2026-W42",
         "cycle_week_recorded": True, "late_cycle": "2026-W42",
         "late_cycle_recorded": True},
    ],
    "g6": [],
}
_bm = ss.build_badges(_LATE_MULTI, now=_w43_now)
_bm_items = [it for it in _bm["by_grade"]["g5"]
             if it["key"] == "jiawuxiaobangshou"]
check("B补发 P3：同key多笔补发合并一条目 count=2 且带 late",
      len(_bm_items) == 1 and _bm_items[0].get("count") == 2
      and _bm_items[0].get("late") is True
      and _bm_items[0].get("origin_cycle") == "2026-W42")

# extract 层：合成台账验证 late_cycle 字段解析（带标记=True、无标记=False）。
_B_LEDGER = """## 五年级

| 日期 | 描述 | 积分 | 余额 |
| --- | --- | --- | --- |
| 10-20 | 🏅家务小帮手徽章 | +5 | 253 | <!-- key:五年级_2026-10-20_badge_jiawuxiaobangshou_家务小帮手 @22:45 | cycle_week=2026-W42 | late_cycle=2026-W42 -->
| 10-20 | 📆三日不断徽章 | +5 | 258 | <!-- key:五年级_2026-10-20_badge_sanribuduan_三日不断 @17:51 | cycle_week=2026-W43 -->

## 六年级

| 日期 | 描述 | 积分 | 余额 |
| --- | --- | --- | --- |
| 10-20 | 📆三日不断徽章 | +5 | 120 | <!-- key:六年级_2026-10-20_badge_sanribuduan_三日不断 @17:51 | cycle_week=2026-W43 -->
"""
_fd2, _tp2 = tempfile.mkstemp(suffix=".md")
with open(_fd2, "w", encoding="utf-8") as _f:
    _f.write(_B_LEDGER)
try:
    ss.LEDGER = Path(_tp2)
    _ev2 = ss.extract_badge_events()
finally:
    ss.LEDGER = _orig_ledger
    os.unlink(_tp2)
_ev2_g5 = {e["key"]: e for e in _ev2["g5"]}
check("B补发 extract：late_cycle 解析（值=W42、recorded=True）",
      _ev2_g5.get("jiawuxiaobangshou", {}).get("late_cycle") == "2026-W42"
      and _ev2_g5.get("jiawuxiaobangshou", {}).get("late_cycle_recorded") is True)
check("B补发 extract：无 late_cycle 行 recorded=False",
      _ev2_g5.get("sanribuduan", {}).get("late_cycle_recorded") is False
      and _ev2_g5.get("sanribuduan", {}).get("late_cycle") is None)
# extract->build 端到端：真实口径事件喂 W43 视角应得 2 枚（1+1补发）
_b_e2e = ss.build_badges(_ev2, now=_w43_now)
check("B补发 端到端：五年级 2 枚且补发带标记",
      len(_b_e2e["by_grade"]["g5"]) == 2
      and {it["key"]: it for it in _b_e2e["by_grade"]["g5"]}
          .get("jiawuxiaobangshou", {}).get("late") is True)

print("----")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL PASS")

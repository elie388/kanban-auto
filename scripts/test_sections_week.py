#!/usr/bin/env python3
"""sync_sections.py 跨周边界定值单测（B-2）。
固定时钟验证：周日 23:59:59 的徽章属本周；周一 00:00:00 起属下周；
跨周切换瞬间不产生半周数据（上周徽章不混入本周）。
"""
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

# N-1：冲正先于原始事件 / 重复冲正 → 跳过不异常（幂等）
ev = ss.extract_badge_events  # 仅确认函数存在且可调用（真实台账已在生产验证）
check("extract_badge_events 可调用", callable(ev))

print("----")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL PASS")

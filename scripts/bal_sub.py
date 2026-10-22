# -*- coding: utf-8 -*-
"""余额卡小字文案生成器（2026-10-22 家长定：脚本化，增减兑换项自动更新）。

口径：
- 兑换目录（build_stats.SHOP_TIERS）未变（价格序列与现行一致）时，
  按家长拍板的第二版分档表逐字输出（PINNED_TIERS）。
- 目录一旦增减/改价，自动改走通用句式：按余额相对目录的位置
  （未起步／未够第一档／已够某档且下一档在望／全部够着）拼句，
  句中项目名取目录现行名称的短称，不留空、不报错。
"""

# 现行目录价格序列（与 build_stats.SHOP_TIERS 对齐；变更即视为目录已改版）
PINNED_COSTS = [70, 100, 150, 250, 600, 1000]

# 第二版定稿（家长 2026-10-22 过目版；deepseek 出稿、Muse 按档位改对三句）
# (下限含, 上限含或 None, {年级: 文案})
PINNED_TIERS = [
    (None, 0,    {5: "从坑里爬出来，就是新起点。", 6: "前面的路还长，慢慢走回来。"}),
    (1,    49,   {5: "攒着攒着，小树苗就冒头了。", 6: "零钱罐叮当响，好开始。"}),
    (50,   99,   {5: "再攒一小把，半小时游戏到手。", 6: "离七十不远啦，半小时先过把瘾。"}),
    (100,  149,  {5: "一小时Minecraft，已经攥在手里啦。", 6: "手里的分，能换一段好时光了。"}),
    (150,  249,  {5: "和平精英一小时，或者再攒攒。", 6: "两个钟头方块世界，已经换得起啦。"}),
    (250,  599,  {5: "两小时吃鸡，够打几局痛快了。", 6: "五十块的心愿，正一点点靠近。"}),
    (600,  999,  {5: "一百块愿望在望，再加把油。", 6: "大愿望的门，已经推开一半了。"}),
    (1000, None, {5: "一百块心愿，你说了算！", 6: "攒到这份上，挑个大件吧！"}),
]

# 短称已收口到 shop_catalog.json 的 short 字段（2026-10-22 目录收口），此处不再另存副本。

_GENERIC = {
    "start":      {5: "从坑里爬出来，就是新起点。", 6: "前面的路还长，慢慢走回来。"},
    "below_first": {5: "攒着攒着，小树苗就冒头了。", 6: "零钱罐叮当响，好开始。"},
    "mid":        {5: "{prev}已经到手，{next}也不远啦。", 6: "{prev}换得起啦，{next}正靠近。"},
    "top":        {5: "想要的都够得着，挑个大件吧！", 6: "攒到这份上，你说了算！"},
}


def _short_name(tier):
    short = str(tier.get("short", "")).strip()
    if short:
        return short
    name = str(tier.get("name", "")).replace("\n", "").strip()
    return name or "这一档"


def compute_bal_sub(grade, balance, shop_tiers):
    """grade: 5/6；balance: int；shop_tiers: [{'pts':int,'name':str}, ...]"""
    tiers = sorted(shop_tiers, key=lambda t: t["pts"])
    costs = [t["pts"] for t in tiers]
    if costs == PINNED_COSTS:
        for lo, hi, lines in PINNED_TIERS:
            if (lo is None or balance >= lo) and (hi is None or balance <= hi):
                return lines[grade]
        return lines[grade]  # pragma: no cover（区间全覆盖，不应到达）
    # 通用句式（目录已增减/改价）
    if balance <= 0:
        return _GENERIC["start"][grade]
    if not tiers or balance < tiers[0]["pts"]:
        return _GENERIC["below_first"][grade]
    prev = None
    nxt = None
    for t in tiers:
        if t["pts"] <= balance:
            prev = t
        elif nxt is None:
            nxt = t
    if nxt is None:
        return _GENERIC["top"][grade]
    return _GENERIC["mid"][grade].format(prev=_short_name(prev), next=_short_name(nxt))

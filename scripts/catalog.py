# -*- coding: utf-8 -*-
"""目录单一数据源加载器（2026-10-22 目录收口，deepseek 路线第一批 3）。

兑换目录 scripts/shop_catalog.json、徽章目录 scripts/badges.json 为唯一源；
sync_sections / build_stats / bal_sub 一律经本模块读取，禁止再在代码里
另存副本。加载期硬校验（原 N1-E2 口径）：cost 缺失/非数值/<=0、徽章 key
重复、别名指向不存在的 key，一律模块加载即熔断。
"""
import hashlib
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_SHOP_PATH = _DIR / "shop_catalog.json"
_BADGES_PATH = _DIR / "badges.json"


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def shop_catalog_hash():
    return file_hash(_SHOP_PATH)


def shop_tiers_raw():
    return _load(_SHOP_PATH)["tiers"]


def shop_table():
    """sync_sections 兼容形：[(id, name, cost, icon), ...]"""
    out = []
    for t in shop_tiers_raw():
        cost = t.get("cost")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
            raise ValueError("E2 商店 cost 配置错误：%s cost=%r" % (t.get("id"), cost))
        out.append((t["id"], t["name"], cost, t["icon"]))
    return out


def shop_tiers():
    """build_stats 兼容形：[{pts, name, kind, short}, ...]（name 用 stats_name）"""
    return [{"pts": t["cost"], "name": t["stats_name"], "kind": t["kind"],
             "short": t["short"]} for t in shop_tiers_raw()]


def badge_catalog():
    """sync_sections 兼容形：[(key, name, icon, cond), ...]"""
    data = _load(_BADGES_PATH)
    out = [(b["key"], b["name"], b["icon"], b["cond"]) for b in data["badges"]]
    keys = [k for k, _, _, _ in out]
    if len(keys) != len(set(keys)):
        raise ValueError("徽章目录 key 重复")
    return out


def legacy_badges():
    """sync_sections 兼容形：{key: (name, icon)}"""
    data = _load(_BADGES_PATH)
    return {b["key"]: (b["name"], b["icon"]) for b in data["legacy"]}


def key_alias():
    data = _load(_BADGES_PATH)
    aliases = dict(data["aliases"])
    valid = {b["key"] for b in data["badges"]}
    for src, dst in aliases.items():
        if dst not in valid:
            raise ValueError("徽章别名指向不存在的 key：%s -> %s" % (src, dst))
    return aliases


def badge_keys():
    return {b["key"] for b in _load(_BADGES_PATH)["badges"]}

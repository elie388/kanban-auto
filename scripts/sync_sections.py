#!/usr/bin/env python3
"""看板全栏目实时同步器：台账/规则手册 -> data/*.json。

KB-SECTIONS-AUTO-2026-10-19，DeepSeek 条文级方案落地。
只读台账与规则手册，唯一写出 data/badges.json、data/badge_goals.json、
data/shop.json、data/rules.json；不碰积分台账内容、point_ledger、broker。

退出码：0=无变更或推送成功 / 2=解析失败（熔断，未推送）/ 3=推送失败 / 4=锁冲突
"""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_balance as sb  # 复用 parse_ledger / norm_date / clean_desc / BEIJING（无副作用）

REPO = Path(__file__).resolve().parent.parent
LEDGER = sb.LEDGER
RULEBOOK = REPO.parent / "规则手册.md"
DATA_DIR = REPO / "data"
LOG_FILE = REPO / "logs" / "sync_sections.log"
FAIL_LOG = REPO / "logs" / "sync_sections.fail"
LOCK_FILE = Path("/tmp/sync_sections.lock")
BEIJING = sb.BEIJING
LOG_KEEP_LINES = 2000

# ---------- 24 枚徽章目录（v3.7 定稿，一律 +5/次）+ 历史 key 兼容 ----------
# key: 台账 badge_<key> 中的拼音；cond: 解锁条件（目标榜展示用）
BADGE_CATALOG = [
    ("quanduizhixing",   "全对之星",   "✅", "单次作业/小测全部题目正确"),
    ("baifentupo",       "百分突破之星", "💯", "单次考试首次卷面满分"),
    ("zonghezhixing",    "综合之星",   "🏅", "一周内≥4类不同徽章"),
    ("tupoxiaoyongshi",  "突破小勇士", "🛡️", "同一类错题连续3次订正全对"),
    ("jianchixiaoyongshi","坚持小勇士", "🔥", "连续14天完成同一固定挑战"),
    ("yueduzhixing",     "阅读之星",   "📖", "周打卡≥6天+整本书+笔记+录音复述"),
    ("yueduxiaodaren",   "阅读小达人", "📚", "周打卡≥5天+累计≥2本+笔记/口述"),
    ("hezuozhixing",     "合作之星",   "🤝", "一周内≥2次主动合作（学校场景）"),
    ("zhoujinbuzhixing", "周进步之星", "🚀", "正确率/分数较上周明显进步"),
    ("sanheyi",          "三合一快手", "⚡", "同日独立完成3项不同基础家务"),
    ("jiawujielishou",   "家务接力手", "🔄", "连续3天每天≥1项家务"),
    ("sanribuduan",      "三日不断",   "📆", "连续3天不断档/全勤一周取高档"),
    ("kousuanxiaodaren", "口算小达人", "🧮", "连续7天口算打卡正确率≥90%"),
    ("shuxiezhixing",    "书写之星",   "✍️", "连续N次作业书写零扣分"),
    ("dancizhixing",     "单词之星",   "🔤", "单次单词听写全对（≥10词）"),
    ("moxiezhixing",     "默写之星",   "📝", "单次古诗文/课文默写全对"),
    ("sikaozhixing",     "思考之星",   "🧠", "作业有可辨识推导文字+题号回链"),
    ("zhenglizhixing",   "整理之星",   "🗂️", "连续5天自主整理书包/书桌"),
    ("jinbumiao",        "进步小苗",   "🌱", "单科单次作业正确率较上次+15百分点"),
    ("cuotizhenglizhixing","错题整理之星","📓", "一周错题本整理≥5题且分类正确"),
    ("biaodazhixing",    "表达之星",   "🗣️", "一周内口头表达/复述≥2次"),
    ("jiawuxiaobangshou","家务小帮手", "🏠", "单次完成1项基础家务"),
    ("zhengdalidaren",   "整理小达人", "🧹", "周内累计5天整理类家务"),
    ("jiawuzhixing",     "家务之星",   "🏆", "周内家务≥10次且≥4类"),
]
# 历史徽章 key（旧体系，台账中可能出现；展示用，不在目标榜目录内）
LEGACY_BADGES = {
    "xueba": ("学霸", "👑"),
    "lingshiwu": ("零失误", "🛡️"),
    "baifen": ("百分俱乐部", "💰"),
    "quanduixiaodaren": ("全对小达人", "🎯"),
    "shoushixiaodaren": ("守时小达人", "⏰"),
    "manfenchaoren": ("满分超人", "💯"),
    "dingzhengxiaonengshou": ("订正小能手", "✏️"),
    "shuxiexiaodaren": ("书写小达人", "✍️"),
    "quanqinxiaobiaobing": ("全勤小标兵", "📅"),
}
BADGE_MAP = {k: {"name": n, "icon": i} for k, n, i, _ in BADGE_CATALOG}
BADGE_MAP.update({k: {"name": n, "icon": i} for k, (n, i) in LEGACY_BADGES.items()})

# ---------- 积分商店（规则手册兑换表） ----------
# (id, name, cost, icon)
SHOP_TABLE = [
    ("mc30",   "30分钟Minecraft", 70, "🕹️"),
    ("mc60",   "1小时Minecraft", 100, "🕹️"),
    ("hy150",  "1小时和平精英或2小时Minecraft", 150, "🎮"),
    ("hy250",  "2小时和平精英", 250, "🎮"),
    ("wish600",  "达成愿望1次（等额50元，可兑现金）", 600, "🌟"),
    ("wish1000", "达成愿望1次（等额100元，可兑现金）", 1000, "💫"),
]

KEY_PAT = re.compile(r"key:[^|]*?badge_([a-z]+)_")
REV_PAT = re.compile(r"key:[^|]*?reverse_badge_([a-z]+)_")
HTML_COMMENT = re.compile(r"<!--.*?-->")


def log(status, reason="", extra=None):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(BEIJING).isoformat(timespec="seconds"),
           "status": status, "reason": reason}
    if extra:
        rec.update(extra)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > LOG_KEEP_LINES:
            LOG_FILE.write_text("\n".join(lines[-LOG_KEEP_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def fail(msg):
    FAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(FAIL_LOG, "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (datetime.now(BEIJING).isoformat(timespec="seconds"), msg[:200]))


def split_icon_name(desc):
    """从事项文本提取图标与徽章名；兜底返回通用值。"""
    d = HTML_COMMENT.sub("", desc)
    i = 0
    while i < len(d) and not ("\u4e00" <= d[i] <= "\u9fff" or d[i].isalnum()):
        i += 1
    icon = d[:i].strip() or "🏅"
    rest = re.split(r"[（(]", d[i:])[0].replace("徽章", "").strip()
    name = rest[:8] if rest else "徽章"
    return icon, name


def extract_badge_events():
    """解析台账，返回 {g5: [event...], g6: [event...]}；event={key,date,icon,name}。
    冲正（reverse_badge_）按行序抵消：后发生的冲正取消对应徽章。"""
    content = LEDGER.read_text(encoding="utf-8")
    result = {}
    for gk, grade in (("g5", "五年级"), ("g6", "六年级")):
        m = re.search(r"## " + grade + r"\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
        if not m:
            raise ValueError("section 缺失：" + grade)
        active = {}   # key -> event（净持有）
        earliest = {}  # key -> 最早发放日
        for line in m.group(1).split("\n"):
            mm = sb.ROW_PAT.match(line)
            if not mm:
                continue
            date = sb.norm_date(mm.group(1))
            desc = mm.group(2)
            rev = REV_PAT.search(line)
            if rev:
                active.pop(rev.group(1), None)
                continue
            b = KEY_PAT.search(line)
            if not b:
                continue
            key = b.group(1)
            meta = BADGE_MAP.get(key)
            if meta:
                icon, name = meta["icon"], meta["name"]
            else:
                icon, name = split_icon_name(desc)
            if key not in earliest or date < earliest[key]:
                earliest[key] = date
            active[key] = {"key": key, "date": date, "icon": icon, "name": name}
        # 净持有徽章，date 取最早发放日
        events = []
        for key, ev in active.items():
            ev = dict(ev)
            ev["date"] = earliest.get(key, ev["date"])
            events.append(ev)
        events.sort(key=lambda e: e["date"])
        result[gk] = events
    return result


def week_bounds(now):
    """给定时刻 -> (week_start, week_end)；周一起始，Asia/Shanghai。
    边界语义：周日 23:59:59 仍属本周；周一 00:00:00 起属下周。"""
    ws = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    we = ws + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return ws, we


def build_badges(events, now=None):
    now = now or datetime.now(BEIJING)
    ws, we = week_bounds(now)
    ws_s, we_s = ws.strftime("%Y-%m-%d"), we.strftime("%Y-%m-%d")
    by_grade = {}
    for gk in ("g5", "g6"):
        week_events = [e for e in events.get(gk, []) if ws_s <= e["date"] <= we_s]
        by_grade[gk] = [{"key": e["key"], "name": e["name"][:8], "icon": e["icon"],
                         "date": e["date"]} for e in week_events]
    # B-2：周界自证元数据（下游可独立验证周界归属）
    return {"schema_version": 1,
            "week": {"start": ws_s, "end": we_s, "tz": "Asia/Shanghai",
                     "week_start": ws.isoformat(timespec="seconds"),
                     "week_end": we.isoformat(timespec="seconds")},
            "by_grade": by_grade}


def build_badge_goals(events):
    catalog = [{"key": k, "name": n, "icon": i, "cond": c, "points": 5}
               for k, n, i, c in BADGE_CATALOG]
    by_grade = {}
    for gk in ("g5", "g6"):
        held = {e["key"]: e["date"] for e in events.get(gk, [])}
        by_grade[gk] = [{"key": k, "obtained": k in held,
                         "date": held.get(k, "")} for k, _, _, _ in BADGE_CATALOG]
    return {"schema_version": 1, "catalog": catalog, "by_grade": by_grade}


def build_shop(balances):
    items = []
    for sid, name, cost, icon in SHOP_TABLE:
        bg = {}
        for gk in ("g5", "g6"):
            bal = balances[gk]
            prog = min(bal / cost, 1.0) if cost > 0 else 0.0
            bg[gk] = {"balance": bal, "progress": round(prog, 4),
                      "redeemable": bal >= cost}
        items.append({"id": sid, "name": name, "cost": cost, "icon": icon,
                      "by_grade": bg})
    return {"schema_version": 1, "items": items}


def parse_rules():
    """解析规则手册 dashboard 白名单块；缺失任一块视为失败（熔断）。"""
    content = RULEBOOK.read_text(encoding="utf-8")
    out = {}
    for key, tag in (("add", "dashboard:add"), ("deduct", "dashboard:deduct")):
        m = re.search(r"<!-- " + tag + r" -->(.*?)<!-- /" + tag + r" -->",
                      content, re.DOTALL)
        if not m:
            raise ValueError("规则手册缺少白名单块：" + tag)
        items = []
        for line in m.group(1).split("\n"):
            line = line.strip()
            if line.startswith("- "):
                text = line[2:].strip()
                if text:
                    items.append(text)
        if not items:
            raise ValueError("白名单块为空：" + tag)
        out[key] = [{"id": "%s%d" % (key[0], i + 1), "text": t}
                    for i, t in enumerate(items)]
    return {"schema_version": 1, "source_version": "规则手册.md", **out}


def check_no_pii(obj):
    """PII 熔断：序列化后不得含姓名模式。"""
    s = json.dumps(obj, ensure_ascii=False)
    if re.search(r"[\u4e00-\u9fff]{2,3}(同学|小朋友)", s):
        raise ValueError("PII 熔断：检测到疑似姓名")
    if "<!--" in s or "<script" in s.lower():
        raise ValueError("注入熔断：检测到 HTML/脚本残留")


def run_git(*args):
    return subprocess.run(["git", "-C", str(REPO)] + list(args),
                          capture_output=True, text=True, timeout=60)


def main():
    try:
        lock = open(LOCK_FILE, "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        log("lock_conflict", "flock 未获取到锁")
        return 4

    if not LEDGER.exists():
        log("parse_error", "台账不存在：" + str(LEDGER))
        return 2
    if not RULEBOOK.exists():
        log("parse_error", "规则手册不存在：" + str(RULEBOOK))
        return 2

    try:
        grades = sb.parse_ledger(LEDGER)  # 含三重校验（期初/链式/余额范围）
        balances = {gk: grades[gk]["balance"] for gk in ("g5", "g6")}
        events = extract_badge_events()
        docs = {
            "data/badges.json": build_badges(events),
            "data/badge_goals.json": build_badge_goals(events),
            "data/shop.json": build_shop(balances),
            "data/rules.json": parse_rules(),
        }
        # 校验门：往返 + 白名单 + PII/注入熔断
        for path, doc in docs.items():
            s = json.dumps(doc, ensure_ascii=False)
            doc2 = json.loads(s)
            if doc2.get("schema_version") != 1:
                raise ValueError("schema 校验失败：" + path)
            check_no_pii(doc2)
        # shop 进度越界熔断
        for it in docs["data/shop.json"]["items"]:
            for gk in ("g5", "g6"):
                p = it["by_grade"][gk]["progress"]
                if not (0.0 <= p <= 1.0):
                    raise ValueError("商店进度越界熔断：" + it["id"])
                if it["by_grade"][gk]["redeemable"] != (it["by_grade"][gk]["balance"] >= it["cost"]):
                    raise ValueError("商店可兑换一致性失败：" + it["id"])
    except Exception as e:
        fail("sections 解析失败：" + str(e))
        try:
            tail = LOG_FILE.read_text(encoding="utf-8").splitlines()[-2:]
            prev = [json.loads(line).get("status") for line in tail]
        except Exception:
            prev = []
        alert = prev.count("parse_error") >= 2
        log("parse_error", str(e)[:200], {"alert": alert})
        if alert:
            fail("ALERT: 连续 3 次解析失败，需人工介入")
        return 2

    now_iso = datetime.now(BEIJING).isoformat(timespec="seconds")
    changed_files = []
    for path, doc in docs.items():
        fp = REPO / path
        old = None
        if fp.exists():
            try:
                old = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                old = None
        new_cmp = dict(doc)
        old_cmp = dict(old) if isinstance(old, dict) else None
        if old_cmp:
            old_cmp.pop("generated_at", None)
        if old_cmp != new_cmp:
            doc["generated_at"] = now_iso
            tmp = fp.with_suffix(".json.tmp")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
            os.replace(tmp, fp)
            changed_files.append(path)

    if not changed_files:
        log("no_change", "四文件均无变化")
        return 0

    r = run_git("add", *changed_files)
    if r.returncode != 0:
        log("push_failed", "git add 失败 rc=%d" % r.returncode)
        return 3
    run_git("commit", "-m",
            "chore(sections): sync badges/shop/rules @ " +
            datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"))
    push_ok = False
    last_rc = None
    for _ in range(3):
        r = run_git("push")
        last_rc = r.returncode
        if r.returncode == 0:
            push_ok = True
            break
        time.sleep(30)
    if push_ok:
        log("pushed", "已推送", {"files": ",".join(changed_files)})
        return 0
    log("push_failed", "push 3 次失败 rc=%s" % last_rc)
    return 3


if __name__ == "__main__":
    sys.exit(main())

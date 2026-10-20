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

# ---------- 徽章目录（v3.7 定稿，一律 +5/次；数量以 BADGE_CATALOG 实际条目为准，不再硬编码）+ 历史 key 兼容 ----------
# key: 台账 badge_<key> 中的拼音；cond: 解锁条件（目标榜展示用）
BADGE_CATALOG = [
    ("quanduizhixing",   "全对之星",   "✅", "单次作业/小测全部题目正确"),
    ("baifentupo",       "满分之星",     "💯", "收到卷面满分的考试试卷（任一科目，单次达成）"),
    ("zonghezhixing",    "综合之星",   "🏅", "一周内≥4类不同徽章"),
    ("tupoxiaoyongshi",  "突破小勇士", "🛡️", "同一类错题连续3次订正全对"),
    ("jianchixiaoyongshi","坚持小勇士", "🔥", "家务/按时完成作业/按时睡觉，任一连续14天"),
    ("hezuozhixing",     "合作之星",   "🤝", "兄弟连续3天一起做家务（需两人协同、缺一不可，如递晾衣、共扫地）"),
    ("sanheyi",          "三合一快手", "⚡", "同日独立完成3项不同基础家务"),
    ("xiaolvxiaoshandian", "效率小闪电", "💨", "当日所有任务22:00前全部完成（可叠加）"),
    ("xidengxiaochaoren", "熄灯小超人", "🌙", "连续5天23:00前睡觉（中断重置）"),
    ("jiawujielishou",   "家务接力手", "🔄", "兄弟接力：同周两兄弟各做≥2项家务（含1次接力动作）"),
    ("sanribuduan",      "三日不断",   "📆", "连续3天不断档/全勤一周取高档"),
    ("shuxiezhixing",    "书写之星",   "✍️", "连续5次作业书写零扣分"),
    ("dancizhixing",     "单词之星",   "🔤", "单次单词听写全对（≥10词）"),
    ("moxiezhixing",     "默写之星",   "📝", "单次古诗文/课文默写全对"),
    ("zhenglizhixing",   "整理之星",   "🗂️", "累计5天整理桌面（每日≤1次计）"),
    ("jinbumiao",        "进步小苗",   "🌱", "同科同类型较上次提升≥15%且本次≥60分"),
    ("biaodazhixing",    "表达之星",   "🗣️", "自然周内背诵/口语达标≥2次"),
    ("jiawuxiaobangshou","家务小帮手", "🏠", "单次完成1项基础家务"),
    ("zhengdalidaren",   "整理小达人", "🧹", "连续5天整理类家务"),
    ("jiawuzhixing",     "家务之星",   "🏆", "周内≥5次（每日≤1次计）"),
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
    "kaoshibuzhixing": ("考试进步之星", "📈"),
    "meirijinbuzhixing": ("每日进步之星", "🚀"),
}
BADGE_MAP = {k: {"name": n, "icon": i} for k, n, i, _ in BADGE_CATALOG}
BADGE_MAP.update({k: {"name": n, "icon": i} for k, (n, i) in LEGACY_BADGES.items()})

# ---------- 积分商店（规则手册兑换表） ----------
# (id, name, cost, icon)
SHOP_TABLE = [
    ("mc30",   "30分钟Minecraft", 70, "🕹️"),
    ("mc60",   "1小时Minecraft", 100, "🕹️"),
    ("hy150",  "1小时和平精英\n或2小时Minecraft", 150, "🎮"),
    ("hy250",  "2小时和平精英", 250, "🎮"),
    ("wish600",  "达成愿望一次\n等额50元", 600, "🌟"),
    ("wish1000", "达成愿望一次\n等额100元", 1000, "💫"),
]

# N1-E2（DeepSeek终审定案 2026-10-20）：SHOP_TABLE 配置期校验——
# cost 缺失/非数值/<=0 视为真正数据异常，模块加载即熔断，不得等到运行时。
for _sid, _name, _cost, _icon in SHOP_TABLE:
    if not isinstance(_cost, (int, float)) or isinstance(_cost, bool) or _cost <= 0:
        raise ValueError("E2 商店 cost 配置错误：%s cost=%r" % (_sid, _cost))
del _sid, _name, _cost, _icon

KEY_PAT = re.compile(r"key:[^|]*?badge_([a-z]+)_")
REV_PAT = re.compile(r"key:[^|]*?reverse_badge_([a-z]+)_")
CYCLE_WEEK_RE = re.compile(r"cycle_week=(\d{4}-W\d{2})")


def cycle_week_of(date_str):
    """日期字符串 YYYY-MM-DD -> ISO 周期标识 YYYY-Www（徽章周期清空机制，
    规则手册《徽章进度口径·徽章周期清空》子节）。"""
    y, w, _ = datetime.strptime(date_str, "%Y-%m-%d").isocalendar()
    return "%04d-W%02d" % (y, w)
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
        active = {}   # key -> [events]（净持有：同徽章多次获得逐条累积，不设上限）
        seen = set()  # (key, date, clean_desc)：同一考试只记一次（防重复录入）
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
            # M2 修复（2026-10-20）：去重键纳入 HTML 注释完整 key。
            # clean_desc 会剥离 <!-- key:... -->，若同徽章同日同描述文本多次获得
            # （如"满分之星"不设上限），仅用 (key,date,clean_desc) 会误杀。
            # 注释 key 不同 → 视为不同事件。
            km = re.search(r"<!--\s*key:\s*(.*?)\s*-->", line)
            full_key = km.group(1).strip() if km else ""
            # 去重键 = badge_key + 当周(date决定) + 考试标识(key+date+事项+完整key)
            ident = (key, date, sb.clean_desc(desc), full_key)
            if ident in seen:
                continue
            seen.add(ident)
            if key not in earliest or date < earliest[key]:
                earliest[key] = date
            # cycle_week：优先读 key 注释中的落账字段（终审落账环节写入）；
            # 历史行无此字段 -> 按日期计算仅供展示归档，不参与周期重算/上限累计
            # （DeepSeek 二次终审执行解释：历史行留空＝不回填、不计入当周 +50）。
            cwm = CYCLE_WEEK_RE.search(line)
            active.setdefault(key, []).append(
                {"key": key, "date": date, "icon": icon, "name": name,
                 "cycle_week": cwm.group(1) if cwm else cycle_week_of(date),
                 "cycle_week_recorded": bool(cwm)})
        # 净持有徽章：同徽章多次获得各自保留，按最早发放日排序
        events = []
        for key, evts in active.items():
            for ev in evts:
                events.append(dict(ev))
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
    # 展示归属按 cycle_week（徽章周期清空机制）：带落账字段的事件按其
    # cycle_week 归档，历史行按日期推算；不用台账写入日期，避免跨周终审
    # 发放的上一周期徽章误入本周展示。
    cur_cycle = cycle_week_of(ws_s)
    by_grade = {}
    for gk in ("g5", "g6"):
        week_events = [e for e in events.get(gk, [])
                       if (e.get("cycle_week") or cycle_week_of(e["date"])) == cur_cycle]
        # 同徽章当周多次获得：按 key 分组累积，输出 count（不设上限）
        grouped = {}
        for e in week_events:
            grouped.setdefault(e["key"], []).append(e)
        items = []
        for key, evts in grouped.items():
            evts.sort(key=lambda x: x["date"])
            items.append({"key": key, "name": evts[0]["name"][:8],
                          "icon": evts[0]["icon"], "date": evts[0]["date"],
                          "count": len(evts)})
        items.sort(key=lambda x: x["date"])
        by_grade[gk] = items
    # 往期徽章（徽章周期清空机制）：本周期之前的全部徽章事件按 key 聚合，
    # 只记次数、不计分；积分与台账历史不受影响（分不回收铁律）。
    history = {}
    for gk in ("g5", "g6"):
        past = {}
        for e in events.get(gk, []):
            if (e.get("cycle_week") or cycle_week_of(e["date"])) >= cur_cycle:
                continue
            p = past.setdefault(e["key"], {"key": e["key"], "name": e["name"],
                                            "icon": e["icon"], "count": 0,
                                            "weeks": set()})
            p["count"] += 1
            p["weeks"].add(e.get("cycle_week") or cycle_week_of(e["date"]))
        hl = []
        for p in past.values():
            p["weeks"] = sorted(p["weeks"])
            p["last_week"] = p["weeks"][-1]
            hl.append(p)
        hl.sort(key=lambda x: (-x["count"], x["name"]))
        history[gk] = hl
    # B-2：周界自证元数据（下游可独立验证周界归属）
    return {"schema_version": 1,
            "week": {"start": ws_s, "end": we_s, "tz": "Asia/Shanghai",
                     "cycle_week": cycle_week_of(ws_s),
                     "week_start": ws.isoformat(timespec="seconds"),
                     "week_end": we.isoformat(timespec="seconds")},
            "by_grade": by_grade,
            "history": history}


# ---------- 徽章进度统计（2026-10-20 徽章进度包，用户拍板+DeepSeek终审） ----------
# 只读台账/题目库/落记文件，产出目标榜展示用进度；零积分写入、不生成徽章事件。
# 口径全文见 规则手册《徽章进度口径》节。每个数字必须可溯源；算不出 -> nodata。
import sqlite3 as _sqlite3
from datetime import date as _date

PROGRESS_FILE = REPO.parent / "徽章进度落记.md"
CHORE_ARCHIVE_FILE = REPO.parent / "家务记录.md"
CHORE_SKIP_LOG = REPO / "logs" / "chore_archive_skip.log"
# desk_untidy 不进进度，仅台账：负面条目（不整理书桌等）只在台账扣分，
# 永不写入家务记录存档、不参与任何徽章进度（DeepSeek 终审 2026-10-20）。
CHORE_NEG_WORDS = ("不整理", "未整理", "未做")
DAILY_FLAG_FILE = REPO.parent / "日结记录.jsonl"
QBANK_DB = REPO.parent / "题目库.db"
WRITING_DECIDE_LOG = REPO / "logs" / "badge_writing_decide.log"
WRITING_EFFECTIVE = "2026-10-20"   # 书写之星正式连续计数起日
SLEEP_START = "2026-10-20"         # 睡眠默认口径数据积累起日
BIAODA_EFFECTIVE = "2026-10-26"    # 表达之星正式计数起日（下个完整周一）

EVENT_BADGES = {"quanduizhixing", "baifentupo", "sanheyi", "xiaolvxiaoshandian",
                "dancizhixing", "moxiezhixing", "jiawuxiaobangshou", "jinbumiao"}
CHORE_KEY_TOKENS = ("xiwan", "saodi", "tuodi", "liangyifu", "liangwazi", "zhengli")
CHORE_DESC_ITEMS = ("洗碗", "扫地", "拖地", "晾衣服", "晾袜子", "整理桌面", "晾衣")
KEY_FULL_RE = re.compile(r"<!--\s*key:\s*(.*?)\s*-->")
CAT_RE = re.compile(r"【规则归类[:：]\s*([^】]+)】")
BATCH_MARK_RE = re.compile(r"第\s*[0-9０-９一二三四五六七八九十]+\s*[-—~～]\s*[0-9０-９一二三四五六七八九十]+\s*页|第\s*[0-9０-９]+\s*页")


def _ledger_rows_full():
    """逐行解析台账（保留行序/key/规则归类），返回 {gk: [row]}。只读。"""
    content = LEDGER.read_text(encoding="utf-8")
    out = {}
    for gk, grade in (("g5", "五年级"), ("g6", "六年级")):
        m = re.search(r"## " + grade + r"\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
        rows = []
        if not m:
            out[gk] = rows
            continue
        for idx, line in enumerate(m.group(1).split("\n")):
            mm = sb.ROW_PAT.match(line)
            if not mm:
                continue
            km = KEY_FULL_RE.search(line)
            cm = CAT_RE.search(mm.group(2))
            rows.append({
                "date": sb.norm_date(mm.group(1)),
                "desc": mm.group(2),
                "delta": int(mm.group(3)),
                "key": km.group(1).strip() if km else "",
                "cat": cm.group(1).strip() if cm else "",
                "idx": idx,
            })
        out[gk] = rows
    return out


def _is_chore(row):
    # 旧台账家务口径：2026-10-20 起家务类进度改读家务记录.md，本函数已无调用方，保留备查。
    if row["delta"] <= 0 or "badge" in row["key"] or "lianzuo" in row["key"]:
        return False
    if "连做" in row["desc"]:
        return False
    if row["cat"].startswith("chore"):
        return True
    if any(t in row["key"] for t in CHORE_KEY_TOKENS):
        return True
    return any(it in row["desc"] for it in CHORE_DESC_ITEMS)


def _streak_from_dates(date_set, today):
    """连续天数：以 today（有记录）或 yesterday（有记录）为锚向回数；两日皆无 -> 0。"""
    def iso(d):
        return d.isoformat()
    if iso(today) in date_set:
        anchor = today
    elif iso(today - timedelta(days=1)) in date_set:
        anchor = today - timedelta(days=1)
    else:
        return 0
    n, d = 0, anchor
    while iso(d) in date_set:
        n += 1
        d -= timedelta(days=1)
    return n


def _parse_progress_notes():
    """解析徽章进度落记.md：仅晚睡报告。（整理口头/家务接力已于 2026-10-20
    迁入家务记录.md 计数，见 _parse_chore_archive；本文件只保留晚睡报告节。）
    注：desk_untidy 不进进度，仅台账——负面条目不落记、不在此解析（DeepSeek 终审）。"""
    res = {"late_sleep": {"g5": set(), "g6": set()}}
    if not PROGRESS_FILE.exists():
        return res
    gmap = {"五年级": "g5", "六年级": "g6"}
    for line in PROGRESS_FILE.read_text(encoding="utf-8").split("\n"):
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", line)
        if not dm:
            continue
        d0 = dm.group(1)
        gk = None
        for gn, k in gmap.items():
            if gn in line:
                gk = k
                break
        if "wanshui_report" in line and gk:
            nm = re.search(r"(\d{1,2})-(\d{1,2})\s*晚", line)
            if nm:
                year = d0[:4]
                res["late_sleep"][gk].add("%s-%02d-%02d" % (year, int(nm.group(1)), int(nm.group(2))))
    return res


def _chore_skip_log(line, reason):
    try:
        CHORE_SKIP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CHORE_SKIP_LOG, "a", encoding="utf-8") as f:
            f.write("%s skip reason=%s row=%s\n" % (
                datetime.now(BEIJING).isoformat(timespec="seconds"),
                reason, line.strip()[:200]))
    except OSError:
        pass


def _parse_chore_archive():
    """解析家务记录.md（家务事实唯一记录面，2026-10-20 起家务类进度只读本存档）。
    返回 {"exists": bool, "rows": [...], "dates": {gk: set(日期)},
          "tidy": {gk: [(日期, 是否回补)]}, "coop_dates": set(日期),
          "relay": {日期: set(gk|'fam')}}。
    负向硬过滤：标记含 negative、或行内命中"不整理/未整理/未做"的行一律 skip
    并记 skip 日志，绝不进入任何进度（写入侧误混的最后一道拦截）。"""
    out = {"exists": False, "rows": [],
           "dates": {"g5": set(), "g6": set()},
           "tidy": {"g5": [], "g6": []},
           "coop_dates": set(), "relay": {}}
    if not CHORE_ARCHIVE_FILE.exists():
        return out
    out["exists"] = True
    gmap = {"五": "g5", "六": "g6", "家庭": "fam"}
    pair = {}  # (date, item) -> set(gk)：兄弟合作成对判定
    for line in CHORE_ARCHIVE_FILE.read_text(encoding="utf-8").split("\n"):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 9 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        dstr, grade, item, mode, partner = cells[0], cells[1], cells[2], cells[3], cells[4]
        evidence, tstr, key, mark = cells[5], cells[6], cells[7], cells[8]
        # 负向过滤（代码拦截，防负面报告误混入存档）
        if "negative" in mark or any(w in (item + mark + key) for w in CHORE_NEG_WORDS):
            _chore_skip_log(line, "negative")
            continue
        gk = gmap.get(grade)
        if gk is None:
            _chore_skip_log(line, "bad_grade")
            continue
        row = {"date": dstr, "gk": gk, "item": item, "mode": mode,
               "partner": partner, "evidence": evidence, "time": tstr,
               "key": key, "mark": mark,
               "backfill": "[回补]" in mark}
        out["rows"].append(row)
        if gk in ("g5", "g6"):
            out["dates"][gk].add(dstr)
            if item == "整理桌面":
                out["tidy"][gk].append((dstr, row["backfill"]))
            if mode == "合作" and partner == "兄弟":
                pair.setdefault((dstr, item), set()).add(gk)
        out["relay"].setdefault(dstr, set()).add(gk)
    out["coop_dates"] = {d for (d, _it), st in pair.items()
                         if st == {"g5", "g6"}}
    return out


def _sleep_streak(late_dates, now):
    """睡眠连续：自 SLEEP_START 起，无人报告晚睡之夜默认按时。
    夜 d 的确认时点为 d+1 的 23:00；未到确认时点的夜不计。"""
    today = now.date()
    last_conf = today - timedelta(days=1) if now.hour >= 23 else today - timedelta(days=2)
    start = _date.fromisoformat(SLEEP_START)
    if last_conf < start:
        return 0
    n, d = 0, last_conf
    while d >= start:
        if d.isoformat() in late_dates:
            break
        n += 1
        d -= timedelta(days=1)
    return n


def _writing_verdicts(rows, gk, now_iso, decide_lines):
    """书写判定（草约 A-3 定稿）：返回按时序的判定列表 [(date, verdict)]，
    verdict in {'zero','deduct'}；同时产出判定日志行。"""
    wrows = [r for r in rows
             if "_writing_" in r["key"] or r["cat"] in ("writing", "writing_deduct")]
    # 分组：(date, subject)；subject 取 key 中 _writing_ 前一段
    groups = {}
    order = []
    for r in wrows:
        km = re.search(r"_([a-z]+)_writing_", r["key"])
        subj = km.group(1) if km else "?"
        gkey = (r["date"], subj)
        if gkey not in groups:
            groups[gkey] = []
            order.append(gkey)
        groups[gkey].append(r)
    verdicts = []
    for gkey in order:
        dstr, subj = gkey
        grows = groups[gkey]
        # 按描述批次标识切分行块
        blocks = {}
        border = []
        for r in grows:
            bm = BATCH_MARK_RE.search(r["desc"])
            marker = bm.group(0) if bm else ""
            if marker not in blocks:
                blocks[marker] = []
                border.append(marker)
            blocks[marker].append(r)
        has_unmarked = "" in blocks
        any_deduct = any(r["cat"] == "writing_deduct" or r["delta"] < 0
                         for r in grows)
        if len(blocks) > 1 and has_unmarked and any_deduct:
            # 兜底：无法明确分批且存在扣分 -> 该科当日合并判扣分
            verdicts.append((dstr, "deduct"))
            decide_lines.append(
                "%s grade=%s date=%s subj=%s verdict=deduct rule=A3-fallback rows=%s"
                % (now_iso, gk, dstr, subj, [r["idx"] for r in grows]))
            continue
        for marker in border:
            brows = blocks[marker]
            is_deduct = any(r["cat"] == "writing_deduct" or r["delta"] < 0
                            for r in brows)
            verdicts.append((dstr, "deduct" if is_deduct else "zero"))
            decide_lines.append(
                "%s grade=%s date=%s subj=%s verdict=%s rule=A3-block marker=%s rows=%s"
                % (now_iso, gk, dstr, subj,
                   "deduct" if is_deduct else "zero", marker or "-",
                   [r["idx"] for r in brows]))
    verdicts.sort(key=lambda x: x[0])
    return verdicts


def _writing_streak(verdicts):
    """正式计数（>= WRITING_EFFECTIVE）：时序尾部连续 zero 数；尾部是 deduct -> 0。"""
    seq = [v for dstr, v in verdicts if dstr >= WRITING_EFFECTIVE]
    n = 0
    for v in reversed(seq):
        if v != "zero":
            break
        n += 1
    return n


def _load_daily_flags():
    """日结记录.jsonl -> {(gk, date): bool}。文件不存在返回空。"""
    out = {}
    if not DAILY_FLAG_FILE.exists():
        return out
    gmap = {"五年级": "g5", "六年级": "g6"}
    for line in DAILY_FLAG_FILE.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        gk = gmap.get(rec.get("grade", ""))
        if gk and rec.get("date"):
            out[(gk, rec["date"])] = bool(rec.get("all_ontime"))
    return out


def _homework_streak(flags, gk):
    """作业通道：按日期序取尾部连续 true（未落记日跳过、不断连）；false 截断。
    无任何落记 -> None（无数据）。"""
    dates = sorted(d for (g, d) in flags if g == gk)
    if not dates:
        return None
    n = 0
    for d in reversed(dates):
        if flags[(gk, d)]:
            n += 1
        else:
            break
    return n


def _tupo_progress(gk):
    """突破小勇士：题目库 questions 按知识点尾部连续'已订正正确'段长最大值。只读。"""
    if not QBANK_DB.exists():
        return None
    grade = "五年级" if gk == "g5" else "六年级"
    try:
        con = _sqlite3.connect("file:%s?mode=ro" % QBANK_DB, uri=True)
        rows = con.execute(
            "SELECT 知识点, 状态, 更新时间 FROM questions WHERE 年级=?",
            (grade,)).fetchall()
        con.close()
    except Exception:
        return None
    by_kp = {}
    for kp, status, upd in rows:
        by_kp.setdefault(kp, []).append((upd or "", status))
    best, best_kp = 0, ""
    for kp, lst in by_kp.items():
        lst.sort(key=lambda x: x[0])
        n = 0
        for _, st in reversed(lst):
            if st == "已订正正确":
                n += 1
            else:
                break
        if n > best:
            best, best_kp = n, kp
    return {"state": "ok", "current": min(best, 3), "target": 3, "unit": "次",
            "mode": "连续", "detail": best_kp}


def compute_badge_progress(events, now=None):
    """返回 {gk: {badge_key: progress_obj}}。progress_obj:
    {"state":"ok", current, target, unit, mode, detail?} |
    {"state":"nodata", note} | {"state":"event"}。"""
    now = now or datetime.now(BEIJING)
    today = now.date()
    now_iso = now.isoformat(timespec="seconds")
    rows_by_g = _ledger_rows_full()
    notes = _parse_progress_notes()
    arch = _parse_chore_archive()
    flags = _load_daily_flags()
    ws, we = week_bounds(now)
    ws_s, we_s = ws.strftime("%Y-%m-%d"), we.strftime("%Y-%m-%d")
    decide_lines = []
    out = {}
    for gk in ("g5", "g6"):
        rows = rows_by_g[gk]
        # 家务类数据源（2026-10-20 切换，DeepSeek 定稿）：只读家务记录存档，
        # 不再读台账家务条目、不做双读过渡。事实只认存档，积分只认台账。
        chore_dates = arch["dates"].get(gk, set())
        tidy_all_dates = {d for d, _bf in arch["tidy"].get(gk, [])}
        # 整理小达人活进度不计 [回补] 行（用户拍板 2026-10-20）
        tidy_live_dates = {d for d, bf in arch["tidy"].get(gk, []) if not bf}
        late_dates = {r["date"] for r in rows
                      if r["delta"] < 0 and ("晚睡" in r["desc"]
                                             or "超23点" in r["desc"]
                                             or "点睡" in r["desc"])}
        late_dates |= notes["late_sleep"][gk]
        p = {}
        for bk in EVENT_BADGES:
            p[bk] = {"state": "event"}
        # 家务之星：本周有家务的天数 / 5
        wk = len({d for d in chore_dates if ws_s <= d <= we_s})
        p["jiawuzhixing"] = {"state": "ok", "current": min(wk, 5), "target": 5,
                             "unit": "天", "mode": "本周"}
        # 整理小达人：整理桌面连续天数 / 5（存档非回补行）
        p["zhengdalidaren"] = {"state": "ok",
                               "current": min(_streak_from_dates(tidy_live_dates, today), 5),
                               "target": 5, "unit": "天", "mode": "连续"}
        # 整理之星：整理桌面累计天数 / 5（存档按日去重，照片+口头同日合 1）
        p["zhenglizhixing"] = {"state": "ok", "current": min(len(tidy_all_dates), 5),
                               "target": 5, "unit": "天", "mode": "累计"}
        # 熄灯小超人：睡眠连续 / 5
        sl = _sleep_streak(late_dates, now)
        p["xidengxiaochaoren"] = {"state": "ok", "current": min(sl, 5),
                                  "target": 5, "unit": "天", "mode": "连续"}
        # 坚持小勇士：三通道取最长
        chans = [("家务", _streak_from_dates(chore_dates, today)),
                 ("睡觉", sl)]
        hw = _homework_streak(flags, gk)
        if hw is not None:
            chans.append(("作业", hw))
        chans.sort(key=lambda x: -x[1])
        p["jianchixiaoyongshi"] = {"state": "ok",
                                   "current": min(chans[0][1], 14), "target": 14,
                                   "unit": "天", "mode": "连续",
                                   "detail": chans[0][0] + "通道"}
        # 合作之星：兄弟同日协同家务连续天数（需两年级数据，循环外补算见下）
        p["hezuozhixing"] = {"state": "ok", "current": 0, "target": 3,
                             "unit": "天", "mode": "连续"}
        # 综合之星：本周已获不同徽章种类数 / 4（按 cycle_week 归属，
        # 与展示/上限口径一致，不用台账写入日期）
        kinds = {e["key"] for e in events.get(gk, [])
                 if (e.get("cycle_week") or cycle_week_of(e["date"]))
                 == cycle_week_of(ws_s)}
        p["zonghezhixing"] = {"state": "ok", "current": min(len(kinds), 4),
                              "target": 4, "unit": "类", "mode": "本周"}
        # 三日不断：台账日期覆盖连续天数 / 3
        all_dates = {r["date"] for r in rows}
        p["sanribuduan"] = {"state": "ok",
                            "current": min(_streak_from_dates(all_dates, today), 3),
                            "target": 3, "unit": "天", "mode": "连续"}
        # 书写之星：正式计数连续零扣分 / 5
        verdicts = _writing_verdicts(rows, gk, now_iso, decide_lines)
        p["shuxiezhixing"] = {"state": "ok",
                              "current": min(_writing_streak(verdicts), 5),
                              "target": 5, "unit": "次", "mode": "连续"}
        # 表达之星：正式计数自 2026-10-26 起
        if today.isoformat() < BIAODA_EFFECTIVE:
            p["biaodazhixing"] = {"state": "nodata",
                                  "note": "2026-10-26 起正式计数"}
        else:
            ev = set()
            for r in rows:
                if "recite_pass" in r["key"]:
                    ev.add((r["date"], "recite"))
                elif "speaking_pass" in r["key"]:
                    ev.add((r["date"], "speaking"))
            cnt = len({x for x in ev if ws_s <= x[0] <= we_s})
            p["biaodazhixing"] = {"state": "ok", "current": min(cnt, 2),
                                  "target": 2, "unit": "次", "mode": "本周"}
        # 突破小勇士（题目库只读）
        tp = _tupo_progress(gk)
        p["tupoxiaoyongshi"] = tp if tp is not None else {
            "state": "nodata", "note": "题目库暂不可用"}
        out[gk] = p
    # 合作之星补算：存档中兄弟同日同项目「合作/兄弟」成对日（只读家务记录.md）
    for gk in ("g5", "g6"):
        out[gk]["hezuozhixing"]["current"] = min(
            _streak_from_dates(arch["coop_dates"], today), 3)
    # 家务接力手补算（2026-10-20 改版，DeepSeek 方案+家长拍板）：
    # 同周两兄弟各≥2项、合计≥4、至少1次接力动作（同项不同日两人做过，或同日不同项目）
    # 数据源 2026-10-20 切换为家务记录.md（原台账读法同日废止）；连做链伪行不计项。
    def _chore_items(gk):
        return [(r["date"], r["item"]) for r in arch["rows"]
                if r["gk"] == gk and ws_s <= r["date"] <= we_s
                and r["item"] != "其他（连做链）"]
    ci5, ci6 = _chore_items("g5"), _chore_items("g6")
    relay = False
    for i in {x for _, x in ci5} & {x for _, x in ci6}:
        d5 = {d for d, x in ci5 if x == i}
        d6 = {d for d, x in ci6 if x == i}
        if any(x != y for x in d5 for y in d6):
            relay = True
    for d in {d for d, _ in ci5} & {d for d, _ in ci6}:
        if {x for dd, x in ci5 if dd == d} != {x for dd, x in ci6 if dd == d}:
            relay = True
    prog = min(min(len(ci5), len(ci6)), 2)
    for gk in ("g5", "g6"):
        out[gk]["jiawujielishou"] = {"state": "ok", "current": prog,
                                     "target": 2, "unit": "项/人",
                                     "mode": "本周"}
    # 书写判定日志（草约 A-4 放行前置）
    try:
        WRITING_DECIDE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(WRITING_DECIDE_LOG, "a", encoding="utf-8") as f:
            for ln in decide_lines:
                f.write(ln + "\n")
    except OSError:
        pass
    return out



def build_badge_goals(events, progress=None, now=None):
    catalog = [{"key": k, "name": n, "icon": i, "cond": c, "points": 5}
               for k, n, i, c in BADGE_CATALOG]
    # 周期清空口径（2026-10-20）：obtained/date/count 按当前 cycle_week 计
    # （本周期是否已获得），历史累计另存 total_count；往期成就看 badges.json
    # 的 history（往期徽章区）。已发积分与台账历史不受此展示口径影响。
    now = now or datetime.now(BEIJING)
    ws, _we = week_bounds(now)
    cur_cycle = cycle_week_of(ws.strftime("%Y-%m-%d"))
    by_grade = {}
    for gk in ("g5", "g6"):
        held = {}
        cnt = {}
        total = {}
        for e in events.get(gk, []):
            total[e["key"]] = total.get(e["key"], 0) + 1
            if (e.get("cycle_week") or cycle_week_of(e["date"])) != cur_cycle:
                continue
            if e["key"] not in held or e["date"] < held[e["key"]]:
                held[e["key"]] = e["date"]
            cnt[e["key"]] = cnt.get(e["key"], 0) + 1
        entries = []
        for k, _, _, _ in BADGE_CATALOG:
            ent = {"key": k, "obtained": k in held,
                   "date": held.get(k, ""), "count": cnt.get(k, 0),
                   "total_count": total.get(k, 0)}
            if progress and gk in progress and k in progress[gk]:
                ent["progress"] = progress[gk][k]
            entries.append(ent)
        by_grade[gk] = entries
    return {"schema_version": 1, "catalog": catalog, "by_grade": by_grade,
            "cycle_week": cur_cycle}


def build_shop(balances):
    # N1 方案A（DeepSeek终审定案 2026-10-20）：
    # progress 是展示量（用钳0后的余额计算，恒 ∈ [0.0, 1.0]）；
    # redeemable 是判定量（用真实余额，恒等式 redeemable == (balance >= cost)）；
    # balance 输出保留真实值（含负），前端显示"−N"。
    # 负余额 → 照常输出（与 build_stats §7.2"负余额照常输出"同口径），不构成熔断。
    items = []
    for sid, name, cost, icon in SHOP_TABLE:
        # E2：cost 配置错误视为真正数据异常（上游熔断分支校验）
        bg = {}
        for gk in ("g5", "g6"):
            bal = balances[gk]  # 真实余额，可能为负
            prog_bal = max(bal, 0)  # 仅用于进度计算，不改输出
            prog = min(prog_bal / cost, 1.0) if cost > 0 else 0.0
            prog = max(prog, 0.0)  # 双保险，杜绝负 progress
            bg[gk] = {"balance": bal, "progress": round(prog, 4),
                      "redeemable": bal >= cost}
        items.append({"id": sid, "name": name, "cost": cost, "icon": icon,
                      "by_grade": bg})
    return {"schema_version": 1, "items": items}


PTS_RE = re.compile(r"[+-]\d+")
REMARK_RE = re.compile(r"[（(][^）)]*20\d\d[^）)]*[）)]")


def parse_tier(seg):
    """多档规则的子项解析：返回 {label, pts}。"""
    seg = seg.strip().rstrip("）)").strip()
    m = re.search(r"[:：](\d+(?:\.\d+)?)$", seg)
    if m and not re.search(r"[+-]\d+$", seg):
        return {"label": seg[:m.start()], "pts": "×" + m.group(1)}
    m = re.search(r"([+-]\d+)$", seg)
    if m:
        return {"label": seg[:m.start()], "pts": m.group(1)}
    m = re.search(r"为(\d+)$", seg)
    if m:
        return {"label": seg[:m.start()], "pts": m.group(1)}
    return {"label": seg, "pts": None}


def try_multi(text):
    """多档规则拆分（2026-10-20用户要求"分开列，不要挤一起"）：
    返回 (header, subs) 或 None。识别两种形态（B 优先于 A）：
    B) "；" 分隔的分组（在校考试；组内可再有括号档位）
    A) " / " 分隔的档位（作业正确率/练习完成分/周日卷/53类/系数型）"""
    if "；" in text and "：" in text:
        header, rest = text.split("：", 1)
        groups = [g.strip() for g in rest.split("；") if g.strip()]
        scored = sum(1 for g in groups if re.search(r"[+-]\d+", g))
        if len(groups) >= 2 and scored >= 2:
            subs = []
            for g in groups:
                # 分组带括号档位："单元（90分以上+30 / 80分以上+16 / 70分以上+5）"
                gm = re.match(r"^(.+?)（(.+)）$", g)
                if gm and " / " in gm.group(2):
                    gname = gm.group(1)
                    for tg in gm.group(2).split(" / "):
                        t = parse_tier(tg)
                        subs.append({"label": gname + " " + t["label"],
                                     "pts": t["pts"]})
                    continue
                ms = re.findall(r"[+-]\d+", g)
                if len(ms) == 1:
                    subs.append(parse_tier(g))
                else:
                    subs.append({"label": g, "pts": None})
            return header.strip(), subs
    if " / " in text:
        segs = text.split(" / ")
        scored = sum(1 for sg in segs
                     if re.search(r"[+-]\d+", sg) or re.search(r"[:：]\d", sg)
                     or re.search(r"为\d+$", sg.strip().rstrip("）)")))
        if len(segs) >= 2 and scored >= 2:
            first = segs[0]
            if "：" in first:
                header, t0 = first.split("：", 1)
            elif "（" in first:
                header, t0 = first.split("（", 1)
            else:
                header, t0 = "", first
            subs = [parse_tier(t0)] + [parse_tier(sg) for sg in segs[1:]]
            return header.strip(), subs
    return None


def parse_rules():
    """解析规则手册 dashboard 白名单块；缺失任一块视为失败（熔断）。

    2026-10-20（用户要求统一格式，DeepSeek方案+Gemini审核）：
    - 显式提取首个 [+-]数字 为 pts，避免前端正则被日期误匹配
    - 日期类括号标注（如"（2026-10-20家长新增）"）移入 remark，正文清理干净
    - 无分值条目 pts=None，前端显示"说明"徽章
    """
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
                    # 说明类条款（互斥/将功补过/作弊疑点）是机制注释，无分值；
                    # 其余取首个分值（主分值）。先提备注，避免日期被误识别。
                    remark = None
                    rm = REMARK_RE.search(text)
                    if rm:
                        remark = rm.group(0)
                        text = (text[:rm.start()] + text[rm.end():]).strip()
                    subs = None
                    if text.startswith(("互斥", "将功补过", "作弊疑点")):
                        pts = None
                    else:
                        multi = try_multi(text)
                        if multi:
                            text, subs = multi
                            pts = None
                        else:
                            # 2026-10-20去重（DeepSeek方案+Gemini审核）：
                            # 首个" -数字"处截断正文，分值进徽章；单位/上限说明留正文
                            pm = re.search(r"\s?([+-]\d+)", text)
                            if pm:
                                pts = pm.group(1)
                                text = (text[:pm.start()] + text[pm.end():]).strip()
                            else:
                                pts = None
                    item = {"id": "%s%d" % (key[0], len(items) + 1),
                            "text": text, "pts": pts, "remark": remark}
                    if subs:
                        item["subs"] = subs
                    items.append(item)
        if not items:
            raise ValueError("白名单块为空：" + tag)
        out[key] = items
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
        # N1-E1（DeepSeek终审定案 2026-10-20）：balances 非 dict 视为真正数据异常
        if not isinstance(balances, dict):
            raise ValueError("E1 balances 非 dict：%r" % type(balances))
        events = extract_badge_events()
        # 进度统计故障不得阻断栏目同步：失败则降级为无进度（前端不显示进度行）
        try:
            progress = compute_badge_progress(events)
        except Exception as _pe:
            log("progress_error", str(_pe)[:200])
            progress = None
        docs = {
            "data/badges.json": build_badges(events),
            "data/badge_goals.json": build_badge_goals(events, progress),
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
        # N1 方案A（DeepSeek终审定案 2026-10-20）：熔断收窄至真正数据异常 E1-E4。
        # 废止"progress 越界即熔断"独立条文——负余额时 progress 已钳0，不构成熔断。
        # E4（钳0后 progress 仍越界）属实现自检，命中说明钳0逻辑回归失败，应熔断报错。
        for it in docs["data/shop.json"]["items"]:
            for gk in ("g5", "g6"):
                bg = it["by_grade"][gk]
                bal, p, red = bg["balance"], bg["progress"], bg["redeemable"]
                # E1：余额类型异常（非数值 / NaN / Inf）
                if not isinstance(bal, (int, float)) or isinstance(bal, bool):
                    raise ValueError("E1 商店余额类型异常：%s/%s" % (it["id"], gk))
                if bal != bal or bal in (float("inf"), float("-inf")):
                    raise ValueError("E1 商店余额 NaN/Inf：%s/%s" % (it["id"], gk))
                # E3：余额量级越界（脏数据）
                if abs(bal) > 1e9:
                    raise ValueError("E3 商店余额量级越界：%s/%s bal=%s" % (it["id"], gk, bal))
                # E4：实现自检——钳0后 progress 仍越界
                if not (0.0 <= p <= 1.0):
                    raise ValueError("E4 商店进度越界（实现bug）：%s/%s p=%s" % (it["id"], gk, p))
                # redeemable 一致性恒等式（恒成立）
                if red != (bal >= it["cost"]):
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

    # Q2（DeepSeek终审 2026-10-20《自动同步cron提交范围限制》）：
    # 提交前检查 scripts/ 是否有未提交改动；有则本次不提交 + 告警日志（fail-safe）。
    # 防 A类修复被功能提交卷入（程序异常台账 #1/#2）重演。
    _dirty = run_git("status", "--porcelain", "--", "scripts/")
    _dirty_out = (_dirty.stdout or "").strip()
    if _dirty_out:
        _dirty_files = "; ".join(l[:80] for l in _dirty_out.splitlines()[:10])
        _alert = "%s | sync_sections.py | scripts/ 脏，跳过本次提交 | %s" % (
            datetime.now(BEIJING).isoformat(timespec="seconds"), _dirty_files)
        log("commit_guard_skip", _alert)
        try:
            with open(REPO / "logs" / "cron_commit_guard.log", "a", encoding="utf-8") as _f:
                _f.write(_alert + "\n")
        except Exception:
            pass
        return 0

    r = run_git("add", *changed_files)
    if r.returncode != 0:
        log("push_failed", "git add 失败 rc=%d" % r.returncode)
        return 3
    # M4（DeepSeek终审 2026-10-20，B1 复审补强）：显式检查 commit 返回码。
    # git 无变更时 rc=1 且输出标准串 "nothing to commit, working tree clean"。
    # 判定必须严格匹配完整标准串（双子串合取），防 pre-commit hook 误输出子串被误判为无变更。
    # 注：依赖 git 英文标准输出（locale 需为 C/en）；若未来 git 措辞变化，安全降级走 rc=3 失败分支（fail-safe）。
    # rc!=0 且非标准无变更串 → 一律视为真失败（return 3，与 push 失败共用返回码）。
    cr = run_git("commit", "-m",
            "chore(sections): sync badges/shop/rules @ " +
            datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"))
    if cr.returncode != 0:
        _err = cr.stderr or ""
        if "nothing to commit" in _err and "working tree clean" in _err:
            log("no_change", "git commit 无变更（working tree clean），跳过推送")
            return 0
        log("push_failed", "git commit 真失败 rc=%d: %s" % (cr.returncode, _err[:200]))
        return 3
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

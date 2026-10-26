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

# ---------- 徽章目录（单一数据源 scripts/badges.json，2026-10-22 目录收口） ----------
# key: 台账 badge_<key> 中的拼音；cond: 解锁条件（目标榜展示用）。目录增删改
# 一律改 badges.json（含 aliases 别名表），禁止在本文件另存副本。
import catalog as _catalog
BADGE_CATALOG = _catalog.badge_catalog()
# 历史徽章 key（旧体系，台账中可能出现；展示用，不在目标榜目录内）
LEGACY_BADGES = _catalog.legacy_badges()
BADGE_MAP = {k: {"name": n, "icon": i} for k, n, i, _ in BADGE_CATALOG}
BADGE_MAP.update({k: {"name": n, "icon": i} for k, (n, i) in LEGACY_BADGES.items()})
# 历史落账 key 变体 -> 目录 canonical key：见 badges.json 的 aliases（单一数据源）。
KEY_ALIAS = _catalog.key_alias()

# ---------- 积分商店（单一数据源 scripts/shop_catalog.json，2026-10-22 目录收口） ----------
# (id, name, cost, icon)；cost 配置期校验在 catalog.shop_table() 内（原 N1-E2 口径，加载即熔断）。
SHOP_TABLE = _catalog.shop_table()

KEY_PAT = re.compile(r"key:[^|]*?badge_([a-z]+)_")
REV_PAT = re.compile(r"key:[^|]*?reverse_badge_([a-z]+)_")
CYCLE_WEEK_RE = re.compile(r"cycle_week=(\d{4}-W\d{2})")
LATE_CYCLE_RE = re.compile(r"late_cycle=(\d{4}-W\d{2})")
BACKFILL_RE = re.compile(r"backfill=(\d{4}-\d{2}-\d{2})")


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
                active.pop(KEY_ALIAS.get(rev.group(1), rev.group(1)), None)
                continue
            b = KEY_PAT.search(line)
            if not b:
                continue
            key = KEY_ALIAS.get(b.group(1), b.group(1))
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
            # （DeepSeek 二次终审执行解释：历史行留空＝不回填、不参与周期重算）。
            cwm = CYCLE_WEEK_RE.search(line)
            # late_cycle：补发标记（归属周早于落账周），由终审落账环节与
            # cycle_week 一并写入 key 注释；无此字段 -> 非补发条目。
            lcm = LATE_CYCLE_RE.search(line)
            rec_cycle = cwm.group(1) if cwm else cycle_week_of(date)
            late_c = lcm.group(1) if lcm else None
            late_rec = bool(lcm)
            # 补记字段缺口修复（DeepSeek 终审 2026-10-23，发现2）：带
            # backfill= 的迟到补记若漏写 late_cycle（如百分俱乐部补记行），
            # 按其 cycle_week（归属周）合成 late 标记，落账日取 backfill 日
            # （post_date）；归属周仍以 cycle_week 为准、不并入当周计数，
            # 使徽章带/目标榜显示与台账一致，杜绝"已发放未显示"。
            bfm = BACKFILL_RE.search(line)
            backfill = bfm.group(1) if bfm else None
            post_date = backfill if backfill else date
            if late_c is None and backfill and rec_cycle != cycle_week_of(post_date):
                late_c, late_rec = rec_cycle, True
            active.setdefault(key, []).append(
                {"key": key, "date": date, "icon": icon, "name": name,
                 "cycle_week": rec_cycle,
                 "cycle_week_recorded": bool(cwm),
                 "late_cycle": late_c,
                 "late_cycle_recorded": late_rec,
                 "backfill": backfill,
                 "post_date": post_date})
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
        # 补发展示（B方案，2026-10-20 用户拍板+DeepSeek 终审）：归属为此前
        # 周期、带 late_cycle 标记、且落账日期（颁发）落在本周期内的徽章，
        # 在本周期徽章带内同带展示并加 late 标记（前端渲染「补发」角标）；
        # 归属周仍记原周（origin_cycle），不并入本周 count、不影响目标榜
        # 计数。补发只在其颁发所在周期展示：下周期视角下其落账日期已不在
        # 当周，自然消失，不会被误标为下周归属。
        late_events = [e for e in events.get(gk, [])
                       if e.get("late_cycle_recorded")
                       and (e.get("cycle_week") or cycle_week_of(e["date"])) != cur_cycle
                       and cycle_week_of(e.get("post_date") or e["date"]) == cur_cycle]
        late_grouped = {}
        for e in late_events:
            late_grouped.setdefault(e["key"], []).append(e)
        for key, evts in late_grouped.items():
            evts.sort(key=lambda x: x["date"])
            items.append({"key": key, "name": evts[0]["name"][:8],
                          "icon": evts[0]["icon"], "date": evts[0]["date"],
                          "count": len(evts), "late": True,
                          "origin_cycle": (evts[0].get("cycle_week")
                                           or cycle_week_of(evts[0]["date"]))})
        items.sort(key=lambda x: x["date"])
        by_grade[gk] = items
    # B-2：周界自证元数据（下游可独立验证周界归属）
    return {"schema_version": 1,
            "week": {"start": ws_s, "end": we_s, "tz": "Asia/Shanghai",
                     "cycle_week": cycle_week_of(ws_s),
                     "week_start": ws.isoformat(timespec="seconds"),
                     "week_end": we.isoformat(timespec="seconds")},
            "by_grade": by_grade}


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
CHORE_NEG_WORDS = ("不整理", "未整理", "未做", "没整理", "没做", "没收拾")
# 负面词表（2026-10-20 P2 保守扩充）：仅加同义口语变体。注意词表越宽误伤
# 正向记录的风险越大（如"没收拾好就出门"类语境），扩充只限明确负面短语。
DAILY_FLAG_FILE = REPO.parent / "日结记录.jsonl"
QBANK_DB = REPO.parent / "题目库.db"
WRITING_DECIDE_LOG = REPO / "logs" / "badge_writing_decide.log"
WRITING_EFFECTIVE = "2026-10-20"   # 书写之星正式连续计数起日
SLEEP_START = "2026-10-20"         # 睡眠默认口径数据积累起日
BIAODA_EFFECTIVE = "2026-10-26"    # 表达之星正式计数起日（下个完整周一）
BAIFEN_EFFECTIVE = "2026-10-26"    # 百分俱乐部周基准新规起算日（W44）

EVENT_BADGES = {"quanduizhixing", "baifentupo", "sanheyi", "xiaolvxiaoshandian",
                "dancizhixing", "moxiezhixing", "jiawuxiaobangshou", "jinbumiao"}
CHORE_KEY_TOKENS = ("xiwan", "saodi", "tuodi", "liangyifu", "liangwazi", "xiyifu", "zhengli")
CHORE_DESC_ITEMS = ("洗碗", "扫地", "拖地", "晾衣服", "晾袜子", "洗衣服", "整理桌面", "晾衣")
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
            key_text = km.group(1).strip() if km else ""
            # 生效时刻（百分俱乐部周基准用）：补记行以 backfill 日为落账日，
            # 其余以名义日期；时刻取 key 注释 @HH:MM，无则 00:00。
            _tm = re.search(r"@(\d{2}:\d{2})", key_text)
            _bm = BACKFILL_RE.search(key_text)
            rows.append({
                "date": sb.norm_date(mm.group(1)),
                "desc": mm.group(2),
                "delta": int(mm.group(3)),
                "balance_after": int(mm.group(4)),
                "key": key_text,
                "cat": cm.group(1).strip() if cm else "",
                "idx": idx,
                "eff_date": _bm.group(1) if _bm else sb.norm_date(mm.group(1)),
                "eff_time": _tm.group(1) if _tm else "00:00",
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


def _streak_live(date_set, today, grace_days=2):
    """与 _streak_from_dates 同源的"活进度"口径（DeepSeek 终审 2026-10-23，
    发现4）：家务判定候选每晚 22:30 在当日数据齐备时运行，today/yesterday
    锚成立；看板同步在任意时刻运行，家务定案顺延（如照片待识别、次日补
    定案）时锚点会误断成 0，与候选公布值脱节。规则：标准锚有值用标准值；
    否则最近记录日在 grace_days 内时，按截至最近记录日的连续段计（即候选
    最近一期公布的同源数值）；再否则 0（连续段确已中断）。"""
    n = _streak_from_dates(date_set, today)
    if n > 0 or not date_set:
        return n
    last = max(date_set)
    last_d = datetime.strptime(last, "%Y-%m-%d").date()
    if (today - last_d).days > grace_days:
        return 0
    n2, d = 0, last_d
    while d.isoformat() in date_set:
        n2 += 1
        d -= timedelta(days=1)
    return n2


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
           "coop_dates": set(), "coop_dates_new": set(), "relay": {},
           "award_idx": {}}
    if not CHORE_ARCHIVE_FILE.exists():
        return out
    out["exists"] = True
    gmap = {"五": "g5", "六": "g6", "家庭": "fam"}
    pair = {}  # (date, item) -> set(gk)：兄弟合作成对判定
    pair_idx = {}  # (date, item) -> 成对完成时的行序（后到一行的 idx）
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
               "backfill": "[回补]" in mark,
               "idx": len(out["rows"])}
        out["rows"].append(row)
        # 颁发消费基线（徽章周期清空机制）：存档行上的 [颁发:徽章名] 标记
        # 表示该行及之前的计数已被那次颁发消费；连续/累计类进度与判定
        # 只统计基线之后的行，实现"达成后计数归零重新起算/累计"。
        if "颁发" in mark:
            for bname in ("整理之星", "整理小达人", "坚持小勇士", "合作之星"):
                if bname in mark:
                    out["award_idx"][(gk, bname)] = row["idx"]
                    if bname == "合作之星":
                        out["award_idx"][("coop", bname)] = row["idx"]
        if gk in ("g5", "g6"):
            out["dates"][gk].add(dstr)
            if item == "整理桌面":
                out["tidy"][gk].append((dstr, row["backfill"]))
            if mode == "合作" and partner == "兄弟":
                st = pair.setdefault((dstr, item), set())
                st.add(gk)
                if st == {"g5", "g6"} and (dstr, item) not in pair_idx:
                    pair_idx[(dstr, item)] = row["idx"]
        out["relay"].setdefault(dstr, set()).add(gk)
    out["coop_dates"] = {d for (d, _it), st in pair.items()
                         if st == {"g5", "g6"}}
    coop_base = out["award_idx"].get(("coop", "合作之星"), -1)
    out["coop_dates_new"] = {d for (d, _it), i in pair_idx.items()
                             if i > coop_base}
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


def _writing_streak_after_award(verdicts, last_date):
    """书写之星颁发后重计（2026-10-26 家长定接通自动颁发）：
    只计上次颁发日之后的 verdict 尾部连续 zero 数；无上次颁发时
    等同 _writing_streak。颁发日以台账徽章行名义日期为准（非补记日）。"""
    seq = [v for dstr, v in verdicts
           if dstr >= WRITING_EFFECTIVE and (last_date is None or dstr > last_date)]
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
    except Exception as _qe:
        # P2（DeepSeek 审查 2026-10-20）：题库查询失败不再静默吞——留痕便于
        # 区分"真无数据"与"查询坏了"（返回值仍为 None 降级，不阻断同步）
        log("qbank_error", "_tupo_progress %s: %s" % (gk, str(_qe)[:150]))
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


def _baifen_week_state(rows, now):
    """百分俱乐部周基准状态（2026-10-23 改版，规则手册《徽章判定》节）：
    基准 = 台账链上周一 00:00 前最后一笔余额（按生效时刻判定，补记行以
    backfill 日为落账日）；当前余额 = 台账链最后一笔余额；本周已涨分 =
    当前 - 基准（可负，展示侧钳 0）。rows 为 _ledger_rows_full 的行序。"""
    ws, _we = week_bounds(now)
    ws_s = ws.strftime("%Y-%m-%d")
    ws_dt = ws_s + " 00:00"
    baseline = 0
    current = 0
    for r in rows:
        current = r.get("balance_after", current)
    for r in rows:
        eff = (r.get("eff_date") or r["date"]) + " " + (r.get("eff_time") or "00:00")
        if eff < ws_dt:
            baseline = r.get("balance_after", baseline)
    return {"week_start": ws_s, "baseline": baseline,
            "current_balance": current, "gain": current - baseline}


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
        # 颁发消费基线（周期清空机制）：整理之星/整理小达人/坚持小勇士
        # 只统计最近一次 [颁发:对应徽章] 标记之后的行，达成后归零重计。
        aidx = arch.get("award_idx", {})
        base_star = aidx.get((gk, "整理之星"), -1)
        base_daren = aidx.get((gk, "整理小达人"), -1)
        base_jian = aidx.get((gk, "坚持小勇士"), -1)
        tidy_all_dates = {r["date"] for r in arch["rows"]
                          if r["gk"] == gk and r["item"] == "整理桌面"
                          and r["idx"] > base_star}
        # 整理小达人活进度不计 [回补] 行（用户拍板 2026-10-20）
        tidy_live_dates = {r["date"] for r in arch["rows"]
                           if r["gk"] == gk and r["item"] == "整理桌面"
                           and not r["backfill"] and r["idx"] > base_daren}
        jian_dates = {r["date"] for r in arch["rows"]
                      if r["gk"] == gk and r["idx"] > base_jian}
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
        # 坚持小勇士：三通道取最长（家务通道按颁发基线归零重计；
        # 家务通道用 _streak_live 与家务判定候选同源，防定案顺延误断，
        # DeepSeek 终审 2026-10-23 发现4）
        chans = [("家务", _streak_live(jian_dates, today)),
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
        # 综合之星：本周已获不同徽章种类数 / 4。口径（DeepSeek 终审
        # 2026-10-23 修订，覆盖 2026-10-20 裁定3）：按台账行名义日期
        # 所属周计种类——终审按此重判彦均 5 类 / 伟豪 3 类（核查时点）。
        # 旧口径要求 key 注释 cycle_week 已落账标注且等于当周，使家务
        # 迟到补发（标注为达成归属周 W42、行日期在本周）的种类被系统性
        # 漏计。综合之星自身不计入；current 不截断（如实显示种类数，
        # 可大于 target）。
        kinds = {e["key"] for e in events.get(gk, [])
                 if e["key"] != "zonghezhixing"
                 and cycle_week_of(e["date"]) == cycle_week_of(ws_s)}
        p["zonghezhixing"] = {"state": "ok", "current": len(kinds),
                              "target": 4, "unit": "类", "mode": "本周"}
        # 百分俱乐部（2026-10-26 起新规展示）：本周已涨分 / 100。
        # 生效日前不挂进度（维持旧展示），生效日起按周基准口径显示。
        if today.isoformat() >= BAIFEN_EFFECTIVE:
            bfs = _baifen_week_state(rows, now)
            p["baifen"] = {"state": "ok",
                           "current": max(bfs["gain"], 0),
                           "target": 100, "unit": "分", "mode": "本周"}
        # 三日不断：台账日期覆盖连续天数 / 3
        all_dates = {r["date"] for r in rows}
        p["sanribuduan"] = {"state": "ok",
                            "current": min(_streak_from_dates(all_dates, today), 3),
                            "target": 3, "unit": "天", "mode": "连续"}
        # 书写之星：正式计数连续零扣分 / 5（颁发后重计，2026-10-26）
        verdicts = _writing_verdicts(rows, gk, now_iso, decide_lines)
        _last_shuxie = max([e["date"] for e in events.get(gk, [])
                            if e["key"] == "shuxiezhixing"], default=None)
        p["shuxiezhixing"] = {"state": "ok",
                              "current": min(_writing_streak_after_award(
                                  verdicts, _last_shuxie), 5),
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
    # 合作之星补算：存档中兄弟同日同项目「合作/兄弟」成对日（只读家务记录.md）；
    # 只统计最近一次 [颁发:合作之星] 之后成对完成的日（达成后归零重计）
    for gk in ("g5", "g6"):
        out[gk]["hezuozhixing"]["current"] = min(
            _streak_from_dates(arch["coop_dates_new"], today), 3)
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
    _points = _catalog.badge_points()
    catalog = [{"key": k, "name": n, "icon": i, "cond": c,
                "points": _points.get(k, 5)}
               for k, n, i, c in BADGE_CATALOG]
    # 周期清空口径（2026-10-20）：obtained/date/count 按当前 cycle_week 计
    # （本周期是否已获得），历史累计另存 total_count；往期徽章不在看板
    # 展示（无 history 区），历史只保留在台账。已发积分与台账历史不受此
    # 展示口径影响。
    now = now or datetime.now(BEIJING)
    ws, _we = week_bounds(now)
    cur_cycle = cycle_week_of(ws.strftime("%Y-%m-%d"))
    by_grade = {}
    for gk in ("g5", "g6"):
        held = {}
        cnt = {}
        total = {}
        late_held = {}
        earliest = {}
        for e in events.get(gk, []):
            total[e["key"]] = total.get(e["key"], 0) + 1
            if e["key"] not in earliest or e["date"] < earliest[e["key"]]:
                earliest[e["key"]] = e["date"]
            if (e.get("cycle_week") or cycle_week_of(e["date"])) != cur_cycle:
                # 补发落账本周期（与 build_badges 同口径）：徽章带已同带展示，
                # 目标榜"已获取"标签须一致（2026-10-21 用户报障修复）；仅影响
                # obtained 标签，count/进度计数仍按归属周不动（B方案口径）。
                if (e.get("late_cycle_recorded")
                        and cycle_week_of(e.get("post_date") or e["date"]) == cur_cycle):
                    if e["key"] not in late_held or e["date"] < late_held[e["key"]]:
                        late_held[e["key"]] = e["date"]
                continue
            if e["key"] not in held or e["date"] < held[e["key"]]:
                held[e["key"]] = e["date"]
            cnt[e["key"]] = cnt.get(e["key"], 0) + 1
        entries = []
        for k, _, _, _ in BADGE_CATALOG:
            ent = {"key": k, "obtained": (k in held) or (k in late_held),
                   "date": held.get(k) or late_held.get(k, ""),
                   "count": cnt.get(k, 0),
                   "total_count": total.get(k, 0),
                   "via_late": (k not in held) and (k in late_held)}
            # 百分俱乐部（2026-10-26 家长定，覆盖 2026-10-23 累计点亮口径）：
            # 目标榜不再显示曾获取，obtained 按本周新发计（本周未获即
            # 待解锁）；total_count 仍为累计次数，date 为本周获得日。
            if progress and gk in progress and k in progress[gk]:
                ent["progress"] = progress[gk][k]
            entries.append(ent)
        by_grade[gk] = entries
    return {"schema_version": 1, "catalog": catalog, "by_grade": by_grade,
            "cycle_week": cur_cycle}


# ---------- 兑换记录（2026-10-21 DeepSeek 方案 + 家长拍板：商店底部、分年级、
# 滚动15天、点击展开；前端渲染在模板 renderRedeem） ----------
# 落账约定（写入规则手册《积分商店》节）：兑换行 key 含
# redeem_<档位>_<场次>（如 redeem_minecraft30_20261021a）；手续费如单列一行，
# 与主行同场次且描述含"手续费"；撤销/退回行 key 为同场次加 _reversal 后缀，
# 描述写明原因。脚本按场次归并：负分合计=花费，手续费行负分=fee，
# 主行（或场次最后一行）余额列=兑后余额；有 _reversal 场次 -> 原条标 revoked。
REDEEM_KEY_RE = re.compile(r"redeem_([a-z0-9]+)_([a-z0-9]+?)(_reversal)?(?:_|\s|$)")
REDEEM_TIERS = {
    "minecraft30": ("30分钟Minecraft", "🕹️"),
    "minecraft60": ("1小时Minecraft", "🕹️"),
    "heping150": ("1小时和平精英或2小时Minecraft", "🎮"),
    "heping250": ("2小时和平精英", "🎮"),
    "wish600": ("达成愿望一次（50元）", "🌟"),
    "wish1000": ("达成愿望一次（100元）", "💫"),
}


def build_redeem_log(content, now=None):
    """从台账文本解析兑换记录：返回最近15天（滚动，含今日）、按年级分组、
    日期倒序的记录。零兑换时 by_grade 为空列表（前端显示空态文案）。"""
    now = now or datetime.now(BEIJING)
    today = now.date()
    win_start = today - timedelta(days=14)
    by_grade = {}
    for gk, grade in (("g5", "五年级"), ("g6", "六年级")):
        m = re.search(r"## " + grade + r"\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
        sessions = {}  # (session) -> 聚合中的记录
        order = []
        if m:
            for line in m.group(1).split("\n"):
                mm = sb.ROW_PAT.match(line)
                if not mm:
                    continue
                km = REDEEM_KEY_RE.search(line)
                if not km:
                    continue
                tier, session = km.group(1), km.group(2)
                is_reversal = bool(km.group(3))
                date = sb.norm_date(mm.group(1))
                desc = sb.clean_desc(mm.group(2))
                try:
                    pts = int(mm.group(3))
                    bal = int(mm.group(4))
                except (TypeError, ValueError):
                    continue
                key = (session, is_reversal)
                if key not in sessions:
                    sessions[key] = {"tier": tier, "session": session,
                                     "reversal": is_reversal, "rows": []}
                    order.append(key)
                sessions[key]["rows"].append(
                    {"date": date, "desc": desc, "pts": pts, "bal": bal})
        records = []
        rev_by_session = {s: v for (s, r), v in sessions.items() if r}
        for (session, is_rev), agg in sessions.items():
            if is_rev:
                continue
            rows = agg["rows"]
            main_date = rows[0]["date"]
            try:
                d0 = datetime.strptime(main_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (win_start <= d0 <= today):
                continue
            spent = -sum(r["pts"] for r in rows if r["pts"] < 0)
            fee = -sum(r["pts"] for r in rows
                       if r["pts"] < 0 and "手续费" in r["desc"])
            # 兑换物名：优先主行描述（去"兑换"前缀与括号注记），缺失用档位表
            name = rows[0]["desc"]
            name = re.sub(r"^兑换", "", name).strip()
            name = re.sub(r"[（(][^）)]*[）)]$", "", name).strip()
            tname, ticon = REDEEM_TIERS.get(agg["tier"], ("", "🎁"))
            if not name:
                name = tname
            bal_after = None
            for r in rows:
                if r["bal"] is not None:
                    bal_after = r["bal"]
            rec = {"date": main_date, "item": name, "icon": ticon,
                   "spent": spent, "fee": fee, "balance_after": bal_after,
                   "revoked": False, "refunded": 0, "revoke_reason": ""}
            rv = rev_by_session.get(session)
            if rv:
                rec["revoked"] = True
                rec["refunded"] = sum(r["pts"] for r in rv["rows"] if r["pts"] > 0)
                reason = rv["rows"][0]["desc"]
                reason = re.sub(r"^(冲正|撤销)[：:\s]*", "", reason).strip()
                rec["revoke_reason"] = reason
            records.append(rec)
        records.sort(key=lambda r: r["date"], reverse=True)
        by_grade[gk] = records
    return {"schema_version": 1, "window_days": 15, "by_grade": by_grade}


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


def _ledger_fingerprint():
    """台账指纹（读入顺序保护，DeepSeek 终审 2026-10-23，发现2）：
    生成期间台账被追加（补记与同步同刻竞态，如 09:58 撞点）时指纹
    变化 -> main 以最新台账重跑一次，杜绝以半旧快照发布。"""
    try:
        st = LEDGER.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _assert_badge_consistency(events, badges_doc, goals_doc):
    """一致性守卫（DeepSeek 终审 2026-10-23）：本周徽章带与目标榜
    obtained/count 必须与台账徽章事件逐项对拍；不齐 -> 抛错熔断不发布
    并留痕，杜绝"已发放未显示"静默重演。"""
    cur = badges_doc["week"]["cycle_week"]
    catalog_keys = {k for k, _, _, _ in BADGE_CATALOG}
    legacy_keys = set(LEGACY_BADGES.keys())
    problems = []
    for gk in ("g5", "g6"):
        evs = events.get(gk, [])
        held, cnt, late_held = {}, {}, {}
        for e in evs:
            wk = e.get("cycle_week") or cycle_week_of(e["date"])
            if wk == cur:
                cnt[e["key"]] = cnt.get(e["key"], 0) + 1
                if e["key"] not in held or e["date"] < held[e["key"]]:
                    held[e["key"]] = e["date"]
            elif e.get("late_cycle_recorded") and \
                    cycle_week_of(e.get("post_date") or e["date"]) == cur:
                if e["key"] not in late_held or e["date"] < late_held[e["key"]]:
                    late_held[e["key"]] = e["date"]
        strip_items = badges_doc["by_grade"].get(gk, [])
        for k, c in cnt.items():
            if not any(it["key"] == k and it.get("count") == c
                       and not it.get("late") for it in strip_items):
                problems.append("%s 徽章带 %s 当周计数不符" % (gk, k))
        for k in late_held:
            if not any(it["key"] == k and it.get("late") for it in strip_items):
                problems.append("%s 徽章带 %s 补发条目缺失" % (gk, k))
        goals = {en["key"]: en for en in goals_doc["by_grade"].get(gk, [])}
        for k in set(list(held) + list(late_held)):
            if k in legacy_keys or k not in catalog_keys:
                continue
            en = goals.get(k)
            if not en or not en.get("obtained"):
                problems.append("%s 目标榜 %s 未点亮" % (gk, k))
            elif en.get("count", 0) != cnt.get(k, 0):
                problems.append("%s 目标榜 %s count 不符" % (gk, k))
    if problems:
        log("badge_guard_fail", "; ".join(problems)[:200])
        raise ValueError("徽章一致性守卫失败：" + "; ".join(problems))


_RACE_RETRIED = [False]


def _strip_board_names(obj):
    """看板禁词（2026-10-24 家长令）：发布数据不得出现 deepseek/gemini 两词。
    递归做中性替换（终审/初审），只动发布副本，源文件原文不动。"""
    if isinstance(obj, str):
        return re.sub(r'(?i)deepseek', '终审', re.sub(r'(?i)gemini', '初审', obj))
    if isinstance(obj, list):
        return [_strip_board_names(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _strip_board_names(v) for k, v in obj.items()}
    return obj

def main():
    # 静默窗闸门（DeepSeek 裁定1 2026-10-20：静默窗全域生效，闸门落脚本层）：
    # [00:00,06:00) 内不 build、退出码 0；与 build_stats/build_trends 同口径，
    # 窗口边界只认 sync_balance.BUILD_SILENT_WINDOW 单点定义。
    if sb.in_build_silent_window():
        log("skip_silent_window", "skip: in silent window [00:00,06:00)")
        return 0
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

    led_fp = _ledger_fingerprint()
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
            # P2（DeepSeek 审查 2026-10-20）：进度计算持续失败要升级告警——
            # 仿 parse_error 模式（看本次之前最近 2 条日志状态），连续第 3 次
            # 失败写 FAIL_LOG ALERT，避免进度长期静默消失无人知。
            try:
                _tail = LOG_FILE.read_text(encoding="utf-8").splitlines()[-2:]
                _prev = [json.loads(_l).get("status") for _l in _tail]
            except Exception:
                _prev = []
            _alert = _prev.count("progress_error") >= 2
            log("progress_error", str(_pe)[:200], {"alert": _alert})
            if _alert:
                fail("ALERT: 徽章进度连续 3 次计算失败，需人工介入")
            progress = None
        docs = {
            "data/badges.json": build_badges(events),
            "data/badge_goals.json": build_badge_goals(events, progress),
            "data/shop.json": build_shop(balances),
            "data/rules.json": parse_rules(),
            "data/redeem_log.json": build_redeem_log(
                LEDGER.read_text(encoding="utf-8")),
        }
        # 看板禁词（2026-10-24 家长令）：发布数据中性替换，源文件原文不动。
        docs = {k: _strip_board_names(v) for k, v in docs.items()}
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
        # 一致性守卫（DeepSeek 终审 2026-10-23）：徽章带/目标榜与台账
        # 事件对拍不齐 -> 熔断不发布（走 parse_error 路径并留痕）。
        _assert_badge_consistency(events, docs["data/badges.json"],
                                  docs["data/badge_goals.json"])
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

    # 读入顺序保护：生成期间台账被追加（补记同刻竞态）-> 释放锁后以
    # 最新台账完整重跑一次（仅一次，避免活锁）；重跑仍遇竞态则以其
    # 最新构建发布并已留痕。
    if _ledger_fingerprint() != led_fp and not _RACE_RETRIED[0]:
        _RACE_RETRIED[0] = True
        log("ledger_race_rebuild",
            "生成期间台账被追加，以最新台账重跑一次（补记同刻竞态防护）")
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass
        return main()

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

    # c项串行化（2026-10-26）：git 区段持共享 git_push.lock（与 do_push/build_stats/watchd 同一把），
    # 60s 未获得则跳过本次推送（fail-safe，本地文件保留待下次重试）；/tmp 本锁仍作本进程单例。
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import repo_lock as _repo_lock
    except Exception as _rl_e:
        log("repo_lock_import_fail", "repo_lock 导入失败，跳过本次推送：%s" % _rl_e)
        return 4
    _rl = _repo_lock.repo_lock(timeout=60)
    if not _rl.__enter__():
        log("repo_lock_busy", "git_push.lock 60s 未获得，跳过本次推送")
        return 4
    try:
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
            # P1#3 修复（DeepSeek 审查 2026-10-20）：git 的 "nothing to commit" 标准串
            # 实际输出在 stdout（本机 git 2.43 实测、stderr 为空），只查 stderr 会把
            # 无变更误判为真失败 return 3。改查 stdout+stderr 合并文本，双子串合取不变。
            _err = (cr.stdout or "") + (cr.stderr or "")
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
    finally:
        _rl.__exit__()


if __name__ == "__main__":
    sys.exit(main())

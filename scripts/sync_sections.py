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
    ("baifentupo",       "满分之星",     "💯", "考试100分即可获得"),
    ("zonghezhixing",    "综合之星",   "🏅", "一周内≥4类不同徽章"),
    ("tupoxiaoyongshi",  "突破小勇士", "🛡️", "同一类错题连续3次订正全对"),
    ("jianchixiaoyongshi","坚持小勇士", "🔥", "连续14天完成同一固定挑战"),
    ("hezuozhixing",     "合作之星",   "🤝", "兄弟连续3天一起做家务（需两人协同、缺一不可，如递晾衣、共扫地）"),
    ("zhoujinbuzhixing", "周进步之星", "🚀", "正确率/分数较上周明显进步"),
    ("sanheyi",          "三合一快手", "⚡", "同日独立完成3项不同基础家务"),
    ("xiaolvxiaoshandian", "效率小闪电", "💨", "当日所有任务22:00前全部完成（可叠加）"),
    ("xidengxiaochaoren", "熄灯小超人", "🌙", "连续5天23:00前睡觉（中断重置）"),
    ("jiawujielishou",   "家务接力手", "🔄", "连续3天每天≥1项家务"),
    ("sanribuduan",      "三日不断",   "📆", "连续3天不断档/全勤一周取高档"),
    ("shuxiezhixing",    "书写之星",   "✍️", "连续N次作业书写零扣分"),
    ("dancizhixing",     "单词之星",   "🔤", "单次单词听写全对（≥10词）"),
    ("moxiezhixing",     "默写之星",   "📝", "单次古诗文/课文默写全对"),
    ("sikaozhixing",     "思考之星",   "🧠", "作业有可辨识推导文字+题号回链"),
    ("zhenglizhixing",   "整理之星",   "🗂️", "连续5天自主整理书包/书桌"),
    ("jinbumiao",        "进步小苗",   "🌱", "单科单次作业正确率较上次+15百分点"),
    ("biaodazhixing",    "表达之星",   "🗣️", "一周内口头表达/复述≥2次"),
    ("jiawuxiaobangshou","家务小帮手", "🏠", "单次完成1项基础家务"),
    ("zhengdalidaren",   "整理小达人", "🧹", "周内累计5天整理类家务"),
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
            active.setdefault(key, []).append(
                {"key": key, "date": date, "icon": icon, "name": name})
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
    by_grade = {}
    for gk in ("g5", "g6"):
        week_events = [e for e in events.get(gk, []) if ws_s <= e["date"] <= we_s]
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
        held = {}
        cnt = {}
        for e in events.get(gk, []):
            if e["key"] not in held or e["date"] < held[e["key"]]:
                held[e["key"]] = e["date"]
            cnt[e["key"]] = cnt.get(e["key"], 0) + 1
        by_grade[gk] = [{"key": k, "obtained": k in held,
                         "date": held.get(k, ""), "count": cnt.get(k, 0)}
                        for k, _, _, _ in BADGE_CATALOG]
    return {"schema_version": 1, "catalog": catalog, "by_grade": by_grade}


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
                    if text.startswith(("互斥", "将功补过", "作弊疑点")):
                        pts = None
                    else:
                        pm = PTS_RE.search(text)
                        pts = pm.group(0) if pm else None
                    items.append({"id": "%s%d" % (key[0], len(items) + 1),
                                  "text": text, "pts": pts, "remark": remark})
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

#!/usr/bin/env python3
"""看板真实数据生成器（DeepSeek 方案 KB-STATS-AUTO-2026-10-19）。

链路：VM 本地 cron（全天，Asia/Shanghai，每 5 分钟；2026-10-20 起取消时间窗口门控，
      2026-10-20 DeepSeek 终审 KB-STATS-AUTO §2.2 废止）→ 本脚本 → data/*.json
      → git commit+push → GitHub workflow（on: push）部署到 Pages。

产出（幂等：内容无变化则不 commit）：
  data/stats_grade_{5,6}.json   余额（§1.1，低频强一致，带 checksum）
  data/points_log_{5,6}.json    积分日志（§1.2+R3：近7天/≤20条，含 filtered_count/filtered）
  data/shop_grade_{5,6}.json    商店档位进度（R1：规则手册第85行档位表，本期接入）

红线：
  - 台账只读，绝不写台账。
  - 解析失败（§7.1）→ 不产出/不覆盖/不 commit，写 logs/build_stats.err。
  - reason 只输出 simplify_desc() 简化结果 + B5 断言；命中即替换为"学习记录"+filtered。
  - 模板三重替换（B2+B4）+ R4 diff 校验；超限即停手不 commit。

用法：
  python3 scripts/build_stats.py            正常运行（cron 调用）
  python3 scripts/build_stats.py --selftest 自测（放行条件2同日排序单测 + §7.1/7.3/7.5）
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

import _assert_lib
from build_kanban import simplify_desc  # 只读复用，不得私改（§5.2）
import sync_balance as sb  # M1（2026-10-20）：复用 norm_date，统一日期归一口径

LEDGER = os.environ.get(
    "LEDGER_PATH",
    os.path.expanduser("~/workspace/goals/goal/hidden_files/积分记录.md"))
DATA_DIR = os.path.join(REPO_DIR, "data")
LOGS_DIR = os.path.join(REPO_DIR, "logs")
TEMPLATE = os.path.join(REPO_DIR, "templates", "积分看板_全新版式样板.html")
TZ = timezone(timedelta(hours=8), "CST")

# R1：档位表（规则手册第85行，唯一权威；变动须走规则手册修订）
SHOP_TIERS = [
    {"pts": 70, "name": "30分钟Minecraft", "kind": "Minecraft"},
    {"pts": 100, "name": "1小时Minecraft", "kind": "Minecraft"},
    {"pts": 150, "name": "1小时和平精英\n或2小时Minecraft", "kind": "和平精英"},
    {"pts": 250, "name": "2小时和平精英", "kind": "和平精英"},
    {"pts": 600, "name": "愿望50元", "kind": "愿望"},
    {"pts": 1000, "name": "愿望100元", "kind": "愿望"},
]

NEG_MINUS = "−"  # U+2212 全角减号（用户拍板1：负余额静态显示 −N）

# 积分日志计分时间（KB-TIME-2026-10-19，DeepSeek 终审）：
# 台账 key 注释追加 @HH:MM 即为该条目的计分时间（唯一时间源红线：只认积分记录.md）。
TIME_ANNOT_RE = re.compile(r"@(\d{2}):(\d{2})")
KEY_COMMENT_RE = re.compile(r"<!--\s*key:\s*(.*?)\s*-->")
TIME_SOURCE_ENUM = ("submit", "parent_confirm", "entry", "deadline")
TIME_SOURCE_FILE = os.path.join(REPO_DIR, "data_src", "points_time_source.json")
TIME_G6_DATE = "2026-10-19"  # 生效日：complete_year(日期) > 此日 且无 @HH:MM → 熔断
POINTS_LOG_SCHEMA = "1.1"

ROW_RE = re.compile(
    # N3 收口（2026-10-20）：日期接受面与 sync_balance.ROW_PAT 对齐
    # （\d{1,2}-\d{1,2} 或 YYYY-MM-DD），存储在 parse_rows 内归一为零填充 MM-DD。
    r"^\|\s*(\d{1,2}-\d{1,2}|\d{4}-\d{2}-\d{2})\s*\|\s*(.*?)\s*\|\s*([+-]?\d+)\s*\|\s*([+-]?\d+)\s*\|"
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def log(msg):
    print("[build_stats] %s" % msg, flush=True)


def warn(msg):
    print("[build_stats][WARN] %s" % msg, flush=True)


def err_write(msg):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(os.path.join(LOGS_DIR, "build_stats.err"), "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (datetime.now(TZ).isoformat(timespec="seconds"), msg))


def git(*args):
    return subprocess.run(
        ["git", "-C", REPO_DIR] + list(args),
        capture_output=True, text=True, timeout=60,
    )


# ---------------------------------------------------------------------------
# 台账解析（B1 断言内建）
# ---------------------------------------------------------------------------
def read_ledger_blocks(ledger_path=LEDGER):
    """按 ## 五年级 / ## 六年级 切块；遇下一个 ## 标题截断。

    B1/核实1补断言：切块非空、含 <!-- key:、'历史归档' 不在块内，否则 raise。
    """
    with open(ledger_path, encoding="utf-8") as f:
        lines = f.readlines()

    def cut(title):
        start = None
        for i, ln in enumerate(lines):
            if ln.strip() == "## " + title:
                start = i
                break
        if start is None:
            raise ValueError("台账缺少分节标记：## %s" % title)
        buf = []
        for ln in lines[start + 1:]:
            if ln.startswith("## "):
                break
            buf.append(ln)
        block = "".join(buf)
        # --- M1（DeepSeek终审 2026-10-20）：校验类逻辑禁用 assert
        # （python3 -O 下 assert 被剥离，会静默失效），改显式 raise ---
        if not block.strip():
            raise ValueError("分节 ## %s 切块为空" % title)
        if "<!-- key:" not in block:
            raise ValueError("分节 ## %s 切块不含 <!-- key: 记录" % title)
        if "历史归档" in block:
            raise ValueError("分节 ## %s 切块混入历史归档" % title)
        return block

    return {5: cut("五年级"), 6: cut("六年级")}


def parse_rows(block, grade):
    """解析 | 日期 | 描述 | 积分 | 余额 | 行；保持台账行顺序。

    畸形行（像数据行但正则不匹配）→ raise（§7.1：不产出）。
    同时解析 key 注释中的 @HH:MM（计分时间，KB-TIME-2026-10-19）；
    @HH:MM 存在但格式非法 → raise（不得静默吞掉，走熔断）。
    """
    rows = []
    for ln in block.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        if "日期" in s and "描述" in s:
            continue  # 表头
        if re.match(r"^\|\s*:?-+", s):
            continue  # 分隔行
        m = ROW_RE.match(s)
        if not m:
            raise ValueError("台账 %d 年级存在无法解析的行：%r" % (grade, s[:80]))
        raw_date, desc, pts_s, bal_s = m.groups()
        # N3 收口：存储统一零填充 MM-DD（经 sync_balance.norm_date 归一后取
        # 月-日部分），下游 complete_year/排序/窗口口径不变。
        mm_dd = sb.norm_date(raw_date)[5:]
        pts = int(pts_s)
        # 允许负数（§7.2 负余额照常输出；交叉契约见 sync_sections.build_shop N1 方案A：
        # 负余额时 progress 钳0、balance 保留真实负值、redeemable 恒等式成立）
        bal = int(bal_s)
        # --- 计分时间解析（KB-TIME） ---
        key_comment = ""
        km = KEY_COMMENT_RE.search(s)
        if km:
            key_comment = km.group(1)
        key = TIME_ANNOT_RE.sub("", key_comment).strip()  # 去掉 @HH:MM 后的纯 key
        annot_time = None
        ta = TIME_ANNOT_RE.search(key_comment)
        if ta:
            hh, mm = int(ta.group(1)), int(ta.group(2))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError("台账 %d 年级 @HH:MM 越界：%r" % (grade, s[:80]))
            annot_time = "%02d:%02d" % (hh, mm)
        rows.append({"date": mm_dd, "desc": desc, "pts": pts, "balance": bal,
                     "key": key, "annot_time": annot_time})
    if not rows:
        raise ValueError("台账 %d 年级未解析出任何数据行" % grade)
    # S3 修复（2026-10-20）：余额链断裂改为熔断（fail-closed），与 sync_balance.parse_ledger
    # 的链式校验对齐。期初行（desc 含"期初余额"）重置基线。DeepSeek 终审阻断项。
    prev = None
    for r in rows:
        if "期初余额" in r["desc"]:
            prev = r["balance"]
            continue
        if prev is not None and r["balance"] != prev + r["pts"]:
            raise ValueError("余额链断裂（%s 年级 %s）：%d+%d != %d" % (
                grade, r["date"], prev, r["pts"], r["balance"]))
        prev = r["balance"]
    return rows


def complete_year(mm_dd, today):
    """MM-DD 补全年：委托 sync_balance.norm_date（M1 统一口径，2026-10-20）。

    保留本函数名以兼容现有调用；逻辑与 sync_balance.parse_ledger 完全一致。
    """
    return sb.norm_date(mm_dd, today)


def load_time_sources():
    """读 data_src/points_time_source.json（DeepSeek 终审记录）；缺失 → 空映射（新条目默认 entry）。"""
    try:
        with open(TIME_SOURCE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        src = d.get("sources", {})
        return src if isinstance(src, dict) else {}
    except FileNotFoundError:
        return {}


DESC_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::\d{2})?")


def extract_desc_time(desc):
    """2026-10-20（用户要求积分日志精确到分）：从条目描述中提取台账记录时间。

    只用于生效日（2026-10-19）及之前的历史条目。新条目仍须用 @HH:MM，缺失即熔断。
    取描述中第一个合法的 HH:MM（照片时间/提交时间均属台账记录时间）。
    """
    m = DESC_TIME_RE.search(desc or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return "%02d:%02d" % (hh, mm)
    return None


def resolve_log_time(r, grade, today, sources):
    """KB-TIME-2026-10-19：由台账 @HH:MM 解析计分时间，附 G1–G6 校验门。

    2026-10-20 补充：历史条目（≤2026-10-19）缺 @HH:MM 时，从描述提取台账记录时间
    作回退（用户要求积分日志精确到分）；仍无时间 → (None, None)，前端只显示 MM-DD。
    返回 (time, time_source)，无时间 → (None, None)。
    触发熔断条件 → raise ValueError。
    """
    key = r.get("key", "")
    annot_time = r.get("annot_time")
    full_date = complete_year(r["date"], today)

    # 历史条目回退：@HH:MM 缺失时从描述提取台账记录时间
    if annot_time is None and full_date <= TIME_G6_DATE:
        annot_time = extract_desc_time(r["desc"])

    # G6：生效日后新增条目缺 @HH:MM → 熔断（描述回退不适用于新条目）
    if annot_time is None and full_date > TIME_G6_DATE:
        raise ValueError("G6 熔断：%s 年级 %s 条目（%s）缺 @HH:MM 入账时间" % (grade, r["date"], key[:40]))

    if annot_time is None:
        return None, None

    # G1：格式已在 parse_rows 校验（00-23/00-59），此处复核
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", annot_time):
        raise ValueError("G1 熔断：time 格式非法 %r（%s）" % (annot_time, key[:40]))

    # time_source：终审记录优先；新条目默认 entry（@HH:MM 即入账时刻 T3）
    rec = sources.get(key)
    if rec is not None:
        src = rec.get("source")
        if src not in TIME_SOURCE_ENUM:
            raise ValueError("G2 熔断：time_source 枚举外 %r（%s）" % (src, key[:40]))
        if rec.get("time") != annot_time:
            raise ValueError("G2 熔断：台账 @%s 与终审记录 %s 不一致（%s），疑似事后改动"
                             % (annot_time, rec.get("time"), key[:40]))
    else:
        src = "entry"

    # deadline 语义校验（DeepSeek 约束3）：仅报警不熔断
    if src == "deadline":
        if not re.search(r"没交|漏交|截止", r["desc"]):
            warn("deadline 源条目描述缺'没交/漏交/截止'语义（%s），请核对" % key[:40])
            err_write("DEADLINE_SEMANTIC grade=%d key=%s" % (grade, key[:40]))
        if r["pts"] >= 0:
            warn("deadline 源出现在非扣分条目（%s），请核对" % key[:40])
            err_write("DEADLINE_NONMINUS grade=%d key=%s" % (grade, key[:40]))

    return annot_time, src


# ---------------------------------------------------------------------------
# 生成：stats / log / shop
# ---------------------------------------------------------------------------
def build_stats(grade, rows, ledger_sha16, today, generated_at):
    last = rows[-1]
    d = {
        "schema_version": "1.0",
        "grade": grade,
        "balance": last["balance"],
        "ledger_date": complete_year(last["date"], today),
        "generated_at": generated_at,
        "source": "hidden_files/积分记录.md",
        "checksum": ledger_sha16,
    }
    _assert_lib.assert_stats_schema(d, "stats_grade_%d.json" % grade)
    return d


def build_log(grade, rows, today, generated_at):
    """§1.2 + R3 + KB-TIME-2026-10-19 + KB-SORT-2026-10-20：近7天、(日期,时刻)倒序、≤20条；
    同日有时刻者按时刻倒序在前、无时刻者随后保持台账正序；同日无时刻条目之间的
    相对顺序 = 台账原始录入顺序，不代表发生先后（详见排序注释）；
    reason 经 simplify_desc + B5 断言；time 精确到分钟（台账 @HH:MM），schema 1.1。"""
    cutoff = today - timedelta(days=7)
    sources = load_time_sources()
    dated = []
    for idx, r in enumerate(rows):
        full = complete_year(r["date"], today)
        try:
            dt = datetime.strptime(full, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("非法日期：%s" % r["date"])
        if dt < cutoff:
            continue
        if "期初余额" in r["desc"]:
            continue  # 基线标记行，非积分变动，不进日志
        # KB-SORT-2026-10-20：排序需要时刻，故在此提前解析计分时间（G1–G6 门内建于
        # resolve_log_time）。副作用（已报 DeepSeek 终审）：G6 熔断与 G3 复核的覆盖范围
        # 由"排序后前 20 条"扩大为"窗口内全部条目"，与"新增条目必须标注 @HH:MM"红线一致。
        t, t_src = resolve_log_time(r, grade, today, sources)
        # G3：time 与 time_source 双向一致（resolve 已保证，此处复核）
        if (t is None) != (t_src is None):
            raise ValueError("G3 熔断：time/time_source 不一致（%s）" % r.get("key", "")[:40])
        dated.append((dt, t, t_src, idx, r))

    # 排序口径 KB-SORT-2026-10-20（用户要求"时间最新的在最前"，DeepSeek 终审裁决）：
    # ① 日期倒序；② 同日内有时刻条目排在无时刻条目之前；③ 有时刻者按时刻倒序；
    # ④ 同键（同日同时刻、同日均无时刻）保持台账正序（idx 正序）；其中无时刻条目间的
    #    相对顺序仅为台账录入顺序，不代表发生先后（DeepSeek 终审 A 点条件）。
    # 实现：先按 idx 正序稳定，再按 (日期, 有时刻标记, 时刻) 一次倒序稳定 → 同键保序。
    dated.sort(key=lambda x: x[3])
    dated.sort(key=lambda x: (x[0], 1 if x[1] else 0, x[1] or ""), reverse=True)

    truncated = len(dated) > 20
    items = []
    filtered_count = 0
    for dt, t, t_src, idx, r in dated[:20]:
        reason = simplify_desc(r["desc"])
        if len(reason) > 40:
            reason = reason[:40] + "…"  # 上限 40 字，超长截断加 …
        filtered = False
        hits = _assert_lib.find_forbidden_terms(reason)
        if hits:
            # B5：命中即替换，不阻断整体；明细打 WARN 供 DeepSeek 抽查
            warn("敏感词命中 grade=%d date=%s hits=%r → 替换为'学习记录'"
                 % (grade, r["date"], hits))
            err_write("FILTERED grade=%d date=%s hits=%s" % (grade, r["date"], ", ".join(hits)))
            reason = "学习记录"
            filtered = True
            filtered_count += 1
        pts = r["pts"]
        # KB-SORT-2026-10-20：t/t_src 已在排序前解析并通过 G3 复核（见 dated 构建处）
        items.append({
            "date": r["date"],  # MM-DD（本期不跨年；跨年时 complete_year 输出 YYYY-MM-DD）
            "time": t,  # HH:MM 或 null（无计分时间）
            "time_source": t_src,  # submit/parent_confirm/entry/deadline 或 null（溯源，不前端展示）
            "reason": reason,
            "pts": pts,
            "sign": "plus" if pts >= 0 else "minus",
            "filtered": filtered,
        })

    d = {
        "schema_version": POINTS_LOG_SCHEMA,
        "grade": grade,
        "generated_at": generated_at,
        "window": {"days": 7, "max_items": 20},  # P1#2（裁定2 甲）：与数据窗口近7天统一
        "items": items,
        "truncated": truncated,
        "filtered_count": filtered_count,
    }
    _assert_lib.assert_points_log_schema(d, "points_log_%d.json" % grade)
    return d


def build_shop(grade, balance, generated_at):
    d = {
        "schema_version": "1.0",
        "grade": grade,
        "tiers": [dict(t) for t in SHOP_TIERS],
        "current_balance": balance,
        "generated_at": generated_at,
        "rule_source": "规则手册第85行",
    }
    _assert_lib.assert_shop_schema(d, "shop_grade_%d.json" % grade)
    return d


# ---------------------------------------------------------------------------
# 模板三重替换（B2+B4）+ R4 diff 校验
# 模板余额注入已废止（2026-10-20 17:27 熔断修复：2cea4ca 按用户拍板将模板余额卡
# 静态值改为占位符'—'，禁止再把余额硬编码进模板；前端加载器已从 data/stats JSON
# 实时写入真实余额，本处不再做构建时替换）。
def r4_diff_check(snapshot_before):
    """R4：替换后 diff 与快照对比 —— 除 data/*.json 外只允许模板变化，且模板行数变动 ≤ 4。"""
    cur = git("-c", "core.quotepath=false", "diff", "--stat")
    if cur.returncode != 0:
        raise ValueError("git diff --stat 执行失败")  # P2：校验禁用 assert（M1 原则）
    allowed_data = {
        "data/stats_grade_5.json", "data/stats_grade_6.json",
        "data/points_log_5.json", "data/points_log_6.json",
        "data/shop_grade_5.json", "data/shop_grade_6.json",
    }
    template_name = "templates/积分看板_全新版式样板.html"
    changed = set()
    for ln in cur.stdout.splitlines():
        m = re.match(r"^\s*(.+?)\s+\|\s+(\d+)", ln)
        if m:
            changed.add(m.group(1).strip())
    unexpected = changed - allowed_data - {template_name}
    if unexpected:
        raise ValueError("R4 拦截：出现非预期变更文件 %r" % sorted(unexpected))
    if template_name in changed:
        numstat = git("-c", "core.quotepath=false", "diff", "--numstat", "--", template_name)
        parts = numstat.stdout.split()
        added, deleted = int(parts[0]), int(parts[1])
        if added + deleted > 4:
            raise ValueError("R4 拦截：模板行数变动 %d 超限（≤4）" % (added + deleted))
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def semantic_diff(old_obj, new_obj, generated_at):
    """幂等 + R3 单调性判定（DeepSeek 终审 2026-10-20 看板全栏目实时同步）。

    返回 (should_write: bool, reason: str)：
      - no_old            旧文件不存在/不可解析 → 写
      - no_semantic_change 忽略 generated_at 后语义一致 → 不写（真实幂等）
      - monotonic_guard    旧文件 generated_at 严格大于新值 → 不写（R3 防乱序回退）
      - changed            语义有变化且时间戳单调 → 写
    """
    if not isinstance(old_obj, dict):
        return True, "no_old"
    _o = dict(old_obj)
    _n = dict(new_obj)
    _o.pop("generated_at", None)
    _n.pop("generated_at", None)
    if _o == _n:
        return False, "no_semantic_change"
    _old_ga = old_obj.get("generated_at") or ""
    if _old_ga > generated_at:
        return False, "monotonic_guard"
    return True, "changed"


def write_json_atomic(path, obj):
    """写 JSON 后读回校验（§7.3 生成端）；失败 raise。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(tmp, encoding="utf-8") as f:
        back = json.load(f)
    if back != obj:
        # P2：校验禁用 assert（M1 原则）；raise 前先写诊断日志，调用方按熔断处理
        err_write("write_json_atomic 读回不一致：%s" % path)
        raise ValueError("JSON 写后读回不一致：%s" % path)
    os.replace(tmp, path)


def main():
    if "--selftest" in sys.argv:
        return selftest()

    # 静默窗闸门（DeepSeek 裁定1 2026-10-20：静默窗全域生效，闸门落脚本层）：
    # [00:00,06:00) 内不 build、退出码 0；watchd 事件触发与兜底 cron 调用同口径，
    # 窗口边界只认 sync_balance.BUILD_SILENT_WINDOW 单点定义。
    if sb.in_build_silent_window():
        _ts = datetime.now(TZ).isoformat(timespec="seconds")
        log("skip: in silent window [00:00,06:00) @ %s" % _ts)
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            with open(os.path.join(LOGS_DIR, "build_stats.log"), "a", encoding="utf-8") as _f:
                _f.write("%s skip: in silent window [00:00,06:00)\n" % _ts)
        except OSError:
            pass
        return 0

    # R3（DeepSeek 终审 2026-10-20 看板全栏目实时同步）：build 串行锁 ——
    # 守护进程事件触发与 15m/5m 兜底 cron 可能并发调用本脚本，NB 锁保证同一时刻
    # 只有一个 build 写 data/*.json，防产物交错 + generated_at 乱序。
    # 抢不到锁 → 跳过本轮（fail-safe），不报错。
    _lock_fp = open("/tmp/build_stats.lock", "w")
    try:
        fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("skip: build lock held（另一 build 正在运行，本轮跳过）")
        return 0

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    today = datetime.now(TZ).date()
    generated_at = datetime.now(TZ).isoformat(timespec="seconds")

    # R4-1（2026-10-20根治，DeepSeek方案）：先自动 stash 未提交改动 → pull --ff-only。
    # R4-2（2026-10-20再根治，用户报障+DeepSeek裁决）：pop 延后到全流程结束
    # （各 return 出口统一调 _restore_stash）。此前 pop 太早 → R4 把操作者
    # 未提交改动当异常拦截，失败回滚的 git checkout 把改动直接抹掉（已真实
    # 发生：前端规则渲染改动被回滚丢失）。pop 延后后，回滚只碰脚本自身产物。
    stash_r = git("stash", "push", "-u", "-m", "build_stats-auto")
    stashed = (stash_r.returncode == 0 and "Saved working directory" in stash_r.stdout)

    def _restore_stash():
        if not stashed:
            return
        pop_r = git("stash", "pop")
        if pop_r.returncode != 0:
            msg = "R4 告警：stash pop 冲突，改动保留在 stash 中，待人工处理"
            warn(msg)
            err_write("NEED-REVIEW " + msg + " :: " + (pop_r.stderr or "")[:200])

    # R4-3（2026-10-20，DeepSeek 方案+家长拍板）：弃用 git pull --ff-only
    # （偶发 fatal: Cannot fast-forward to multiple branches），改显式三步校验：
    # fetch → 比对 HEAD 与 origin/master（相等放行 / 本地是祖先则 ff 合并 /
    # 真分叉 exit 2 熔断）。校验全程在共享 git_push.lock 内（与 watchd /
    # task_ingest 串行）；瞬时失败重试 2 次（间隔 5s）再熔断。
    # R4"真分叉必须停"原则不变。
    import time as _time
    _GIT_PUSH_LOCK = os.path.join(os.path.dirname(REPO_DIR), "logs",
                                  "git_push.lock")

    def _acquire_git_lock(timeout=10):
        # P1#5 配套（2026-10-20）：watchd 持 git_push.lock 运行消费者时经
        # KANBAN_GIT_LOCK_HELD=1 声明——本进程已在外层临界区内，不再重复取锁
        # （flock 跨进程互斥，重复取必然超时误判）。返回 /dev/null 句柄占位。
        if os.environ.get("KANBAN_GIT_LOCK_HELD") == "1":
            return open(os.devnull, "w")
        os.makedirs(os.path.dirname(_GIT_PUSH_LOCK), exist_ok=True)
        fp = open(_GIT_PUSH_LOCK, "w")
        t0 = _time.time()
        while True:
            try:
                fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fp
            except OSError:
                if _time.time() - t0 >= timeout:
                    fp.close()
                    return None
                _time.sleep(0.5)

    def _sync_once():
        lock = _acquire_git_lock()
        if lock is None:
            return "git_push.lock 10s 未获得"
        try:
            fr = git("fetch", "origin")
            if fr.returncode != 0:
                return "fetch 失败：" + (fr.stderr or "")[:150]
            head = git("rev-parse", "HEAD").stdout.strip()
            remote = git("rev-parse", "origin/master").stdout.strip()
            if head == remote:
                return None
            anc = git("merge-base", "--is-ancestor", "HEAD", "origin/master")
            if anc.returncode == 0:
                mg = git("merge", "--ff-only", "origin/master")
                if mg.returncode != 0:
                    return "ff 合并失败：" + (mg.stderr or "")[:150]
                return None
            # P1#4 修复（DeepSeek 审查 2026-10-20）：origin 是 HEAD 的祖先 = 本地
            # 领先（上次 push 失败遗留未推送提交），不是真分叉——放行走正常
            # commit/push，把滞留提交一并推走；此前误判 DIVERGED 停手，
            # stats/log/shop 会一直停更到有其他推送者顺带推走为止。
            ahead = git("merge-base", "--is-ancestor", "origin/master", "HEAD")
            if ahead.returncode == 0:
                return None
            return "DIVERGED"
        finally:
            lock.close()

    sync_err = None
    for _attempt in range(3):
        sync_err = _sync_once()
        if sync_err is None or sync_err == "DIVERGED":
            break
        if _attempt < 2:
            _time.sleep(5)
    if sync_err is not None:
        if sync_err == "DIVERGED":
            msg = "R4 拦截：本地与 origin/master 真分叉，停手待人工处理"
        else:
            msg = "R4 拦截：同步校验失败（已重试2次）：" + sync_err
        warn(msg)
        err_write("NEED-REVIEW " + msg)
        _restore_stash()
        return 2
    snap = git("diff", "--stat").stdout

    try:
        with open(LEDGER, "rb") as f:
            ledger_bytes = f.read()
        ledger_sha16 = hashlib.sha256(ledger_bytes).hexdigest()[:16]
        blocks = read_ledger_blocks()
        all_rows = {g: parse_rows(blocks[g], g) for g in (5, 6)}
    except Exception as e:
        # §7.1：解析失败 → 不产出/不覆盖/不 commit
        msg = "台账解析失败（§7.1 熔断）：%s" % e
        warn(msg)
        err_write(msg)
        _restore_stash()
        return 2

    balances = {}
    for grade in (5, 6):
        rows = all_rows[grade]
        bal = rows[-1]["balance"]
        balances[grade] = bal
        if bal < 0:
            warn("§7.2 负余额：%d 年级余额 %d（照常输出，前端静态显示）" % (grade, bal))

    # N3 对拍观察（DeepSeek 审查 2026-10-20 第一步：先告警、后收口）：本脚本
    # parse_rows 与 sync_balance.parse_ledger 是两套独立台账解析器（日期接受面
    # 与期初基线口径曾分叉）。此处对同一台账并行比对双年级余额（两边都是链式
    # 校验，余额一致即结算结果一致）——不一致只告警留痕、不改变产出；收口
    # （日期接受面/存储归一对齐）在后续提交完成，此哨兵长期保留作回归监测。
    try:
        _alt = sb.parse_ledger(Path(LEDGER))
        for _g, _gk in ((5, "g5"), (6, "g6")):
            if _alt[_gk]["balance"] != balances[_g]:
                warn("N3 对拍不一致：%d 年级 本解析余额=%d / sync_balance 余额=%d"
                     % (_g, balances[_g], _alt[_gk]["balance"]))
                err_write("N3_DIVERGENCE grade=%d self_bal=%d alt_bal=%d"
                          % (_g, balances[_g], _alt[_gk]["balance"]))
    except Exception as _e:
        warn("N3 对拍自身异常（忽略，不影响产出）：%s" % str(_e)[:120])

    outputs = {}
    for grade in (5, 6):
        rows = all_rows[grade]
        outputs["data/stats_grade_%d.json" % grade] = build_stats(
            grade, rows, ledger_sha16, today, generated_at)
        outputs["data/points_log_%d.json" % grade] = build_log(
            grade, rows, today, generated_at)
        outputs["data/shop_grade_%d.json" % grade] = build_shop(
            grade, balances[grade], generated_at)

    # 写文件（原子写 + 读回校验；幂等判定见 semantic_diff）
    # 说明（DeepSeek 终审 2026-10-20）：忽略 generated_at 比较语义内容，
    # 无语义变化 → 不重写文件、不 commit。旧实现因 generated_at 每轮变化导致
    # 每次运行都 commit，闸门删除后会形成 push 风暴，威胁 GitHub Actions
    # 2000 分钟/月免费额度 —— 本修复为设计文档"无变更不 push"的真实落实。
    changed_files = []
    for rel, obj in outputs.items():
        path = os.path.join(REPO_DIR, rel)
        old_obj = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old_obj = json.load(f)
            except Exception:
                old_obj = None
        should_write, reason = semantic_diff(old_obj, obj, generated_at)
        if not should_write:
            if reason != "no_semantic_change":
                warn("跳过覆盖 %s（原因：%s）" % (rel, reason))
                err_write("SKIP_WRITE %s reason=%s" % (rel, reason))
            continue
        write_json_atomic(path, obj)
        changed_files.append((rel, old_obj is None))  # (路径, 是否为新建)

    if not changed_files:
        log("幂等：6 数据文件均无变化，不 commit")
        _restore_stash()
        return 0

    # R4 diff 校验
    try:
        r4_diff_check(snap)
    except (AssertionError, ValueError) as e:  # P2：r4 校验已改 raise ValueError
        msg = "NEED-REVIEW R4 diff 校验失败：%s" % e
        warn(msg)
        err_write(msg)
        # 回滚：模板 checkout；数据文件：新建的删除、已存在的 checkout
        git("checkout", "--", TEMPLATE)
        for rel, was_new in changed_files:
            p = os.path.join(REPO_DIR, rel)
            if was_new and os.path.exists(p):
                os.remove(p)
            elif not was_new:
                git("checkout", "--", rel)
        _restore_stash()
        return 2

    # Q2（DeepSeek终审 2026-10-20《自动同步cron提交范围限制》）：
    # 提交前检查 scripts/ 是否有未提交改动；有则本次不提交 + 告警日志（fail-safe）。
    # 防 A类修复被功能提交卷入（程序异常台账 #1/#2）重演。
    _dirty = git("status", "--porcelain", "--", "scripts/")
    _dirty_out = (_dirty.stdout or "").strip()
    if _dirty_out:
        _dirty_files = "; ".join(l[:80] for l in _dirty_out.splitlines()[:10])
        _alert = "%s | build_stats.py | scripts/ 脏，跳过本次提交 | %s" % (
            datetime.now(TZ).isoformat(timespec="seconds"), _dirty_files)
        warn(_alert)
        log("commit_guard_skip: " + _alert)
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            with open(os.path.join(LOGS_DIR, "cron_commit_guard.log"), "a", encoding="utf-8") as _f:
                _f.write(_alert + "\n")
        except Exception:
            pass
        _restore_stash()
        return 0

    # 提交（只加本方案的 6 数据文件；模板余额注入已废止，不再提交模板）
    git("add", *outputs.keys())
    msg = "chore(balance): sync from ledger [auto]" 
    cp = git("commit", "-m", msg)
    if cp.returncode != 0:
        warn("git commit 失败：%s" % cp.stderr[:200])
        err_write("git commit 失败：" + cp.stderr[:200])
        _restore_stash()
        return 3
    pp = git("push")
    if pp.returncode != 0:
        warn("git push 失败（本地文件已保留，下次重试）：%s" % pp.stderr[:200])
        err_write("git push 失败：" + pp.stderr[:200])
        _restore_stash()
        return 3
    log("已提交并推送：%s" % msg)
    with open(os.path.join(LOGS_DIR, "build_stats.log"), "a", encoding="utf-8") as f:
        f.write("%s ledger_sha256=%s balances=%s commit=%s\n" % (
            generated_at, hashlib.sha256(ledger_bytes).hexdigest(),
            balances, msg))
    _restore_stash()
    return 0


# ---------------------------------------------------------------------------
# 自测：放行条件2（同日排序单测）+ §7.1 / §7.3 / §7.5
# ---------------------------------------------------------------------------
def selftest():
    """自测 4 项，各录 1 段日志到 logs/build_stats_selftest.log。"""
    os.makedirs(LOGS_DIR, exist_ok=True)
    slog = os.path.join(LOGS_DIR, "build_stats_selftest.log")
    results = []

    def rec(name, ok, detail):
        results.append((name, ok, detail))
        line = "%s [%s] %s :: %s" % (
            datetime.now(TZ).isoformat(timespec="seconds"),
            "PASS" if ok else "FAIL", name, detail)
        print(line, flush=True)
        with open(slog, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    today = datetime.now(TZ).date()
    gen = datetime.now(TZ).isoformat(timespec="seconds")

    # --- T1 放行条件2：同日多条排序（日期倒序，同日按台账正序；KB-SORT-2026-10-20 后：
    # 本用例均为无时刻历史行，兼容新口径）---
    try:
        rows = [
            {"date": "10-17", "desc": "第一条", "pts": 1, "balance": 1},
            {"date": "10-18", "desc": "第二条", "pts": 1, "balance": 2},
            {"date": "10-17", "desc": "第三条", "pts": 1, "balance": 3},
            {"date": "10-19", "desc": "第四条", "pts": 1, "balance": 4},
            {"date": "10-17", "desc": "第五条", "pts": 1, "balance": 5},
        ]
        d = build_log(5, rows, today, gen)
        got = [it["reason"] for it in d["items"]]
        # simplify_desc("第一条")=="第一条" 等；期望：10-19第四条, 10-18第二条, 10-17第一条/第三条/第五条
        exp = ["第四条", "第二条", "第一条", "第三条", "第五条"]
        assert got == exp, "排序错误：%r != %r" % (got, exp)
        # S1 防回归（2026-10-20）：死代码删除后，schema 必须为 1.1（含 time/time_source 字段）
        assert d["schema_version"] == "1.1", "schema_version=%r（期望 1.1，死代码回退？）" % d.get("schema_version")
        rec("T1 同日排序单测", True, "输出顺序=%s" % got)
    except Exception as e:
        rec("T1 同日排序单测", False, str(e)[:200])

    # --- T2 §7.1：台账解析失败 → 不产出 ---
    try:
        bad_block = ("## X\n\n| 日期 | 描述 | 积分 | 余额 |\n| --- |\n"
                     "| 10-19 | 正常行 | +1 | 1 | <!-- key:a -->\n"
                     "| 10-19 | 坏行缺列 |\n")
        try:
            parse_rows(bad_block, 5)
            raise SystemExit("畸形行未被拦截")
        except ValueError as ve:
            assert "无法解析" in str(ve)
        rec("T2 §7.1 解析失败熔断", True, "畸形行 raise ValueError，不产出")
    except SystemExit as se:
        rec("T2 §7.1 解析失败熔断", False, str(se))
    except Exception as e:
        rec("T2 §7.1 解析失败熔断", False, str(e)[:200])

    # --- T2b 核实1补断言：切块无 key → 报错 ---
    try:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tf:
            tf.write("## 五年级\n\n| 10-19 | 无key行 | +1 | 1 |\n\n## 六年级\n\n| x |\n")
            tp = tf.name
        try:
            read_ledger_blocks(tp)
            rec("T2b 切块断言", False, "无 key 切块未被拦截")
        except (AssertionError, ValueError) as ve:
            rec("T2b 切块断言", True, "拦截信息：%s" % str(ve)[:120])
        finally:
            os.unlink(tp)
    except Exception as e:
        rec("T2b 切块断言", False, str(e)[:200])

    # --- T2c S3（2026-10-20）：余额链断裂 → 熔断（fail-closed），不再 warn 放行 ---
    try:
        block_ok = ("| 10-19 | 期初余额 | +100 | 100 |\n"
                    "| 10-19 | 正常+5 | +5 | 105 |\n")
        rows = parse_rows(block_ok, 5)
        assert len(rows) == 2, "期初行识别失败"
        rec("T2c 余额链正常+期初行", True, "2行解析通过，期初行重置基线")
    except Exception as e:
        rec("T2c 余额链正常+期初行", False, str(e)[:200])
    try:
        block_bad = ("| 10-19 | 期初余额 | +100 | 100 |\n"
                     "| 10-19 | 手误行 | +5 | 999 |\n")  # 100+5 != 999，链断裂
        try:
            parse_rows(block_bad, 5)
            rec("T2c 余额链断裂熔断", False, "断裂未被拦截（仍在 warn 放行）")
        except ValueError as ve:
            assert "余额链断裂" in str(ve)
            rec("T2c 余额链断裂熔断", True, "熔断信息：%s" % str(ve)[:80])
    except Exception as e:
        rec("T2c 余额链断裂熔断", False, str(e)[:200])

    # --- T3 §7.3：JSON 写后读回校验 ---
    try:
        with open("/tmp/_selftest_json", "w", encoding="utf-8") as f:
            f.write("{坏 json")
        try:
            with open("/tmp/_selftest_json", encoding="utf-8") as f:
                json.load(f)
            rec("T3 §7.3 JSON损坏", False, "坏 JSON 未被拦截")
        except json.JSONDecodeError:
            rec("T3 §7.3 JSON损坏", True, "JSONDecodeError 正确抛出；前端按§7.3降级，生成端写后读回校验")
    except Exception as e:
        rec("T3 §7.3 JSON损坏", False, str(e)[:200])

    # --- T4 §7.5：敏感词命中 → 替换+计数，不阻断 ---
    try:
        rows = [
            {"date": "10-19", "desc": "数学作业按时完成", "pts": 2, "balance": 10},
            # "家长特批"在括号外，simplify_desc 去不掉，必须被 B5 断言捕获
            {"date": "10-19", "desc": "晾衣服家长特批+3", "pts": 3, "balance": 13},
            {"date": "10-18", "desc": "语文抄写迟交", "pts": -2, "balance": 11},
        ]
        d = build_log(5, rows, today, gen)
        reasons = [(it["date"], it["reason"], it["filtered"]) for it in d["items"]]
        assert d["filtered_count"] == 1, "filtered_count=%d" % d["filtered_count"]
        hit = [r for r in reasons if r[1] == "学习记录"]
        assert len(hit) == 1 and hit[0][2] is True
        # 生成端 schema 断言本身应通过（含 filtered 字段）
        _assert_lib.assert_points_log_schema(d, "points_log_5.json")
        # workflow 端零命中：替换后的 reason 不得再命中
        for _, reason, _ in reasons:
            assert not _assert_lib.find_forbidden_terms(reason), reason
        rec("T4 §7.5 敏感词过滤", True, "items=%s" % reasons)
    except Exception as e:
        rec("T4 §7.5 敏感词过滤", False, str(e)[:200])

    # --- T5 负余额：允许输出（§7.2），模板静态 −N ---
    try:
        rows = [{"date": "10-19", "desc": "测试扣分", "pts": -5, "balance": -5}]
        d = build_stats(5, rows, "0" * 16, today, gen)
        assert d["balance"] == -5
        rec("T5 §7.2 负余额", True, "balance=-5 允许输出；前端走静态 −N 分支")
    except Exception as e:
        rec("T5 §7.2 负余额", False, str(e)[:200])

    # --- T6a/T6b/T6c KB-TIME G6：生效日后缺标注熔断（正向/负向/边界）---
    # 注：用 mock_today=2026-10-25（complete_year 跨年分支会把"今天之后"的日期视为去年，
    # 故在真实 today=10-19 时 10-20 不可达，mock 是为了让 G6 负向场景可测试）
    try:
        import datetime as _dt
        mock_today = _dt.date(2026, 10, 25)
        # T6a 负向：2026-10-20 条目缺 @HH:MM → 必须熔断
        r = {"key": "t6a_key", "date": "10-20", "annot_time": None, "desc": "测试"}
        try:
            resolve_log_time(r, 5, mock_today, {})
            rec("T6a G6负向熔断", False, "10-20缺标注未熔断")
        except ValueError as ve:
            assert "G6" in str(ve)
            rec("T6a G6负向熔断", True, "熔断信息：%s" % str(ve)[:60])
        # T6b 正向：2026-10-20 条目有 @HH:MM → 放行
        r2 = {"key": "t6b_key", "date": "10-20", "annot_time": "19:30", "desc": "测试"}
        t, src = resolve_log_time(r2, 5, mock_today, {})
        assert t == "19:30" and src == "entry"
        rec("T6b G6正向放行", True, "time=19:30, source=entry")
        # T6c 边界：生效日当日 2026-10-19 缺标注 → 豁免放行
        r3 = {"key": "t6c_key", "date": "10-19", "annot_time": None, "desc": "测试"}
        t3, _ = resolve_log_time(r3, 5, mock_today, {})
        assert t3 is None
        rec("T6c G6边界豁免", True, "10-19缺标注豁免，time=None")
    except Exception as e:
        rec("T6a/T6b/T6c G6", False, str(e)[:200])

    # --- T7 幂等 + R3 单调性判定（DeepSeek 终审 2026-10-20 看板全栏目实时同步）---
    try:
        ga_new = "2026-10-20T11:00:00+08:00"
        ga_old = "2026-10-20T10:00:00+08:00"
        ga_future = "2026-10-20T12:00:00+08:00"
        base = {"schema_version": "1.0", "grade": 5, "balance": 10}
        # 无语义变化（仅 generated_at 不同）→ 不写
        w, r = semantic_diff(dict(base, generated_at=ga_old), dict(base, generated_at=ga_new), ga_new)
        assert (w, r) == (False, "no_semantic_change"), (w, r)
        # 语义变化 + 时间戳单调 → 写
        w, r = semantic_diff(dict(base, generated_at=ga_old), dict(base, balance=11, generated_at=ga_new), ga_new)
        assert (w, r) == (True, "changed"), (w, r)
        # 语义变化但旧时间戳在未来 → 单调性拦截
        w, r = semantic_diff(dict(base, generated_at=ga_future), dict(base, balance=11, generated_at=ga_new), ga_new)
        assert (w, r) == (False, "monotonic_guard"), (w, r)
        # 无旧文件 → 写
        w, r = semantic_diff(None, dict(base, generated_at=ga_new), ga_new)
        assert (w, r) == (True, "no_old"), (w, r)
        rec("T7 幂等+R3单调性", True, "semantic_diff 四分支（no_semantic_change/changed/monotonic_guard/no_old）均正确")
    except Exception as e:
        rec("T7 幂等+R3单调性", False, str(e)[:200])

    # --- T8 KB-SORT-2026-10-20：(日期,时刻)倒序（用户要求"时间最新在最前"）---
    # 口径：日期倒序 → 同日有时刻在前、无时刻在后 → 时刻倒序 → 同键保台账序
    try:
        import datetime as _dt2
        mock_today2 = _dt2.date(2026, 10, 25)
        rows = [
            {"date": "10-19", "desc": "无时刻甲", "pts": 1, "balance": 1, "key": "t8k0", "annot_time": None},
            {"date": "10-20", "desc": "早九点", "pts": 1, "balance": 2, "key": "t8k1", "annot_time": "09:00"},
            {"date": "10-20", "desc": "晚十点", "pts": 1, "balance": 3, "key": "t8k2", "annot_time": "22:00"},
            {"date": "10-19", "desc": "历史十点", "pts": 1, "balance": 4, "key": "t8k3", "annot_time": "10:00"},
            {"date": "10-19", "desc": "无时刻乙", "pts": 1, "balance": 5, "key": "t8k4", "annot_time": None},
            {"date": "10-20", "desc": "晚十点同刻", "pts": 1, "balance": 6, "key": "t8k5", "annot_time": "22:00"},
        ]
        d = build_log(5, rows, mock_today2, gen)
        got = [it["reason"] for it in d["items"]]
        exp = ["晚十点", "晚十点同刻", "早九点", "历史十点", "无时刻甲", "无时刻乙"]
        assert got == exp, "KB-SORT 排序错误：%r != %r" % (got, exp)
        rec("T8 (日期,时刻)倒序", True, "输出顺序=%s" % got)
    except Exception as e:
        rec("T8 (日期,时刻)倒序", False, str(e)[:200])

    failed = [r for r in results if not r[1]]
    log("自测完成：%d 通过 / %d 失败" % (len(results) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

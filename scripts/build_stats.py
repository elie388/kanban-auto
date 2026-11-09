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

import ast
import fcntl
import hashlib
import time
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

import _assert_lib
from build_kanban import simplify_desc  # 只读复用，不得私改（§5.2）
import sync_balance as sb  # M1（2026-10-20）：复用 norm_date，统一日期归一口径
import catalog  # 目录单一数据源（shop_catalog.json / badges.json）

def _validate_ledger_path():
    """B2-144：LEDGER_PATH 环境变量校验——须为 .md 文件且在工作区内，
    防环境变量被控指向非台账文件。非法时回落默认路径并告警。"""
    _default = os.path.expanduser("~/workspace/goals/goal/hidden_files/积分记录.md")
    _env = os.environ.get("LEDGER_PATH")
    if not _env:
        return _default
    _p = os.path.realpath(os.path.expanduser(_env))
    _ws = os.path.realpath(os.path.expanduser("~/workspace"))
    if not _p.endswith(".md") or os.path.commonpath([_ws, _p]) != _ws:
        print("[build_stats][WARN] LEDGER_PATH 非法（%s），回落默认台账路径" % _env, flush=True)
        return _default
    return _p


LEDGER = _validate_ledger_path()
DATA_DIR = os.path.join(REPO_DIR, "data")
LOGS_DIR = os.path.join(REPO_DIR, "logs")
TEMPLATE = os.path.join(REPO_DIR, "templates", "积分看板_全新版式样板.html")
TZ = timezone(timedelta(hours=8), "CST")


# 产物命名模式（注意 points_log 历史命名无 _grade 段，三者不可统一拼装，
# selftest T9 以字面集合断言防此类漂移）
_OUTPUT_PATTERNS = {"stats": "data/stats_grade_%d.json",
                    "points_log": "data/points_log_%d.json",
                    "shop": "data/shop_grade_%d.json"}


def _output_key(kind, grade):
    return _OUTPUT_PATTERNS[kind] % grade


# A 方案（2026-10-20 DeepSeek 双根因定案 T+1）：stash 显式路径——只 stash 本
# 脚本自己的 6 个产物文件。模块级单点定义，与 outputs 构造同源（都走
# _output_key；selftest T9 断言键集合相等防漂移），供 stash_orphan_scan
# import 复用。回滚 checkout 前的脏检查范围另含模板（定案问题②：原事故
# 被抹掉的正是模板改动，模板 checkout 与数据 checkout 在同一回滚段）。
STASH_PATHS = [_output_key(kind, grade)
               for grade in (5, 6)
               for kind in ("stats", "points_log", "shop")]
TEMPLATE_PATHS = ["templates/积分看板_全新版式样板.html"]


def _n3_sentinel_already_logged_today(log_path=None, today_str=None):
    """N3 哨兵当日首轮判定（定案问题④）：logs/build_stats.log 今日是否已有
    N3 sentinel OK 行。该日志按时间追加，读尾部 300 行足够判定。"""
    log_path = log_path or os.path.join(LOGS_DIR, "build_stats.log")
    today_str = today_str or datetime.now(TZ).strftime("%Y-%m-%d")
    # B2-159：只读文件尾部（8KB 块倒读），不再全文读入；当日日志超 300 行时哨兵仍可被找到。
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            _size = f.tell()
            _buf = b""
            _pos = _size
            while len(_buf.splitlines()) < 300 and _pos > 0:
                _step = min(8192, _pos)
                _pos -= _step
                f.seek(_pos)
                _buf = f.read(_step) + _buf
            lines = _buf.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any(ln.startswith(today_str) and "N3 sentinel OK" in ln
               for ln in lines[-300:])

# R1：档位表（规则手册第85行，唯一权威；变动须走规则手册修订）
SHOP_TIERS = catalog.shop_tiers()  # 单一数据源 scripts/shop_catalog.json（2026-10-22 目录收口）
# B2-155：档位表校验——非空、无重复 pts、升序、非负；非法即熔断（不静默用坏档位）。
if not SHOP_TIERS:
    raise ValueError("SHOP_TIERS 为空，无法生成商店档位")
_pts_list = [t.get("pts") for t in SHOP_TIERS]
if any(not isinstance(_x, (int, float)) or isinstance(_x, bool) or _x < 0 for _x in _pts_list):
    raise ValueError("SHOP_TIERS 含非法 pts（非数值/负值）")
if len(set(_pts_list)) != len(_pts_list) or _pts_list != sorted(_pts_list):
    raise ValueError("SHOP_TIERS pts 重复或非升序")

NEG_MINUS = "−"  # U+2212 全角减号（用户拍板1：负余额静态显示 −N）

# 积分日志计分时间（KB-TIME-2026-10-19，DeepSeek 终审）：
# 台账 key 注释追加 @HH:MM 即为该条目的计分时间（唯一时间源红线：只认积分记录.md）。
TIME_ANNOT_RE = re.compile(r"@(\d{2}):(\d{2})")
# 补记行结构化字段（KB-POST-2026-10-22，DeepSeek 组合3 定案）：key 注释内
# backfill=YYYY-MM-DD 表示该条的真实入账日期；排序键用 backfill 日期 + @HH:MM。
BACKFILL_RE = re.compile(r"backfill=(\d{4}-\d{2}-\d{2})")
KEY_COMMENT_RE = re.compile(r"<!--\s*key:\s*(.*?)\s*-->")
TIME_SOURCE_ENUM = ("submit", "parent_confirm", "entry", "deadline")
TIME_SOURCE_FILE = os.path.join(REPO_DIR, "data_src", "points_time_source.json")
TIME_G6_DATE = "2026-10-19"  # 生效日：complete_year(日期) > 此日 且无 @HH:MM → 熔断
POINTS_LOG_SCHEMA = "1.3"

ROW_RE = re.compile(
    # 日期格允许后跟括号备注（如 10-19（补记10-21）），捕获组仍取纯日期。
    # N3 收口（2026-10-20）：日期接受面与 sync_balance.ROW_PAT 对齐
    # （\d{1,2}-\d{1,2} 或 YYYY-MM-DD），存储在 parse_rows 内归一为零填充 MM-DD。
    r"^\|\s*(\d{1,2}-\d{1,2}|\d{4}-\d{2}-\d{2})(?:\s*[\(（][^\)）]*[\)）])?\s*\|\s*(.*?)\s*\|\s*([+-]?\d+)\s*\|\s*([+-]?\d+)\s*\|"
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def log(msg):
    print("[build_stats] %s" % msg, flush=True)


def warn(msg):
    print("[build_stats][WARN] %s" % msg, flush=True)


def err_write(msg):
    # B2-156：写失败不抛错（此前 OSError 会上抛）；B2-157：敏感词原文不进本地日志。
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        _m = str(msg)
        _m = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱已脱敏]", _m)
        _m = re.sub(r"1[3-9]\d{9}", "[手机号已脱敏]", _m)
        with open(os.path.join(LOGS_DIR, "build_stats.err"), "a", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write("%s %s\n" % (datetime.now(TZ).isoformat(timespec="seconds"), _m))
                f.flush()
            finally:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except Exception:
                    pass
    except OSError:
        pass


def git(*args):
    # B2-158：参数校验——拒绝含 shell 元字符/换行的参数（防御性，本段调用点均为固定字面量）。
    for _a in args:
        if not isinstance(_a, str) or any(_c in _a for _c in ("\n", "\r", ";", "|", "&", "`", "$")):
            raise ValueError("git 参数非法：%r" % (_a,))
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
        # B2-145：重复标题熔断（此前取第一个、第二小节静默丢弃）。
        _hits = [i for i, ln in enumerate(lines) if ln.strip() == "## " + title]
        if len(_hits) > 1:
            raise ValueError("台账分节 ## %s 重复出现 %d 次（行 %s），疑似重复录入，已熔断"
                             % (title, len(_hits), [i + 1 for i in _hits]))
        start = _hits[0] if _hits else None
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

    # --- 防复发（2026-10-20）：数据行写到年级小节外必须熔断，不得静默忽略。
    # 当晚事故：五年级22:00结算17笔被追加到文件末尾"历史归档"节，生成器按小节
    # 切块读不到，余额长期停在旧值且无任何报错。本检查扫描全文件：凡匹配
    # ROW_RE 的数据行，若不在任一年级小节范围内 → raise（走 exit 2 报告路径）。
    spans = []
    cur = None
    for i, ln in enumerate(lines):
        if ln.strip() in ("## 五年级", "## 六年级"):
            if cur is not None:
                spans.append((cur, i))
            cur = i
        elif ln.startswith("## ") and cur is not None:
            spans.append((cur, i))
            cur = None
    if cur is not None:
        spans.append((cur, len(lines)))
    stray = []
    # B2-204 豁免（2026-11-07）：以下小节自带合法表格，其行首 | 且含日期的行
    # 不是写错位置的台账数据行，B2-204 检查须跳过；台账数据行（ROW_RE）
    # 的分节外熔断不受影响（仍按原口径）。
    _B2_204_EXEMPT = ("## 待终审登记",)
    # 每行所属小节（最近的 ## 标题行；标题行之前为 None）。
    _section_of = []
    _cur_sec = None
    for _ln in lines:
        if _ln.startswith("## "):
            _cur_sec = _ln.strip()
        _section_of.append(_cur_sec)
    # B2-204：分节外畸形行也熔断——行首为 | 且含日期形态却不匹配 ROW_RE 的行，
    # 此前 ROW_RE/stray 双重漏判、静默丢失。
    for i, ln in enumerate(lines):
        s = ln.strip()
        _in_span = any(a < i < b for a, b in spans)
        if _in_span:
            continue
        if ROW_RE.match(s):
            stray.append(i + 1)
        elif s.startswith("|") and re.search(r"\d{1,2}-\d{1,2}|\d{4}-\d{2}-\d{2}", s) and s.count("|") >= 3:
            _sec = _section_of[i]
            if _sec is not None and any(_sec.startswith(p) for p in _B2_204_EXEMPT):
                continue
            stray.append(i + 1)
    if stray:
        raise ValueError("台账存在写在年级小节外的数据行（行号 %s），疑似追加位置错误，已熔断" % stray[:10])

    return {5: cut("五年级"), 6: cut("六年级")}


def parse_rows(block, grade):
    """解析 | 日期 | 描述 | 积分 | 余额 | 行；保持台账行顺序。

    畸形行（像数据行但正则不匹配）→ raise（§7.1：不产出）。
    同时解析 key 注释中的 @HH:MM（计分时间，KB-TIME-2026-10-19）；
    @HH:MM 存在但格式非法 → raise（不得静默吞掉，走熔断）。
    """
    rows = []
    _seen_keys = set()
    # B2-146 豁免（2026-11-07）：2026-10-24 UTC 时间标签误标，冲正 id 256–265
    # 先记一批、抵销行冲掉、北京时间重走一批，同 key 出现两次是故意的，
    # 余额链已验证一致（156→135→156→153），不是重复计分。只放行这一批，
    # 其他重复 key 照样熔断；豁免 key 第三次出现仍熔断。
    _B2_146_EXEMPT_KEYS = frozenset((
        "五年级_2026-10-24_冲正id256_53chn_done_-3",
        "五年级_2026-10-24_冲正id257_53chn_acc100_-3",
        "五年级_2026-10-24_冲正id258_53math_done_-3",
        "五年级_2026-10-24_冲正id259_53math_acc86_-1",
        "五年级_2026-10-24_冲正id260_53eng_done_-3",
        "五年级_2026-10-24_冲正id261_53eng_acc58_+2",
        "五年级_2026-10-24_冲正id262_53chn_done_-3",
        "五年级_2026-10-24_冲正id263_53chn_acc80_-1",
        "五年级_2026-10-24_冲正id264_53math_done_-3",
        "五年级_2026-10-24_冲正id265_53eng_done_-3",
    ))
    _exempt_used = set()
    _seen_opening = False
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
        # B2-148：ROW_RE 未锚行尾，行尾多余列静默忽略——此处校验行尾只剩空白/注释。
        _tail = s[m.end():].strip()
        if _tail and not _tail.startswith("<!--"):
            raise ValueError("台账 %d 年级行尾有多余列：%r" % (grade, s[:80]))
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
        bf = BACKFILL_RE.search(key_comment)
        # B2-146：同一 key 重复计分熔断（逐字重复会被余额链熔断，但带重算余额的
        # 重新录入可虚增，此处直接按 key 去重）。
        # B2-146 修法（2026-11-07，家长选 B）：去重 key 带上 @HH:MM 时间。
        # 同一天同一项目在不同时间各记一次（如 11-01 晾衣服 01:07 与 14:33、
        # 11-06 口语易 11:09 与 11:10、10-30 读背 21:41 与 21:49）是不同事件，
        # 不应熔断；同一时间记两次仍熔断（真记重）。无时间标注的行沿用旧口径
        # （纯 key 去重）。10-24 豁免（见上方 _B2_146_EXEMPT_KEYS）保留作
        # 同时间撞 key 时的兜底。
        dedup_key = (key + "@" + annot_time) if (key and annot_time) else key
        if dedup_key and dedup_key in _seen_keys:
            if key in _B2_146_EXEMPT_KEYS and key not in _exempt_used:
                _exempt_used.add(key)
            else:
                raise ValueError("台账 %d 年级 key 重复：%r（%s）" % (grade, key[:40], mm_dd))
        if dedup_key:
            _seen_keys.add(dedup_key)
        # B2-147：期初行多次出现时只第一次重置基线，后续期初行须符合余额链（见下）。
        if "期初余额" in desc:
            if _seen_opening:
                warn("台账 %d 年级期初行多次出现（%s），仅第一次重置基线" % (grade, mm_dd))
            _seen_opening = True
        rows.append({"date": mm_dd, "desc": desc, "pts": pts, "balance": bal,
                     "key": key, "annot_time": annot_time,
                     "backfill": bf.group(1) if bf else None,
                     "backfill_remark": bool(re.search(r"[（(]\s*补记", s))})
    if not rows:
        raise ValueError("台账 %d 年级未解析出任何数据行" % grade)
    # S3 修复（2026-10-20）：余额链断裂改为熔断（fail-closed），与 sync_balance.parse_ledger
    # 的链式校验对齐。期初行（desc 含"期初余额"）重置基线。DeepSeek 终审阻断项。
    prev = None
    _opening_seen = False
    for r in rows:
        # B2-147：首行非期初行时直接信任为基线——改为告警（基线未经期初行锚定）。
        if prev is None and "期初余额" not in r["desc"] and r is rows[0]:
            warn("台账 %d 年级首行非期初行，直接信任为基线（%s）" % (grade, r["date"]))
        if "期初余额" in r["desc"]:
            if _opening_seen:
                # 后续期初行不再重置基线，须符合余额链。
                if prev is not None and r["balance"] != prev + r["pts"]:
                    raise ValueError("余额链断裂（期初行重复，%s 年级 %s）：%d+%d != %d" % (
                        grade, r["date"], prev, r["pts"], r["balance"]))
            else:
                _opening_seen = True
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
    # B2-160：JSON 损坏/权限错误/非 dict 均返回空映射并告警，不再整轮熔断。
    try:
        with open(TIME_SOURCE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            warn("时间源文件非 dict，已按空映射处理：%s" % TIME_SOURCE_FILE)
            return {}
        src = d.get("sources", {})
        return src if isinstance(src, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as _e:
        warn("时间源文件读取失败（按空映射处理）：%s" % _e)
        return {}


DESC_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::\d{2})?")


def extract_desc_time(desc):
    """2026-10-20（用户要求积分日志精确到分）：从条目描述中提取台账记录时间。

    只用于生效日（2026-10-19）及之前的历史条目。新条目仍须用 @HH:MM，缺失即熔断。
    取描述中第一个合法的 HH:MM（照片时间/提交时间均属台账记录时间）。
    """
    # P2（DeepSeek 审查 2026-10-20）：描述本身在讲没交/漏交/未交/截止时，
    # 其中的 HH:MM 多为截止时刻而非记录时间，提取会把日志时间记错——整条跳过。
    _d = desc or ""
    if any(_k in _d for _k in ("没交", "漏交", "未交", "截止")):
        return None
    # B2-154：描述中不含截止语义的 HH:MM 仍被误取为计分时间——须含时间语境词
    # （提交/发回/照片/时间/交）才提取，否则不认。
    if not any(_k in _d for _k in ("提交", "发回", "照片", "时间", "交", "上传", "发送")):
        return None
    m = DESC_TIME_RE.search(_d)
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
    # B2-152：key 规范化（strip＋小写＋内部空格折叠），防大小写/空格差异绕过 G2 比对。
    key = re.sub(r"\s+", " ", (r.get("key", "") or "").strip()).lower()
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
    # sources 的 key 同样规范化后比对。
    _norm_sources = {re.sub(r"\s+", " ", str(_k).strip()).lower(): _v
                     for _k, _v in (sources or {}).items()}
    rec = _norm_sources.get(key)
    if rec is not None:
        src = rec.get("source")
        if src not in TIME_SOURCE_ENUM:
            raise ValueError("G2 熔断：time_source 枚举外 %r（%s）" % (src, key[:40]))
        _rec_time = (rec.get("time") or "").strip()
        if _rec_time != annot_time:
            raise ValueError("G2 熔断：台账 @%s 与终审记录 %s 不一致（%s），疑似事后改动"
                             % (annot_time, _rec_time, key[:40]))
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
    # 余额卡优化（2026-10-23 家长拍板）：今日净变化（台账名义日期口径，DeepSeek 定）+ 分档信息
    _today_s = today.isoformat()
    _today_delta = sum(r["pts"] for r in rows
                       if complete_year(r["date"], today) == _today_s)
    _costs = sorted(t["pts"] for t in SHOP_TIERS)
    _prev_t = None
    _next_t = None
    for _c in _costs:
        if _c <= last["balance"]:
            _prev_t = _c
        elif _next_t is None:
            _next_t = _c
    d = {
        "schema_version": "1.0",
        "grade": grade,
        "balance": last["balance"],
        "today_delta": _today_delta,
        "tier_prev": _prev_t,
        "tier_next": _next_t,
        "tier_costs": _costs,
        "ledger_date": complete_year(last["date"], today),
        "generated_at": generated_at,
        "source": "hidden_files/积分记录.md",
        "checksum": ledger_sha16,
    }
    _assert_lib.assert_stats_schema(d, "stats_grade_%d.json" % grade)
    return d


def build_log(grade, rows, today, generated_at):
    """§1.2 + R3 + KB-TIME-2026-10-19 + KB-POST2-2026-10-22：近7天全量、按
    真实入账时间（补记行 = backfill 日期 + @HH:MM）倒序输出（安全上限
    300 条）；前端只展示最近 3 天、区域内滚动；同入账日有时刻者按时刻
    倒序在前、无时刻者随后保持台账正序；reason 经 simplify_desc + B5
    断言；time 精确到分钟（台账 @HH:MM），schema 1.3。"""
    # B2-197：窗口参数集中为常量（days/max_items），改一处即全改。
    LOG_WINDOW_DAYS = 7
    LOG_MAX_ITEMS = 300
    # B2-150：cutoff 用 today-6（< 比较保留 7 天；此前 today-7 保留 8 天，与“近 7 天”差一天）。
    cutoff = today - timedelta(days=LOG_WINDOW_DAYS - 1)
    sources = load_time_sources()
    dated = []
    for idx, r in enumerate(rows):
        full = complete_year(r["date"], today)
        try:
            dt = datetime.strptime(full, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("非法日期：%s" % r["date"])
        # B2-149：窗口过滤用入账日期 pdt（补记行 = backfill 日），而非名义日期 dt。
        _pdt0 = dt
        if r.get("backfill"):
            try:
                _pdt0 = datetime.strptime(r["backfill"], "%Y-%m-%d").date()
            except ValueError:
                raise ValueError("backfill 日期非法：%s" % r["backfill"])
        if _pdt0 < cutoff:
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
        # KB-POST-2026-10-22（家长口径"只显示20条、但是显示最新的" + DeepSeek
        # 组合3 定案）：排序键改真实入账时间。普通行入账日 = 名义日期；补记行
        # 入账日 = key 注释 backfill=YYYY-MM-DD（结构化字段，非备注文字解析）。
        # 日期格带（补记…）备注却缺 backfill 字段 → 告警留痕、按名义日期排。
        pdt = dt
        if r.get("backfill"):
            try:
                pdt = datetime.strptime(r["backfill"], "%Y-%m-%d").date()
            except ValueError:
                raise ValueError("backfill 日期非法：%s" % r["backfill"])
        elif r.get("backfill_remark"):
            warn("补记行缺 backfill 结构化字段，按名义日期排序 grade=%d date=%s"
                 % (grade, r["date"]))
            err_write("BACKFILL_TOKEN_MISSING grade=%d date=%s key=%s"
                      % (grade, r["date"], r.get("key", "")[:40]))
        dated.append((dt, pdt, t, t_src, idx, r))

    # 排序口径 KB-POST-2026-10-22（取代 KB-SORT-2026-10-20 的名义日期键）：
    # ① 入账日期倒序；② 同入账日内有时刻条目排在无时刻条目之前；③ 有时刻者
    # 按时刻倒序；④ 同键保持台账正序（idx 正序，DeepSeek 定案的次级键）。
    # 实现：先按 idx 正序稳定，再按 (入账日期, 有时刻标记, 时刻) 一次倒序稳定。
    dated.sort(key=lambda x: x[4])
    dated.sort(key=lambda x: (x[1], 1 if x[2] else 0, x[2] or ""), reverse=True)

    # 截断口径 KB-POST2-2026-10-22（DeepSeek 改良版定案，取代组合3的负分
    # 额外保留）：7 天窗口全量输出、不再按条数截断（前端只展示最近 3 天、
    # 区域内滚动，见模板 LOG_DISPLAY_DAYS）；仅保留 300 条安全上限防异常
    # 数据膨胀。取消负分额外保留：3 天内全量可见，不存在被挤出。
    # B2-151：先对全部窗口条目做敏感词过滤（filtered_count 含被截条目），再截断到上限；
    # 超限时告警（此前先截断再过滤，filtered_count 失真且超限无告警）。
    _all_items = []
    filtered_count = 0
    for dt, pdt, t, t_src, idx, r in dated:
        # B2-200：敏感词检查用完整 desc（此前先截断 40 字再检查，敏感词在第 40 字后可漏检）。
        reason_full = simplify_desc(r["desc"])
        filtered = False
        hits = _assert_lib.find_forbidden_terms(reason_full)
        if hits:
            warn("敏感词命中 grade=%d date=%s hits=%r → 替换为'学习记录'"
                 % (grade, r["date"], hits))
            err_write("FILTERED grade=%d date=%s hits=%s" % (grade, r["date"], ", ".join(hits)))
            reason_full = "学习记录"
            filtered = True
            filtered_count += 1
        reason = reason_full
        if len(reason) > 40:
            reason = reason[:40] + "…"  # 上限 40 字，超长截断加 …
        _all_items.append((dt, pdt, t, t_src, idx, r, reason, filtered))
    selected = _all_items[:LOG_MAX_ITEMS]
    truncated = len(_all_items) > len(selected)
    if truncated:
        warn("积分日志超限：窗口内 %d 条，仅保留最新 %d 条" % (len(_all_items), len(selected)))
    items = []
    for dt, pdt, t, t_src, idx, r, reason, filtered in selected:
        pts = r["pts"]
        # KB-POST-2026-10-22：t/t_src 已在排序前解析并通过 G3 复核（见 dated 构建处）
        item = {
            # B2-199：跨年时输出完整 YYYY-MM-DD，MM-DD 在跨年窗口内会错位。
            "date": complete_year(r["date"], today) if complete_year(r["date"], today)[:4] != str(today.year) else r["date"],
            "time": t,  # HH:MM 或 null（无计分时间）
            "time_source": t_src,  # submit/parent_confirm/entry/deadline 或 null（溯源，不前端展示）
            "reason": reason,
            "pts": pts,
            "sign": "plus" if pts >= 0 else "minus",
            "filtered": filtered,
        }
        if r.get("backfill"):
            # 补记行：前端主显示名义日期，副标"补记 · 入账日期"（DeepSeek 定案）
            item["backfill"] = True
            item["posted_date"] = r["backfill"][5:]  # MM-DD
        items.append(item)

    d = {
        "schema_version": POINTS_LOG_SCHEMA,
        "grade": grade,
        "generated_at": generated_at,
        "window": {"days": LOG_WINDOW_DAYS, "max_items": LOG_MAX_ITEMS},
        "items": items,
        "truncated": truncated,
        "filtered_count": filtered_count,
    }
    _assert_lib.assert_points_log_schema(d, "points_log_%d.json" % grade)
    return d


def build_shop(grade, balance, generated_at):
    # B2-196：balance 须为数值，tiers 缺键即报错（此前缺键抛 KeyError 无上下文）。
    if not isinstance(balance, (int, float)) or isinstance(balance, bool):
        raise ValueError("build_shop balance 非数值：%r" % (balance,))
    _tiers = []
    for _t in SHOP_TIERS:
        if not all(_k in _t for _k in ("pts", "name", "kind")):
            raise ValueError("SHOP_TIERS 缺键：%r" % (_t,))
        _tiers.append({"pts": _t["pts"], "name": _t["name"], "kind": _t["kind"]})
    d = {
        "schema_version": "1.0",
        "grade": grade,
        "tiers": _tiers,
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
# 熔断根因修复（2026-10-21，家长拍板选项3）：docs/程序异常台账.md 的操作者
# 正当更新不得再阻断数据同步。构建本身从不写该文件、提交段也不 add 它，
# 故列入容忍清单：仅免除 R4 拦截，文件内容原样保留，绝不代人 stash/commit。
# 原有三条防线不变：台账解析失败 §7.1 独立熔断、模板行数 ≤4 校验保留、
# 其余非预期路径照旧拦截。
R4_TOLERATED_PATHS = {
    "docs/程序异常台账.md",
}


def r4_diff_check(snapshot_before):
    """R4：替换后 diff 与快照对比 —— 除 data/*.json 外只允许模板变化，且模板行数变动 ≤ 4。"""
    cur = git("-c", "core.quotepath=false", "diff", "--stat")
    if cur.returncode != 0:
        raise ValueError("git diff --stat 执行失败")  # P2：校验禁用 assert（M1 原则）
    allowed_data = set(STASH_PATHS)
    template_name = "templates/积分看板_全新版式样板.html"
    changed = set()
    for ln in cur.stdout.splitlines():
        m = re.match(r"^\s*(.+?)\s+\|\s+(\d+)", ln)
        if m:
            changed.add(m.group(1).strip())
        else:
            # B2-162：--stat 二进制文件行（如 "x.bin | Bin 0 -> 123 bytes"）不匹配
            # 数字正则，此前静默跳过致 R4 绕过；现一并计入 changed。
            mb = re.match(r"^\s*(.+?)\s+\|\s+Bin\b", ln)
            if mb:
                changed.add(mb.group(1).strip())
    # B2-161：snapshot_before 形参此前未使用——快照是构建前 diff，构建前已脏的
    # 文件不是本次构建的改动，不计入 unexpected（本次构建只对新增变更负责）。
    _before = set()
    if snapshot_before:
        for ln in str(snapshot_before).splitlines():
            m = re.match(r"^\s*(.+?)\s+\|\s+(\d+)", ln)
            if m:
                _before.add(m.group(1).strip())
            else:
                mb = re.match(r"^\s*(.+?)\s+\|\s+Bin\b", ln)
                if mb:
                    _before.add(mb.group(1).strip())
    tolerated = sorted(changed & R4_TOLERATED_PATHS)
    if tolerated:
        err_write("R4 容忍：docs 程序异常台账有未提交变更，不阻断数据同步：%s"
                  % ",".join(tolerated))
    unexpected = changed - allowed_data - {template_name} - R4_TOLERATED_PATHS - _before
    if unexpected:
        _e = ValueError("R4 拦截：出现非预期变更文件 %r" % sorted(unexpected))
        _e.unexpected_files = sorted(unexpected)
        raise _e
    if template_name in changed:
        numstat = git("-c", "core.quotepath=false", "diff", "--numstat", "--", template_name)
        # B2-162：numstat 空 stdout 抛 IndexError、二进制返回 "-" 抛 ValueError——
        # 空输出即无变更（放行），"-" 按 0 计（行数变动另由 --stat 兜底）。
        parts = (numstat.stdout or "").split()
        if len(parts) < 2:
            raise ValueError("R4 拦截：模板 numstat 解析失败（输出为空）")
        def _num(x):
            return 0 if x == "-" else int(x)
        try:
            added, deleted = _num(parts[0]), _num(parts[1])
        except ValueError:
            raise ValueError("R4 拦截：模板 numstat 解析失败（非数字）")
        if added + deleted > 4:
            raise ValueError("R4 拦截：模板行数变动 %d 超限（≤4）" % (added + deleted))
    return True

def _r4_recovery_alert(kind, detail):
    """R4恢复 fail-closed 告警落盘（只写日志，不改工作区、不碰 stash）。"""
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        rec = {"ts": datetime.now(TZ).isoformat(timespec="seconds"), "kind": kind,
               "detail": detail[:300], "pid": os.getpid()}
        with open(os.path.join(LOGS_DIR, "r4_recovery_alert.log"), "a", encoding="utf-8") as _f:
            _f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as _e:
        # B2-177：告警落盘失败时打印到 stderr，不再 except: pass 静默丢弃。
        print("[build_stats][WARN] r4_recovery_alert 落盘失败：%s" % _e, file=sys.stderr, flush=True)


def _r4_try_recover(exc_msg, files=None):
    """c项 R4恢复：仅处理“全部非预期文件均为 data/*.json 自动化脏”的情形。

    返回 (recovered, detail)：
    - True：已归档（logs/dirty_archive/<时间戳>_<pid>/ + manifest.json）并 checkout 重置，
      调用方应重跑 r4_diff_check 复检；复检通过方可继续提交。
    - False：不满足归档条件（非 data 脏 / 含已暂存文件 / 解析失败）→ fail-closed：
      已写 logs/r4_recovery_alert.log 告警，不归档、不重置、不碰 stash，调用方走原流程。
    归档范围铁律：data/*.json（单层）才可归档——看板 data 均为可再生副本（源在 hidden_files），
    归档仅保全重置前字节；模板/css/docs/scripts/其它一律不可归档。stash 绝不触碰（不用 git stash）。
    B2-165：文件列表由调用方直接传入，不再从异常文案正则 + ast.literal_eval 解析
    （文案一改恢复即失效）；exc_msg 仅作告警文本。
    """
    if files is None:
        m = re.search(r"非预期变更文件\s*(\[.*\])", exc_msg)
        if not m:
            _r4_recovery_alert("unparsed", exc_msg[:200])
            return False, "非 R4 非预期文件异常，不恢复"
        try:
            files = ast.literal_eval(m.group(1))
        except Exception:
            _r4_recovery_alert("unparsed", exc_msg[:200])
            return False, "非预期文件列表解析失败，不恢复"
    if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
        _r4_recovery_alert("unparsed", exc_msg[:200])
        return False, "非预期文件列表非法，不恢复"
    staged = git("-c", "core.quotepath=false", "diff", "--cached", "--name-only").stdout.split()
    if set(files) & set(staged):
        _r4_recovery_alert("staged", ",".join(sorted(set(files) & set(staged)))[:200])
        return False, "含已暂存文件，不恢复"
    non_data = [f for f in files if not re.fullmatch(r"data/[^/]+\.json", f)]
    if non_data:
        _r4_recovery_alert("non_data", ",".join(sorted(non_data))[:300])
        return False, "含不可归档改动：" + ",".join(sorted(non_data))
    # B2-166：归档目录名加微秒，同秒多次恢复不再覆盖。
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f")
    adir = os.path.join(LOGS_DIR, "dirty_archive", "%s_%d" % (stamp, os.getpid()))
    os.makedirs(adir, exist_ok=True)
    head = git("rev-parse", "HEAD").stdout.strip()
    manifest = {"ts": datetime.now(TZ).isoformat(timespec="seconds"), "pid": os.getpid(),
                "head": head, "reason": "R4 data dirt recovery", "files": sorted(files),
                "note": "data 均为可再生副本（源在 hidden_files）；归档仅保全重置前字节",
                "sha256": {}, "missing": []}
    for f in sorted(files):
        src_p = os.path.join(REPO_DIR, f)
        if os.path.exists(src_p):
            with open(src_p, "rb") as _fi:
                blob = _fi.read()
            # B2-164：归档后读回校验字节完整性（sha256 存入 manifest）。
            _dest = os.path.join(adir, os.path.basename(f))
            with open(_dest, "wb") as _fo:
                _fo.write(blob)
            _h = hashlib.sha256(blob).hexdigest()
            with open(_dest, "rb") as _fr:
                if hashlib.sha256(_fr.read()).hexdigest() != _h:
                    _r4_recovery_alert("archive_corrupt", f)
                    return False, "归档校验失败（%s），不恢复" % f
            manifest["sha256"][f] = _h
        else:
            # B2-164：已删除文件不静默跳过，在 manifest 里记 missing。
            manifest["missing"].append(f)
    with open(os.path.join(adir, "manifest.json"), "w", encoding="utf-8") as _fm:
        _fm.write(json.dumps(manifest, ensure_ascii=False, indent=1))
    r = git("checkout", "--", *sorted(files))
    if r.returncode != 0:
        _r4_recovery_alert("checkout_fail", r.stderr[:200])
        return False, "checkout 重置失败，不恢复"
    return True, "已归档 %d 个 data 文件至 %s 并重置" % (len(files), adir)



# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _strip_generated_at(o):
    """递归移除嵌套 generated_at（B2-175；B2-184 根治 2026-11-08：
    写决策（semantic_diff）与写后校验（B2-184 守卫）须用同一语义比较，
    否则未重写文件因 generated_at 陈旧被守卫误判为“写出后被改动”。"""
    if isinstance(o, dict):
        return {k: _strip_generated_at(v) for k, v in o.items() if k != "generated_at"}
    if isinstance(o, list):
        return [_strip_generated_at(x) for x in o]
    return o


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
    # B2-175：递归移除嵌套 generated_at（此前只移除顶层，嵌套时间戳仍参与比较致无谓重写）；
    # 时间戳解析为 datetime 比较（此前字符串比较在格式不一致时误判）。
    _o = _strip_generated_at(old_obj)
    _n = _strip_generated_at(new_obj)
    if _o == _n:
        return False, "no_semantic_change"
    def _parse_ga(x):
        try:
            return datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        except Exception:
            return None
    _old_ga = _parse_ga(old_obj.get("generated_at") or "")
    _new_ga = _parse_ga(generated_at)
    if _old_ga is not None and _new_ga is not None and _old_ga > _new_ga:
        return False, "monotonic_guard"
    if _old_ga is None or _new_ga is None:
        # 无法解析时回落字符串比较（缺失时单调保护失效，告警）。
        if (old_obj.get("generated_at") or "") > generated_at:
            return False, "monotonic_guard"
    return True, "changed"


def _strip_board_names(obj):
    """看板禁词（2026-10-24 家长令）：发布数据不得出现 deepseek/gemini 两词。
    递归做中性替换（终审/初审），只动发布副本，源文件原文不动。"""
    if isinstance(obj, str):
        # B2-176：词边界防误伤子串；键也替换（此前只处理值）。
        return re.sub(r'(?i)(?<![A-Za-z])deepseek(?![A-Za-z])', '终审',
                      re.sub(r'(?i)(?<![A-Za-z])gemini(?![A-Za-z])', '初审', obj))
    if isinstance(obj, list):
        return [_strip_board_names(x) for x in obj]
    if isinstance(obj, dict):
        return {_strip_board_names(k) if isinstance(k, str) else k: _strip_board_names(v)
                for k, v in obj.items()}
    return obj


def write_json_atomic(path, obj):
    """写 JSON 后读回校验（§7.3 生成端）；失败 raise。
    fix2：tmp 名加 pid，避免并发写同一 tmp 交错（与 build_trends 同款）。"""
    # B2-174：写后 fsync 再 replace；失败清理残留 .tmp.<pid> 文件（此前校验失败不清理）。
    obj = _strip_board_names(obj)
    tmp = path + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        with open(tmp, encoding="utf-8") as f:
            back = json.load(f)
        if back != obj:
            # P2：校验禁用 assert（M1 原则）；raise 前先写诊断日志，调用方按熔断处理
            err_write("write_json_atomic 读回不一致：%s" % path)
            raise ValueError("JSON 写后读回不一致：%s" % path)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


# B2-181/B2-170：main 级状态（stash 恢复与 build 锁关闭须在异常时也执行）。
_BS_STATE = {"stashed": False, "stash_ref": None, "restored": False,
             "lock_fp": None}


def _pop_stash_by_ref(ref):
    """B2-205（2026-11-08 根治）：git 2.43 的 `stash pop <完整 SHA>` 报错
    "'...' is not a stash reference"——pop 只认 stash@{n} 形式的引用，SHA
    本身不是合法 stash 引用。B2-167 按 SHA 定位本意是防下标漂移错 pop 别人
    的 stash，但 SHA 根本 pop 不了，致 62+ 条 build_stats-auto stash 堆积
    （stash 日志：101 次 push、仅 8 次 popped、93 次 CONFLICT 全是此报错）。
    本函数在 pop 时刻把 SHA 现查为 stash@{n} 再 pop：下标只在 list→pop 的
    瞬间窗口内有效，漂移风险远小于 B2-167 之前全程用下标；若首次 pop 报
    "not a stash reference"（窗口内他人 push/pop 改了下标），重查一次下标
    再试一次；仍失败则返回 stderr，由调用方按原 CONFLICT 口径留痕，不放宽
    任何守卫。返回 (ok, stderr)。"""
    for _attempt in range(2):
        _lst = git("stash", "list", "--format=%H")
        _hashes = [ln.strip() for ln in (_lst.stdout or "").splitlines() if ln.strip()]
        if ref not in _hashes:
            return False, "stash ref not in list: %s" % ref
        _idx = _hashes.index(ref)
        _pr = git("stash", "pop", "stash@{%d}" % _idx)
        if _pr.returncode == 0:
            return True, ""
        _err = _pr.stderr or ""
        if "not a stash reference" in _err and _attempt == 0:
            continue  # 下标在窗口内漂移，重查一次再试
        return False, _err
    return False, "stash pop 重试后仍失败"


def _bs_restore_stash_fallback():
    """B2-181：main 异常退出时的 stash 恢复兜底（按 ref 直接 pop）。"""
    if not _BS_STATE.get("stashed") or _BS_STATE.get("restored"):
        return
    _BS_STATE["restored"] = True
    try:
        _ref = _BS_STATE.get("stash_ref")
        if not _ref:
            return
        _lst = git("stash", "list", "--format=%H")
        _hashes = [ln.strip() for ln in (_lst.stdout or "").splitlines() if ln.strip()]
        if _ref not in _hashes:
            err_write("NEED-REVIEW B2-181 兜底恢复：stash 条目已不在 list（ref=%s）" % _ref)
            return
        # B2-205：经 _pop_stash_by_ref 按现查 stash@{n} 弹出（SHA 直 pop 在
        # git 2.43 上必报 'not a stash reference'）。
        _ok, _err = _pop_stash_by_ref(_ref)
        if not _ok:
            err_write("NEED-REVIEW B2-181 兜底恢复 pop 失败：" + (_err or "")[:200])
    except Exception as _e:
        try:
            err_write("NEED-REVIEW B2-181 兜底恢复异常：%s" % _e)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# 余额实时推送（2026-11-09 根治：余额不再等 Pages 部署）
# 板上余额来自 data/stats_grade_*.json 静态文件，须走 Pages 全量部署（数分钟）
# 才更新。根治：本脚本算出余额并提交推送后，另把余额 POST 到 Worker
# /hw-api/balances（写 KV kb:balances），板子每次打开直接向 Worker 取最新数。
# fail-soft：任何失败只记日志，绝不影响本次同步/提交/推送（静态文件仍是兜底）。
# 密钥复用 KB_INGEST_KEY（与 /kb-data-ingest 同密钥），只从
# ~/.kb-dynamic/credentials.env 读，值绝不打印。
# ---------------------------------------------------------------------------
_BALANCES_WORKER_BASE = "https://whyjjf.lol/hw-api"
_BALANCES_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"


def _read_ingest_key():
    try:
        for line in open(os.path.expanduser("~/.kb-dynamic/credentials.env"), encoding="utf-8"):
            line = line.strip()
            if line.startswith("KB_INGEST_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def _post_balances_to_worker(balances):
    """把余额 POST 到 Worker /hw-api/balances（写 KV）。fail-soft，返回 True/False。"""
    try:
        key = _read_ingest_key()
        if not key:
            warn("余额实时推送跳过：~/.kb-dynamic/credentials.env 无 KB_INGEST_KEY")
            return False
        import tempfile
        body = {"5": int(balances[5]), "6": int(balances[6])}
        fd, tmp = tempfile.mkstemp(prefix="bs_bal_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(body))
            cmd = ["curl", "-s", "--max-time", "30", "-w", "\n%{http_code}", "-X", "POST",
                   "-H", "User-Agent: " + _BALANCES_UA,
                   "-H", "Content-Type: application/json",
                   "-H", "X-Ingest-Key: " + key,
                   "--data", "@" + tmp,
                   _BALANCES_WORKER_BASE + "/balances"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            out = r.stdout or ""
            code = 0
            if "\n" in out:
                _btxt, _code = out.rsplit("\n", 1)
                try:
                    code = int(_code.strip())
                except Exception:
                    code = 0
            if code == 200:
                log("余额实时推送成功：g5=%d g6=%d" % (body["5"], body["6"]))
                return True
            warn("余额实时推送失败（不影响本次同步）：HTTP %s" % (code or "0"))
            err_write("BALANCES_POST_FAIL http=%s" % (code or "0"))
            return False
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception as _e:
        warn("余额实时推送异常（不影响本次同步）：%s" % str(_e)[:120])
        err_write("BALANCES_POST_EXC %s" % str(_e)[:120])
        return False


def _main_impl():
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
    _BS_STATE["lock_fp"] = _lock_fp  # B2-170：锁句柄登记，异常时由外层 finally 关闭。
    try:
        fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("skip: build lock held（另一 build 正在运行，本轮跳过）")
        try:
            _lock_fp.close()
        except Exception:
            pass
        _BS_STATE["lock_fp"] = None
        return 0

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    # B2-198：today 与 generated_at 用同一次 now（此前两次调用跨午夜可差一天）。
    _now0 = datetime.now(TZ)
    today = _now0.date()
    generated_at = _now0.isoformat(timespec="seconds")

    # R4-1（2026-10-20根治，DeepSeek方案）：先自动 stash 未提交改动 → pull --ff-only。
    # R4-2（2026-10-20再根治，用户报障+DeepSeek裁决）：pop 延后到全流程结束
    # （各 return 出口统一调 _restore_stash）。此前 pop 太早 → R4 把操作者
    # 未提交改动当异常拦截，失败回滚的 git checkout 把改动直接抹掉（已真实
    # 发生：前端规则渲染改动被回滚丢失）。pop 延后后，回滚只碰脚本自身产物。
    # D 台账壳（2026-10-20 DeepSeek 定案 T+0，只记账不改行为）：stash
    # push/pop/CONFLICT 结构化留痕到 logs/build_stats_stash.log，供 T+1
    # 上线的 orphan 扫描对账（push 后超时未 pop 即滞留告警的数据源）。
    def _boot_id():
        try:
            with open("/proc/sys/kernel/random/boot_id") as _f:
                return _f.read().strip()
        except OSError:
            return "unknown"

    def _stash_log(event, **kw):
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            rec = {"ts": datetime.now(TZ).isoformat(timespec="seconds"),
                   "event": event, "pid": os.getpid(), "boot_id": _boot_id()}
            rec.update(kw)
            with open(os.path.join(LOGS_DIR, "build_stats_stash.log"),
                      "a", encoding="utf-8") as _f:
                _f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 记账失败绝不影响构建本身

    def _orphan_alert(event, ref, detail):
        # DeepSeek 定案 1c 修正：stash_missing 视为 orphan，直接写入 orphan
        # 告警文件（与 stash_orphan_scan 同一告警面），级别高于 pop 冲突。
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            rec = {"ts": datetime.now(TZ).isoformat(timespec="seconds"),
                   "event": event, "ref": ref, "detail": detail[:200],
                   "source": "build_stats"}
            with open(os.path.join(LOGS_DIR, "stash_orphan_alert.log"),
                      "a", encoding="utf-8") as _f:
                _f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 告警落盘失败不影响构建本身（stdout/err 已另行留痕）

    # A 方案（定案 T+1）：显式 pathspec——只 stash 本脚本 6 个产物文件，
    # STASH_PATHS 外的操作者改动全程留在工作区、永不被本脚本代收。
    # B2-168：stash 判定不用 stdout 含 "Saved working directory"（locale 不同可误判），
    # 改用 push 前后 stash list 对比（新增条目即 stashed）。
    _stash_before = git("stash", "list", "--format=%H").stdout.split()
    stash_r = git("stash", "push", "-u", "-m", "build_stats-auto",
                  "--", *STASH_PATHS)
    _stash_after = git("stash", "list", "--format=%H").stdout.split()
    _new_refs = [h for h in _stash_after if h not in _stash_before]
    stashed = (stash_r.returncode == 0 and len(_new_refs) == 1)
    _stash_ref = None
    if stashed:
        _stash_ref = _new_refs[0]
        _BS_STATE.update({"stashed": True, "stash_ref": _stash_ref, "restored": False})
        _stash_log("push", ref=_stash_ref, msg="build_stats-auto")

    _restored = [False]  # B2-169：已恢复标志，重复调用不再误报 stash_missing。
    def _restore_stash():
        if not stashed or _restored[0]:
            return
        _restored[0] = True
        _BS_STATE["restored"] = True
        # B2-167：按 push 时记录的 ref（commit hash）直接 pop，不再按下标定位
        # （窗口内他人 stash 会使下标漂移、错 pop 别人的 stash）。
        lst = git("stash", "list", "--format=%H")
        hashes = [ln.strip() for ln in (lst.stdout or "").splitlines()
                  if ln.strip()]
        if _stash_ref not in hashes:
            msg = ("R4 严重告警：本次 stash 条目已不在 stash list"
                   "（ref=%s），改动可能已被外部处理或丢失" % _stash_ref)
            warn(msg)
            err_write("NEED-REVIEW " + msg)
            _stash_log("stash_missing", ref=_stash_ref)
            _orphan_alert("stash_missing", _stash_ref, msg)
            return
        # B2-205：经 _pop_stash_by_ref 按现查 stash@{n} 弹出（SHA 直 pop 在
        # git 2.43 上必报 'not a stash reference'，此为 62+ 条 stash 堆积根因）。
        _pop_ok, _pop_err = _pop_stash_by_ref(_stash_ref)
        if not _pop_ok:
            msg = "R4 告警：stash pop 冲突，改动保留在 stash 中，待人工处理"
            warn(msg)
            err_write("NEED-REVIEW " + msg + " :: " + (_pop_err or "")[:200])
            _stash_log("CONFLICT", ref=_stash_ref,
                       detail=(_pop_err or "")[:200])
        else:
            _stash_log("popped", ref=_stash_ref)

    # R4-3（2026-10-20，DeepSeek 方案+家长拍板）：弃用 git pull --ff-only
    # （偶发 fatal: Cannot fast-forward to multiple branches），改显式三步校验：
    # fetch → 比对 HEAD 与 origin/master（相等放行 / 本地是祖先则 ff 合并 /
    # 真分叉 exit 2 熔断）。校验全程在共享 git_push.lock 内（与 watchd /
    # task_ingest 串行）；瞬时失败重试 2 次（间隔 5s）再熔断。
    # R4"真分叉必须停"原则不变。
    _GIT_PUSH_LOCK = os.path.join(os.path.dirname(REPO_DIR), "logs",
                                  "git_push.lock")

    def _acquire_git_lock(timeout=10):
        # P1#5 配套（2026-10-20）：watchd 持 git_push.lock 运行消费者时经
        # KANBAN_GIT_LOCK_HELD=1 声明——本进程已在外层临界区内，不再重复取锁
        # （flock 跨进程互斥，重复取必然超时误判）。返回 /dev/null 句柄占位。
        # B2-171：旁路使用时记日志（此前无日志、无二次校验）；锁文件打开失败捕获 OSError。
        if os.environ.get("KANBAN_GIT_LOCK_HELD") == "1":
            warn("git 锁旁路：KANBAN_GIT_LOCK_HELD=1（外层已持锁），跳过取锁")
            return open(os.devnull, "w")
        try:
            os.makedirs(os.path.dirname(_GIT_PUSH_LOCK), exist_ok=True)
            fp = open(_GIT_PUSH_LOCK, "w")
        except OSError as _e:
            warn("git_push.lock 打开失败：%s" % _e)
            return None
        t0 = time.time()
        while True:
            try:
                fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fp
            except OSError:
                if time.time() - t0 >= timeout:
                    fp.close()
                    return None
                time.sleep(0.5)

    def _foreign_dirty_paths():
        """A 方案 1d（定案）：当前工作区脏路径中落在 STASH_PATHS 外的集合。
        显式路径 stash 后，本脚本产物恒干净；同步失败若与外部脏改动相关，
        绝不代人 stash——由调用方记 foreign_dirty_tree 跳过本轮。"""
        # B2-173：用 --porcelain -z（NUL 分隔），重命名/引号/换行文件名不再错判。
        st = git("-c", "core.quotepath=false", "status", "--porcelain", "-z")
        dirty = []
        for entry in (st.stdout or "").split("\0"):
            if len(entry) < 4:
                continue
            p = entry[3:].strip()
            if " -> " in p:
                p = p.split(" -> ", 1)[1]
            dirty.append(p)
        return sorted(set(dirty) - set(STASH_PATHS))

    def _sync_once():
        lock = _acquire_git_lock()
        if lock is None:
            return "git_push.lock 10s 未获得"
        try:
            fr = git("fetch", "origin")
            if fr.returncode != 0:
                return "fetch 失败：" + (fr.stderr or "")[:150]
            # B2-172：rev-parse 查 returncode，origin/master 缺失不再误判 DIVERGED。
            _head_r = git("rev-parse", "HEAD")
            _remote_r = git("rev-parse", "origin/master")
            if _head_r.returncode != 0:
                return "rev-parse HEAD 失败：" + (_head_r.stderr or "")[:150]
            if _remote_r.returncode != 0:
                return "rev-parse origin/master 失败（上游分支缺失）：" + (_remote_r.stderr or "")[:150]
            head = _head_r.stdout.strip()
            remote = _remote_r.stdout.strip()
            if head == remote:
                return None
            anc = git("merge-base", "--is-ancestor", "HEAD", "origin/master")
            if anc.returncode == 0:
                mg = git("merge", "--ff-only", "origin/master")
                if mg.returncode != 0:
                    # B2-172：merge 失败先查 FOREIGN_DIRTY，再区分冲突与其它失败，不再一律掩盖真因。
                    foreign = _foreign_dirty_paths()
                    if foreign:
                        return "FOREIGN_DIRTY:" + ",".join(foreign)
                    _mg_err = (mg.stderr or "")[:150]
                    if "CONFLICT" in (mg.stdout or "") + (mg.stderr or ""):
                        return "ff 合并冲突：" + _mg_err
                    return "ff 合并失败：" + _mg_err
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
        if sync_err.startswith("FOREIGN_DIRTY:"):
            break  # 脏源在 STASH_PATHS 外：重试无意义，直接走跳过本轮
        if _attempt < 2:
            time.sleep(5)
    if sync_err is not None and sync_err.startswith("FOREIGN_DIRTY:"):
        # A 方案 1d 落点（定案问题①：与 orphan 同源，记 stash 台账）：
        # 不代人 stash、不熔断，结构化留痕后跳过本轮（fail-safe exit 0）。
        _fpaths = sync_err.split(":", 1)[1]
        warn("A 方案：同步被 STASH_PATHS 外未提交改动阻塞，跳过本轮"
             "（foreign_dirty_tree）：%s" % _fpaths)
        _stash_log("foreign_dirty_tree",
                   paths=[p for p in _fpaths.split(",") if p])
        err_write("foreign_dirty_tree paths=%s" % _fpaths)
        _restore_stash()
        return 0
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
        # B2-188：空年级块时 rows[-1] 抛 IndexError 致 main 异常退出且 stash 未恢复——
        # 此处显式熔断（parse_rows 已保证非空，此为防御性守卫）。
        if not rows:
            raise ValueError("台账 %d 年级行列表为空，无法取余额" % grade)
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
        _n3_diverged = False
        for _g, _gk in ((5, "g5"), (6, "g6")):
            if _alt[_gk]["balance"] != balances[_g]:
                _n3_diverged = True
                warn("N3 对拍不一致：%d 年级 本解析余额=%d / sync_balance 余额=%d"
                     % (_g, balances[_g], _alt[_gk]["balance"]))
                err_write("N3_DIVERGENCE grade=%d self_bal=%d alt_bal=%d"
                          % (_g, balances[_g], _alt[_gk]["balance"]))
        if not _n3_diverged and not _n3_sentinel_already_logged_today():
            # 定案问题④：哨兵通过仅当日首轮留痕——既证明哨兵每日真实
            # 生效，又不让高频运行把日志淹没；不一致仍走 err_write 全量。
            with open(os.path.join(LOGS_DIR, "build_stats.log"),
                      "a", encoding="utf-8") as _f:
                _f.write("%s N3 sentinel OK g5=%d g6=%d\n" % (
                    datetime.now(TZ).isoformat(timespec="seconds"),
                    balances[5], balances[6]))
    except Exception as _e:
        # B2-187：N3 对拍异常留完整 traceback（此前只截 120 字、无 traceback，排障困难）。
        import traceback as _tb
        warn("N3 对拍自身异常（忽略，不影响产出）：%s" % str(_e)[:120])
        err_write("N3_TRACEBACK\n" + _tb.format_exc())

    outputs = {}
    for grade in (5, 6):
        rows = all_rows[grade]
        outputs[_output_key("stats", grade)] = build_stats(
            grade, rows, ledger_sha16, today, generated_at)
        outputs[_output_key("points_log", grade)] = build_log(
            grade, rows, today, generated_at)
        outputs[_output_key("shop", grade)] = build_shop(
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
        # B2-180：was_new 区分“文件不存在”（True）与“文件存在但 JSON 损坏”（False，
        # 旧文件损坏时 semantic_diff 误判为无旧文件、was_new 被误标 True，后续回滚
        # 会 os.remove 删除旧文件）。损坏的旧文件记日志并按已存在处理。
        _existed = os.path.exists(path)
        if _existed:
            try:
                with open(path, encoding="utf-8") as f:
                    old_obj = json.load(f)
            except Exception as _je:
                warn("旧文件 %s JSON 损坏（按已存在处理，回滚时 checkout 恢复）：%s" % (rel, _je))
                old_obj = None
        should_write, reason = semantic_diff(old_obj, obj, generated_at)
        if not should_write:
            if reason != "no_semantic_change":
                warn("跳过覆盖 %s（原因：%s）" % (rel, reason))
                err_write("SKIP_WRITE %s reason=%s" % (rel, reason))
            continue
        write_json_atomic(path, obj)
        changed_files.append((rel, not _existed))  # (路径, 是否为新建)

    if not changed_files:
        # B2-183：无语义变化时仍检查有无滞留未推提交（上次 push 失败遗留），有则推走，
        # 不再因“无变化不 commit、不 push”让提交长期搁置。
        _ahead = git("rev-list", "--count", "HEAD", "--not", "--remotes")
        try:
            _n_ahead = int((_ahead.stdout or "0").strip())
        except Exception:
            _n_ahead = 0
        if _ahead.returncode == 0 and _n_ahead > 0:
            _pp = git("push")
            if _pp.returncode == 0:
                log("幂等：无语义变化，但推走 %d 个滞留提交" % _n_ahead)
            else:
                warn("滞留提交 push 失败（下次重试）：%s" % (_pp.stderr or "")[:200])
                err_write("滞留提交 push 失败")
            _restore_stash()
            return 0 if _pp.returncode == 0 else 3
        log("幂等：6 数据文件均无变化，不 commit")
        # 余额实时推送（2026-11-09 根治）：无语义变化也推一次（冷启动/KV 被清空时补齐）。
        _post_balances_to_worker(balances)
        _restore_stash()
        return 0

    # R4 diff 校验
    try:
        r4_diff_check(snap)
    except (AssertionError, ValueError) as e:  # P2：r4 校验已改 raise ValueError
        msg = "NEED-REVIEW R4 diff 校验失败：%s" % e
        warn(msg)
        err_write(msg)
        # c项 R4恢复（2026-10-26）：仅当全部非预期文件均为 data/*.json 自动化脏时，
        # 归档+重置后重试一次；含人工/预览脏（模板/css/docs/其他）或解析不出时 fail-closed：
        # 不归档不重置，走原回滚+return 2。恢复绝不碰 stash（只 cp 归档 + checkout 指定文件）。
        _rec_done = False
        try:
            _rec_done, _rec_detail = _r4_try_recover(str(e), files=getattr(e, "unexpected_files", None))
        except Exception as _rec_e:
            warn("R4恢复异常（不阻断原流程）：%s" % _rec_e)
            _rec_done = False
        if _rec_done:
            try:
                r4_diff_check(snap)
            except (AssertionError, ValueError) as e2:
                warn("R4恢复后复检仍失败：%s" % e2)
                err_write("NEED-REVIEW R4恢复后复检仍失败：%s" % e2)
                _rec_done = False
            else:
                warn("R4恢复成功：%s，校验通过，继续提交" % _rec_detail)
                err_write("R4恢复成功：" + _rec_detail)
        if not _rec_done:
            # A 方案 1e（定案问题②：6 产物+模板）：回滚 checkout 前脏检查——
            # 检查范围内任一路径有未提交改动则全部跳过 checkout，宁可不回滚
            # 也不静默抹掉改动，留 NEED-REVIEW 由人工处置。
            _rb_dirty = git("-c", "core.quotepath=false", "status", "--porcelain",
                            "--", *(STASH_PATHS + TEMPLATE_PATHS)).stdout.strip()
            if _rb_dirty:
                msg2 = ("rollback_blocked_dirty：回滚范围存在未提交改动，"
                        "跳过 checkout（首行：%s）" % _rb_dirty.splitlines()[0][:120])
                warn(msg2)
                err_write("NEED-REVIEW " + msg2)
                _restore_stash()
                return 2
            # 回滚：模板 checkout；数据文件：新建的删除、已存在的 checkout。
            # B2-178：回滚 checkout 查 returncode，失败即告警（此前静默误判回滚完成）；
            # B2-179：os.remove 前校验文件内容确为本次写入（防 was_new 误标误删旧文件）。
            _rb_errs = []
            try:
                _rc = git("checkout", "--", TEMPLATE)
                if _rc.returncode != 0:
                    _rb_errs.append("模板 checkout 失败 rc=%d" % _rc.returncode)
                for rel, was_new in changed_files:
                    p = os.path.join(REPO_DIR, rel)
                    if was_new and os.path.exists(p):
                        # 校验：文件内容须与本次写入的 outputs[rel] 一致才删。
                        # B2-184 根治 2026-11-08：与守卫同口径忽略 generated_at（语义一致即视为本次写入）。
                        try:
                            with open(p, encoding="utf-8") as _f:
                                _cur = json.load(_f)
                            if _strip_generated_at(_strip_board_names(_cur)) != _strip_generated_at(_strip_board_names(outputs[rel])):
                                _rb_errs.append("%s 内容与本次写入不一致，跳过删除（防误删）" % rel)
                                continue
                        except Exception as _je:
                            _rb_errs.append("%s 删除前校验失败，跳过删除：%s" % (rel, _je))
                            continue
                        os.remove(p)
                    elif not was_new:
                        _rc2 = git("checkout", "--", rel)
                        if _rc2.returncode != 0:
                            _rb_errs.append("%s checkout 失败 rc=%d" % (rel, _rc2.returncode))
            finally:
                if _rb_errs:
                    warn("回滚不完整：%s" % "; ".join(_rb_errs))
                    err_write("NEED-REVIEW 回滚不完整：" + "; ".join(_rb_errs))
                _restore_stash()
            return 2

    # Q2（DeepSeek终审 2026-10-20《自动同步cron提交范围限制》）：
    # 提交前检查 scripts/ 是否有未提交改动；有则本次不提交 + 告警日志（fail-safe）。
    # 防 A类修复被功能提交卷入（程序异常台账 #1/#2）重演。
    # B2-185：git status 查 returncode、加 -c core.quotepath=false（非 ASCII 文件名不再错判）。
    _dirty = git("-c", "core.quotepath=false", "status", "--porcelain", "--", "scripts/")
    if _dirty.returncode != 0:
        warn("Q2 守卫 git status 失败 rc=%d，按脏处理跳过本次提交" % _dirty.returncode)
        _restore_stash()
        return 4
    _dirty_out = (_dirty.stdout or "").strip()
    if _dirty_out:
        # B2-202：过滤空行，告警日志不再出现空条目。
        _dirty_files = "; ".join(l[:80] for l in _dirty_out.splitlines()[:10] if l.strip())
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
        # B2-186：脏守卫触发返回 4（此前返回 0，cron 无法区分“跳过”与“成功同步”）。
        return 4

    # 提交（只加本方案的 6 数据文件；模板余额注入已废止，不再提交模板）
    # B2-184：git add 前校验 outputs 文件内容仍与本次写入一致（防写出后被手改卷入提交）。
    # B2-184 根治 2026-11-08：比较须与 semantic_diff 同口径忽略 generated_at；
    # 否则未重写文件（无语义变化、盘上 generated_at 陈旧）会被误判为“写出后被改动”，
    # 部分变更运行（仅子集文件重写，如午夜 today_delta 翻转只改 stats/points_log）
    # 必熔断 exit 2。外部手改仍因语义不同被拦截（见自测 T12）。
    for _rel, _obj in outputs.items():
        _p = os.path.join(REPO_DIR, _rel)
        try:
            with open(_p, encoding="utf-8") as _f:
                _cur = json.load(_f)
            if _strip_generated_at(_strip_board_names(_cur)) != _strip_generated_at(_strip_board_names(_obj)):
                warn("输出文件 %s 写出后被改动，跳过本次提交（防手改内容卷入）" % _rel)
                err_write("NEED-REVIEW 输出文件写出后被改动：%s" % _rel)
                _restore_stash()
                return 2
        except Exception as _e:
            warn("输出文件 %s 校验失败，跳过本次提交：%s" % (_rel, _e))
            _restore_stash()
            return 2
    _add_r = git("add", *outputs.keys())
    if _add_r.returncode != 0:
        warn("git add 失败 rc=%d：%s" % (_add_r.returncode, _add_r.stderr[:200]))
        err_write("git add 失败：" + _add_r.stderr[:200])
        _restore_stash()
        return 3
    msg = "chore(balance): sync from ledger [auto]" 
    cp = git("commit", "-m", msg)
    if cp.returncode != 0:
        # B2-182：commit 失败后 git reset HEAD，6 个产物不留在暂存区。
        git("reset", "HEAD", "--", *outputs.keys())
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
    # B2-189：提交后日志写入加 OSError 保护（此前写失败会使已提交的运行以异常退出，
    # 调用方误判为提交失败）；B2-203：一次写出，不再 log() 与 open().write() 两写。
    _log_line = "%s 已提交并推送：%s ledger_sha256=%s balances=%s\n" % (
        generated_at, msg, hashlib.sha256(ledger_bytes).hexdigest(), balances)
    print("[build_stats] " + _log_line.strip(), flush=True)
    try:
        with open(os.path.join(LOGS_DIR, "build_stats.log"), "a", encoding="utf-8") as f:
            f.write(_log_line)
    except OSError as _e:
        warn("build_stats.log 写入失败（提交已成功，不影响结果）：%s" % _e)
    # 余额实时推送（2026-11-09 根治）：静态提交已成功，再把余额写进 Worker KV；
    # fail-soft，推送失败不影响本次同步结果（静态文件仍是兜底）。
    _post_balances_to_worker(balances)
    _restore_stash()
    return 0


# ---------------------------------------------------------------------------
# 自测：放行条件2（同日排序单测）+ §7.1 / §7.3 / §7.5
# ---------------------------------------------------------------------------
def selftest():
    """自测 11 组（T1–T11），各录 1 段日志到 logs/build_stats_selftest.log。"""
    os.makedirs(LOGS_DIR, exist_ok=True)
    slog = os.path.join(LOGS_DIR, "build_stats_selftest.log")
    results = []

    def rec(name, ok, detail):
        results.append((name, ok, detail))
        line = "%s [%s] %s :: %s" % (
            datetime.now(TZ).isoformat(timespec="seconds"),
            "PASS" if ok else "FAIL", name, detail)
        print(line, flush=True)
        # B2-201：自测日志写入加保护，LOGS_DIR 异常时自测整体不再异常退出。
        try:
            with open(slog, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    today = datetime.now(TZ).date()
    gen = datetime.now(TZ).isoformat(timespec="seconds")

    # --- T1 放行条件2：同日多条排序（日期倒序，同日按台账正序；KB-SORT-2026-10-20 后：
    # 本用例均为无时刻历史行，兼容新口径）---
    # B2-205（2026-11-08 根治）：日期改相对 today（此前硬编码 10-17/10-18/10-19，
    # 2026-10-26 后全部滑出 build_log 的近 7 天窗口 → items=[]、T1 假失败；
    # 生产代码按 §1.2 窗口过滤无误，是测试本身过时）。annot_time 全行同值 19:30：
    # 2026-10-19 后条目缺 @HH:MM 即触 G6 熔断；同值时刻不改变同日按台账正序的断言意图。
    try:
        _d0 = today.strftime("%m-%d")
        _d1 = (today - timedelta(days=1)).strftime("%m-%d")
        _d2 = (today - timedelta(days=2)).strftime("%m-%d")
        rows = [
            {"date": _d2, "desc": "第一条", "pts": 1, "balance": 1, "annot_time": "19:30"},
            {"date": _d1, "desc": "第二条", "pts": 1, "balance": 2, "annot_time": "19:30"},
            {"date": _d2, "desc": "第三条", "pts": 1, "balance": 3, "annot_time": "19:30"},
            {"date": _d0, "desc": "第四条", "pts": 1, "balance": 4, "annot_time": "19:30"},
            {"date": _d2, "desc": "第五条", "pts": 1, "balance": 5, "annot_time": "19:30"},
        ]
        d = build_log(5, rows, today, gen)
        got = [it["reason"] for it in d["items"]]
        # simplify_desc("第一条")=="第一条" 等；期望：10-19第四条, 10-18第二条, 10-17第一条/第三条/第五条
        exp = ["第四条", "第二条", "第一条", "第三条", "第五条"]
        assert got == exp, "排序错误：%r != %r" % (got, exp)
        # S1 防回归（2026-10-20）：死代码删除后，schema 必须为当前版本（含 time/time_source 字段）
        # KB-POST2-2026-10-22：版本随口径升版 1.2 → 1.3（7天全量输出、取消负分额外保留、前端3天滚动展示）
        assert d["schema_version"] == "1.3", "schema_version=%r（期望 1.3，死代码回退？）" % d.get("schema_version")
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
    # B2-195：T3b 真测 write_json_atomic 的读回校验（此前 T3 只测了 json.load 对坏 JSON
    # 的反应，write_json_atomic 自身的写→读回→比对路径未覆盖）；固定路径文件用后清理。
    _t3b_p = None
    try:
        import tempfile
        _t3b_p = tempfile.mktemp(suffix=".json")
        write_json_atomic(_t3b_p, {"x": 1, "y": [1, 2]})
        with open(_t3b_p, encoding="utf-8") as _f:
            assert json.load(_f) == {"x": 1, "y": [1, 2]}
        rec("T3b 写后读回校验", True, "write_json_atomic 写→读回一致")
    except Exception as e:
        rec("T3b 写后读回校验", False, str(e)[:200])
    finally:
        if _t3b_p:
            try:
                os.unlink(_t3b_p)
            except Exception:
                pass
        try:
            os.unlink("/tmp/_selftest_json")
        except Exception:
            pass

    # --- T4 §7.5：敏感词命中 → 替换+计数，不阻断 ---
    # B2-205（2026-11-08 根治）：与 T1 同因——硬编码 10-18/10-19 已滑出近 7 天窗口
    # → filtered_count=0 假失败；日期改相对 today（复用 T1 的 _d0/_d1），annot_time
    # 同值 19:30 避 G6 熔断；断言本身（替换为"学习记录"+计数 1）未动。
    try:
        rows = [
            {"date": _d0, "desc": "数学作业按时完成", "pts": 2, "balance": 10, "annot_time": "19:30"},
            # "家长特批"在括号外，simplify_desc 去不掉，必须被 B5 断言捕获
            {"date": _d0, "desc": "晾衣服家长特批+3", "pts": 3, "balance": 13, "annot_time": "19:30"},
            {"date": _d1, "desc": "语文抄写迟交", "pts": -2, "balance": 11, "annot_time": "19:30"},
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

    # --- T9 A 方案单点口径（DeepSeek 定案 T+1 验收）：STASH_PATHS 与产物
    # 键集合严格相等、TEMPLATE_PATHS 非空且与 TEMPLATE 常量同源 ---
    try:
        derived = {_output_key(kind, grade)
                   for grade in (5, 6)
                   for kind in ("stats", "points_log", "shop")}
        literal = {"data/stats_grade_5.json", "data/stats_grade_6.json",
                   "data/points_log_5.json", "data/points_log_6.json",
                   "data/shop_grade_5.json", "data/shop_grade_6.json"}
        assert set(STASH_PATHS) == derived == literal, \
            "STASH_PATHS 漂移：%r" % (STASH_PATHS,)
        assert len(STASH_PATHS) == 6 and len(set(STASH_PATHS)) == 6
        assert TEMPLATE_PATHS and \
            TEMPLATE_PATHS[0] == os.path.relpath(TEMPLATE, REPO_DIR), \
            "TEMPLATE_PATHS 与 TEMPLATE 常量不一致"
        rec("T9 A方案单点口径", True,
            "STASH_PATHS=6 产物（与 _output_key 派生集合、字面集合三方一致），TEMPLATE_PATHS 同源")
    except Exception as e:
        rec("T9 A方案单点口径", False, str(e)[:200])

    # --- T10 N3 哨兵当日首轮判定（DeepSeek 定案问题④验收）---
    # B2-192/B2-194：tp 预初始化为 None，unlink 放 finally（此前 unlink 在 try 末尾，
    # 断言失败即残留临时文件；临时文件创建失败时 finally 的 unlink 也不抛 NameError）。
    _t10_tp = None
    try:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as tf:
            _t10_tp = tf.name
        assert _n3_sentinel_already_logged_today(_t10_tp, "2026-10-20") is False, "空日志应为首轮"
        with open(_t10_tp, "a", encoding="utf-8") as f:
            f.write("2026-10-19T23:00:00+08:00 N3 sentinel OK g5=1 g6=2\n")
        assert _n3_sentinel_already_logged_today(_t10_tp, "2026-10-20") is False, "只有昨日行应为首轮"
        with open(_t10_tp, "a", encoding="utf-8") as f:
            f.write("2026-10-20T08:00:00+08:00 N3 sentinel OK g5=170 g6=274\n")
        assert _n3_sentinel_already_logged_today(_t10_tp, "2026-10-20") is True, "今日已有行应非首轮"
        assert _n3_sentinel_already_logged_today(_t10_tp + ".missing", "2026-10-20") is False, "文件缺失应为首轮"
        rec("T10 N3首轮判定", True, "空/只有昨日行/文件缺失→首轮；今日已有行→非首轮")
    except Exception as e:
        rec("T10 N3首轮判定", False, str(e)[:200])
    finally:
        if _t10_tp:
            try:
                os.unlink(_t10_tp)
            except Exception:
                pass

    # --- T11 R4 容忍 docs/程序异常台账.md（2026-10-21 熔断根因修复验收）---
    try:
        _real_git = globals()["git"]

        class _FR:
            returncode = 0
            stdout = (" data/stats_grade_5.json | 2 +-\n"
                      " docs/程序异常台账.md | 3 ++-\n")

        class _FR2:
            returncode = 0
            stdout = " scripts/evil.py | 9 ++++++++-\n"

        globals()["git"] = lambda *a: _FR()
        try:
            assert r4_diff_check("") is True, "docs 台账变更应被容忍放行"
        finally:
            globals()["git"] = _real_git
        globals()["git"] = lambda *a: _FR2()
        try:
            # B2-191：SystemExit 显式捕获（此前外层只捕 Exception，SystemExit 可逃逸致
            # rec FAIL 与 failed 汇总不执行）；mock 校验调用参数（此前 lambda 忽略参数）。
            try:
                r4_diff_check("")
            except ValueError as ve:
                assert "R4 拦截" in str(ve), str(ve)
            else:
                raise AssertionError("未知文件未被拦截")
        finally:
            globals()["git"] = _real_git
        rec("T11 R4容忍docs", True,
            "docs/程序异常台账.md 未提交变更放行；未知文件仍 R4 拦截")
    except BaseException as e:
        # B2-191：BaseException 捕获 SystemExit，mock 恢复后记 FAIL。
        try:
            globals()["git"] = _real_git
        except Exception:
            pass
        rec("T11 R4容忍docs", False, str(e)[:200])

    # --- T12 B2-184 根治 2026-11-08：写后校验守卫须与 semantic_diff 同口径忽略 generated_at ---
    # 场景：午夜 today_delta 翻转只改 stats/points_log，shop 文件无语义变化未重写，
    # 盘上 generated_at 陈旧；守卫若全量比较即误判“写出后被改动”exit 2。
    try:
        def _guard_eq(disk, mem):
            # 与 B2-184 守卫、回滚校验同一比较表达式
            return _strip_generated_at(_strip_board_names(disk)) == _strip_generated_at(_strip_board_names(mem))
        _stale = {"schema_version": "1.0", "grade": 5, "current_balance": 332,
                  "generated_at": "2026-11-07T14:20:03+08:00"}
        _fresh = {"schema_version": "1.0", "grade": 5, "current_balance": 332,
                  "generated_at": "2026-11-08T00:00:04+08:00"}
        assert _guard_eq(_stale, _fresh), "仅 generated_at 不同应判相等（未重写文件不触发守卫）"
        _tampered = dict(_fresh, current_balance=333)
        assert not _guard_eq(_stale, _tampered), "余额被改仍须判不等（外部手改照常拦截）"
        _nested_stale = {"items": [{"generated_at": "2026-11-07T14:20:03+08:00", "reason": "x"}],
                         "generated_at": "2026-11-07T14:20:03+08:00"}
        _nested_fresh = {"items": [{"generated_at": "2026-11-08T00:00:04+08:00", "reason": "x"}],
                         "generated_at": "2026-11-08T00:00:04+08:00"}
        assert _guard_eq(_nested_stale, _nested_fresh), "嵌套 generated_at 不同亦应判相等"
        rec("T12 B2-184守卫语义比较", True,
            "陈旧 generated_at 判相等不熔断；语义被改仍判不等拦截；嵌套时间戳亦忽略")
    except Exception as e:
        rec("T12 B2-184守卫语义比较", False, str(e)[:200])

    failed = [r for r in results if not r[1]]
    log("自测完成：%d 通过 / %d 失败" % (len(results) - len(failed), len(failed)))
    return 1 if failed else 0


def main():
    """B2-181/B2-170：外层 try/finally——异常退出时也恢复 stash、关闭 build 锁。
    _main_impl 内各 return 出口仍手动调 _restore_stash（正常路径）；本层只兜底异常路径。"""
    try:
        return _main_impl()
    finally:
        try:
            _bs_restore_stash_fallback()
        except Exception:
            pass
        _lf = _BS_STATE.get("lock_fp")
        if _lf is not None:
            try:
                fcntl.flock(_lf, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                _lf.close()
            except Exception:
                pass
            _BS_STATE["lock_fp"] = None


if __name__ == "__main__":
    sys.exit(main())

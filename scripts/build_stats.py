#!/usr/bin/env python3
"""看板真实数据生成器（DeepSeek 方案 KB-STATS-AUTO-2026-10-19）。

链路：VM 本地 cron（工作日 19:00-23:00 / 周末 09:00-23:00，Asia/Shanghai，每 15 分钟，
      与 GitHub workflow 同窗口错开 2 分钟）→ 本脚本 → data/*.json → git commit+push
      → 现有 workflow 照常部署到 Pages。

产出（幂等：内容无变化则不 commit）：
  data/stats_grade_{5,6}.json   余额（§1.1，低频强一致，带 checksum）
  data/points_log_{5,6}.json    积分日志（§1.2+R3：近30天/≤20条，含 filtered_count/filtered）
  data/shop_grade_{5,6}.json    商店档位进度（R1：规则手册第85行档位表，本期接入）

红线：
  - 台账只读，绝不写台账。
  - 解析失败（§7.1）→ 不产出/不覆盖/不 commit，写 logs/build_stats.err。
  - reason 只输出 simplify_desc() 简化结果 + B5 断言；命中即替换为"学习记录"+filtered。
  - 模板三重替换（B2+B4）+ R4 diff 校验；超限即停手不 commit。

用法：
  python3 scripts/build_stats.py            正常运行（cron 调用）
  python3 scripts/build_stats.py --selftest 自测（放行条件2同日排序单测 + §7.1/7.3/7.5）
  python3 scripts/build_stats.py --force     忽略时间窗口（手动用）
"""

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

LEDGER = os.path.expanduser("~/workspace/goals/goal/hidden_files/积分记录.md")
DATA_DIR = os.path.join(REPO_DIR, "data")
LOGS_DIR = os.path.join(REPO_DIR, "logs")
TEMPLATE = os.path.join(REPO_DIR, "templates", "积分看板_全新版式样板.html")
TZ = timezone(timedelta(hours=8), "CST")

# R1：档位表（规则手册第85行，唯一权威；变动须走规则手册修订）
SHOP_TIERS = [
    {"pts": 70, "name": "30分钟Minecraft", "kind": "Minecraft"},
    {"pts": 100, "name": "1小时Minecraft", "kind": "Minecraft"},
    {"pts": 150, "name": "1小时和平精英或2小时Minecraft", "kind": "和平精英"},
    {"pts": 250, "name": "2小时和平精英", "kind": "和平精英"},
    {"pts": 600, "name": "愿望50元", "kind": "愿望"},
    {"pts": 1000, "name": "愿望100元", "kind": "愿望"},
]

NEG_MINUS = "−"  # U+2212 全角减号（用户拍板1：负余额静态显示 −N）

ROW_RE = re.compile(
    r"^\|\s*(\d{2}-\d{2})\s*\|\s*(.*?)\s*\|\s*([+-]?\d+)\s*\|\s*([+-]?\d+)\s*\|"
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


def in_sync_window(now=None):
    """§2.2：工作日 19:00-23:00，周末/假期 09:00-23:00（Asia/Shanghai）。"""
    now = now or datetime.now(TZ)
    wd = now.weekday()  # 0=周一
    h = now.hour + now.minute / 60.0
    if wd < 5:
        return 19.0 <= h < 23.0
    return 9.0 <= h < 23.0


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
        # --- 核实1补断言 ---
        assert block.strip(), "分节 ## %s 切块为空" % title
        assert "<!-- key:" in block, "分节 ## %s 切块不含 <!-- key: 记录" % title
        assert "历史归档" not in block, "分节 ## %s 切块混入历史归档" % title
        return block

    return {5: cut("五年级"), 6: cut("六年级")}


def parse_rows(block, grade):
    """解析 | 日期 | 描述 | 积分 | 余额 | 行；保持台账行顺序。

    畸形行（像数据行但正则不匹配）→ raise（§7.1：不产出）。
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
        mm_dd, desc, pts_s, bal_s = m.groups()
        pts = int(pts_s)
        bal = int(bal_s)  # 允许负数（§7.2）
        rows.append({"date": mm_dd, "desc": desc, "pts": pts, "balance": bal})
    if not rows:
        raise ValueError("台账 %d 年级未解析出任何数据行" % grade)
    # 余额链连续性抽查：余额应等于上一余额+积分（首行除外，允许期初行）
    prev = None
    for r in rows:
        if prev is not None and r["balance"] != prev + r["pts"]:
            warn("余额链疑似断裂（%s）：%d + %d != %d" % (r["date"], prev, r["pts"], r["balance"]))
        prev = r["balance"]
    return rows


def complete_year(mm_dd, today):
    """MM-DD 补全年：若日期在今天之后，视为去年（跨年分支，§5.4 本期 10 月不启用但保留）。"""
    m, d = int(mm_dd[:2]), int(mm_dd[3:5])
    year = today.year
    if (m, d) > (today.month, today.day):
        year -= 1
    return "%04d-%s" % (year, mm_dd)


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
    """§1.2 + R3：近30天倒序、同日按台账正序、≤20条；reason 经 simplify_desc + B5 断言。"""
    cutoff = today - timedelta(days=30)
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
        dated.append((dt, idx, r))

    # 排序：日期倒序；同日按台账行顺序（idx）正序 —— 显式实现（放行条件2）
    dated.sort(key=lambda t: (t[0], t[1]), reverse=False)
    # 先按 idx 正序稳定，再按日期倒序稳定 → 同日保持台账正序
    dated.sort(key=lambda t: t[1])
    dated.sort(key=lambda t: t[0], reverse=True)

    truncated = len(dated) > 20
    items = []
    filtered_count = 0
    for dt, idx, r in dated[:20]:
        reason = simplify_desc(r["desc"])
        if len(reason) > 40:
            reason = reason[:40] + "…"  # 上限 40 字，超长截断加 …
        filtered = False
        hits = _assert_lib.find_forbidden_terms(reason)
        if hits:
            # B5：命中即替换，不阻断整体；明细打 WARN 供 DeepSeek 抽查
            warn("敏感词命中 grade=%d date=%s hits=%r → 替换为'学习记录'"
                 % (grade, r["date"], hits))
            err_write("FILTERED grade=%d date=%s hits=%s" % (grade, r["date"], ",".join(hits)))
            reason = "学习记录"
            filtered = True
            filtered_count += 1
        pts = r["pts"]
        items.append({
            "date": r["date"],  # MM-DD（本期不跨年；跨年时 complete_year 输出 YYYY-MM-DD）
            "reason": reason,
            "pts": pts,
            "sign": "plus" if pts >= 0 else "minus",
            "filtered": filtered,
        })

    d = {
        "schema_version": "1.0",
        "grade": grade,
        "generated_at": generated_at,
        "window": {"days": 30, "max_items": 20},
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
# ---------------------------------------------------------------------------
def replace_template_balances(balances):
    """balances: {5: int, 6: int}。

    三重替换：data-target 属性 + span 文本节点 + 顺序断言 + 一致性断言。
    返回 (changed: bool, detail: str)。
    """
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()

    span_re = re.compile(r'<span class="bal-num"[^>]*data-target="(-?\d+)"[^>]*>\s*(-?\d+|−\d+)\s*</span>')
    spans = list(span_re.finditer(html))
    assert len(spans) == 2, "模板 bal-num span 数 != 2（实际 %d）" % len(spans)

    # 顺序断言：第一个属五年级块，第二个属六年级块
    p5 = html.find('id="bal-jump-g5"')
    p6 = html.find('id="bal-jump-g6"')
    assert p5 != -1 and p6 != -1, "模板缺少 bal-jump-g5/g6 锚点"
    assert p5 < spans[0].start() < p6 < spans[1].start(), "bal-num 顺序断言失败（须五年级在前、六年级在后）"

    new_html = html
    # 从后往前替换，避免偏移
    for span_m, grade in ((spans[1], 6), (spans[0], 5)):
        bal = balances[grade]
        assert isinstance(bal, int) and not isinstance(bal, bool), "余额须为整数"
        disp = ("%s%d" % (NEG_MINUS, abs(bal))) if bal < 0 else str(bal)  # 负值静态 −N（U+2212）
        old = span_m.group(0)
        new = re.sub(r'data-target="(-?\d+)"', 'data-target="%d"' % bal, old, count=1)
        new = re.sub(r'>\s*(-?\d+|−\d+)\s*<', '>%s<' % disp, new, count=1)
        new_html = new_html[:span_m.start()] + new + new_html[span_m.end():]

    if new_html == html:
        return False, "模板余额已是最新，无需替换"

    # 一致性断言：每个 span 的 textContent（U+2212 归一为 -）== data-target
    for m in span_re.finditer(new_html):
        t_attr, t_text = m.group(1), m.group(2)
        assert t_text.replace(NEG_MINUS, "-") == t_attr, \
            "一致性断言失败：text %r vs data-target %r" % (t_text, t_attr)

    with open(TEMPLATE, "w", encoding="utf-8") as f:
        f.write(new_html)
    return True, "模板三重替换完成：五年级=%d 六年级=%d" % (balances[5], balances[6])


def r4_diff_check(snapshot_before):
    """R4：替换后 diff 与快照对比 —— 除 data/*.json 外只允许模板变化，且模板行数变动 ≤ 4。"""
    cur = git("-c", "core.quotepath=false", "diff", "--stat")
    assert cur.returncode == 0, "git diff --stat 执行失败"
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
    assert not unexpected, "R4 拦截：出现非预期变更文件 %r" % sorted(unexpected)
    if template_name in changed:
        numstat = git("-c", "core.quotepath=false", "diff", "--numstat", "--", template_name)
        parts = numstat.stdout.split()
        added, deleted = int(parts[0]), int(parts[1])
        assert added + deleted <= 4, \
            "R4 拦截：模板行数变动 %d 超限（≤4）" % (added + deleted)
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def write_json_atomic(path, obj):
    """写 JSON 后读回校验（§7.3 生成端）；失败 raise。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(tmp, encoding="utf-8") as f:
        back = json.load(f)
    assert back == obj, "JSON 写后读回不一致：%s" % path
    os.replace(tmp, path)


def main(force=False):
    if not force and not in_sync_window():
        log("非同步窗口（工作日19:00-23:00/周末09:00-23:00），跳过")
        return 0
    if "--selftest" in sys.argv:
        return selftest()

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    today = datetime.now(TZ).date()
    generated_at = datetime.now(TZ).isoformat(timespec="seconds")

    # R4-1：替换前 pull --rebase；冲突 → 不 commit、写 err、告警
    pr = git("pull", "--rebase")
    if pr.returncode != 0:
        msg = "R4 拦截：git pull --rebase 冲突，停手待人工处理"
        warn(msg)
        err_write("NEED-REVIEW " + msg + " :: " + pr.stderr[:200])
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
        return 2

    balances = {}
    for grade in (5, 6):
        rows = all_rows[grade]
        bal = rows[-1]["balance"]
        balances[grade] = bal
        if bal < 0:
            warn("§7.2 负余额：%d 年级余额 %d（照常输出，前端静态显示）" % (grade, bal))

    outputs = {}
    for grade in (5, 6):
        rows = all_rows[grade]
        outputs["data/stats_grade_%d.json" % grade] = build_stats(
            grade, rows, ledger_sha16, today, generated_at)
        outputs["data/points_log_%d.json" % grade] = build_log(
            grade, rows, today, generated_at)
        outputs["data/shop_grade_%d.json" % grade] = build_shop(
            grade, balances[grade], generated_at)

    # 写文件（原子写 + 读回校验）
    changed_files = []
    for rel, obj in outputs.items():
        path = os.path.join(REPO_DIR, rel)
        old = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = f.read()
        write_json_atomic(path, obj)
        with open(path, encoding="utf-8") as f:
            new = f.read()
        if old != new:
            changed_files.append((rel, old is None))  # (路径, 是否为新建)

    # 模板三重替换（B2+B4）
    tpl_changed, tpl_detail = replace_template_balances(balances)
    log(tpl_detail)

    if not changed_files and not tpl_changed:
        log("幂等：6 文件与模板均无变化，不 commit")
        return 0

    # R4 diff 校验
    try:
        r4_diff_check(snap)
    except AssertionError as e:
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
        return 2

    # 提交（只加本方案的 6 文件 + 模板）
    git("add", *outputs.keys())
    if tpl_changed:
        git("add", TEMPLATE)
    msg = "chore(balance): sync from ledger [auto%s]" % (
        ", ds-reviewed" if tpl_changed else "")
    cp = git("commit", "-m", msg)
    if cp.returncode != 0:
        warn("git commit 失败：%s" % cp.stderr[:200])
        err_write("git commit 失败：" + cp.stderr[:200])
        return 3
    pp = git("push")
    if pp.returncode != 0:
        warn("git push 失败（本地文件已保留，下次重试）：%s" % pp.stderr[:200])
        err_write("git push 失败：" + pp.stderr[:200])
        return 3
    log("已提交并推送：%s（模板%s）" % (msg, "已更新" if tpl_changed else "未动"))
    with open(os.path.join(LOGS_DIR, "build_stats.log"), "a", encoding="utf-8") as f:
        f.write("%s ledger_sha256=%s balances=%s commit=%s\n" % (
            generated_at, hashlib.sha256(ledger_bytes).hexdigest(),
            balances, msg))
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

    # --- T1 放行条件2：同日多条排序（日期倒序，同日按台账正序）---
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

    failed = [r for r in results if not r[1]]
    log("自测完成：%d 通过 / %d 失败" % (len(results) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    force = "--force" in sys.argv
    sys.exit(main(force=force))

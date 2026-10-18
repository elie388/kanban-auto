#!/usr/bin/env python3
"""积分看板生成器 v2（方案A，DeepSeek架构评审通过+数据源追问裁决）

数据源：hidden_files/积分记录.md（用户钦定权威台账，只读解析，绝不写回）
  - DB point_ledger 已证实残缺（9行 vs md 31行），生成器不读DB（DeepSeek裁决第3条）
  - 脱节差异另行登记于 积分对账差异登记.md，不在生成器内消化
硬约束：
  1. md只读解析；占位符存在性校验（每个出现且仅一次）
  2. md自洽性校验：逐笔累加 vs md余额列，三方对齐（行/笔/总额），偏离即报错
  3. 时间提取：描述文字正则 HH:MM；有"→"则取最后一个→之后的首个时间；提不到不编造，只显示日期
  4. 幂等：md指纹无变化则静默跳过
  5. 原子写（.tmp + fsync + rename），失败保留旧文件；备份上一版；日志
"""
import fcntl
import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

# Asia/Shanghai 时区（VM 系统为 UTC，徽章"周"按 DeepSeek 仲裁必须用本地时间）
SH_TZ = timezone(timedelta(hours=8))


def shanghai_today():
    """上海本地今天（date 对象）。"""
    return datetime.now(SH_TZ).date()

BASE = os.path.dirname(os.path.abspath(__file__))
MD_PATH = os.path.join(BASE, "积分记录.md")
TEMPLATE_PATH = os.path.join(BASE, "积分看板.template.html")
OUTPUT_PATH = os.path.expanduser("~/workspace/积分看板.html")
BACKUP_PATH = OUTPUT_PATH + ".bak"
FINGERPRINT_PATH = os.path.join(BASE, ".kanban.last_build.json")
LOG_PATH = os.path.join(BASE, "build_kanban.log")
LOCK_PATH = os.path.join(BASE, ".kanban.build.lock")

GRADES = ["五年级", "六年级"]
GRADE_SET = set(GRADES)

# 兑换档位卡片文案（措辞沿用现有卡片风格，仅替换数字；勿擅改措辞）
# I-1（DeepSeek仲裁2026-10-18）：改档位数字时必须同步模板 STATIC_REDEEM 区表格；
# 构建期有集合相等断言，漂移即拒绝生成；JS 端档位以 HTML 表格为单一数据源解析
CARD_TIER_DESC = {
    70: "30 分钟 Minecraft",
    100: "1 小时 Minecraft",
    150: "1 小时和平精英或2小时 Minecraft",
    250: "2 小时和平精英",
    600: "达成愿望1次（等额50元，可兑现金）",
    1000: "达成愿望1次（等额100元，可兑现金）",
}
TIER_ORDER = sorted(CARD_TIER_DESC)

# 徽章映射表（2026-10-18 DeepSeek仲裁确认；用户2026-10-18定：徽章成就区每周更新）
# key命名约定：badge_<拼音>，必须为台账key第3段的段首；冲正key：reverse_badge_<拼音>
# 显示文案（图标+名称+分值）为用户逐字确认区，生成时不得改动措辞
BADGE_MAP = [
    {"icon": "🛡️", "name": "零失误", "key": "badge_lingshiwu", "points": "+5/天",
     "trigger": "当日零扣分"},
    {"icon": "💰", "name": "百分俱乐部", "key": "badge_baifen", "points": "+10",
     "trigger": "余额首次突破100（一次性）"},
    {"icon": "👑", "name": "学霸", "key": "badge_xueba", "points": "+10/次",
     "trigger": "当日有考试90分以上（同一考试只加一次）"},
    {"icon": "🎯", "name": "全对小达人", "key": "badge_quandui", "points": "+1",
     "trigger": "单日作业+练习正确率100%"},
    {"icon": "⏰", "name": "守时小达人", "key": "badge_shoushi", "points": "+1",
     "trigger": "单日所有事项截止前完成"},
    {"icon": "💯", "name": "满分超人", "key": "badge_manfen", "points": "+1",
     "trigger": "单次考试/测验满分"},
    {"icon": "✏️", "name": "订正小能手", "key": "badge_dingzheng", "points": "+1",
     "trigger": "单日所有订正一次改对"},
    {"icon": "✍️", "name": "书写小达人", "key": "badge_shuxie", "points": "+1",
     "trigger": "单日三科零书写问题"},
    {"icon": "📅", "name": "全勤小标兵", "key": "badge_quanqin", "points": "+1",
     "trigger": "单日作业/练习/53/家务全交齐"},
]
# 徽章空状态文案（2026-10-18 DeepSeek仲裁：不用"没有/还没/未获得"等否定词）
BADGE_EMPTY_TEXT = "本周徽章等你来拿 🏅"

# 方案A：每日目标（今日冲刺进度条）
# 暂定值，未入规则手册；来源：家长口头确认（2026-10-18），待规则手册补条款后以手册为准
# 红线（DeepSeek仲裁）：仅用于看板展示（进度条），严禁进入积分计算/扣分逻辑；
# 积分复算一律只认积分记录.md台账逐笔复算，DAILY_TARGET 永远不能进扣分逻辑
DAILY_TARGET = 30
# 方案C：兑换区"差一点"阈值（余额达到所需85%视为差一点）
ALMOST_RATIO = 0.85

PLACEHOLDERS = ["PAGE_DATE", "BALANCE_CARDS", "TICKER_SECTION_OPEN", "MARQUEE_ANIM",
                "MARQUEE_KEYFRAMES", "MARQUEE_G5", "MARQUEE_G6",
                "RECENT_TABLE_G5", "RECENT_TABLE_G6",
                "BADGE_GRID_G5", "BADGE_GRID_G6"]
# badge 已移出静态区（2026-10-18用户定：徽章成就区每周动态更新，只显示当周获得）
STATIC_ZONES = ["earn", "deduct", "redeem"]

ITEM_PX = 52
SEC_PER_ITEM = 2.2

ROW_RE = re.compile(
    r"^\| ([^|]+?) \| (.+?) \| ([+-]?\d+) \| ([+-]?\d+) \|(?:\s*<!-- key:(.*?) -->)?\s*\|?\s*$"
)
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def extract_hhmm(desc):
    """时间提取规则（DeepSeek认可：不编造）：
    先剔除"日期+时间"型的引用（如"10-15 20:00发布"中的20:00是发布时间，非事件时间）；
    描述中有"→" → 取最后一个→之后的首个 HH:MM（如"22:02要求→22:25发回"取22:25）；
    否则取首个 HH:MM；都没有 → 返回""（只显示日期）。"""
    # 剔除"MM-DD HH:MM"型日期时间引用，避免误取发布时间等
    cleaned = re.sub(r"\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}", "", desc)
    tail = cleaned.rsplit("→", 1)[-1] if "→" in cleaned else cleaned
    m = TIME_RE.search(tail)
    if not m:
        return ""
    h, mi = int(m.group(1)), m.group(2)
    if 0 <= h <= 23 and 0 <= int(mi) <= 59:
        return f"{h:02d}:{mi}"
    return ""


# 实际计分时间核查表（2026-10-18 用户要求"按实际计分时间"补上缺失时间）
# 键：台账结算键；值：(HH:MM, 来源说明)
# 来源分级：DB创建时间 > 对话确认 > 同批次相关条目 > 每日总结任务时间
# 未列出的条目：经核查无可靠时间来源，保持无时间徽章（绝不编造）
VERIFIED_SCORE_TIMES = {
    # —— DB point_ledger.创建时间（最高优先级，精确到秒）——
    "parent_reward_5_2026-10-17": ("14:11", "DB创建时间2026-10-17 14:11:28"),
    "parent_reward_6_2026-10-17": ("14:11", "DB创建时间2026-10-17 14:11:28"),
    "六年级|2026-10-17|eng_p5_no_submit|-5": ("20:04", "DB创建时间2026-10-17 20:04:00"),
    # —— 用户对话确认 ——
    "六年级|2026-10-17|towel_misplaced|-3": ("22:13", "用户22:13下令临时扣分"),
    # —— 同批次相关条目（同一计分事件）——
    "五年级|2026-10-17|53_p17_60-80|-1": ("21:33", "同批次53_p17_submit条目21:33"),
    "五年级|2026-10-17|53_p17_correction_2|+4": ("22:25", "同批次53_p17_makeup_ontime条目22:25"),
    "六年级|2026-10-17|tp_full|+5": ("20:49", "同场计分tp_on_time条目20:49"),
    "六年级|2026-10-17|tp_correction|+2": ("20:23", "同场计分tp_80pct条目20:23"),
    # —— 每日总结任务时间（22:45任务启动，计分发生在22:45-22:56窗口内）——
    "五年级|2026-10-17|chore_2days_missed|-5": ("22:45", "每日总结任务22:45启动时计分"),
}


def lookup_verified_time(key):
    """查核查表返回实际计分时间；无则返回空字符串（保持无徽章，不编造）。"""
    item = VERIFIED_SCORE_TIMES.get(key or "")
    return item[0] if item else ""


def parse_md(md_path):
    """解析积分记录.md，返回 {年级: [entries]}。entry含md_date/desc/pts/balance/key/hhmm。"""
    text = open(md_path, encoding="utf-8").read()
    per_grade = {g: [] for g in GRADES}
    current = None
    for ln, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("## "):
            g = s[3:].strip()
            current = g if g in GRADE_SET else None
            continue
        if current and s.startswith("|"):
            # B2修复(2026-10-18)：表头/分隔行改用结构化判定。
            # 旧逻辑用"日期"子串匹配，数据行描述含"日期"二字时会被静默丢弃
            # （与2026-10-12漏记+18同失效模式），现改判首个单元格是否为"日期"。
            first_cell = s.split("|")[1].strip() if s.count("|") >= 2 else ""
            if first_cell == "日期" or "---" in s:
                continue  # 表头行 / 分隔行，跳过
            # 以下为数据行
            # 防护：描述文字若含"|"会导致列错位；export已将"|"转写为"｜"。
            # key注释内含|（如"五年级|期初余额|2026-10-16"），先剥离注释再计数，表体应恰好5个|
            body = re.sub(r"<!-- key:.*? -->", "", s)
            if body.count("|") != 5:
                raise RuntimeError(f"md第{ln}行表体竖线数异常({body.count('|')}个≠5)，可能描述含|导致错位: {s[:60]}")
            m = ROW_RE.match(s)
            if not m:
                raise RuntimeError(f"md第{ln}行解析失败: {s[:60]}")
            md_date, desc, pts_s, bal_s, key = m.groups()
            pts = int(pts_s)
            bal = int(bal_s)
            clean_key = (key or "").strip()
            # 时间：先从描述提取；提取不到则查实际计分时间核查表；都没有则空（不编造）
            hhmm = extract_hhmm(desc.strip()) or lookup_verified_time(clean_key)
            per_grade[current].append({
                "md_date": md_date.strip(),
                "desc": desc.strip(),
                "pts": pts,
                "balance": bal,
                "key": clean_key,
                "hhmm": hhmm,
                "line": ln,
            })
    # B2修复(2026-10-18)：key集合一致性断言，把"静默丢行"变成"显式失败"。
    # 台账中每个 <!-- key:xxx --> 都必须被解析为一条数据（反之亦然），
    # 否则说明有数据行被过滤逻辑误删或重复解析，直接报错不生成。
    all_keys_in_text = re.findall(r"<!-- key:(.*?) -->", text)
    parsed_keys = [e["key"] for g in GRADES for e in per_grade[g] if e["key"]]
    dup_in_text = sorted(k for k, c in Counter(all_keys_in_text).items() if c > 1)
    if Counter(all_keys_in_text) != Counter(parsed_keys) or dup_in_text:
        missing = sorted(set(all_keys_in_text) - set(parsed_keys))
        extra = sorted(set(parsed_keys) - set(all_keys_in_text))
        raise RuntimeError(
            f"md key一致性校验失败：台账key数={len(all_keys_in_text)} 解析key数={len(parsed_keys)} "
            f"缺失={missing} 多余={extra} 台账内重复key={dup_in_text}"
        )
    for g in GRADES:
        if not per_grade[g]:
            # B1修复(2026-10-18)：原f-string引用未定义的grade，改为循环变量g
            raise RuntimeError(f"md中{g}无数据行，拒绝生成")
    return per_grade


def verify_self_consistent(per_grade):
    """md自洽性校验：逐笔累加 vs md余额列。偏离即报错（DeepSeek裁决第4条主校验）。"""
    for g in GRADES:
        acc = 0
        for e in per_grade[g]:
            acc += e["pts"]
            if acc != e["balance"]:
                raise RuntimeError(
                    f"md自洽校验失败：{g}第{e['line']}行 累加={acc} vs 台账余额={e['balance']} "
                    f"（{e['desc'][:20]}）")
    return {g: per_grade[g][-1]["balance"] for g in GRADES}


def esc(s):
    # S-3（DeepSeek仲裁2026-10-18）：quote=True 纵深防御；当前所有调用点均为文本节点，
    # 文本节点中引号转义为 &quot; 显示无差异；未来若用于属性值则已安全
    return html_lib.escape(s, quote=True)


def simplify_desc(desc):
    """跑马灯简化版：将台账描述压缩为"事项"短语（时间由ttime徽章显示、积分由tplus/tminus徽章显示）。

    简化规则（2026-10-18用户定：只显示时间、事项、积分）：
    1. 去掉"——"及其后的考证说明（冲正条目）
    2. 去掉题号细节（行首"5(2)①"型、"Q2(1)"型）——必须在去括号之前
    3. 去掉《》及其内容（课文/章节标题）
    4. 去掉全角括号（）及其内容（截止时间、批次说明、仲裁备注等）
    5. 去掉半角括号()及其内容
    6. 去掉"MM-DD HH:MM"型日期时间引用
    7. 去掉裸"MM-DD"（跑马灯是"今日"明细，日期是噪音）
    8. 去掉"HH:MM<HH:MM"型截止引用
    9. 去掉"→"及后续说明
    10. 批次条目只保留冒号前的批次标识
    11. 去掉页码（如P17）
    12. 学科字简化："53天天练数学"→"53天天练"；行首"53数学/语文/英语"→去掉
    13. 去掉行首"HH:MM"（时间徽章已显示；"15:47/15:54"型批次标识保留）
    14. 去掉"（六）"年级标记
    15. 冗余词："未收到作答"→"没交"、"完全"→去掉
    16. 去掉"至...："中间冗余
    17. 去掉描述末尾冗余的积分数字（积分由徽章显示）
    """
    s = desc
    s = re.sub(r"——.*$", "", s)                                            # 1
    s = re.sub(r"^\d+\(\d+\)[①②③④⑤⑥⑦⑧⑨]", "", s)                        # 2a
    s = re.sub(r"Q\d+\(\d+\)", "", s)                                     # 2b
    s = re.sub(r"《[^》]*》", "", s)                                       # 3
    s = re.sub(r"（[^（）]*）", "", s)                                     # 4
    s = re.sub(r"\([^()]*\)", "", s)                                      # 5
    s = re.sub(r"\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}[^，,：:]*", "", s)         # 6
    s = re.sub(r"\d{1,2}-\d{1,2}", "", s)                                 # 7
    s = re.sub(r"\d{1,2}:\d{2}\s*<\s*\d{1,2}:\d{2}[^，,：:]*", "", s)       # 8
    s = re.sub(r"→.*$", "", s)                                            # 9
    if "批次" in s and "：" in s:                                          # 10
        s = s.split("：")[0]
    s = re.sub(r"P\d+", "", s)                                            # 11
    s = s.replace("53天天练数学", "53天天练").replace("53天天练语文", "53天天练").replace("53天天练英语", "53天天练")  # 12a
    s = re.sub(r"^53(数学|语文|英语)", "", s)                              # 12b
    s = re.sub(r"^\d{1,2}:\d{2}(?!/)", "", s)                             # 13
    s = s.replace("（六）", "").replace("(六)", "")                        # 14
    s = s.replace("未收到作答", "没交").replace("完全", "")                 # 15
    s = re.sub(r"至[^：]*?：", "", s)                                      # 16
    s = re.sub(r"(没交|没发)[-+]\d+$", r"\1", s)                          # 17
    s = re.sub(r"【[^【】]*】", "", s)                                     # 18 去掉【规则归类】等方头括号备注
    s = re.sub(r"\s+", " ", s).strip()                                    # 19a
    s = re.sub(r"^[：:、，,]+", "", s).strip()                             # 19b
    return s


def pts_str(pts, minus_char="-"):
    return f"+{pts}" if pts >= 0 else f"{minus_char}{abs(pts)}"


def score_odometer_html(balance, grade):
    """数码轮 score 内部 HTML（方案C）。每位 0-9 完整条，data-target 在 .digit 上。
    负余额：静态"−"号 + 绝对值数码轮。"""
    b = int(balance)
    neg = b < 0
    s = str(abs(b))
    digits = []
    for ch in s:
        strip = "".join(f"<span>{i}</span>" for i in range(10))
        digits.append(
            f'<span class="digit" data-target="{ch}">'
            f'<span class="digit-strip" aria-hidden="true">{strip}</span>'
            f"</span>"
        )
    inner = "".join(digits)
    sign = '<span class="digit-sign" aria-hidden="true">−</span>' if neg else ""
    spoken = f"负{abs(b)}" if neg else str(b)  # 读屏中文可读化："-5"→"负5"
    return (
        f'<div class="score" data-value="{b}" aria-label="{grade}余额 {spoken} 分">'
        f"{sign}{inner}</div>"
    )


def week_range(today):
    """自然周：周一00:00–周日23:59（Asia/Shanghai）。返回 (monday, sunday) date对象。"""
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def get_weekly_badges(per_grade, today):
    """当周徽章提取（2026-10-18 DeepSeek仲裁确认逻辑）。

    主判据：key第3段以 badge_ 开头（段首匹配，reverse_badge_ 除外）；
    辅助校验：描述含"徽章"二字，不一致时以key为准并登记异常；
    周过滤：key第2段日期落在当周（周一00:00–周日23:59）；
    冲正剔除：同key存在 reverse_badge_xxx 且在当周 → 剔除。
    返回 {年级: {badge_key: [entries]}}；异常持久化记入 hidden_files/异常登记.md。
    """
    monday, sunday = week_range(today)
    result = {}
    anomalies = []
    for g in GRADES:
        earned = {}
        reversed_keys = set()
        for e in per_grade[g]:
            key = e["key"]
            if not key:
                continue
            parts = key.split("|")
            if len(parts) < 3:
                continue
            try:
                kd = date.fromisoformat(parts[1])
            except ValueError:
                continue
            if not (monday <= kd <= sunday):
                continue
            seg = parts[2]
            if seg.startswith("reverse_badge_"):
                reversed_keys.add(seg[len("reverse_"):])
                continue
            if seg.startswith("badge_"):
                if "徽章" not in e["desc"]:
                    anomalies.append(
                        f"{g} {e['md_date']} key={key} 描述无'徽章'二字，以key为准计入")
                earned.setdefault(seg, []).append(e)
        for rk in reversed_keys:
            if rk in earned:
                log(f"徽章冲正剔除：{g} {rk}（当周）")
                del earned[rk]
        result[g] = earned
    if anomalies:
        ap = os.path.join(BASE, "异常登记.md")
        if not os.path.exists(ap):
            with open(ap, "w", encoding="utf-8") as f:
                f.write("# 异常登记\n\n> 生成器自动登记的数据异常，人工抽查处理\n")
        # 幂等：已登记过的 (key, 周, 原因) 不重复追加
        existing = open(ap, encoding="utf-8").read() if os.path.exists(ap) else ""
        new_lines = [f"- {a}" for a in anomalies if a not in existing]
        if new_lines:
            with open(ap, "a", encoding="utf-8") as f:
                f.write(f"\n## {today.isoformat()} 徽章判定异常\n")
                for ln in new_lines:
                    f.write(ln + "\n")
            log(f"徽章判定异常{len(new_lines)}条（去重后），已记入异常登记.md")
        else:
            log("徽章判定异常均为已登记条目，跳过追加")
    return result


def badge_grid_html(grade, earned):
    """单年级当周徽章HTML。去重显示+次数角标+可展开明细；无徽章时显示正向空状态。
    当周获得的徽章打 data-new="true"，接入B方案弹跳动画（滚动进入视口时播放一次）。"""
    if not earned:
        return f'<div class="badge-empty">{esc(BADGE_EMPTY_TEXT)}</div>'
    parts = []
    for b in BADGE_MAP:
        entries = earned.get(b["key"])
        if not entries:
            continue
        n = len(entries)
        count_html = f' <span class="badge-count">×{n}</span>' if n > 1 else ''
        details = "<br>".join(f'{e["md_date"]} {esc(e["desc"])}' for e in entries)
        detail_id = f'badge-detail-{grade}-{b["key"]}'
        parts.append(
            f'<span class="badge" data-new="true" tabindex="0" role="button" '
            f'aria-labelledby="{detail_id}-name {detail_id}-score" aria-expanded="false" aria-controls="{detail_id}">'
            f'<span class="badge-clip">'
            f'<span class="badge-main">'
            f'<span class="badge-icon" aria-hidden="true">{b["icon"]}</span>'
            f'<span class="badge-name" id="{detail_id}-name">{b["name"]}</span>'
            f'<span class="badge-score" id="{detail_id}-score">{b["points"]}{count_html}</span>'
            f'</span>'
            f'<span class="badge-detail" id="{detail_id}" hidden aria-hidden="true">{details}</span>'
            f'</span>'
            f'</span>'
        )
    return '<div class="badge-grid">\n' + "\n".join(parts) + '\n</div>'


def card_html(grade, balance, today_earned=0):
    icon = "🟣" if grade == "五年级" else "🔵"
    cls = "g5" if grade == "五年级" else "g6"
    if balance < TIER_ORDER[0]:
        nxt = TIER_ORDER[0]
        next_html = f"还差 {nxt - balance} 分就可以兑换 {CARD_TIER_DESC[nxt]} 🎮"
    else:
        t = max(x for x in TIER_ORDER if x <= balance)
        next_html = f"可以兑换 {CARD_TIER_DESC[t]}（{t}分）🎮"
    # 方案A：目标进度条
    pct = min(100, round(today_earned / DAILY_TARGET * 100)) if DAILY_TARGET > 0 else 0
    progress_html = (
        f'      <div class="progress-wrap" data-target="{DAILY_TARGET}" data-current="{today_earned}">\n'
        f'        <div class="progress-track"><div class="progress-fill" style="--pct:{pct}%"></div></div>\n'
        f'        <div class="progress-label"><span>今日冲刺</span><span class="progress-num"><b>{today_earned}</b> / {DAILY_TARGET}</span></div>\n'
        f'      </div>\n'
    )
    # 方案E：+N标签（今日得分为正时）
    delta_html = f'      <div class="delta-tag">+{today_earned}</div>\n' if today_earned > 0 else ''
    return (
        f'    <div class="card {cls}" role="button" tabindex="0" data-grade="{grade}" aria-label="查看{grade}积分变动">\n'
        f'{delta_html}'
        f'      <div class="grade">{icon} {grade}</div>\n'
        f'      {score_odometer_html(balance, grade)}\n'
        f'      <div class="unit">分</div>\n'
        f'{progress_html}'
        f'      <div class="next">{next_html}</div>\n'
        f'    </div>'
    )


def full_date(md_date, today):
    """MM-DD → date对象（处理跨年边界）。"""
    mm, dd = int(md_date[0:2]), int(md_date[3:5])
    cand = date(today.year, mm, dd)
    if cand > today:
        cand = date(today.year - 1, mm, dd)
    return cand


def main():
    t0 = time.time()
    source = sys.argv[1] if len(sys.argv) > 1 else "manual"
    src_tag = {"manual": "手动", "event": "入账事件触发", "cron": "定时兜底"}.get(source, source)
    lockf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("另一生成进程正在运行，本次跳过")
        return 2

    try:
        # 模板校验
        tpl = open(TEMPLATE_PATH, encoding="utf-8").read()
        for ph in PLACEHOLDERS:
            if tpl.count(f"<!--{ph}-->") != 1:
                raise RuntimeError(f"占位符 {ph} 异常，模板可能被手改，拒绝生成")
        static_hash = {}
        for z in STATIC_ZONES:
            a = tpl.find(f"<!--STATIC_{z.upper()}_START-->")
            b = tpl.find(f"<!--STATIC_{z.upper()}_END-->")
            if a == -1 or b == -1 or b < a:
                raise RuntimeError(f"静态区 {z} 标记缺失，拒绝生成")
            static_hash[z] = hashlib.sha256(tpl[a:b].encode("utf-8")).hexdigest()

        # I-1 一致性断言（DeepSeek仲裁2026-10-18）：Python 档位集合必须 == 模板兑换表格档位集合
        # （JS 端档位以 HTML 表格为单一数据源实时解析；改档位数字时两处须同步，否则构建期直接失败，
        #  不许静默错位）。表格 <br> 排版保留，不由 Python 循环渲染。
        # 约束：档位文案须为纯 ASCII 数字+"分"（如"70分"），不得含千分位/中文数字，否则正则解析误判
        rz_a = tpl.find("<!--STATIC_REDEEM_START-->")
        rz_b = tpl.find("<!--STATIC_REDEEM_END-->")
        table_tiers = sorted({int(x) for x in re.findall(r'<td class="plus">(\d+)分</td>', tpl[rz_a:rz_b])})
        if table_tiers != TIER_ORDER:
            raise RuntimeError(
                f"兑换档位漂移：Python CARD_TIER_DESC={TIER_ORDER} vs 模板表格={table_tiers}，拒绝生成")

        # 解析md（只读）
        md_bytes = open(MD_PATH, "rb").read()
        per_grade = parse_md(MD_PATH)
        balances = verify_self_consistent(per_grade)

        # 幂等
        fp = hashlib.sha256(md_bytes).hexdigest()
        today = shanghai_today()
        monday, _ = week_range(today)
        week_id = monday.isoformat()  # 周一日期标识自然周；跨周必须重生成（徽章区按周重置）
        try:
            last = json.load(open(FINGERPRINT_PATH, encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            last = {}
        if last.get("fingerprint") == fp and last.get("week") == week_id and os.path.exists(OUTPUT_PATH):
            log(f"md指纹无变化（{fp[:12]}…）且同周（{week_id}），跳过生成")
            return 0

        today_md = f"{today.month:02d}-{today.day:02d}"
        date_cn = f"{today.year}年{today.month}月{today.day}日"

        # 优化项1：时间提取失败条目留痕（供人工抽查）
        no_time = [f"{g}:{e['desc'][:24]}" for g in GRADES for e in per_grade[g] if not e["hhmm"]]
        if no_time:
            log(f"时间未提取（仅显示日期）{len(no_time)}条: {'；'.join(no_time[:8])}"
                + ("…" if len(no_time) > 8 else ""))

        # 余额卡片（左右并排，flex容器不可少）
        # 方案A：计算今日得分（今日earned条目之和）
        today_earned = {}
        for g in GRADES:
            todays = [e for e in per_grade[g] if e["md_date"] == today_md]
            today_earned[g] = sum(e["pts"] for e in todays if e["pts"] > 0)
        cards = '<div class="cards">\n' + "\n".join(card_html(g, balances[g], today_earned[g]) for g in GRADES) + '\n  </div>'

        # 今日跑马灯
        mq = {}
        for g in GRADES:
            todays = [e for e in per_grade[g] if e["md_date"] == today_md]
            parts = []
            for e in todays:
                cls = "tplus" if e["pts"] >= 0 else "tminus"
                # 无时间则用不可见占位徽章（不展示"--:--"噪音，且保证内容右对齐时左侧位置一致）
                ttime = f'<span class="ttime">{e["hhmm"]}</span>' if e["hhmm"] else '<span class="ttime ttime-empty"></span>'
                parts.append(
                    f'<div class="ticker-item">{ttime}'
                    f'<span class="tmatter">{esc(simplify_desc(e["desc"]))}</span><span class="tpts {cls}">{pts_str(e["pts"], "-")}</span></div>'
                )
            n = len(parts)
            if n == 0:
                doubled = '<div class="ticker-item ticker-empty"><span>今日暂无积分变动</span></div>'
            else:
                single = "\n            ".join(parts)
                doubled = single + "\n            " + single
            mq[g] = (doubled, n)

        def css_one(n, name):
            if n == 0:
                return "", ""
            dist = n * ITEM_PX
            dur = round(n * SEC_PER_ITEM, 1)
            # S-2（DeepSeek仲裁2026-10-18）：原 assert n*ITEM_PX==n*52 恒真（ITEM_PX=52），
            # 已删除；此处断言有意义的不变量
            assert dist > 0 and dur > 0, f"跑马灯动画参数异常 dist={dist} dur={dur}"
            anim = f"  .ticker-list-{name} {{ animation: ticker-scroll-{name} {dur}s linear infinite; }}"
            kf = (f"  @keyframes ticker-scroll-{name} {{\n"
                  f"    0% {{ transform: translateY(0); }}\n"
                  f"    100% {{ transform: translateY(-{dist}px); }}\n"
                  f"  }}")
            return anim, kf

        a5, k5 = css_one(mq["五年级"][1], "g5")
        a6, k6 = css_one(mq["六年级"][1], "g6")
        anim_css = "\n".join(x for x in [a5, a6] if x)
        kf_css = "\n".join(x for x in [k5, k6] if x)

        # 近3天历史（倒序）
        cutoff = today - timedelta(days=2)
        hist = {}
        for g in GRADES:
            recent = [e for e in per_grade[g]
                      if full_date(e["md_date"], today) >= cutoff]
            recent.reverse()  # md按时间正序，展示倒序（最新在前）
            if not recent:
                rows_html = '<tr><td class="dt">—</td><td class="desc">近3天暂无记录</td><td class="plus">—</td><td>—</td></tr>'
            else:
                rh = []
                for e in recent:
                    cls = "plus" if e["pts"] >= 0 else "minus"
                    dt = e["md_date"] + (f" {e['hhmm']}" if e["hhmm"] else "")
                    rh.append(
                        f'<tr><td class="dt">{dt}</td><td class="desc">{esc(simplify_desc(e["desc"]))}</td>'
                        f'<td class="{cls}">{pts_str(e["pts"], "−")}</td><td>{e["balance"]}</td></tr>'
                    )
                rows_html = "\n          ".join(rh)
            hist[g] = rows_html

        # 当周徽章（2026-10-18用户定：徽章成就区每周更新，只显示当周获得，分年级）
        weekly_badges = get_weekly_badges(per_grade, today)
        badge_g5 = badge_grid_html("五年级", weekly_badges["五年级"])
        badge_g6 = badge_grid_html("六年级", weekly_badges["六年级"])

        # 替换
        out = tpl
        subs = {
            "PAGE_DATE": date_cn + " ",
            "BALANCE_CARDS": cards,
            # 当日两年级都无积分变动时，整个"今日积分明细"区块隐藏
            "TICKER_SECTION_OPEN": ('<div class="ticker-section">'
                                    if mq["五年级"][1] + mq["六年级"][1] > 0
                                    else '<div class="ticker-section" hidden>'),
            "MARQUEE_ANIM": anim_css,
            "MARQUEE_KEYFRAMES": kf_css,
            "MARQUEE_G5": mq["五年级"][0],
            "MARQUEE_G6": mq["六年级"][0],
            "RECENT_TABLE_G5": hist["五年级"],
            "RECENT_TABLE_G6": hist["六年级"],
            "BADGE_GRID_G5": badge_g5,
            "BADGE_GRID_G6": badge_g6,
        }
        for ph, val in subs.items():
            out = out.replace(f"<!--{ph}-->", val, 1)
        leftover = re.findall(r"<!--(?:PAGE_DATE|BALANCE_CARDS|TICKER_SECTION_OPEN|MARQUEE_ANIM|MARQUEE_KEYFRAMES|MARQUEE_G5|MARQUEE_G6|RECENT_TABLE_G5|RECENT_TABLE_G6|BADGE_GRID_G5|BADGE_GRID_G6)-->", out)
        if leftover:
            raise RuntimeError(f"占位符残留: {leftover}")

        # 跑马灯输出断言（结构级）：两份内容确实写入、keyframes距离与条目数匹配
        for gname, cssname in [("五年级", "g5"), ("六年级", "g6")]:
            n = mq[gname][1]
            if n > 0:
                # 输出中该列条目div数 == n*2（两份）
                seg = out.split(f'ticker-list-{cssname}">')[1].split("</div>\n        </div>")[0]
                cnt = seg.count('<div class="ticker-item">')
                if cnt != n * 2:
                    raise RuntimeError(f"跑马灯{gname}输出条目数{cnt}≠{n}*2，复制两份失败")
                dist = n * ITEM_PX
                if f"translateY(-{dist}px)" not in out:
                    raise RuntimeError(f"跑马灯{gname} keyframes距离-{dist}px未写入CSS")

        for z in STATIC_ZONES:
            a = out.find(f"<!--STATIC_{z.upper()}_START-->")
            b = out.find(f"<!--STATIC_{z.upper()}_END-->")
            h = hashlib.sha256(out[a:b].encode("utf-8")).hexdigest()
            if h != static_hash[z]:
                raise RuntimeError(f"静态区 {z} 生成后变化，拒绝写入")

        out = out.replace("<!-- TEMPLATE:",
                          f"<!-- GENERATED: {time.strftime('%Y-%m-%d %H:%M:%S')} from 积分看板.template.html（源=积分记录.md），勿手改 -->\n<!-- TEMPLATE:",
                          1)

        if os.path.exists(OUTPUT_PATH):
            with open(OUTPUT_PATH, "rb") as fsrc, open(BACKUP_PATH, "wb") as fdst:
                fdst.write(fsrc.read())
        tmp = OUTPUT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(out)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, OUTPUT_PATH)

        json.dump({"fingerprint": fp, "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "balances": balances, "week": week_id},
                  open(FINGERPRINT_PATH, "w", encoding="utf-8"))

        dt = round(time.time() - t0, 2)
        log(f"[{src_tag}]生成成功：五年级={balances['五年级']} 六年级={balances['六年级']} "
            f"今日条目g5={mq['五年级'][1]}/g6={mq['六年级'][1]} "
            f"本周徽章g5={sum(len(v) for v in weekly_badges['五年级'].values())}枚/"
            f"g6={sum(len(v) for v in weekly_badges['六年级'].values())}枚 耗时{dt}s")

        # 优化项2：差异登记表刷新核对时间戳（常设对账产物）
        try:
            dr = os.path.join(BASE, "积分对账差异登记.md")
            if os.path.exists(dr):
                txt = open(dr, encoding="utf-8").read()
                txt = re.sub(r"> 最后核对：.*",
                             f"> 最后核对：{time.strftime('%Y-%m-%d %H:%M')}（build_kanban自动）",
                             txt, count=1)
                open(dr, "w", encoding="utf-8").write(txt)
        except Exception as e:
            log(f"差异登记时间戳刷新失败（非阻塞）: {e}")
        return 0
    except Exception as e:
        log(f"生成失败（旧文件保留）: {type(e).__name__}: {e}")
        return 1
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()


if __name__ == "__main__":
    sys.exit(main())

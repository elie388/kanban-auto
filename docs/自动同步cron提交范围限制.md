# 自动同步cron提交范围限制

制定：2026-10-20（DeepSeek A类终审 Q2 裁决）
生效：即刻，不设试运行
修订：须经 DeepSeek 终审

## 背景

2026-10-20 发生两笔混合提交（`6d3a3cf`、`b7f0930`，见《程序异常台账》异常 #1/#2）：A类修复代码因未及时提交，被后续功能提交时的宽泛 `git add` 卷入，提交信息与内容不符。为杜绝此类程序异常，特立本条文。

## 条文

### 1. 禁止宽泛 add

在 `kanban-deploy` 仓库内，任何场景（cron 脚本、子代理手动操作、人工操作）均**禁止**使用：

- `git add -A`
- `git add .`
- `git commit -a` / `git commit --all`
- 任何等价的全量暂存操作

### 2. 自动同步脚本 add 白名单

自动同步 cron 调用的脚本（`sync_sections.py`、`build_stats.py`、`sync_balance.py`）在提交时，`git add` 仅允许以下目录：

- `data/`（生成的 JSON 数据文件）
- `logs/`（运行日志）

**禁止** add 以下内容：

- `scripts/`（代码）
- `templates/`（模板代码）
- 配置文件（`CNAME`、`*.json` 配置等）
- 任何代码/配置类文件

### 3. 提交前代码区脏检查（fail-safe）

自动同步脚本在执行 `git add` 之前，必须先检查：

```
git status --porcelain -- scripts/
```

- 若输出非空（`scripts/` 有未提交改动）→ **本次不提交**，写告警日志，静默退出（exit 0，不触发失败报告阈值）
- 告警日志位置：`logs/cron_commit_guard.log`，格式：`时间 ISO8601 | 触发脚本 | 脏文件列表（前10个）`
- 数据文件已写入本地，下次 cron 或人工可补推，不丢失数据

> 设计说明：fail-safe 而非 fail-loud。`scripts/` 脏说明有其他任务正在进行中，
> 此时提交数据文件虽技术上可行，但工作区处于未知状态，跳过本次提交是最安全的选择。
> 告警日志供事后审计，不打扰用户。
>
> exit code 选择说明（2026-10-20 Gemini 初审问询）：
> guard 触发时返回 exit 0（而非新增非零码），理由：
> 1. 数据文件已写入本地，无数据丢失，下次 cron 自动重试；
> 2. 这不是同步失败，不应触发 cron 的失败报告阈值（避免用户被"连续N次失败"误报打扰）；
> 3. 新增 exit code 需同步修改两个 cron 定义的报告规则，属范围蔓延；
> 4. 告警日志 `logs/cron_commit_guard.log` 为审计依据， weekly review 时检查。

### 4. 例外

- 人工/子代理的功能提交不受第 3 条约束，但受第 1 条约束（必须用白名单式 `git add <具体路径>`，禁止宽泛 add）
- 紧急止血场景需宽泛 add 时，须经 DeepSeek 终审特批，并在《程序异常台账》登记

## 落实情况

- [x] `scripts/sync_sections.py`：已加 `scripts/` 脏检查（2026-10-20）
- [x] `scripts/build_stats.py`：已加 `scripts/` 脏检查（2026-10-20）
- [ ] `scripts/sync_balance.py`：已退役（2026-10-19 R2），余额通道由 build_stats.py 接管；其本次 M4 修复提交为最后一次允许混合的窗口，不适用本条文追溯

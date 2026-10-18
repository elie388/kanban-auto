# 积分看板自动更新（GitHub Actions → GitHub Pages）

私有仓库，数据不公开。按用户时间表定时从 Google Drive 拉取积分台账，
重新生成看板 HTML，部署到 GitHub Pages。

2026-10-18 迁移：原"FTP 上传到香港空间"通道因空间架构性不支持
SFTP/FTPS（仅明文 FTP，安全禁用）而停用，改为 GitHub Pages
（用户批准，DeepSeek 安全评审通过）。

## 时间表（北京时间）

| 时段 | 时间 | 频率 |
|------|------|------|
| 上学工作日 | 19:00–23:00 | 每15分钟 |
| 上学周末 | 09:00–23:00 | 每15分钟 |
| 假期 | 09:00–23:00 | 每15分钟 |

寒暑假时间用户另行通知后调整 cron。

## 首次搭建步骤

### 1. 创建私有仓库

```bash
gh repo create kanban-auto --private --clone
cd kanban-auto
# 把本目录所有文件复制进来
```

### 2. 同步生成器与模板

从主工作区复制（每次生成器/模板有改动时同步）：

```bash
cp ~/workspace/goals/goal/hidden_files/build_kanban.py scripts/
cp ~/workspace/goals/goal/hidden_files/积分看板.template.html scripts/
```

### 3. 创建 Google Service Account

1. Google Cloud Console → 新建项目（或复用）→ 启用 Google Drive API
2. IAM → Service Accounts → 新建 → 下载 JSON 密钥
3. 把 `积分记录.md`（Drive 文件 ID 见 Secrets）在 Drive 中共享给 SA 邮箱（查看者权限）

### 4. 设置 GitHub Secrets

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret | 说明 |
|--------|------|
| `GDRIVE_SA_JSON` | Service Account JSON 全文 |
| `GDRIVE_FILE_ID` | Drive 文件 ID（积分记录.md） |

> 2026-10-18 起不再使用 FTP 相关 Secrets（`FTP_HOST`/`FTP_USER`/`FTP_PASS`
> 已可删除）。部署改走 GitHub Pages，需在仓库 Settings → Pages 中
> 将 Source 设为 "GitHub Actions"。

### 5. 验证

Actions 页面 → 手动触发 `workflow_dispatch` → 检查运行日志。

## 文件说明

- `.github/workflows/kanban-update.yml` — 定时 workflow
- `scripts/fetch_ledger.py` — Drive 拉取（SA 只读）
- `scripts/build_wrapper.py` — 调用原版生成器
- `scripts/build_kanban.py` — 原版生成器（从主工作区同步，勿手改）
- `scripts/积分看板.template.html` — 模板（从主工作区同步，勿手改）
- `scripts/deploy_ftp.py` — 已删除（2026-10-18，FTP 通道停用）
- `index.html` — 生成产物（gitignore，不提交）
- `public/` — Pages 发布目录（构建时生成，gitignore，不提交）

## 安全

- 所有凭证只存 GitHub Secrets，不进代码、不进日志
- SA 只有 `drive.readonly` scope，且仅被共享了单个文件
- 构建产物经完整性校验（体积/闭合/标题标记）后才发布，防残缺页面上线
- 失败时不上传调试产物（防 PII 外泄）；调试看 Actions 日志（私有仓库）

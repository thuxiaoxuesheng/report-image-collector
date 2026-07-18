# 报告图片采集与审核系统

一个自托管的多用户 Web 工具：用户使用自己的平台账号完成授权登录，按关键词和日期范围采集图文笔记，快速排除无关图片，并将保留图片按检索词导出为 ZIP 和 CSV。系统还提供可选的视觉模型筛选，但最终选择始终可以由用户人工修改。

> 本项目只使用正常网页与用户自行完成的登录，不读取短信验证码，不处理验证码，不绕过访问限制、风控或页面权限。部署者和使用者应确认其采集、保存、导出和使用数据的权限，并负责图片中的个人信息处理。

## 核心能力

- 管理员在网页创建、停用、重置用户，不开放公开注册。
- 一名用户一个独立工作空间；任务、图片、历史去重、浏览器目录和模型配置互不共享。
- 用户在受限的远程浏览器中自行扫码、输入手机号和验证码并处理登录确认。
- 一个任务最多配置 10 个关键词，每个关键词分别设置目标笔记数；默认 50，不设置产品级总量和每日数量上限。
- 搜索固定选择“最新”，支持最长 180 天的明确日期范围，仅保存图文笔记。
- 同一用户历史去重；同一笔记被多个关键词发现时归入首次命中的关键词。
- 审核页按检索词和笔记分组，支持单击排除/恢复、批量选择、反选、大图预览和筛选。
- AI 筛选是可选且与人工操作同级的操作：它给出“报告/非报告”、置信度和简短理由，用户之后仍可复核、修改或撤销。
- 支持 MiniMax Token Plan 和 OpenAI Chat Completions 兼容的视觉模型。
- 导出 ZIP 按检索词创建文件夹，并生成 `manifest.csv`。
- 待审核图片默认 24 小时到期、最长 48 小时强制清理；导出包默认保留 2 小时。浏览器登录目录遵循独立的长期保存规则。

## 技术实现

```text
浏览器
  └─ HTTPS 反向代理（Caddy / IIS / Nginx 均可）
       └─ FastAPI + 静态前端（127.0.0.1:8765）
            ├─ SQLAlchemy ─ SQLite / PostgreSQL
            ├─ 采集队列 ─ Playwright + 每用户持久化 Chrome 目录
            ├─ 详情适配 ─ social-media-copilot 本机桥接（127.0.0.1:3000）
            ├─ AI 队列 ─ MiniMax SDK / OpenAI-compatible HTTPS API
            └─ 文件层 ─ 候选图片、缩略图、CSV、ZIP、定时清理
```

后端使用 FastAPI、Pydantic、SQLAlchemy 和 Playwright；前端是由 FastAPI 直接提供的原生 HTML/CSS/JavaScript，无需单独部署前端服务。SQLite 以 WAL 模式运行，适合单机；PostgreSQL 支持数据库行锁和 `SKIP LOCKED`，用于多执行器扩展。

队列通过带心跳的数据库租约恢复异常中断任务。不同用户可以并行采集，同一用户的采集和登录浏览器由用户级浏览器租约严格串行，避免同一个持久化目录被两个 Chrome 实例同时使用。采集与 AI 各自拥有独立执行池。

每位用户的模型 API Key 使用 AES-GCM 加密后写入数据库，完整密钥不会返回前端。网站会话使用随机不透明令牌，数据库仅保存令牌哈希；写操作还检查 Origin、Fetch Metadata 和登录 CSRF 令牌。登录失败有窗口限速和锁定机制。

采集器使用可见 Chromium、固定的用户目录和随机操作间隔。遇到登录失效、验证码、访问限制、搜索异常或页面结构变化时，任务进入“需要处理”状态并保留已获得结果，不自动绕过限制。

## 目录结构

```text
backend/app/                    FastAPI、认证、队列、采集、审核与导出
backend/app/static/             Web 前端
backend/tests/                  后端测试
deploy/windows-server/          Windows Server 安装、启动和打包脚本
docs/MIGRATION.md               公司服务器迁移手册
docs/USER_GUIDE.md              网站使用手册
scripts/                        本机 API 与模型适配脚本
third_party/social-media-copilot/ 固定版本的开源详情适配组件
```

运行后生成但不会提交到 Git 的内容包括：`.env`、`data/`、`logs/`、`.playwright-browsers/`、`.deploy-cache/`、`dist/` 和各类依赖/构建缓存。

## 本地开发

需要 Windows 10/11、Python 3.11+、Node.js 20+ 和 Google Chrome。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
.\start.ps1
```

打开 <http://127.0.0.1:8765>。`.env.example` 的 `DEV_AUTH_BYPASS=true` 只允许本机开发使用；接入域名或公网前必须设为 `false` 并启用站内认证。

运行检查：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check backend
```

## Windows Server 部署

推荐使用带桌面环境的 Windows Server。安装脚本会安装/配置 Python、Node.js、Chrome、Playwright、第三方适配组件和 Caddy，并生成认证密钥与文件权限。

交互安装：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows-server\install-server.ps1
```

也可以显式传入公司的域名与内部管理员邮箱：

```powershell
.\deploy\windows-server\install-server.ps1 `
  -Domain "collector.example.com" `
  -InternalEmail "admin@example.com"
```

后端和详情适配服务只应监听回环地址。公网只开放 80/443，远程管理端口限制为公司出口 IP。采集使用有界面 Chrome，因此服务器重启后需要管理员登录 Windows 一次；可以断开远程桌面，但不要注销该会话。

完整迁移与验收步骤见 [迁移注意事项](docs/MIGRATION.md)，最终用户操作见 [网站使用文档](docs/USER_GUIDE.md)。

## 生产配置要点

复制 `.env.example` 为 `.env`，至少正确配置：

- `ENVIRONMENT=production`
- `DEV_AUTH_BYPASS=false`
- `SITE_AUTH_ENABLED=true`
- `SITE_AUTH_SESSION_SECRET`：独立随机值
- `SECRETS_MASTER_KEY`：独立随机值，部署后不得随意更换
- `DATABASE_URL`：留空时使用本机 SQLite，扩展部署建议 PostgreSQL
- `BROWSER_HEADLESS=false`

不要提交 `.env`、数据库、浏览器目录、导出包或部署私钥。尤其要妥善备份 `SECRETS_MASTER_KEY`：丢失或更换后，数据库中已有的模型 API Key 将无法解密。

## 数据保留与边界

运行数据默认位于 `data/`：

- `app.db`：用户、任务、图片元数据、选择状态、去重记录、审计记录和加密模型配置。
- `users/<user-id>/browser-profile/`：用户独占的长期登录状态。
- `users/<user-id>/tasks/`：候选图片与缩略图。
- `exports/`：短期 ZIP 导出包。

建议在服务器磁盘启用 BitLocker，并使用 NTFS ACL 将 `.env`、`data/` 和 `logs/` 限制给运行账号、Administrators 与 SYSTEM。第一版不做 OCR、自动脱敏或医学结论判断；模型只判断图片是否像检查检验报告单。

## 开源与第三方

项目自身采用 [AGPL-3.0-only](LICENSE)。第三方来源、固定版本及许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。第三方商标与平台名称归各自权利人所有。

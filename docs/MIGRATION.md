# 公司服务器迁移注意事项

本文面向负责迁移和运维的 IT 人员。目标是把应用、用户数据、浏览器登录状态和密钥完整迁移到新的 Windows 服务器，并保留可回滚能力。

## 1. 迁移前必须明确的边界

- 推荐目标系统：带桌面体验的 Windows Server 2022/2025，64 位。
- 采集浏览器必须运行在已登录的交互式 Windows 会话中。远程桌面可以断开，但不能注销。
- 新旧服务器不得同时使用同一份用户浏览器目录运行采集，否则 Chrome 配置可能损坏，账号也会出现异常会话。
- 应用端口 `8765` 和本机适配端口 `3000` 只监听 `127.0.0.1`，不得直接开放公网。
- Caddy 只是随附的默认反向代理。公司可以改用 IIS、Nginx 或现有网关，只要保留 HTTPS、原始 Host 和正常 Cookie 行为。

建议最低资源为 4 核 CPU、8 GB 内存和足够的 SSD 空间。并发用户较多时，Chrome 通常是主要内存消耗，应按每个活跃浏览器约 750 MB 预留并通过实际压测校准。

## 2. 需要迁移与不能提交到 Git 的内容

GitHub 仓库只包含源码。以下内容必须通过公司的加密文件传输或备份系统单独迁移：

| 内容 | 是否必须 | 说明 |
|---|---:|---|
| `.env` | 必须 | 包含会话密钥、主加密密钥和部署配置 |
| `data/app.db` | 使用 SQLite 时必须 | 用户、任务、模型配置、去重与审计数据 |
| `data/users/` | 希望保留登录与图片时必须 | 每用户浏览器目录、候选图片与任务文件 |
| `data/exports/` | 可选 | 仅为短期导出包，通常无需迁移 |
| `.playwright-browsers/` | 可选 | 可在新机重新安装，迁移可减少下载时间 |
| `logs/` | 可选 | 仅在需要审计或排障时迁移 |

最关键的是 `.env` 中原有的 `SECRETS_MASTER_KEY`。它是数据库中用户模型 API Key 的解密根密钥。迁移时必须原样保留，不得重新生成、不得添加空格或换行。若已经丢失，只能让每位用户重新填写模型 API Key。

任何已经通过聊天、邮件或临时文本明文传递过的 API Key、服务器密码和部署密钥，都应在迁移完成后轮换。不要把它们写入 README、工单截图或 Git 提交。

## 3. 推荐迁移流程

### 3.1 在旧服务器冻结写入

1. 通知用户停止新建任务和审核操作。
2. 等待正在运行的采集或 AI 任务结束，或由管理员安全暂停。
3. 停止 `XHS Collector` 计划任务/进程。
4. 确认没有 `python`、`uvicorn`、`chrome` 正在占用项目中的浏览器目录和 SQLite 数据库。
5. 备份 `.env` 与整个 `data/`，并为备份生成 SHA-256 清单。

不要在应用仍写入 SQLite 或 Chrome 仍打开用户目录时直接复制文件。

### 3.2 准备新服务器

1. 安装系统补丁，创建专用的本地或域运行账号，并启用磁盘加密。
2. 将公司出口 IP 加入 RDP/SSH 白名单；公网只开放 TCP 80、443。
3. 使用 Git 克隆私有仓库到固定目录，例如 `C:\ReportCollector`。
4. 以管理员 PowerShell 运行安装脚本，并填写正式域名：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
Set-Location C:\ReportCollector
.\deploy\windows-server\install-server.ps1 `
  -Domain "collector.example.com" `
  -InternalEmail "admin@example.com"
```

安装脚本首次执行会创建新的 `.env`。在恢复旧数据前停止刚创建的计划任务，然后用旧服务器的 `.env` 覆盖它；域名由反向代理配置管理，不需要为了换域名重新生成业务密钥。

### 3.3 恢复数据

1. 确认新服务器上的应用和 Chrome 全部停止。
2. 将旧服务器 `.env` 和 `data/` 恢复到新项目根目录。
3. 确认 `DATA_DIR` 与实际恢复目录一致。
4. 为 `.env`、`data/`、`logs/` 设置严格 NTFS ACL，只允许运行账号、Administrators 和 SYSTEM 完全控制。
5. 若运行账号改变，必须让新账号取得每个 `browser-profile` 的所有权和读写权限。
6. 启动应用，先检查 `/api/health`，再登录网站验证用户和历史任务。

示例 ACL 原则如下；请由 IT 根据公司的域账号替换 `<RUN_ACCOUNT>`：

```powershell
icacls .env /inheritance:r /grant:r "<RUN_ACCOUNT>:F" "Administrators:F" "SYSTEM:F"
icacls data /inheritance:r /grant:r "<RUN_ACCOUNT>:(OI)(CI)F" "Administrators:(OI)(CI)F" "SYSTEM:(OI)(CI)F" /T
```

### 3.4 数据库选择

- 单机和少量并发：先原样保留 SQLite，迁移风险最低。
- 多服务器或较高并发：使用 PostgreSQL，并将 `DATABASE_URL` 配置为公司数据库 DSN。

代码支持 PostgreSQL 运行，但仓库没有把既有 SQLite 数据自动搬到 PostgreSQL 的一键工具。首次迁移不要同时更换服务器和数据库。建议先在新服务器用 SQLite 完成验收，再单独安排数据库迁移、记录数核对和回滚演练。

## 4. 域名、HTTPS 与反向代理

1. 为正式子域名添加指向新服务器公网 IP 的 A/AAAA 记录。
2. 切换前将 DNS TTL 降低；验收完成后再恢复常规 TTL。
3. 证书必须覆盖正式域名并自动续期。
4. 代理目标固定为 `http://127.0.0.1:8765`。
5. 不要篡改 `Host`；若经过多层代理，应确保浏览器看到的 Origin 与公开 Host 一致，否则跨站保护会拒绝写操作。
6. 不要缓存登录页、API、图片和导出响应。

若使用 Caddy，安装脚本会为指定域名生成站点配置。若使用公司网关，应保留安全 Cookie、WebSocket/长请求兼容和足够的上传/响应超时。

## 5. 配置核对

生产环境至少应满足：

```dotenv
ENVIRONMENT=production
DEV_AUTH_BYPASS=false
SITE_AUTH_ENABLED=true
BROWSER_HEADLESS=false
SOCIAL_COPILOT_URL=http://127.0.0.1:3000
```

以下值必须是非空、独立随机值：

- `SITE_AUTH_SESSION_SECRET`
- `SECRETS_MASTER_KEY`

数量配置为 `0` 代表不设置产品级上限：

- `DAILY_NEW_NOTE_LIMIT=0`
- `MAX_SCAN_PER_KEYWORD=0`
- `MAX_SCROLL_ROUNDS_PER_KEYWORD=0`

这不会取消低频间隔，也不会绕过页面结果耗尽、验证码或访问限制。任务时间范围仍最多 180 天，每个任务最多 10 个关键词。

## 6. 验收清单

迁移交付前至少完成以下检查：

- [ ] Git 仓库不含 `.env`、数据库、浏览器目录、私钥、真实 API Key、导出包或日志。
- [ ] HTTPS 证书有效，HTTP 自动跳转 HTTPS。
- [ ] 公网无法访问 8765、3000，远程管理端口仅公司白名单可达。
- [ ] 管理员可以登录、创建/停用/重置用户。
- [ ] 普通用户看不到其他用户的任务、图片、模型配置和浏览器状态。
- [ ] 旧用户的模型配置能通过“测试连接”。
- [ ] 至少一个旧用户的浏览器登录状态可用；不可用时能正常重新登录。
- [ ] 创建一个小范围测试任务，能经历排队、采集、审核和导出完整流程。
- [ ] AI 可选筛选后可以人工恢复或修改。
- [ ] ZIP 中按检索词分文件夹，`manifest.csv` 可正常打开且内容匹配。
- [ ] 定时清理、日志轮转、磁盘监控和数据库备份已启用。
- [ ] 服务器重启后，运行账号登录一次即可启动；断开 RDP 后任务继续运行。

## 7. DNS 切换与回滚

1. 在本机 hosts 或内部测试域名上完成验收。
2. 冻结旧站写入，做最后一次备份和差异复制。
3. 将正式 DNS 指向新服务器。
4. 在一个完整采集和导出流程完成前，旧服务器保持停止但不要删除。
5. 若需回滚，先停止新服务器，恢复旧 DNS，再启动旧服务器；禁止两边同时使用同一浏览器状态。

建议保留迁移前完整备份至少 7 天，并定期演练 `.env + 数据库 + data/users` 的恢复。

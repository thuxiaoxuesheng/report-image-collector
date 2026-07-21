# 小红书图片采集器 IT 部署指导

> 适用对象：负责 Windows Server、域名、网络、安全和备份的 IT 管理员
> 部署方式：单机 Windows Server + Caddy HTTPS + FastAPI + SQLite
> 重要边界：采集浏览器必须运行在已登录的 Windows 桌面会话中。可以断开 RDP，但不能注销该 Windows 用户。

## 1. 部署前结论

首次正式部署请使用全新的发布目录，并直接运行服务器安装脚本。

**不要先运行根目录的 `setup.ps1`。** `setup.ps1` 用于本机开发，会生成开发版 `.env`。如果安装目录已存在该文件，服务器安装脚本会将其视为升级环境，不再生成网站登录账号和密码，可能导致安装完成后无人能够登录。

推荐部署目录：

```text
C:\Apps\XhsCollector
```

部署完成后不要移动或重命名该目录，因为计划任务、虚拟环境和数据路径都以此目录为基础。

## 2. 服务器与网络准备

### 2.1 建议配置

- Windows Server 2022/2025，安装“桌面体验”。
- 建议起步配置：8 vCPU、16 GB 内存、100 GB 以上 SSD。
- 一个专用 Windows 运行账号；安装时用该账号登录，并以管理员权限执行安装脚本。
- 为数据盘启用 BitLocker。
- 准备正式域名，例如 `collector.example.com`。
- 为正式域名添加指向服务器公网 IP 的 A/AAAA 记录。

### 2.2 防火墙

外部只开放：

- TCP 80：Caddy 自动申请证书和跳转 HTTPS。
- TCP 443：正式网站访问。
- RDP/远程管理端口：只允许公司固定出口 IP 或 VPN 网段。

以下端口必须只监听回环地址，不得向公网开放：

- `127.0.0.1:8765`：FastAPI 后端。
- `127.0.0.1:3000`：浏览器详情桥接服务。

服务器需要通过 TCP 443 访问 Python、Node.js、Chrome、Caddy、PyPI、npm、小红书及所配置的视觉模型服务。应用的模型请求不使用系统代理，如公司网络强制代理，应提前验证服务器能够直接访问模型 HTTPS 地址。

## 3. 发布包校验与解压

将发布 ZIP 上传到服务器后，先与交付方提供的 SHA256 比对：

```powershell
Get-FileHash .\xhs-collector-release.zip -Algorithm SHA256
```

校验一致后解压到：

```text
C:\Apps\XhsCollector
```

进入部署目录：

```powershell
Set-Location C:\Apps\XhsCollector
```

确认首次部署目录中没有 `.env`：

```powershell
Test-Path .\.env
```

首次部署应返回 `False`。如果返回 `True`，请停止安装并确认该目录是否曾用于开发或生产：

- 全新部署：重新解压到一个干净目录，不要直接删除来源不明的 `.env`。
- 生产升级：必须保留原 `.env`，先按本文“升级部署”章节检查和备份。

## 4. 执行首次安装

使用准备好的 Windows 运行账号登录服务器，右键 PowerShell 选择“以管理员身份运行”，执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
Set-Location C:\Apps\XhsCollector
.\deploy\windows-server\install-server.ps1 `
  -Domain "collector.example.com" `
  -InternalEmail "admin@example.com"
```

也可以双击：

```text
deploy\windows-server\安装服务器.cmd
```

安装脚本将自动完成：

1. 下载并验证 Chrome、Node.js 和 Python 安装包签名。
2. 安装 Python 依赖和 Playwright Chromium。
3. 安装 Node.js 依赖并构建浏览器扩展。
4. 生成生产 `.env`、网站管理员账号、密码哈希、会话密钥和模型密钥加密主密钥。
5. 收紧 `.env`、`data` 和 `logs` 的 NTFS 权限。
6. 安装并配置 Caddy HTTPS 服务。
7. 注册当前 Windows 用户登录时自动启动的 `XHS Collector` 计划任务。
8. 运行后端测试和代码检查。

安装过程中设置的网站密码至少 12 位。安装完成后将管理员账号保存到公司密码管理器，不要通过个人聊天工具传递。

## 5. 安装后配置核对

不要在终端或工单中打印 `.env` 的真实密钥。可以用下面的命令仅检查关键项是否非空：

```powershell
$requiredNames = @(
  "SITE_AUTH_USERNAME",
  "SITE_AUTH_PASSWORD_HASH",
  "SITE_AUTH_SESSION_SECRET",
  "SECRETS_MASTER_KEY"
)
$environmentLines = Get-Content .\.env
foreach ($settingName in $requiredNames) {
  $exists = $environmentLines | Where-Object {
    $_ -match ("^" + [Regex]::Escape($settingName) + "=.+$")
  } | Select-Object -First 1
  "{0}: {1}" -f $settingName, $(if ($exists) { "OK" } else { "MISSING" })
}
```

四项都必须显示 `OK`。同时人工确认 `.env` 包含：

```dotenv
ENVIRONMENT=production
DEV_AUTH_BYPASS=false
SITE_AUTH_ENABLED=true
BROWSER_HEADLESS=false
DATABASE_URL=sqlite:///./data/app.db
```

特别注意：

- `DEV_AUTH_BYPASS` 在正式环境必须为 `false`。
- `SECRETS_MASTER_KEY` 部署后不得随意修改；丢失或轮换会导致数据库中已有模型 API Key 无法解密。
- 不要把 `.env` 提交到 Git、复制到普通共享盘或附加到工单。

## 6. 服务状态检查

检查 Caddy：

```powershell
Get-Service Caddy
```

检查采集器计划任务：

```powershell
Get-ScheduledTask -TaskName "XHS Collector"
Get-ScheduledTaskInfo -TaskName "XHS Collector"
```

检查监听端口：

```powershell
Get-NetTCPConnection -State Listen |
  Where-Object LocalPort -In 80,443,8765,3000 |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

预期结果：

- 80/443 由 Caddy 监听。
- 8765 和 3000 只监听 `127.0.0.1` 或 `::1`。

检查健康接口和证书：

```powershell
Invoke-RestMethod https://collector.example.com/api/health
```

应返回类似：

```json
{
  "ok": true,
  "database": "ok"
}
```

## 7. Windows 会话要求

系统使用有界面的 Chrome，运行账号必须保持 Windows 桌面会话存在：

- 服务器重启后，运行账号必须至少登录 Windows 一次。
- 可以关闭 RDP 客户端或“断开连接”。
- 不要在开始菜单中选择“注销”。
- 不要手工关闭采集过程中出现的 Chrome 窗口。
- 如果服务器安全策略会自动注销断开的会话，需要为该运行账号设置例外或延长会话保留时间。

## 8. 首次业务验收

建议使用专门的测试账号和少量无敏感风险的数据完成以下步骤：

1. 使用正式 HTTPS 域名登录管理员账号。
2. 创建一个普通用户，记录一次性初始密码。
3. 使用普通用户登录并完成首次改密。
4. 打开“小红书登录”，确认远程浏览器画面、点击、输入、滚动和截图正常。
5. 使用专用小红书测试账号完成登录。
6. 创建一个测试任务：1 个关键词、目标 1 篇、最近 7 天。
7. 确认任务能从“排队中”进入“采集中”，最终进入“待审核”或给出明确的人工处理提示。
8. 检查图片预览、排除/恢复、批量操作。
9. 生成 ZIP，确认目录分类和 `manifest.csv` 正常。
10. 如启用视觉模型，由管理员使用测试图片完成“保存配置”和“测试连接”，再确认普通用户可以运行 AI 筛选但不能修改模型账号。
11. 创建第二个普通用户，确认两个用户互相看不到任务、图片和浏览器状态。
12. 等待或手动执行清理，确认过期图片和导出文件按保留策略删除。

未经以上验收，不建议开放给正式用户。

## 9. 数据目录与备份

需要备份：

```text
.env
data\app.db
data\users\
```

其中：

- `.env` 包含认证密钥和模型密钥加密主密钥。
- `data\app.db` 包含用户、任务、图片元数据、审计和加密后的模型配置。
- `data\users\<user-id>\browser-profile` 包含用户的小红书登录状态。
- 候选图片和导出 ZIP 是短期数据，不应作为长期归档来源。

备份必须进入公司受控、加密且限制访问的存储。数据库和 `.env` 必须属于同一备份版本。

执行一致性备份前建议暂停采集任务并停止计划任务：

```powershell
Stop-ScheduledTask -TaskName "XHS Collector"
```

备份完成后重新启动：

```powershell
Start-ScheduledTask -TaskName "XHS Collector"
```

至少每季度做一次恢复演练，并验证管理员登录、模型配置解密和用户浏览器登录目录。

## 10. 日志与故障排查

主要日志目录：

```text
C:\Apps\XhsCollector\logs
```

优先检查：

```text
logs\startup-errors.log
```

常见问题：

### 网站无法打开

- 检查 DNS、80/443 防火墙和 Caddy 服务。
- 执行 `C:\Caddy\caddy.exe validate --config C:\Caddy\Caddyfile --adapter caddyfile`。
- 确认 8765 只在本机监听且计划任务正在运行。

### 网站能打开但无法登录

- 确认使用正式 HTTPS 域名，不要混用 IP、HTTP 和多个域名。
- 用第 5 节命令检查四项认证配置是否为 `OK`。
- 检查登录失败锁定时间，默认锁定 15 分钟。
- 如果这是已有 `.env` 的首次服务器安装，停止继续尝试并联系维护人员生成或重置管理员凭据。

### 任务一直排队

- 确认 Windows 运行账号仍处于已登录桌面会话。
- 确认计划任务和 8765 服务正常。
- 检查是否已有同一用户的采集或登录浏览器占用。

### 显示“需要处理”

- 进入“小红书登录”页面人工处理登录失效、验证码或访问限制。
- 系统不会自动处理验证码或绕过平台限制。

### 浏览器无法启动

- 不要注销 Windows 运行账号。
- 检查 Chrome、Playwright 浏览器和服务器剩余内存。
- 检查是否有遗留 Chrome 进程占用同一用户目录。

## 11. 升级部署

升级前必须：

1. 备份当前发布目录、`.env`、数据库和用户浏览器目录。
2. 记录当前版本和发布包 SHA256。
3. 停止 `XHS Collector` 计划任务。
4. 确认关键认证配置和 `SECRETS_MASTER_KEY` 非空。
5. 在维护窗口内执行升级并完整验收。

不要用开发环境的 `.env` 覆盖生产 `.env`，也不要重新生成 `SECRETS_MASTER_KEY`。

当前版本首次生产部署建议使用 SQLite 单机模式。已有 PostgreSQL 数据库不要未经迁移验证直接升级，因为新增数据库字段需要版本化迁移脚本和预发布演练。

## 12. 回滚原则

出现升级失败时：

1. 停止计划任务和 Caddy 对该站点的流量。
2. 恢复上一版程序目录。
3. 恢复同一时间点的 `.env` 和数据库备份。
4. 恢复用户浏览器目录。
5. 启动计划任务，依次验证健康接口、管理员登录和一个普通用户。

不得只恢复数据库而使用另一版本的 `.env`，也不得在回滚过程中轮换 `SECRETS_MASTER_KEY`。

## 13. 正式上线检查表

- [ ] 使用全新发布目录完成首次安装，没有先运行 `setup.ps1`。
- [ ] 发布 ZIP 的 SHA256 已核验。
- [ ] DNS、HTTPS 证书和 80/443 防火墙正常。
- [ ] 8765、3000 无法从公网访问。
- [ ] `ENVIRONMENT=production`。
- [ ] `DEV_AUTH_BYPASS=false`。
- [ ] `SITE_AUTH_ENABLED=true`。
- [ ] 网站用户名、密码哈希、会话密钥和加密主密钥均非空。
- [ ] `.env`、`data`、`logs` 的 NTFS 权限已限制。
- [ ] Windows 运行账号不会被自动注销。
- [ ] 管理员及普通用户登录、改密和停用流程正常。
- [ ] 两个普通用户的数据隔离已经验证。
- [ ] 小红书登录、少量采集、审核和导出全链路通过。
- [ ] 视觉模型测试通过，或确认正式环境不使用 AI。
- [ ] 备份与恢复演练完成。
- [ ] 数据合规、平台使用权限和个人信息处理流程已经由责任部门确认。

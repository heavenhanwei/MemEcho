# memEcho Gateway 与 Windows 安装包配置

## 交付架构

Windows 安装包连接独立部署的 HTTPS Gateway。安装包不携带百炼、OSS 或 Gateway 访问密钥，也不在用户电脑上自动启动 Python 服务。

运行时地址优先级：

1. 用户在“我的 → 分析网关”中测试并保存的地址；
2. 构建安装包时写入的公开 HTTPS Gateway 地址；
3. 仅供本地开发使用的 `http://127.0.0.1:8787`。

Gateway 访问令牌在安装后由用户输入，保存到 Windows Credential Manager。不要在生产构建中设置 `VITE_GATEWAY_TOKEN`。

## Gateway 服务端

在服务器上从 `services/gateway/.env.example` 复制一份仅存在于部署环境的 `.env`，填写百炼、OSS 和服务端访问令牌。`.env` 不得提交到 Git、复制进桌面安装包或写入日志。

发布前至少确认：

- Gateway 已通过 HTTPS 对外提供服务；
- `/v1/health` 可访问；
- `MEMECHO_DEMO_TOKEN` 已设置为强随机值；
- 百炼文本、音频与 OSS 参数在服务端可用；
- OSS Bucket 为私有，并启用 24 小时生命周期兜底清理；
- 日志不包含音频、逐字稿、姓名、报告正文或访问密钥。

本地开发可运行：

```powershell
.\scripts\start-gateway.ps1
```

默认本地地址是 `http://127.0.0.1:8787`，默认开发令牌是 `change-me`，二者不得用于生产环境。

## Windows 发布构建

构建机需要 Windows 11、Rust、Node.js/Corepack、Tauri 2 的 Windows 构建依赖，以及干净的 Git 工作区。执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-windows-release.ps1 -GatewayUrl "https://gateway.example.com"
```

脚本会：

- 拒绝非 HTTPS、localhost、带路径/参数/凭据的地址；
- 拒绝环境中存在 `VITE_GATEWAY_TOKEN`；
- 拒绝有未提交 tracked 文件的工作区；
- 运行前端和 Rust 自动化测试；
- 构建 MSI 与 NSIS 安装包；
- 将安装包、SHA-256、大小和签名状态写入 `release-artifacts/`。

只验证发布参数而不构建：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-windows-release.ps1 -GatewayUrl "https://gateway.example.com" -ValidateOnly
```

`ExecutionPolicy Bypass` 只作用于这一次子进程，不修改系统的全局 PowerShell 策略。

安装后进入“我的 → 分析网关”，确认地址连通并输入 Gateway 访问令牌。令牌仅保存到 Windows Credential Manager，不写入项目文件。

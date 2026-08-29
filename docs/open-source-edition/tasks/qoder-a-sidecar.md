# Qoder A：桌面 Sidecar 生命周期

## 目标

让 Tauri 桌面端具备管理本地 Gateway Sidecar 的可靠基础，不再以固定 `127.0.0.1:8787` 和手工 PowerShell 启动为默认产品路径。

## 实施范围

1. 审计 `apps/desktop/src-tauri` 现有 Gateway URL、Credential Manager、Tauri state 和启动流程。
2. 新增可测试的 Gateway Supervisor：
   - 开发模式允许使用外部 Gateway；
   - Sidecar 模式选择随机回环端口并启动受管进程；
   - 等待 `/v1/health`，校验 Gateway 版本/协议；
   - 保存本次运行的 URL 和临时令牌到内存，不写磁盘；
   - 应用退出时优雅终止；异常退出或健康失败时返回稳定错误。
3. 定义最小启动握手合同，不能把令牌放入 URL 查询参数。
4. 前端通过 Tauri command 获取当前运行时连接信息；保留显式的远程 Gateway 高级设置。
5. 为随机端口、启动超时、版本不匹配、退出清理和开发模式写 Rust 测试。
6. 补充 Sidecar 打包所需的 Tauri 配置或构建脚本骨架，但不要内置 `.env` 或真实密钥。

## 边界

- 不实现 Provider Profile。
- 不重构 FileTrans、OSS 或分析流水线。
- 如果当前 Python Gateway 尚不能产出 Sidecar 可执行文件，使用可注入的测试进程/命令完成 Supervisor，并明确记录后续打包阻塞。

## 验收

- 普通模式不依赖固定端口或 `change-me`。
- 开发者仍能显式连接外部 Gateway。
- 自动化测试证明进程生命周期和握手失败路径。
- 不泄露启动令牌，不影响现有录音与上传测试。


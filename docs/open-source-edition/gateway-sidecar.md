# Gateway Sidecar 生命周期与启动握手合同

> 对应任务：`docs/open-source-edition/tasks/qoder-a-sidecar.md`
> 实现：`apps/desktop/src-tauri/src/gateway_supervisor.rs`

## 运行模式

| 模式 | 说明 |
|---|---|
| `sidecar`（目标默认） | 桌面端启动受管的 `memecho-gateway` 进程：随机 `127.0.0.1` 端口、一次性令牌、启动握手、退出时清理 |
| `external`（开发/自托管） | 显式连接外部 Gateway（设置页保存的 URL），不管理进程；凭据仍保存在系统凭据库 |
| 开发固定端口 | `SupervisorConfig.port = Some(port)` 时保留固定端口行为，仅供开发 |

前端通过 `gateway_connection` Tauri command 获取当前运行时连接信息
（URL、一次性令牌、Gateway 版本、协议版本）；显式远程 Gateway 高级设置
（`set_gateway_url` / 凭据库令牌）保持不变，作为 sidecar 不可用时的回退。

## 启动握手合同（v1）

桌面端 → Sidecar 进程，仅通过环境变量传递，不落盘、不进命令行参数：

| 环境变量 | 含义 |
|---|---|
| `MEMECHO_GATEWAY_HOST` | 固定 `127.0.0.1` |
| `MEMECHO_GATEWAY_PORT` | 桌面端选定的随机回环端口 |
| `MEMECHO_GATEWAY_TOKEN` | 本次运行的一次性 Bearer 令牌（32 位十六进制） |

Sidecar → 桌面端：在启动超时内监听 `127.0.0.1:$MEMECHO_GATEWAY_PORT` 并对
`GET /v1/health` 返回：

```json
{ "status": "ok", "version": "<semver>", "protocol_version": 1, "provider": "..." }
```

规则：

1. 桌面端轮询时把令牌放在 `Authorization: Bearer <token>` 请求头中，
   **绝不放入 URL 查询参数**，也不写入日志、磁盘或错误消息。
2. `version` 必须等于桌面端期望的 Gateway 版本，否则返回稳定错误
   `VersionMismatch` 并终止子进程。
3. `protocol_version` 缺省按 1 处理；不一致返回 `ProtocolMismatch`。
4. 子进程在握手完成前退出 → `EarlyExit(<status>)`；超时 → `StartupTimeout`；
   健康响应不合法 → `InvalidHealth`。所有失败路径都会终止并回收子进程。
5. 应用退出（`RunEvent::Exit`）时 Supervisor 终止 Sidecar；运行时连接信息
   只保存在内存中。

## 稳定错误码

| 错误 | 含义 |
|---|---|
| `PortAllocation` | 无法分配回环端口 |
| `Spawn` | 无法启动 sidecar 可执行文件 |
| `EarlyExit(code)` | sidecar 在健康前退出 |
| `StartupTimeout` | 握手超时 |
| `VersionMismatch` | Gateway 版本不匹配 |
| `ProtocolMismatch` | 协议版本不匹配 |
| `HealthFailed` / `InvalidHealth` | 健康检查失败或响应不合法 |
| `InvalidUrl` | 外部 Gateway URL 不合法（拒绝查询参数/明文凭据/远程 HTTP） |

## Sidecar 构建

`scripts/build-gateway-sidecar.ps1` 使用仓库虚拟环境中的 PyInstaller，
生成 `apps/desktop/src-tauri/binaries/memecho-gateway-<target-triple>.exe`。
Tauri `externalBin` 在安装包构建时携带该文件；生成物不进入 Git。

自动化生命周期测试仍使用可注入的
`memecho-gateway-testsidecar`，发布验收另外执行真实冻结 Gateway 的
健康握手和安装后启动测试。

## 安全不变量

- 默认只绑定 `127.0.0.1`；每次运行生成新令牌，不复用、不内置 `change-me`。
- 普通模式不依赖固定端口 `8787`。
- 令牌不出现在 URL、查询参数、日志、错误消息或磁盘配置中。

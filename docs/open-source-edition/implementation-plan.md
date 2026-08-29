# memEcho 开源版实施任务

本轮由三位 QoderCLI 工程师并行实施第一阶段基础能力。完整目标架构见 [architecture.md](architecture.md)。

## 并行分工

| 工程师 | 任务 | 主要目录 | 交付分支 |
|---|---|---|---|
| A | 桌面 Sidecar 生命周期与握手 | `apps/desktop/src-tauri/`、桌面运行时桥接 | `worktree-qoder-open-a-sidecar-20260829` |
| B | BYOK Provider Profile 与密钥引用 | `services/gateway/`、`packages/contracts/`、设置 UI | `worktree-qoder-open-b-byok-20260829` |
| C | Media Transport 与异步任务恢复 | `services/gateway/providers/`、持久化、处理详情 | `worktree-qoder-open-c-media-20260829` |

## 共用规则

- 不读取、打印、修改或提交 `.env`；不得输出任何真实凭据。
- 不删除或回退用户已有改动。
- 不把目标架构描述成已经实现。
- 先审计现有实现，复用 Credential Manager、FileTrans 轮询、processing-details 和持久化能力。
- 新接口必须有合同与测试；错误必须使用稳定错误码。
- 完成后在各自分支提交，并在最终输出给出提交哈希、测试结果、改动文件和未完成项。
- 禁止推送远程、发布 Release 或修改生产环境。

## 合并顺序

1. A：建立桌面本地 Gateway 运行基线。
2. B：合并 Provider Profile、凭据引用和能力接口。
3. C：合并媒体策略与异步恢复，最后处理 Gateway 集成冲突。

主工程师负责审查、合并、生成 SDK、执行全量门禁和 Windows 安装验证。


# memEcho 工程内容地图

## 唯一工程主线

- 仓库：`memecho-desktop/`
- 当前集成分支：`integration/roadshow-v0`
- 桌面端：`apps/desktop/`
- Gateway：`services/gateway/`
- 分析合同：`packages/contracts/`
- 部署与安装：`infra/`、`scripts/`
- 测试：各应用和服务目录中的 `*.test.*`、`tests/`
- 工程文档：`docs/`

开发成果只有在进入上述受版本控制目录并提交后，才视为已保存。Agent 的 worktree、运行日志、测试缓存和本地会话不作为交付源。

## 工程文档

- [系统架构](architecture.md)
- [连通性审计](connectivity-audit.md)
- [FileTrans 异步接口](filetrans-async-interface.md)
- [Gateway 本地配置](gateway-setup.md)
- [部署说明](deployment.md)
- [Windows 发布](windows-release.md)
- [发布门禁](release-checklist.md)
- [路演验收](roadshow-acceptance.md)
- [缺陷记录](bugs/)

## memEcho 项目内的其他资产

以下目录位于同一个 memEcho 项目根目录，但属于独立交付物或历史参考，不应复制进桌面端源码：

- `../memecho-skills/`：memEcho Skill 独立仓库与发布源。
- `../web-demo/`：早期 Web 视觉原型，仅作设计参考。
- `../docs/`：产品需求、路演文案、Skill 使用说明等产品文档。
- `../brand/`：品牌源文件和导出资源。
- `../test-data/`：授权、脱敏的测试输入。

`sources/` 是 ChatGPT 项目同步的只读资料，不能作为可编辑工程目录。

## Agent 成果归并结论

2026-08-11 对 QoderCLI、Mimo、OpenCode 和集成 worktree 做了补丁等价性审计：

- 音频捕获、实时字幕、Gateway 生产适配、文本导入、报告证据回听、历史会话、关系数据、发布门禁等成果均已由等价或更新实现进入 `integration/roadshow-v0`。
- 连通性审计文档此前只存在于 Agent 分支，现已归入 `docs/connectivity-audit.*`。
- 旧分支中与主线不同的历史会话、Gateway 启动和发布门禁补丁已被主线的更新实现覆盖，不再重复合并，以免功能回退。
- OpenCode realtime worktree 中只剩未完成的未使用 import，没有功能实现或测试价值，不作为工程成果归档。

## 不进入版本库的内容

- `.env`、API Key、OSS 凭据、Credential Manager 数据。
- `.runtime/` 和历史 Agent 日志；日志可能含任务标识或脱敏前上下文。
- `tmp/` 中的录音、分块、临时报告和本地会话。
- `.test-tmp/`、缓存、虚拟环境、`node_modules/`、构建产物和 `*.tsbuildinfo`。
- `.mimo/`、`.qoder/`、`memecho-agent-worktrees/` 等工具会话和工作副本。

这些目录可以按需重新生成。需要长期保留的结论应整理为 `docs/` 文档；需要长期保留的实现必须合并并提交到主线。

## 安全要求

- `.env` 只保存在 `services/gateway/.env` 本地文件中，禁止提交。
- `.env.example` 只能包含变量名和安全占位值。
- 生产日志、音频、逐字稿和报告正文不进入 Git。
- 删除本地会话时，应同步删除其音频、报告和派生记忆。

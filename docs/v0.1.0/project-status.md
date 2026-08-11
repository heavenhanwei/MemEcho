# 项目状态与功能清单

> 快照日期：2026-08-11 · 分支：`integration/roadshow-v0` · 阶段：路演版（roadshow-v0）收尾

## 一、项目状态

- **阶段**：路演版打磨收尾。近期提交集中于桌面端官方品牌资产落地、gateway FileTrans 异步转写管线修复（百炼 ASR 合同、签名媒体 URL、可观测性）、web 端轮询与状态展示、数据链路连通性审计（见 [connectivity-audit.md](connectivity-audit.md)）。
- **技术栈**：
  - Monorepo 管理：pnpm 11 workspace
  - 桌面端：Tauri 2 + React 19 + TypeScript + Vite 7 + Zustand + react-three（3D 可视化）；Rust 侧使用 rusqlite（本地 SQLite）、reqwest、tokio、Windows WASAPI（双轨音频捕获）与 Windows Credential Manager（凭证存储）
  - 网关：Python 3.12，FastAPI + Pydantic + httpx + sse-starlette + websockets + oss2

## 二、架构概览

### 模块划分

| 目录 | 职责 |
| --- | --- |
| `apps/desktop/` | Tauri 2 桌面客户端：双轨录音、本地会话与记忆（SQLite）、球体可视化、报告展示 |
| `services/gateway/` | FastAPI 百炼网关：转写编排、说话人对齐、声学证据融合、memEcho 1.1 报告生成 |
| `packages/contracts/` | 前后端共享的 memEcho 1.1 分析类型，由网关 Pydantic 模型生成，禁止手工维护 |
| `infra/` | Docker 与阿里云（SAE/ALB/OSS）部署参考 |
| `scripts/` | 启动与冒烟脚本 |

分析合同单一来源：修改网关模型后运行 `python services/gateway/scripts/generate_types.py`，生成 `packages/contracts/src/generated.ts`。

### 通信方式

- 前端 ↔ Rust：Tauri IPC（`apps/desktop/src-tauri/src/lib.rs`、`commands.rs`）
- 客户端 ↔ 网关：HTTP（默认端口 8787）
- 实时转写：WebSocket `/live`
- 任务事件流：SSE（jobs 状态推送）

### 数据链路

```
双轨录音（麦克风 + 系统输出）
  → 分块上传（8 MiB 分块、断点续传）
  → OSS 存储
  → Fun-ASR FileTrans 异步转写
  → 说话人对齐 + 声学质量降权
  → Qwen 分析报告
  → 产物落盘（JSON / Markdown / HTML）
```

未配置百炼凭据时，网关默认使用确定性 mock 适配器，可完整演示上传、处理、报告、记忆与追问流程（`MEMECHO_PROVIDER=bailian` 切换真实链路）。

## 三、功能清单

权威验收口径见 [roadshow-acceptance.md](roadshow-acceptance.md)。代码中无 TODO/FIXME 标记，缺口均以文档跟踪。

### 已完成

- **录音**：状态机（开始/暂停/恢复/停止）、设备选择、异常崩溃恢复、Web 降级录音
- **可视化**：球体五态可视化（WebGL + Canvas 降级）
- **上传与分析**：分块上传合同、分析流水线、processing-details 处理详情面板、官方逐字稿视图
- **报告**：报告视图与产物落盘（JSON/MD/HTML）
- **追问**：`/v1/chat/stream` SSE 追问接口
- **说话人**：说话人确认（resolve / candidates）
- **导入**：媒体文件与纯文本导入
- **配置**：凭证管理（Credential Manager）、网关地址配置
- **演示**：确定性 mock 演示链路

### 半成品 / 待实现

- 证据回听跳转（`read_evidence_clip` IPC 已有，UI 跳转未完成）
- 关系记忆 UI（`RelationsView` 基础视图已有，功能不完整）
- 导入会话与派生记忆的级联删除
- 实时字幕真实链路联调（当前为降级/演示路径）
- 断网降级策略
- MSI 安装包（构建指南已备，见 [windows-release.md](windows-release.md)）
- 脱敏联调样例（`tests/fixtures`）

> 说明：网关中的 "semantic evidence" 指报告的证据引用修复，不是向量检索/embedding 能力；当前架构无 embedding 语义检索。

## 四、docs/ 内容梳理

### 文档清单

| 文档 | 主题 |
| --- | --- |
| [project-content-map.md](project-content-map.md) | 工程内容地图 / 总索引 |
| [architecture.md](architecture.md) | 数据链路图、连通性结论与验收顺序 |
| [connectivity-audit.md](connectivity-audit.md) | 2026-08-10 只读连通性审计（16 项链路测试、134 项测试结果） |
| [gateway-setup.md](gateway-setup.md) | Gateway `.env` 配置与客户端连接配置 |
| [deployment.md](deployment.md) | SAE 单实例云端网关部署 |
| [windows-release.md](windows-release.md) | Windows MSI / NSIS 构建指南 |
| [release-checklist.md](release-checklist.md) | 发布门禁清单 |
| [roadshow-acceptance.md](roadshow-acceptance.md) | 产品端到端验收矩阵（功能完成度权威口径） |
| [filetrans-async-interface.md](filetrans-async-interface.md) | FileTrans 异步任务状态机与 UI 文案 |
| [frontend-performance.md](frontend-performance.md) | WebGL 分包构建性能测量记录 |
| [bugs/BUG-007-filetrans-long-audio-observability.md](bugs/BUG-007-filetrans-long-audio-observability.md) | 长音频链路不可观察缺陷定义（P0） |

### 索引关系与已知问题

- 根目录 `README.md` 只链接 4 份交付文档（deployment / windows-release / release-checklist / roadshow-acceptance）；完整索引以 `project-content-map.md` 为准。
- `architecture.md` 与 `connectivity-audit.md` 的链路图与连通性结论高度重叠，前者是精简版。
- `frontend-performance.md` 此前未被任何索引收录，本次已补入内容地图。

### 过时标注（仅标注，未修改原文档）

`connectivity-audit.md`（2026-08-10）中的部分失败/未实现状态已过期：

- P0 发现 "DashScope 轮询误用 POST"：已由提交 `2215a83`、`4b84162` 修复。
- BUG-007 要求的 `processing-details` 接口：已由提交 `c5eb27a`、`68339be` 等实现，网关现已提供该端点。

阅读审计报告时请以当前代码与本文档的功能清单为准。

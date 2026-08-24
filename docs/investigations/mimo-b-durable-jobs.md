# Mimo B：Gateway 会话与任务持久化恢复

## 目标

将 Gateway 的会话、上传、分析任务、处理详情和 FileTrans task_id 从纯内存状态升级为重启后可恢复的本地持久化状态，使后续问题和报告追问不因 Gateway 重启中断。

## 已知证据

- `services/gateway/src/memecho_gateway/store.py` 明确使用 `MemoryStore`，进程重启后 session/job 全部丢失。
- 历史日志存在 upload、analyze、processing-details 请求，但当前服务重启后无法再查询原任务。
- 媒体与报告需要遵守现有删除联动和隐私约束。

## 工作范围

1. 提供最小可靠持久层，优先 SQLite；保持现有 Store API 或通过兼容接口迁移。
2. 持久化 session、job、request_id 幂等关系、处理详情、上游 task_id、恢复状态和必要的非敏感错误码。
3. Gateway 启动时恢复未完成任务；明确哪些阶段可自动续跑、哪些阶段必须标记可重试。
4. 不在日志或数据库中持久化 API key；音频/逐字稿正文仍遵循既有数据保留策略。
5. 增加测试：重启恢复、幂等、并发更新、任务失败恢复、媒体删除后派生状态失效。

## 文件所有权

- `services/gateway/src/memecho_gateway/store.py`
- 可新增 `services/gateway/src/memecho_gateway/persistence.py`
- `services/gateway/src/memecho_gateway/config.py`
- 与持久化直接相关的新测试文件

不要修改 `apps/desktop/**`、`providers/dashscope.py`、`providers/transcription.py` 或 FileTrans 探针接口。

## 安全与交付

- 当前工作区已有用户改动：禁止 `git reset`、`git stash`、`git checkout`、清理文件或提交 commit。
- 不读取、打印、复制或提交 `.env` 和任何密钥。
- 完成后输出：迁移策略、修改文件、测试结果、兼容性与数据清理说明。

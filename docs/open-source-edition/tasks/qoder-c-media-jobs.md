# Qoder C：Media Transport 与异步任务恢复

## 目标

解除分析流水线对 OSS 的硬编码依赖，并让 FileTrans 等异步上游任务在 Gateway 重启后可继续轮询，不重复提交付费任务。

## 实施范围

1. 审计现有 `providers/oss.py`、`providers/transcription.py`、Bailian Provider、orchestrator、persistence 和 processing-details。
2. 定义 Media Transport 接口和能力：
   - `local_path`；
   - `binary_upload`；
   - `base64_inline`（有严格大小上限）；
   - `public_url`（OSS/S3 类可选实现）。
3. Provider 声明接受的媒体输入，流水线选择兼容 Transport；不兼容时返回 `media_input_unsupported`，不能伪装成 `upstream_task_failed`。
4. OSS 保持可选适配器。未配置 OSS 时，只要 Provider 支持直接上传或本地路径，正式转写仍能运行。
5. 把异步上游任务引用持久化：provider、capability、upstream task ID、状态、轮询次数、下次轮询时间、最后错误码。
6. Gateway 启动时恢复未完成轮询；超时状态不得自动重新提交任务。
7. processing-details 输出 `submitted/polling/downloading/parsing/completed` 和可恢复状态，所有供应商错误必须脱敏。
8. 增加集成测试：无 OSS 直接上传、必须 URL 但无 Transport、重启恢复、幂等提交、超时后继续轮询、完成后临时媒体清理。

## 边界

- 不设计 Provider Profile UI；为将来 Profile 传入 Transport 配置保留接口即可。
- 不改变正式分析合同中的证据规则。
- 不调用真实付费服务，使用可控 Fake Provider 验证。

## 验收

- OSS 不再是 Gateway 核心初始化的必需条件。
- FileTrans 任务重启后从已有 task ID 继续。
- 不重复提交相同 request_id 的上游任务。
- UI 所需的处理详情可以明确区分提交、轮询、下载、解析和失败。


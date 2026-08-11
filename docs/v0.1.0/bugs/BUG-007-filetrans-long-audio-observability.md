# BUG-007：长音频、FileTrans 与 Qwen 分析链路不可观察

## 背景

实时流式字幕已经可用，但会后长音频分析仍可能返回 `insufficient`。当前用户无法判断问题发生在录音保存、Gateway 上传、OSS、FileTrans、Fun-ASR、情绪识别、对齐，还是 Qwen3.7 输入阶段。

## 已确认现状

1. Tauri 桌面端持续写入本地双轨 WAV；Chrome 端将 `MediaRecorder` 分片保存在内存，停止时合成单个 WebM，再通过 Gateway 的 8MB 分块接口上传。
2. Gateway 完成上传后将媒体写入 `MEMECHO_DATA_DIR/<session>/<upload>/`，再上传至私有 OSS。OSS 对象在任务结束时删除，但 Gateway 本地上传副本目前没有等价的任务级清理。
3. FileTrans 已接入异步提交、GET 轮询、`transcription_url` 下载和句级归一化。
4. FileTrans 归一化结果进入 `transcription_segments`，与说话人/情绪区间对齐后作为 `session.observations.aligned_segments` 提交给 Qwen3.7。
5. 报告页面只展示最终证据、VAD 和内容分析；不展示 FileTrans 状态、任务耗时、句数、逐句文本或各上游错误。
6. `_aligned_segments`、`_model_errors` 等内部字段不是稳定公开合同，前端没有读取或展示它们。
7. 现有测试覆盖单模块解析和失败降级，但缺少“FileTrans 文本确实进入 Qwen Provider 输入”的明确集成断言。

## 用户影响

- 长音频失败时只能看到泛化的 `RuntimeError` 或 `insufficient`，无法定位责任模块。
- Chrome 长录音在停止前没有持久化恢复能力，刷新或崩溃可能丢失。
- 用户无法核对正式转写是否正确，也无法判断 Qwen 报告是否基于 FileTrans 文本。
- Gateway 临时媒体生命周期不透明。

## 实施范围

### P0：FileTrans 可观察性与 Qwen 输入证明

1. 新增独立、脱敏的处理详情合同和接口，例如：
   `GET /v1/sessions/{session_id}/processing-details`。
2. 返回每条上传轨道的：
   - 文件名、track、mime type、大小和上传完成状态；
   - OSS 上传/签名状态；
   - Fun-ASR、FileTrans、emotion 的 `queued/running/succeeded/failed`；
   - 安全错误码，不返回密钥、签名 URL、完整供应商响应或绝对路径；
   - FileTrans 句数、语言、音频时长、耗时；
   - 归一化后的正式转写片段；
   - 对齐片段数量，以及是否已提交给 Qwen。
3. 在处理页增加“会后处理详情”区域，随 SSE/轮询刷新状态。
4. 在报告页增加“正式转写”折叠区域，按时间、说话人展示 FileTrans 片段，并标明“正式转写”和“实时临时字幕”的区别。
5. FileTrans 失败时展示模块名、稳定错误码和可重试提示，不显示原始异常正文。
6. 新增集成测试，使用捕获型 Provider 断言：FileTrans 文本 → 对齐片段 → `session.observations.aligned_segments` → Qwen Provider 输入。

### P1：长音频可靠性和清理

1. 保持 Tauri 双轨 WAV 的现有持续落盘行为。
2. Chrome 端至少明确显示“停止前仅保存在本页内存”；若能在现有范围安全实现，则用 IndexedDB/OPFS 持久化录音分片并支持异常恢复。不要引入远程存储。
3. 对 Gateway 本地上传副本定义明确生命周期：报告和导出完成后删除，失败任务保留有限时间以允许重试；提供有界清理机制和测试。
4. 删除会话时继续联动删除本地音频、报告和派生数据。

## UI 最小呈现

处理页建议显示：

```text
本地录音       已保存  48:12  92.4 MB
Gateway 上传   已完成  12/12 分块
OSS 临时媒体   已就绪
FileTrans      已完成  438 句  37.8 秒
Fun-ASR        已完成  3 位说话人
情绪识别       已降级  upstream_timeout
证据对齐       已完成  421 个片段
Qwen3.7        已接收  421 个片段
```

报告页正式转写建议显示：

```text
00:12–00:18  Speaker 1  我想先确认一下今天讨论的范围……
00:18–00:24  Speaker 2  我同意先处理时间节点……
```

## 验收标准

- 10–20 秒授权 WAV：FileTrans 成功时，处理页能看到句数，报告页能看到逐句文本。
- 捕获型 Provider 测试证明相同的标准化文本出现在 Qwen 输入中。
- FileTrans 失败时，页面明确显示 `transcription` 模块失败，对齐不会伪造片段。
- 实时字幕始终标记为临时；报告只使用会后正式转写。
- API 和日志不暴露 API Key、OSS Secret、签名 URL、完整供应商错误正文或本地绝对路径。
- Chrome 长录音的持久化能力和限制在 UI 中明确说明。
- Gateway 临时文件和 OSS 对象具有自动化清理测试。
- 后端全量 pytest、前端 TypeScript 与相关 Vitest、Rust 相关测试通过。
- 不修改或提交 `.env`、`.env.example` 中的用户生产参数。

## 非目标

- 不改变 memEcho 1.1 核心分析合同的语义。
- 不把实时临时字幕直接当作正式报告证据。
- 不在调试接口中返回原始模型响应、完整签名 URL 或生产密钥。

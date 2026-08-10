# FileTrans 异步接口与界面反馈

## 目标

FileTrans 是异步任务。Gateway 负责向百炼提交任务、轮询任务状态、下载
`transcription_url`、归一化句级文本并进入对齐；1420 页面只查询 Gateway，
不得直接接触百炼密钥或任务接口。

## 状态机

```text
not_started
  -> submitting
  -> queued
  -> polling
  -> downloading
  -> normalizing
  -> succeeded

任一阶段 -> failed
轮询超时 -> timed_out
```

状态语义：

| phase | 含义 | 界面文案 |
|---|---|---|
| `not_started` | 尚未收到可提交媒体 | 等待录音上传 |
| `submitting` | 正在向百炼提交任务 | 正在提交正式转写 |
| `queued` | 百炼已接收，等待执行 | 已提交，等待百炼处理 |
| `polling` | Gateway 正在查询任务状态 | 正在等待正式转写，第 N 次查询 |
| `downloading` | 正在下载 `transcription_url` | 转写完成，正在读取结果 |
| `normalizing` | 正在转换为句级标准结构 | 正在整理正式转写 |
| `succeeded` | 已得到可用句级文本 | 正式转写完成，共 N 句 |
| `failed` | 上游任务或结果处理失败 | 正式转写失败，可重试 |
| `timed_out` | 超过最大轮询时间 | 等待超时，可重试 |

## Gateway 接口

分析仍通过现有接口启动：

```http
POST /v1/sessions/{session_id}/analyze
```

页面每 2 秒读取一次处理详情：

```http
GET /v1/sessions/{session_id}/processing-details
```

FileTrans 字段扩展为：

```json
{
  "status": "running",
  "phase": "polling",
  "poll_attempts": 4,
  "next_poll_after_ms": 2000,
  "elapsed_ms": 8400,
  "last_polled_at": "2026-08-10T12:30:42Z",
  "sentence_count": null,
  "language": null,
  "audio_duration_ms": 62000,
  "error_code": null,
  "retryable": false
}
```

约束：

- 不返回百炼 API Key、签名 URL、供应商原始响应或本地绝对路径。
- 不要求前端保存或使用百炼 `task_id`；如需排查，只返回经过脱敏的
  `task_reference`。
- `status` 保持兼容现有合同；`phase` 提供异步任务的细分状态。
- `sentence_count=0` 不能标记为成功，应返回 `invalid_upstream_result`。
- 超时使用 `upstream_timeout`，HTTP 错误使用 `upstream_http_error`，连接失败
  使用 `upstream_connection_error`。

## 轮询规则

- Gateway 首次轮询间隔 2 秒。
- 连续轮询采用有上限的退避，例如 2、2、3、3、5 秒。
- 最长等待时间由服务端配置，默认不超过 5 分钟。
- 前端只轮询 `processing-details`，不因单次请求失败终止正式分析；显示“状态
  暂时不可用，正在重试”。
- `succeeded`、`failed`、`timed_out` 为终态。
- 页面离开后停止前端轮询；Gateway 后端任务继续运行。

## 1420 界面

处理页按顺序展示：

```text
录音上传      已完成  4.8 MB
OSS 临时媒体  已完成
FileTrans     正在等待正式转写 · 第 4 次查询 · 8.4 秒
说话人分离    处理中
情绪识别      处理中
证据对齐      等待正式转写
Qwen3.7       等待对齐结果
```

FileTrans 成功后，在处理页立即显示最近若干句；报告页展示完整的有界正式转写：

```text
FileTrans 正式转写已完成 · 128 句 · 01:02
[00:00–00:06] speaker_self：……
[00:06–00:11] speaker_2：……
```

失败时展示稳定错误和动作：

```text
FileTrans 等待超时（upstream_timeout）
录音已安全保存，可重试正式转写，不需要重新录音。
```

## 验收条件

1. 结束录音后先看到 Gateway 上传成功，再进入 FileTrans。
2. FileTrans 排队和轮询期间页面持续更新，不显示为卡死。
3. 成功时能看到句数和正式转写片段。
4. 正式转写片段进入 `aligned_segments`，并可证明提交给 Qwen3.7。
5. 失败或超时时显示模块、稳定错误码和可重试状态。
6. 任一上游失败时不伪造文本、声学指标或对齐片段。

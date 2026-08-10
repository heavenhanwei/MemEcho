# memEcho 路演版数据链路与连通性

```mermaid
flowchart LR
  UI[浏览器/Tauri 桌面端\nlocalhost:1420] -->|GET /v1/health\nREST + SSE + WebSocket| GW[FastAPI Gateway\n127.0.0.1:8787]
  UI -->|本地双轨录音| DB[(SQLite + 本地音频)]
  DB -->|8MB 分块上传| GW
  GW -->|临时对象 + 签名 URL| OSS[(私有 OSS\n24h 生命周期)]
  GW -->|POST 音频任务| FUN[Fun-ASR\n说话人分离/时间戳]
  GW -->|POST 音频任务| EMO[Qwen ASR FileTrans\n情绪标签]
  GW -->|POST + GET 任务轮询| TXT[Qwen ASR FileTrans\n正式转写]
  FUN -->|diarization| ALIGN[时间区间对齐]
  EMO -->|emotion intervals| ALIGN
  TXT -->|transcript sentences| ALIGN
  ALIGN -->|aligned evidence + DSP| LLM[Qwen3.7\n纪要/VAD/事实观点态度/自我回声]
  LLM -->|合同校验 + HTML/Markdown| GW
  GW -->|结果/进度/证据| UI
  GW -.->|失败时 errors[]，不臆测| UI
```

## 当前连通性结论

| 链路 | 状态 | 说明 |
|---|---|---|
| 1420 → Gateway `/v1/health` | 已通 | 本地返回 `status=ok, provider=bailian`。 |
| Gateway → Fun-ASR | 已修复并已冒烟 | 任务查询使用官方要求的 `GET /api/v1/tasks/{task_id}`。 |
| Gateway → Qwen FileTrans（转写） | 代码已接通，真实样例待复验 | 原先把二进制音频误读为 JSON，现改为提交 FileTrans 任务并读取 `transcription_url`；公共样例曾超时。 |
| Gateway → Qwen FileTrans（情绪） | 代码已接通，真实样例待复验 | 失败会进入 `model_errors`，不会伪造声学结论。 |
| Gateway → OSS | 配置依赖环境变量 | 上传、签名 URL、分析后删除由编排器串联；需确认当前进程加载了有效 endpoint/bucket/key。 |
| 转写 + 分离 + 情绪 → 对齐 | 条件式 | 只有获得转写且至少有分离或情绪区间时才产生 `aligned`；当前报错中的“无对齐片段”表示上游都没有返回可用区间。 |
| 对齐/DSP → Qwen3.7 | 未进入本次失败会话 | 因上游音频证据为空，分析只能降级为 insufficient。 |

## 验收顺序

1. 先确认 `GET http://127.0.0.1:8787/v1/health`。
2. 用一段 10–20 秒 WAV 验证 OSS 上传和签名 URL 可被百炼访问。
3. 单独记录 Fun-ASR、Qwen FileTrans 转写、情绪三个任务的 task ID、HTTP 状态和最终状态。
4. 只有三类返回中至少有转写和一类区间证据，才验收对齐与 Qwen3.7 报告。
5. 任一上游失败时，报告必须展示失败来源和降级原因，不把空结果标记为成功。

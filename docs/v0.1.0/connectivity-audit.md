# memEcho 连通性审计报告

**审计时间**: 2026-08-10T07:43Z  
**审计范围**: 前端 1420 → Gateway 8787 → OSS → Fun-ASR / Qwen FileTrans → 对齐 → Qwen3.7  
**审计类型**: 只读连通性测试 + 代码路径审查  
**工作树**: connectivity-audit

---

## 架构图

```
┌─────────────┐     REST / SSE / WS      ┌──────────────────┐
│  浏览器/Tauri│ ──────────────────────── │  FastAPI Gateway  │
│  localhost:  │     :8787/v1/*            │  127.0.0.1:8787  │
│    1420      │                           └──────┬───────────┘
└──────┬───────┘                                  │
       │ 本地双轨录音                              │
       ▼                                          ├─► OSS (私有, 24h 生命周期)
┌──────────────┐                                  ├─► Fun-ASR (说话人分离)
│  SQLite +    │                                  ├─► Qwen FileTrans (转写)
│  本地音频     │                                  ├─► Qwen FileTrans (情绪)
└──────────────┘                                  └─► Qwen3.7 (纪要/分析)
                                                     │
                                                     ▼
                                              ┌──────────────┐
                                              │  时间区间对齐  │
                                              │  + DSP 质量   │
                                              └──────────────┘
```

### 数据流详解

1. **前端 → Gateway**: 浏览器/Tauri 通过 REST 创建 Session、8MB 分块上传音频、触发分析、SSE 监听进度、WebSocket 实时字幕
2. **Gateway → OSS**: 上传完成后写入私有 OSS，生成签名 URL 供百炼访问
3. **Gateway → Fun-ASR**: `POST /api/v1/services/audio/asr/transcription` + `GET /api/v1/tasks/{task_id}` 轮询
4. **Gateway → Qwen FileTrans**: 同一端点，不同 model 参数 (转写/情绪)
5. **Gateway → 对齐**: `alignment.align_intervals()` 合并转写、说话人、情绪区间
6. **对齐 → Qwen3.7**: `POST {BAILIAN_TEXT_BASE_URL}/chat/completions` 含 aligned_segments

---

## 链路可达性测试结果

| # | 链路 | 端点 | 状态 | 说明 |
|---|------|------|------|------|
| 1 | Frontend 1420 → Gateway | `http://127.0.0.1:1420` | ⚠️ 未验证 | Vite dev server 未运行 (Tauri 桌面端正常时不需独立 dev server) |
| 2 | Gateway /v1/health | `GET http://127.0.0.1:8787/v1/health` | ✅ 可达 | `{"status":"ok","provider":"bailian","version":"0.1.0"}` |
| 3 | Gateway CORS (localhost:1420) | `OPTIONS` + `Origin: http://localhost:1420` | ✅ 通过 | `access-control-allow-origin: http://localhost:1420` |
| 4 | Gateway CORS (tauri://localhost) | `OPTIONS` + `Origin: tauri://localhost` | ✅ 通过 | `access-control-allow-origin: tauri://localhost` |
| 5 | Gateway CORS (恶意来源) | `OPTIONS` + `Origin: https://evil.example.com` | ✅ 拒绝 | `Disallowed CORS origin` |
| 6 | Gateway 鉴权 (无 token) | `POST /v1/sessions` 无 Authorization | ✅ 拒绝 | HTTP 401 |
| 7 | Gateway 鉴权 (有效 token) | `POST /v1/sessions` + Bearer change-me | ✅ 通过 | HTTP 200, 返回 session id |
| 8 | Gateway Session 创建 | `POST /v1/sessions` | ✅ 可用 | `{"id":"ses_...","request_id":"req_...","status":"queued"}` |
| 9 | Gateway 未知路由 | `GET /v1/nonexistent-route` | ✅ 正确 | HTTP 404 `{"detail":"Not Found"}` |
| 10 | Gateway SSE 事件 | `GET /v1/jobs/{id}/events` | ✅ 正确 | 不存在的 job 返回 HTTP 404 |
| 11 | Gateway 处理详情 | `GET /v1/sessions/{id}/processing-details` | ❌ 不存在 | HTTP 404 — P0 待实现 (BUG-007) |
| 12 | 百炼文本域名 | `BAILIAN_TEXT_BASE_URL` | 🔒 未配置 | 运行实例未加载 .env，值为空 |
| 13 | 百炼音频域名 | `BAILIAN_AUDIO_BASE_URL` | 🔒 未配置 | 运行实例未加载 .env，值为空 |
| 14 | 百炼实时 WS | `BAILIAN_REALTIME_WS_URL` | 🔒 未配置 | 运行实例未加载 .env，值为空 |
| 15 | OSS Endpoint | `OSS_ENDPOINT` | 🔒 未配置 | 运行实例未加载 .env，值为空 |
| 16 | WebSocket 端点 | `WS /v1/sessions/{id}/live` | ✅ 正确 | 无 upgrade 时返回 HTTP 404 |

> **安全说明**: 本次审计不读取 .env 文件内容，不输出 API Key、OSS Secret、签名 URL 或原始供应商响应。百炼域名仅从 .env.example 配置模板确认字段存在。

---

## Gateway 路由一致性

| 路由 | 方法 | 鉴权 | CORS | 前端调用 | 代码实现 |
|------|------|------|------|---------|---------|
| `/v1/health` | GET | 无 | ✅ | `gateway.health()` | `main.py:141` |
| `/v1/sessions` | POST | Bearer | ✅ | `gateway.createSession()` | `main.py:146` |
| `/v1/sessions/{id}/uploads` | POST | Bearer | ✅ | `gateway.uploadBlob()` | `main.py:158` |
| `/v1/sessions/{id}/uploads/{id}/chunks/{i}` | PUT | Bearer | ✅ | `gateway.uploadBlob()` loop | `main.py:189` |
| `/v1/sessions/{id}/uploads/{id}/complete` | POST | Bearer | ✅ | `gateway.uploadBlob()` | `main.py:227` |
| `/v1/sessions/{id}/participants/resolve` | POST | Bearer | ✅ | `gateway.resolveParticipants()` | `main.py:280` |
| `/v1/sessions/{id}/participants/candidates` | GET | Bearer | ✅ | `gateway.participantCandidates()` | `main.py:305` |
| `/v1/sessions/{id}/analyze` | POST | Bearer | ✅ | `gateway.analyze()` | `main.py:341` |
| `/v1/jobs/{id}` | GET | Bearer | ✅ | `gateway.job()` | `main.py:372` |
| `/v1/jobs/{id}/events` | GET (SSE) | Bearer | ✅ | `gateway.jobEvents()` | `main.py:383` |
| `/v1/sessions/{id}/result` | GET | Bearer | ✅ | `gateway.result()` | `main.py:399` |
| `/v1/sessions/{id}/artifacts` | GET | Bearer | ✅ | `gateway.artifacts()` | `main.py:413` |
| `/v1/chat/stream` | POST (SSE) | Bearer | ✅ | `gateway.chat()` | `main.py:470` |
| `/v1/sessions/{id}/live` | WebSocket | token query | N/A | `gateway.liveUrl()` | `main.py:485` |

**结论**: 前端 `api.ts` 中所有 Gateway 调用均在 `main.py` 中有对应路由实现，CORS 允许 `localhost:1420` 和 `tauri://localhost`，鉴权通过 `require_token` 依赖注入。

---

## 测试套件结果

**总计**: 134 tests — **123 通过, 11 失败**

### 失败详情

| 测试 | 根因 | 严重度 |
|------|------|--------|
| `test_cors::test_local_frontend_origin_can_read_health` | httpx 测试客户端未返回 CORS header | 低 (测试环境差异) |
| `test_dashscope::test_submit_sends_correct_headers_and_body` | `_poll_result` 用 POST 而非 GET 轮询 | **高** |
| `test_dashscope::test_submit_with_full_endpoint_url` | 同上 | **高** |
| `test_dashscope::test_polling_uses_get_method` | 同上 | **高** |
| `test_dashscope::test_polling_completes_on_succeeded` | 同上 | **高** |
| `test_dashscope::test_polling_retries_then_succeeds` | 同上 | **高** |
| `test_dashscope::test_polling_raises_on_failed_task` | 同上 | **高** |
| `test_realtime::test_client_forwards_pcm_and_maps_official_events` | 缺少 `OpenAI-Beta` header 断言 | 中 |
| `test_text_only::test_bailian_text_provider_receives_strict_text_only_prompt` | 测试断言过期 | 中 |
| `test_text_only::test_bailian_provider_repairs_structurally_invalid_json_once` | 测试断言过期 | 中 |
| `test_text_only::test_text_only_rejects_provider_acoustic_hallucination` | 错误类型不匹配 | 中 |

### 关键 Bug: DashScope `_poll_result` 使用 POST 而非 GET

`dashscope.py:93` — `_poll_result` 方法使用 `resp = await client.post(url, headers=headers)`，但 DashScope 官方任务查询 API 要求 `GET /api/v1/tasks/{task_id}`。测试文件 `test_dashscope.py` 正确地 mock 了 GET 并断言 `method == "GET"`，但实现代码使用了 POST。这意味着**所有6个 DashScope 轮询测试失败**。

**业务影响**: Fun-ASR、Qwen FileTrans (转写)、Qwen FileTrans (情绪) 的任务状态轮询可能在真实百炼环境中被拒绝，导致长音频分析永远无法完成。

---

## 处理详情接口 (BUG-007 P0)

**状态**: ❌ 未实现

`GET /v1/sessions/{session_id}/processing-details` 端点不存在 (返回 404)。当前用户在长音频分析失败时只能看到泛化的 `RuntimeError` 或 `insufficient`，无法定位责任模块。

---

## 业务影响评估

| 风险 | 影响 | 优先级 |
|------|------|--------|
| DashScope 轮询使用 POST | 百炼任务查询可能被拒绝，长音频分析阻塞 | **P0** |
| 处理详情接口缺失 | 用户无法定位长音频失败原因 | **P0** (BUG-007) |
| 百炼/OSS 未配置 | Gateway 以 mock 模式运行，无法进行真实分析 | 环境依赖 |
| CORS 测试环境差异 | 生产 CORS 行为正确，测试客户端兼容性问题 | P2 |
| 实时字幕 OpenAI-Beta header | 测试断言不完整，实际代码可能正确 | P2 |

---

## 下一步建议

1. **修复 `_poll_result` 方法** (`dashscope.py:93`): 将 `client.post()` 改为 `client.get()`，使 DashScope 任务轮询符合官方 API 规范
2. **实现处理详情接口** (BUG-007 P0): 新增 `GET /v1/sessions/{id}/processing-details` 返回脱敏的处理状态
3. **配置真实百炼凭证**: 在 `.env` 中配置 `BAILIAN_TEXT_BASE_URL`、`BAILIAN_AUDIO_BASE_URL`、`BAILIAN_AUDIO_API_KEY`、`OSS_ENDPOINT` 等
4. **修复过期测试**: 更新 `test_text_only.py` 和 `test_realtime.py` 中的断言
5. **端到端冒烟测试**: 使用 10-20 秒 WAV 验证完整链路

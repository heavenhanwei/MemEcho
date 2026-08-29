# Qoder B：BYOK Provider Profile

## 目标

把当前分散的 Gateway Token、文本模型和音频模型设置整理成正式 Provider Profile，使一个 Session 从实时字幕到正式报告始终引用同一配置档案。

## 实施范围

1. 审计现有 `llm_config.rs`、`credential.rs`、前端设置页和 Gateway `config.py`/Provider 构造逻辑。
2. 在合同与 Gateway 中定义：
   - `ProviderProfile`；
   - 非敏感配置、能力清单、模型选择；
   - `credential_ref`，禁止 API Key 进入 SQLite；
   - Profile 创建、更新、删除、列表和验证接口。
3. Session 创建时接受并持久化 `provider_profile_id`；重试、身份确认和恢复沿用该 Profile。
4. 定义能力探测结果，至少覆盖 `realtime_asr`、`file_transcription`、`diarization`、`audio_emotion`、`text_analysis`。
5. 重构 Provider 选择，使业务按 Profile/能力解析，不再只依赖全局 `MEMECHO_PROVIDER`；保留环境变量作为 headless/开发兼容路径。
6. 在桌面设置中提供 Profile 列表、编辑、连接验证和删除入口；继续使用 Windows Credential Manager 保存秘密。
7. 增加后端、合同与前端测试，断言响应、事件、日志和持久化数据中没有明文 Key。

## 边界

- 不实现第三方动态插件加载。
- 不更改音频媒体上传方式。
- 不把系统凭据读取逻辑移动到 React；原生层保持秘密所有权。

## 验收

- 用户可以创建至少一个百炼 Profile 和一个 OpenAI-compatible 文本 Profile。
- Profile 验证返回能力和稳定错误码。
- Session 能绑定 Profile，后续任务不会回退到另一个全局 Key。
- SQLite、日志和 API JSON 不包含明文密钥。


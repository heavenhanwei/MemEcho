# Mimo C：FileTrans 真实探针、轮询与端到端门禁

## 目标

修正当前音频连通性测试的 HTTP 400 误报，并验证 FileTrans 的标准异步流程：提交合法音频、获得 `task_id`、轮询、解析文本、送入对齐和 Qwen。

## 已知证据

- 当前 `/v1/llm/test` 音频分支对转写地址执行无音频参数 GET，将 HTTP 400 直接判失败。
- 正式链路已经具备 task_id 轮询代码和处理详情 UI，但缺少轻量、真实、不会污染正式数据的连通性探针。
- 过去曾出现 `upstream_task_failed`、`KeyError` 和“缺少 evidence 引用”。

## 工作范围

1. 将音频连接测试改为协议正确的能力探针；若不应产生付费任务，则拆分为认证/端点探针和显式真实烟测。
2. 加固 FileTrans 响应解析，兼容提交/轮询结果的合法字段差异，禁止裸 `KeyError`。
3. 明确保存并暴露 task_id、轮询次数、耗时、句子数和稳定错误码，但不暴露密钥、签名 URL 或逐字稿正文到日志。
4. 增加离线契约测试及一个显式环境变量开启的真实烟测脚本；默认 CI 不调用付费模型。
5. 验证 FileTrans 文本确实进入 alignment 与 Qwen 输入，并覆盖 evidence 引用校验失败的回归测试。

## 文件所有权

- `services/gateway/src/memecho_gateway/main.py` 中 `/v1/llm/test` 相关逻辑
- `services/gateway/src/memecho_gateway/providers/dashscope.py`
- `services/gateway/src/memecho_gateway/providers/transcription.py`
- `services/gateway/src/memecho_gateway/processing_details.py`
- FileTrans、alignment、production orchestration 相关测试与新烟测脚本

不要修改 `apps/desktop/**` 或 `store.py`。

## 安全与交付

- 当前工作区已有用户改动：禁止 `git reset`、`git stash`、`git checkout`、清理文件或提交 commit。
- 不读取、打印、复制或提交 `.env` 和任何密钥；真实烟测必须默认关闭。
- 完成后输出：修改文件、协议说明、测试结果、真实烟测所需人工步骤。

# memEcho Desktop

memEcho Windows 桌面路演版。客户端负责双轨录音、本地会话与记忆；网关负责百炼实时转写、会后说话人分离、声学证据融合与 memEcho 1.1 报告生成。

## 目录

- `apps/desktop`：Tauri 2 + React + TypeScript 桌面客户端
- `services/gateway`：FastAPI 百炼网关
- `packages/contracts`：前后端共享的 memEcho 1.1 类型
- `infra`：Docker 与阿里云部署参考
- `tests/fixtures`：脱敏联调样例

## 本地启动

```powershell
pnpm install
pnpm dev
```

网关：

```powershell
cd services/gateway
python -m pip install -e ".[dev]"
python -m uvicorn memecho_gateway.main:app --reload --port 8787
```

复制 `services/gateway/.env.example` 为 `.env` 并填入自己的百炼与 OSS 配置。密钥不得写入桌面客户端或提交到仓库。

## 演示模式

未配置百炼时，网关默认使用确定性 mock 适配器，仍可完整演示上传、处理、报告、记忆与追问流程。设置 `MEMECHO_PROVIDER=bailian` 后启用真实链路。


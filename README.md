# memEcho Desktop

memEcho Windows 桌面路演版。客户端负责双轨录音、本地会话与记忆；网关负责百炼实时转写、会后说话人分离、声学证据融合与 memEcho 1.1 报告生成。

## 目录

- `apps/desktop`：Tauri 2 + React + TypeScript 桌面客户端
- `services/gateway`：FastAPI 百炼网关
- `packages/contracts`：前后端共享的 memEcho 1.1 类型
- `infra`：Docker 与阿里云部署参考
- `tests/fixtures`：脱敏联调样例

## 分析合同

FastAPI 的 Pydantic 模型是 memEcho 1.1 分析合同的唯一来源。修改模型或 `/analyze`、`/result` 接口后，在已激活网关 Python 3.12 虚拟环境的终端中运行：

```powershell
python services/gateway/scripts/generate_types.py
python services/gateway/scripts/generate_types.py --check
```

生成文件为 `packages/contracts/src/generated.ts`；桌面端通过 `@memecho/contracts` 使用该文件，不应手工维护第二份分析类型。

## 交付文档

- [SAE / ALB / OSS 与生产容器部署](docs/deployment.md)
- [Windows MSI / NSIS 构建](docs/windows-release.md)
- [路演版发布验收清单](docs/release-checklist.md)
- [产品端到端验收矩阵](docs/roadshow-acceptance.md)

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

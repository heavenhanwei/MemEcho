# memEcho 网关生产部署指南

本指南面向 Windows 路演版的单实例云端网关。网关保存进程内任务状态，因此当前版本必须部署为 **1 个 SAE 实例**；扩容、缩容、滚动重启或进程退出都会使进行中的会话失效。桌面端本地录音不受影响，但需重新上传并启动分析。

## 1. 交付边界

- 容器仅运行 FastAPI 网关，不包含桌面端、测试代码或开发依赖。
- 镜像以 UID/GID `10001` 的非特权用户运行，根文件系统可设为只读。
- 音频和报告不得写入镜像层；临时会话数据写入 `/var/lib/memecho`。
- 百炼、OSS 和桌面访问令牌必须由部署环境注入，不得写入镜像、Compose 文件、日志或仓库。
- 生产入口会拒绝 `mock`、短令牌、空密钥以及非 HTTPS/WSS 的外部地址，并只输出缺失的变量名。
- 当前镜像未在本地构建验证时，不能仅凭静态检查标记为可发布。

## 2. 构建并验证镜像

在仓库根目录执行。先将模板复制到仓库外的受控路径并填写所有空值：

```powershell
Copy-Item .\infra\deploy.env.template C:\secure\memecho-deploy.env
docker compose --env-file C:\secure\memecho-deploy.env -f .\infra\docker-compose.yml config
docker compose --env-file C:\secure\memecho-deploy.env -f .\infra\docker-compose.yml build --pull
docker compose --env-file C:\secure\memecho-deploy.env -f .\infra\docker-compose.yml up -d
```

验收：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/v1/health
docker inspect --format '{{.State.Health.Status}}' memecho-gateway-1
docker compose --env-file C:\secure\memecho-deploy.env -f .\infra\docker-compose.yml down
```

必须确认：

- Compose 在任一必填密钥为空时拒绝解析。
- 健康检查返回 `status=ok` 且 provider 为 `bailian`；该路由不要求 Authorization。
- 容器用户不是 root，`CapDrop` 包含 `ALL`，`ReadonlyRootfs=true`。
- 镜像历史和 `docker inspect` 中不含真实密钥。
- 容器停止后 OSS 临时对象已由应用删除；失败路径也需验证。

## 3. ACR 与 SAE

1. 在与 SAE、OSS 相同地域创建私有 ACR 仓库。
2. 使用不可变版本标签推送镜像，例如 `memecho-gateway:0.1.0-<git-sha>`；不要使用 `latest` 作为发布证据。
3. 在 SAE 创建镜像应用，选择同账号私有 ACR 镜像并固定 digest 或不可变标签。
4. 路演版实例数固定为 1，容器端口为 `8787`，启动命令沿用镜像默认值。
5. 配置 HTTP 存活和就绪检查：路径 `/v1/health`、端口 `8787`、初始等待至少 15 秒、超时 3 秒、周期 30 秒。
6. 将 `MEMECHO_DATA_DIR` 设为 `/var/lib/memecho`。SAE 重启会丢失进程内任务状态，因此发布窗口必须避开现场会谈。
7. 通过 SAE 的保密配置/环境变量注入令牌和密钥；更新密钥后执行一次新会话烟雾测试。
8. 日志只允许任务 ID、模型版本、耗时和错误码。部署前搜索并确认不记录音频、逐字稿、姓名、报告正文或 Authorization 头。

SAE 支持从同账号 ACR 部署镜像、注入机密配置并设置健康检查，操作入口以[阿里云 SAE 官方文档](https://help.aliyun.com/en/sae/deploy-applications-using-acr-images-of-your-account)为准。

## 4. ALB HTTPS/WSS

1. 为专用域名配置 HTTPS 监听器 `443` 和有效证书，仅保留符合组织要求的 TLS 安全策略。
2. 后端转发到 SAE `8787`，健康检查使用 `GET /v1/health`。
3. ALB 的 HTTPS 监听器原生支持 WSS；将桌面端实时地址设置为 `wss://<domain>/v1/sessions/{id}/live`。
4. 将连接空闲超时设置为控制台允许的合适上限，并保证客户端/服务端心跳间隔显著短于该值。不要假定单条连接可持续完整的两小时会谈，断线重连与本地录音是必要降级路径。
5. SSE 路径不得启用响应缓冲或内容缓存。用真实客户端分别验证 WebSocket 升级、SSE 流式事件和普通 HTTPS API。
6. 安全组/ACL 只允许 ALB 访问 SAE 后端；不要将 `8787` 直接暴露公网。

协议支持与超时能力应在发布当日按[ALB 监听器官方文档](https://help.aliyun.com/en/slb/application-load-balancer/create-and-manage-listeners)复核。

## 5. 私有 OSS 临时媒体

- Bucket 必须为私有、禁止公共读、与 SAE 同地域，优先使用内网 endpoint。
- 专用凭据只授予 `OSS_PREFIX` 下上传、读取、删除和必要的分片权限；不要授予 Bucket 管理或跨前缀访问。
- 应用在分析成功、失败或取消后的 `finally` 路径立即删除对象。
- 配置生命周期规则，对 `OSS_PREFIX` 下当前版本、历史版本、删除标记和未完成分片设置 **1 天**清理。
- OSS 生命周期按计划批处理，1 天规则不是“上传后精确 24 小时删除”的 SLA，只能作为应用即时删除失败后的兜底。
- 验收时记录对象 key、创建时间、应用删除结果和生命周期规则截图，但不得保存媒体内容或签名 URL。

生命周期行为和覆盖式更新注意事项见[OSS 生命周期官方说明](https://help.aliyun.com/en/oss/user-guide/overview-54/)。

## 6. 必填环境变量

| 类别 | 变量 | 要求 |
|---|---|---|
| 网关 | `MEMECHO_PROVIDER` | 生产固定为 `bailian` |
| 网关 | `MEMECHO_DEMO_TOKEN` | 至少 32 个随机字节；空值会关闭 HTTP 鉴权，禁止部署 |
| 网关 | `MEMECHO_PUBLIC_BASE_URL` | ALB 对外 HTTPS origin |
| 文本模型 | `BAILIAN_TEXT_BASE_URL`、`BAILIAN_TEXT_API_KEY`、`BAILIAN_TEXT_MODEL` | 按所选百炼地域填写 |
| 音频模型 | `BAILIAN_AUDIO_BASE_URL`、`BAILIAN_AUDIO_API_KEY`、`BAILIAN_REALTIME_WS_URL` | 必须同时验证 HTTP 与 WSS 可达 |
| 音频模型 | `BAILIAN_REALTIME_MODEL`、`BAILIAN_DIARIZATION_MODEL`、`BAILIAN_EMOTION_MODEL` | 与获批模型版本一致 |
| OSS | `OSS_ENDPOINT`、`OSS_BUCKET`、`OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`OSS_PREFIX` | 私有 Bucket、最小权限 |
| 限制 | `CHUNK_SIZE_BYTES` | 路演默认 `8388608` |
| 限制 | `MAX_SESSION_SECONDS` | 路演上限 `7200` |

## 7. 发布与回滚

1. 在预发布 SAE 环境完成 3–5 分钟授权音频的真实烟雾测试。
2. 保存镜像 digest、Git commit、环境变量名清单（不含值）、模型版本和验收记录。
3. 现场发布前停止创建新会话，等待现有任务完成，再切换镜像。
4. 回滚只切回上一个已验收 digest；回滚不会恢复旧进程内会话。
5. 发布或回滚后依次验证健康检查、鉴权失败路径、WebSocket、分块上传、分析、报告下载和 OSS 清理。

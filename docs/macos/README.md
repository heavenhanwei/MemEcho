# memEcho macOS 版本

## 支持范围

- macOS 13 Ventura 或更高版本。
- 首发构建目标为 Apple Silicon（M1 及更新芯片，`aarch64-apple-darwin`）。
- 麦克风录音使用 AVFoundation，系统声音使用 ScreenCaptureKit。
- 实时字幕可选择麦克风、系统声音或二者混合；会后分析继续保存麦克风与系统声音两条独立 WAV。
- Gateway 以无终端窗口的 Sidecar 随 `.app` 启停；用户不需要安装 Python。
- API Key 与 Gateway Token 保存在 macOS 登录钥匙串，配置文件只保存非敏感字段。

macOS 不是把 Windows EXE 改后缀。`WASAPI`、Windows Credential Manager、NSIS/MSI 和 `.exe` Sidecar 均不可复用，代码采用以下平台边界：

```text
React / Tauri commands
        │
        ├── Windows: WASAPI ────────────── mic.wav + loopback.wav
        │             └── Credential Manager
        │
        └── macOS: Swift audio helper ─── mic.wav + loopback.wav
                      ├── AVFoundation (microphone)
                      ├── ScreenCaptureKit (system audio)
                      └── Login Keychain (secrets)

Both platforms ── managed memecho-gateway Sidecar ── realtime / analysis APIs
```

生产代码禁止在不支持的平台回退到静音 Mock。权限、采集组件或设备不可用时必须返回明确错误，避免“字幕连接成功但内容为空”以及基于伪音频生成报告。

## 权限行为

首次开始录音时 macOS 会请求：

1. 麦克风权限；
2. “屏幕与系统录音”权限（ScreenCaptureKit 捕获系统声音）。

用户拒绝权限时，memEcho 保留本地会话元数据并显示可恢复提示，不写入伪造音频。授予屏幕与系统录音权限后需要完全退出并重新打开 memEcho。发布验收必须覆盖同意、拒绝、撤销、恢复四种路径。

## 本机构建

必须在 Apple Silicon Mac 上完成，Windows 不能交叉生成可发布的 `.app` 或 `.dmg`。

```bash
xcode-select --install
corepack enable
pnpm install --frozen-lockfile
python3 -m venv .venv
.venv/bin/pip install -e 'services/gateway[dev,packaging]'
bash scripts/build-macos-sidecars.sh
pnpm typecheck
pnpm test
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
pnpm --filter @memecho/desktop tauri build --bundles app,dmg
```

输出位置：

- `apps/desktop/src-tauri/target/release/bundle/macos/memEcho.app`
- `apps/desktop/src-tauri/target/release/bundle/dmg/*.dmg`

`scripts/build-macos-sidecars.sh` 同时生成当前架构的：

- `memecho-gateway-aarch64-apple-darwin`
- `memecho-audio-capture-aarch64-apple-darwin`

这些生成物不会提交 Git，也不得包含 `.env`、API Key、OSS 密钥、用户音频或数据库。

## 签名与公证

GitHub Actions 的 `Build macOS application` 工作流会在 Apple Silicon Runner 编译 Swift 助手、Rust/Tauri 和 Gateway，并输出临时验证包。公开下载版本必须使用 `Developer ID Application` 证书签名并通过 Apple Notary Service 公证；CI 的 ad-hoc 签名包只能内部验证，不能作为正式发行版。

正式发布由 `.github/workflows/release-macos.yml` 执行。它与 Windows 正式发布使用同一个 `v*` 标签，把已签名、公证的 Apple Silicon `.dmg` 附加到同一个 GitHub Release 草稿。正式发布前需要在 GitHub 仓库的 `Settings > Secrets and variables > Actions` 配置：

- `APPLE_CERTIFICATE`：包含私钥的 Developer ID Application `.p12` 文件的单行 Base64；
- `APPLE_CERTIFICATE_PASSWORD`：导出 `.p12` 时设置的密码；
- `KEYCHAIN_PASSWORD`：只供临时 CI Keychain 使用的随机强密码；
- `APPLE_SIGNING_IDENTITY`：完整的 `Developer ID Application: ... (TEAMID)` 身份名称；
- `APPLE_API_ISSUER`：App Store Connect API Issuer ID；
- `APPLE_API_KEY`：App Store Connect API Key ID；
- `APPLE_API_KEY_P8_BASE64`：`AuthKey_*.p8` 私钥文件的单行 Base64。

Base64 值应在安全的 Mac 上生成，不要通过聊天或工单传递：

```bash
openssl base64 -A -in DeveloperIDApplication.p12
openssl base64 -A -in AuthKey_XXXXXXXXXX.p8
security find-identity -v -p codesigning
```

工作流会对缺失秘密、版本号不匹配、签名失败、公证失败或 stapling 校验失败执行 fail-closed，不会把失败产物放进正式 Release。构建成功后仍保持 Draft，必须完成下方真机验收后再由维护者手动发布。

### GitHub 打包操作

1. 先把上述 Secrets 配置完整；
2. 同步修改根目录 `package.json`、桌面端 `package.json`、`tauri.conf.json` 和 `Cargo.toml` 中的版本号；
3. 推送代码后创建同版本标签，例如 `git tag v0.1.2`；
4. 执行 `git push origin v0.1.2`；
5. 在 GitHub `Actions` 中查看 `Release macOS installer`；
6. 成功后到 `Releases` 检查 Draft，其中应包含 `.dmg`、应用压缩包和 `SHA256SUMS-macos.txt`。

日常分支与 Pull Request 仍由 `Build macOS application` 生成 ad-hoc 签名的验证 Artifact。也可以在 GitHub `Actions` 页面手动运行该工作流；其产物只用于内部测试，不应面向普通用户发布。

不要在仓库、workflow YAML、构建日志或 Release 附件中写入上述秘密。

## macOS 真机验收（发布阻断）

- [ ] 新用户安装 DMG 后可正常打开，Gatekeeper 验证通过。
- [ ] 首次启动显示加载态，无终端窗口。
- [ ] Gateway 自动启动；退出 memEcho 后 Gateway 和音频助手均退出。
- [ ] Keychain 中能保存、读取、更新和删除 Gateway、ASR、LLM 密钥。
- [ ] 允许麦克风权限后，实时字幕包含真实麦克风内容。
- [ ] 允许屏幕与系统录音权限并重启后，系统声音可实时转写。
- [ ] 混合实时流包含自己与对方的声音；本地仍生成两条独立 WAV。
- [ ] 结束录音后，两轨 WAV 可播放，FileTrans、说话人分离、情绪、证据对齐和文本分析全部完成。
- [ ] 拒绝任一权限时不生成假数据，UI 提示正确设置入口和重启要求。
- [ ] 强制退出后可恢复录音；删除会话会同步删除录音、报告和派生数据。
- [ ] 日志和应用包内不含 Token、API Key、OSS 密钥、逐字稿或报告正文。

## 已知边界

- 当前 macOS 设备列表使用系统默认麦克风；切换默认输入设备后重新开始录音。后续可把 CoreAudio 设备枚举加入 Swift 助手协议。
- 系统声音需要 macOS 13+ 与 ScreenCaptureKit 权限；旧系统不提供静音降级。
- Intel Mac 尚未作为首发发布目标。需要支持时，必须在 `macos-15-intel` Runner 分别构建两个 x86_64 Sidecar，再制作 Universal 2 应用并做完整真机验收。

## 官方依据

- [Tauri GitHub Actions Pipeline](https://v2.tauri.app/distribute/pipelines/github/)
- [Tauri macOS Application Bundle](https://v2.tauri.app/distribute/macos-application-bundle/)
- [Tauri External Binaries](https://v2.tauri.app/develop/sidecar/)
- [Tauri macOS Code Signing](https://v2.tauri.app/distribute/sign/macos/)
- [Apple ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit)
- [Apple Keychain Services](https://developer.apple.com/documentation/security/keychain-services)

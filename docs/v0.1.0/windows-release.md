# memEcho Windows MSI / NSIS 构建指南

首版仅支持 Windows 11 x64。开发者本地构建默认不签名，只能用于内部验收；GitHub 的公开发布链路必须通过 SignPath 签署桌面端、Gateway 和安装包。当前范围仍不包含自动更新和 Microsoft Store 发布。

## 1. 构建机要求

- Windows 11 x64，所有系统更新已安装。
- Visual Studio 2022 Build Tools，启用“使用 C++ 的桌面开发”和当前 Windows SDK。
- Rust stable 的 `x86_64-pc-windows-msvc` 工具链。
- Node.js 与仓库声明的 pnpm 版本。
- MSI 构建需要 Windows 可选功能 VBSCRIPT；只有出现 `light.exe` 相关错误时再检查是否被禁用。
- 具有网络访问，以便首次取得 Rust、pnpm、WiX/NSIS 和 WebView2 引导程序依赖。
- 构建机不得保存百炼、OSS 或生产网关密钥。

Tauri 2 使用 WiX Toolset v3 生成 `.msi`，使用 NSIS 生成 `-setup.exe`；MSI 必须在 Windows 上构建。以[Tauri Windows Installer 官方说明](https://v2.tauri.app/distribute/windows-installer/)为准。

## 2. 版本与源代码冻结

发布前同步以下版本，三者必须一致：

- `apps/desktop/src-tauri/tauri.conf.json` 的 `version`
- `apps/desktop/src-tauri/Cargo.toml` 的 package `version`
- `apps/desktop/package.json` 的 `version`

记录 Git commit，并确认工作树没有未跟踪的构建输入或未提交源码：

```powershell
git status --short
git rev-parse HEAD
```

构建只允许从已验收 commit 进行，不得从开发者的脏工作树打包。

## 3. 构建前质量门

在仓库根目录执行：

```powershell
corepack pnpm install --frozen-lockfile
corepack pnpm typecheck
corepack pnpm test
corepack pnpm build
cargo test --locked --manifest-path .\apps\desktop\src-tauri\Cargo.toml
```

全部退出码必须为 0。失败、跳过或仅在其他分支通过都不能作为当前发布 commit 的证据。

## 4. 同时生成 MSI 与 NSIS

当前 `tauri.conf.json` 已配置 `targets: ["nsis", "msi"]`：

```powershell
corepack pnpm tauri build
```

预期产物：

- `apps/desktop/src-tauri/target/release/bundle/msi/*.msi`
- `apps/desktop/src-tauri/target/release/bundle/nsis/*-setup.exe`

若只验证单一格式，可临时通过 CLI target 参数构建，但最终路演交付必须由同一 commit 同时生成两种格式。不要把 `target/` 或安装包提交到源码仓库。

当前 NSIS `installMode` 为 `both`，安装时允许选择当前用户或全局安装；全局安装会请求管理员权限。路演前要分别验证选择路径和取消权限提升后的行为。

## 5. 产物完整性

为每个安装包生成 SHA-256：

```powershell
Get-FileHash .\apps\desktop\src-tauri\target\release\bundle\msi\*.msi -Algorithm SHA256
Get-FileHash .\apps\desktop\src-tauri\target\release\bundle\nsis\*-setup.exe -Algorithm SHA256
Get-AuthenticodeSignature .\apps\desktop\src-tauri\target\release\bundle\msi\*.msi
Get-AuthenticodeSignature .\apps\desktop\src-tauri\target\release\bundle\nsis\*-setup.exe
```

保存文件名、大小、SHA-256、Git commit、构建时间和签名状态。开发者本地构建若为 `NotSigned`，必须在内部交付记录中明确且不得公开发布；GitHub Release 的生产工作流要求状态为 `Valid` 并包含可信时间戳。

## 6. 干净 Windows 11 验收

MSI 与 NSIS 分别在干净的 Windows 11 x64 虚拟机完成：

1. 安装、首次启动、关闭、再次启动和卸载。
2. 检查应用名称、图标、版本、开始菜单入口和卸载项。
3. 验证 WebView2 可用；默认引导方式可能需要联网，离线会场必须提前安装 WebView2 或按官方指南选择离线打包策略。
4. 选择麦克风和系统输出设备，分别录制并生成可播放的 `mic.wav` 与 `loopback.wav`。
5. 拒绝麦克风权限后应用给出可恢复提示，不崩溃、不伪造音频。
6. 断网时本地录音继续；恢复网络后可重新上传。
7. 凭据只进入 Windows Credential Manager，安装目录和日志中不出现 token。
8. 完成真实会话闭环并保存 JSON、Markdown、HTML；删除会话后核对本地音频、报告与派生记忆联动删除。
9. 卸载后记录仍保留的用户数据目录；如存在，路演交付说明必须明确清理方式。

## 7. 路演交付目录

建议在源码仓库外创建只读交付目录：

```text
memEcho-<version>-windows-x64/
├── memEcho_<version>_x64_en-US.msi
├── memEcho_<version>_x64-setup.exe
├── SHA256SUMS.txt
├── RELEASE-NOTES.md
├── ACCEPTANCE-EVIDENCE.md
└── sample/
    ├── authorized-sample.json
    ├── authorized-sample.md
    └── authorized-sample.html
```

不得包含 `.env`、API key、OSS 签名 URL、真实用户音频、未脱敏逐字稿、数据库副本或 Credential Manager 导出。

## 8. 发布失败处理

- 任一安装包无法安装、启动或卸载：停止发布，保留日志并修复后从新 commit 重建两种格式。
- WebView2 下载受限：不要设置 `skip`；选择官方支持的嵌入引导程序或离线安装器方案，并重新测试安装包体积和离线安装。
- SmartScreen 告警：不得指导用户关闭系统安全功能。先核验 Authenticode 与时间戳；签名有效但信誉不足时，保持同一发布者身份并向 Microsoft 提交误报复核。
- 安装后真实录音或 Credential Manager 失败：视为发布阻断，不得以 Web mock 页面代替验收。

## 9. 使用 GitHub Actions 生成 Release

GitHub Release 不是源码仓库中的 `release/` 目录。Release 基于 Git Tag，`.exe`、`.msi` 和校验文件属于 Release Assets，由 CI 构建后上传；`target/`、`release-artifacts/` 和 sidecar `.exe` 继续保持在 `.gitignore` 中，不能提交进 Git。

仓库已提供 `.github/workflows/release-windows.yml`。当 `v*` 标签推送到 GitHub 时，工作流会在 `windows-latest` Runner 上完成：

1. 安装锁定版本的 pnpm、Node.js、Python 和 Rust。
2. 从源码构建 `memecho-gateway-x86_64-pc-windows-msvc.exe` sidecar，不打包本地 `.env` 或 Credential Manager 凭据。
3. 运行前端、Gateway 与 Rust 测试。
4. 生成 NSIS `*-setup.exe` 和 MSI 安装包。
5. 通过 SignPath 签署桌面端、Gateway、NSIS 与 MSI，并执行 Authenticode 验证。
6. 创建草稿 GitHub Release，上传安装包、`SHA256SUMS.txt` 和 `SIGNATURES.json`。

签名服务的申请和仓库变量配置见 [Windows 可信代码签名](windows-code-signing.md)。缺少任一生产签名配置时，工作流会主动失败，不会发布未签名安装包。

发布前先确认四个版本号与标签一致：

- `package.json`
- `apps/desktop/package.json`
- `apps/desktop/src-tauri/Cargo.toml`
- `apps/desktop/src-tauri/tauri.conf.json`

例如发布 `0.1.0`：

```powershell
git switch main
git pull --ff-only
git tag -a v0.1.0 -m "memEcho v0.1.0"
git push origin v0.1.0
```

然后在 GitHub 仓库中依次打开 `Actions` → `Release Windows installers` 查看构建。构建成功后，打开 `Releases` → 对应草稿版本，下载并在干净 Windows 11 x64 机器上完成第 6 节验收；确认无误后再点击 `Publish release`。

如果工作流无法创建 Release，在 GitHub 仓库 `Settings` → `Actions` → `General` → `Workflow permissions` 中确认允许工作流获得写权限。组织策略仍可能覆盖仓库设置。

当前工作流不会读取或上传本地 `.env`。OSS 等本机环境配置不会自动进入 GitHub Runner，也不应该进入安装包。应用的 BYOK 配置由最终用户安装后写入配置文件和 Windows Credential Manager。

> 代码签名不等同于首个版本必然立即获得 SmartScreen 信誉。保持同一发布者身份持续签名，并对误报文件提交 Microsoft 复核；若要求首次安装即由平台背书，应同时评估 Microsoft Store MSIX 渠道。

## 10. 未签名 Preview 发布通道

个人开发者在可信签名获批前，可使用 `v<版本>-preview.<序号>` 标签发布明确标识的 GitHub Prerelease。例如应用内部版本为 `0.1.0` 时：

```powershell
git tag -a v0.1.0-preview.1 -m "memEcho v0.1.0 preview 1"
git push origin v0.1.0-preview.1
```

该标签仅触发 `.github/workflows/release-windows-preview.yml`；正式签名工作流明确排除 `*-preview.*` 标签。Preview 工作流会运行完整测试、构建 Gateway sidecar、NSIS 和 MSI，公开发布为 GitHub Prerelease，并附带：

- `SHA256SUMS.txt`
- `UNSIGNED-PREVIEW.json`
- Git Tag 与 commit 对应关系
- 明确的未签名和早期测试警告

应用内部四处版本仍保持 `0.1.0`。不要把 Cargo、Tauri 或 MSI 版本改为 `0.1.0-preview.1`；Preview 序号只存在于 Git Tag 和 GitHub Release 名称中。

Preview 安装包不能描述为“可信签名版”“正式版”或“已解决 SmartScreen”。不得指导用户关闭 Windows 安全功能。SignPath 配置完成后，再用不带 `-preview.*` 的正式 Tag 触发签名发布链路。

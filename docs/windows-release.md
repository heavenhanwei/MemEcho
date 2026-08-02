# memEcho Windows MSI / NSIS 构建指南

首版仅支持 Windows 11 x64。当前范围不包含代码签名、自动更新和 Microsoft Store 发布；因此路演安装包必须明确标注“未签名”，不能作为正式公开发行包。

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

保存文件名、大小、SHA-256、Git commit、构建时间和签名状态。未签名是当前路演范围内的已知限制，必须在交付记录中明确；不得把 `NotSigned` 描述为已签名或可信发布。

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
- SmartScreen 告警：路演版明确告知未签名；不得指导用户关闭系统安全功能。正式对外发布前补充可信代码签名。
- 安装后真实录音或 Credential Manager 失败：视为发布阻断，不得以 Web mock 页面代替验收。

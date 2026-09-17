# memEcho Windows 可信代码签名

## 结论

面向公众发布的 GitHub Release 必须使用受 Windows 信任的 Authenticode 发布者身份。自签名证书只能用于内部测试，不能解决公众下载时的 SmartScreen 信任问题。

memEcho 是 MIT 许可的开源项目，默认方案采用 SignPath Foundation：由 SignPath 托管受信任的签名密钥，GitHub Actions 只提交由官方仓库、官方工作流产生的待签名产物，不保存或接触私钥。

由于 2024 年起 EV 证书不再获得 SmartScreen 的即时特殊待遇，新发布者的首批签名文件仍可能显示信誉提示。必须长期使用同一发布者身份；如发生误报，再向 Microsoft 提交文件复核。若要求首次安装也完全不显示 SmartScreen，优先评估 Microsoft Store 的 MSIX 发布渠道。

## 1. 申请 SignPath Foundation

在 <https://signpath.org/apply> 为以下项目申请开源签名：

- 项目：`memEcho`
- 仓库：`https://github.com/heavenhanwei/MemEcho`
- 许可证：MIT
- 构建系统：GitHub Actions，Windows GitHub-hosted runner
- 发布物：Tauri 2 生成的 Windows x64 NSIS `.exe` 与 `.msi`
- 需要签名的中间文件：`memecho-desktop.exe`、`memecho-gateway-x86_64-pc-windows-msvc.exe`

申请获批后，在 SignPath 中建立：

1. 一个项目，例如 `memecho`。
2. 一个仅允许官方仓库 `v*` 标签的生产签名策略，例如 `release-signing`。
3. 一个签署两个应用可执行文件并保持文件名的 Artifact Configuration。
4. 一个签署 NSIS `.exe` 与 MSI 的 Artifact Configuration。
5. 一个只有 Submit 权限的 CI API Token；生产策略建议保留人工审批。

具体 Artifact Configuration 由 SignPath 审核人员按本仓库的两阶段签名流程确认。不要在未确认输入和输出目录结构的情况下直接使用生产策略。

## 2. 配置 GitHub

在 GitHub 仓库 `Settings` → `Secrets and variables` → `Actions` 中配置：

Secret：

| 名称 | 内容 |
| --- | --- |
| `SIGNPATH_API_TOKEN` | SignPath CI Submitter API Token |

Variables：

| 名称 | 示例 |
| --- | --- |
| `SIGNPATH_ORGANIZATION_ID` | SignPath Organization ID |
| `SIGNPATH_PROJECT_SLUG` | `memecho` |
| `SIGNPATH_SIGNING_POLICY_SLUG` | `release-signing` |
| `SIGNPATH_BINARIES_ARTIFACT_CONFIGURATION_SLUG` | 签署桌面端和 Gateway 的配置 slug |
| `SIGNPATH_INSTALLERS_ARTIFACT_CONFIGURATION_SLUG` | 签署 NSIS/MSI 的配置 slug |

不要把 API Token、证书、私钥或云签名凭据写入仓库、`.env`、安装包、构建日志或 Release Assets。

## 3. 发布链路

`.github/workflows/release-windows.yml` 使用失败即关闭策略：缺少任一签名配置时，不会创建 Release。

```text
源码与 v* Tag
  → 构建 Gateway sidecar
  → Tauri build --no-bundle
  → SignPath 签署 desktop.exe + gateway.exe
  → Tauri bundle 生成 NSIS + MSI
  → SignPath 签署安装包
  → Get-AuthenticodeSignature 验证签名和时间戳
  → 生成 SHA256SUMS.txt + SIGNATURES.json
  → 创建草稿 GitHub Release
```

两次签名都必须完成。只签安装包会留下未签名的 Gateway 和桌面主程序，不符合当前发布门禁。

## 4. 发布验收

下载草稿 Release 后，在未安装开发证书的干净 Windows 11 x64 机器执行：

```powershell
Get-AuthenticodeSignature .\memEcho_*_x64-setup.exe | Format-List Status, StatusMessage, SignerCertificate, TimeStamperCertificate
Get-FileHash .\memEcho_*_x64-setup.exe -Algorithm SHA256
```

必须满足：

- `Status` 为 `Valid`。
- 发布者名称与获批的 SignPath 发布者身份一致。
- 存在可信时间戳。
- SHA-256 与 Release 中的 `SHA256SUMS.txt` 一致。
- 安装目录内的桌面主程序和 `memecho-gateway.exe` 同样显示有效签名。
- 安装、升级、退出 Gateway、卸载以及真实录音闭环全部通过。

验收完成后再发布草稿。签名验证失败、SignPath 请求未批准或只有部分文件签名时，不得公开 Release。

在 SignPath 获批前，早期测试只能走独立的未签名 Preview 通道，标签格式为 `v<版本>-preview.<序号>`。该通道发布为 GitHub Prerelease，不得与本页描述的正式可信签名版本混用。

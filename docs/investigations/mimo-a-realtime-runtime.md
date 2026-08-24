# Mimo A：实时音频与本地运行闭环

## 目标

修复 Windows/Tauri 模式下实时字幕只读取 WebView 麦克风、无法读取原生 WASAPI 系统回环的问题，并让本地 Gateway 具备可观察的启动与存活状态。

## 已知证据

- `apps/desktop/src/App.tsx` 在 Tauri 模式启动原生双轨录音后，仍通过 `navigator.mediaDevices.getUserMedia()` 创建实时 PCM。
- 最近录音中 `mic.wav` RMS 为 0/3，而 `loopback.wav` RMS 为 1417/703。
- WebSocket 到 Gateway/百炼握手曾验证成功；当前主要问题是发送的音频源为空。
- 当前录音目录有 WAV，但 SQLite `sessions` 表为空。

## 工作范围

1. 设计并实现从 Tauri 原生捕获向前端或 Gateway 持续发送 16kHz PCM 的安全通道，支持 `mic`、`system`、`mixed`。
2. Tauri 模式实时字幕必须复用原生录音音源；Web 模式继续使用浏览器 MediaStream。
3. 录音开始时立即创建本地 SQLite 会话，结束/失败时更新状态，确保 WAV 与会话索引一致。
4. 补足 Gateway 本地启动、健康检查和退出提示；不得将 mock 作为真实百炼模式的静默回退。
5. 添加 Rust/TypeScript 测试，覆盖静音麦克风但系统轨有声、暂停恢复、断线重连和本地会话写入。

## 文件所有权

- `apps/desktop/src-tauri/**`
- `apps/desktop/src/App.tsx`
- `apps/desktop/src/lib/livePcm*`
- `apps/desktop/src/lib/tauri*`
- `scripts/start-gateway.ps1`
- `scripts/roadshow-launch.ps1`

不要修改 `services/gateway/src/memecho_gateway/store.py`、FileTrans provider 和 Gateway FileTrans 测试。

## 安全与交付

- 当前工作区已有用户改动：禁止 `git reset`、`git stash`、`git checkout`、清理文件或提交 commit。
- 不读取、打印、复制或提交 `.env` 和任何密钥。
- 完成后输出：修改文件、设计说明、测试命令与结果、仍需人工验证项。

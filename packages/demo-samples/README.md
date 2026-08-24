# @memecho/demo-samples

路演脱敏演示样例包。每个样例包含完整的 memEcho 1.1 分析报告（JSON / Markdown / HTML），可直接导入桌面端演示。

## 样例清单

| 样例 | 类型 | 时长 | 说话人 | 说明 |
|---|---|---|---|---|
| `single-speaker/` | 录音导入 | ~3 分钟 | 1 人 | 个人工作复盘独白 |
| `multi-speaker/` | 录音导入 | ~4 分钟 | 2 人 | 项目范围讨论会议 |
| `text-import/` | 文本导入 | — | 2 人 | 纯文本会议转写 |

## 脱敏声明

所有样例内容均为虚构，不包含可识别真实个人的信息。语音音频文件需由授权方另行提供并放置在对应目录下。

## 使用方式

### 桌面端导入

1. 将 `samples/` 目录复制到 `%APPDATA%/memecho/demo/`
2. 在桌面端"此刻"页面使用"导入音频"或"分析文本"功能
3. 或直接在路演启动脚本中自动加载

### 路演一键加载

```powershell
scripts/roadshow-launch.ps1
```

## 音频文件说明

每个录音样例目录包含 `metadata.json` 描述音频元数据。实际音频文件（`.wav`）需由授权方提供：

- `single-speaker/mic.wav` — 单人麦克风录音
- `multi-speaker/mic.wav` — 麦克风录音
- `multi-speaker/loopback.wav` — 系统回环录音

音频文件不在版本控制中，请通过安全渠道获取。

## 验证

```powershell
pnpm --filter @memecho/demo-samples validate
```

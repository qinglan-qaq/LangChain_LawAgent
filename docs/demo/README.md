# 演示视频录制说明

本目录是一套可复现的项目演示视频流水线：真实浏览器 + 真实 FastAPI/LangGraph 后端，不用 mock 数据。

## 文档索引（2026-11 起）

| 文件 | 用途 |
|------|------|
| [`2026-11-demo-flow.md`](./2026-11-demo-flow.md) | **当前主台本**：15 分钟全功能演示的操作步骤、时间码、预期画面、失败预案 |
| [`2026-11-demo-narration.md`](./2026-11-demo-narration.md) | **当前口播**：与主台本逐段对齐，含 3 分钟投递版 |
| `DEMO_SCRIPT.md` | 历史成片（4:57）口播，保留作对照；不再代表当前能力 |
| `FULL_DEMO_FLOW.md` | 历史版全功能流程（2026-11-06），已归档；差异说明见文首 |
| `record_demo.mjs` / `make_video.py` / `narration.json` | 旧 4:57 成片的自动录制与合成脚本 |

## 当前能力（2026-11-09 实测）

| 能力 | 状态 | 证据 |
|------|------|------|
| 代理律师多轮：稀疏提问 → 澄清 → 补充 → 续跑终答 | 可用 | 会话 `AT-20261109-114422-006`，父 run 4 阶段 + 续跑 23 阶段，终答 3398 字 |
| 律师助理起诉状：74 字段预览 → `docx_confirm` → Word/PDF | 可用 | 会话 `AS-20261109-113700-003`，Word 15,245B，PDF 3 页 |
| 律师助理答辩状：41 字段预览 → `docx_confirm` → Word/PDF | 可用 | 会话 `AS-20261109-113812-004`，Word 13,720B，PDF 2 页 |
| 监控统一审计：四结论卡 + 时间轴 + 证据/上下文/性能/节点抽屉 | 可用 | `/monitor`，父子 run 链、`audit-view` 聚合 |
| 历史记录：抽屉、导出 JSON、会话 ID 恢复、URL 恢复 | 可用 | `/?session=...`、左栏恢复框、`exports/` |
| PostgreSQL checkpoint / 会话历史 / 审计落库 | 可用 | 使用本机既有 PostgreSQL；`/home` 显示 `checkpoint_backend=postgres` |

## 已失效的旧结论

以下问题在本分支已修复或被环境变化推翻，不要再写进脚本或口播：

1. **「PG 密码校验失败，只能 inmemory」**：当前直接使用本机既有 PostgreSQL，不新建库、不新建表；会话历史、监控、审计都有真实数据。
2. **「律师助理撞递归上限，文书段不可录」**：`request_element_fill` 无缺口时已推进 `current_step_index`，起诉状与答辩状均实测走到 `docx_confirm` 并生成文件。
3. **「聊天页没有 PDF 下载入口」**：`DocReadyModal` 提供「下载 Word」与「下载 PDF」两个按钮；后端 PDF 端点已修复 Word COM 线程初始化（`pythoncom.CoInitialize()`），起诉状/答辩状 PDF 均实测 200。
4. **「监控页无数据、不可录」**：监控 v2 四端点与统一审计已接入真实 `trace_runs / stage_chain / minor_states` 数据。

## 4:57 成片（历史）

| 文件 | 说明 |
|------|------|
| `output/demo-2026-11-06T17-50-23.mp4` | 4 分 57 秒，1440×900，中文旁白 |
| `output/demo-raw-2026-11-06T17-50-23.webm` | 原始无声录像（VP9），便于重新配音/剪辑 |
| `output/recording-log-2026-11-06T17-50-23.json` | 分段与实际时间戳 |
| `output/shot-*.png` | 关键画面截图 |

> 该成片录制于 2026-11-06，当时未覆盖律师助理文书、监控审计、会话恢复。当前版本请按
> `2026-11-demo-flow.md` 重录；旧片只作配音/剪辑参考。

## 重新录制

前置：

```bash
# 后端（仓库根目录；Windows + PostgreSQL 必须指定 selector loop；仅本机）
F:/Anaconda_env/lawApp_LangGraph/python.exe -m uvicorn \
  lawApp_LangGraph.FastAPI.api:app --host 127.0.0.1 --port 8000 \
  --loop lawApp_LangGraph.FastAPI.loop:selector_loop_factory

# 前端
cd frontend && npm run dev -- --host 127.0.0.1 --port 5173

# 录制依赖（如需自动录制旧脚本）
npm i -D playwright
npx playwright install chromium
```

就绪检查与预热等待见 `2026-11-demo-flow.md` 第 0 节。RAG 模型加载实测约 153 秒，未完成时首轮检索会明显变慢。

录制并合成（旧 4:57 流水线）：

```bash
node docs/demo/record_demo.mjs
python docs/demo/make_video.py
```

可选环境变量：

```bash
BASE_URL=http://127.0.0.1:5173
OUT_DIR=docs/demo/output
HEADLESS=0                 # 显示浏览器窗口，便于观察
PLAYWRIGHT_MODULE=...      # 指定 playwright ESM 模块
FFMPEG=F:/path/to/ffmpeg.exe
```

`record_demo.mjs` 只覆盖「落地 → 要素卡 → 主链路 → 终答」。要覆盖文书、监控、历史恢复，按新台本手动录屏，
或扩展脚本的 marks 与截图。

## 口播与剪辑

- 当前口播在 `2026-11-demo-narration.md`，按时间码与操作台本对齐。
- 旧成片旁白在 `narration.json`，每段含 `startMs` / `endMs` / `text`；重录后按新的
  `recording-log-*.json` 改时间戳。
- `make_video.py` 用 Windows 自带 `Microsoft Huihui Desktop` 合成中文语音；某段旁白超过窗口时
  自动用 `atempo` 加速（上限 1.45×）。
- 换真人配音：保留 `demo-raw-*.webm`，按时间轴录音后替换 `output/audio/*.wav`，或用剪辑软件对轨。

## 数据与隐私

- `exports/`、`docx_outputs/`、`pdf_outputs/`、`logs/` 已在 `.gitignore`；导出 JSON 与生成的文书含案情/个人信息，只在本机留存。
- `docs/demo/verify/` 不在忽略列表，验证结束后必须手工删除，避免把 SSE/JSON/PDF 临时文件带进提交。
- 录制画面不要露出 `.env`、API Key、数据库口令；不要在成片里展示真实当事人信息（演示案情均为虚构）。
- 演示输出是辅助材料，需执业律师复核，不构成法律意见。

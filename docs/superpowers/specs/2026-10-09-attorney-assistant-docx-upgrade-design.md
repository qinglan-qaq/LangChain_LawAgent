# 律师助理模式升级:文书双格式输出 + 字段补全循环 + 交互增强 — 设计文档

日期: 2026-10-09
状态: 已与需求方逐段确认
前置 spec: `2026-09-23-assistant-docx-generation-design.md`(assistant 模式与 docx 链路一期)

## 0. 背景与目标

律师助理模式(assistant,文书起草)已具备:粘贴案情 → 要素问诊循环(element_assess ⇄ ask_element,每轮 1 问,上限 5 轮)→ 规划(末步 generate_docx,模板可用才规划)→ 字段一次性抽取(74 项)→ `docx_confirm` 面板确认 → docxtpl 渲染法院模板 → toast 提示 + `/sessions/{sid}/docx/latest` 下载。

本次升级七项:

| # | 需求 | 落点 |
|---|------|------|
| 1 | 答辩状 Word 输出上线(与起诉状同链路) | §4 答辩状模板 |
| 2 | 字段抽取后缺关键项继续循环问询补全 | §1 field_clarify 循环 |
| 3 | 默认 Word 输出,可指定 PDF | §2 PDF 端点(Word COM) |
| 4 | 文件生成后模态弹窗 + 双格式下载 | §3 DocReadyModal |
| 5 | 案情长文折叠(模糊渐变) | §5 CollapsibleText |
| 6 | 监控页"触顶"列改"运行时长"列 | §6 MonitorView |
| 7 | 监控页综合分数口径解释 | 已在对话中答复,零代码(score.py 六分量加权,本文不展开) |

方案定调(已确认):**增量扩展** — 不动图拓扑、不加节点、两层问诊结构(高层要素循环 + 一次性字段抽取)保持不变,只在既有挂点上扩展。

## 1. 字段补全循环 field_clarify(缺关键再问)

### 触发与流程

挂点:executor HITL-4 块(`LangGraph_lawApp.py:1294`,`generate_docx` 步骤内,`_extract_doc_fields` 之后、`docx_confirm` 之前)插入 while 循环:

1. 抽取字段(`_extract_doc_fields`,现有 M-3 重试一次逻辑不动)→ 计算 `critical_missing`(现有 preview 构建逻辑复用)。
2. `critical_missing` 非空且 `doc_field_rounds < settings.max_doc_field_rounds`(新增,默认 3)→ 发 interrupt:

```python
interrupt({
    "type": "field_clarify",
    "message": f"为生成完整《{doc_label}》,请补充以下关键信息:{'、'.join(labels[:5])}",
    "round": f"{n}/{max_doc_field_rounds}",
    "field_preview": preview,   # 复用 docx_confirm 的预览结构
})
```

问题文本规则拼接(缺失字段 label,每轮最多 5 个),不耗 LLM。

3. 用户回答(自由文本)→ 答案追加进抽取上下文(`【补充问答】Q/A` 段,累积所有轮次)→ **重新执行 `_extract_doc_fields`** → 再查缺口 → 循环。
4. 退出条件(任一):
   - 关键字段(critical)全部非空 → 进 `docx_confirm`;
   - 轮数达上限 → 进 `docx_confirm`(带剩余缺口提示,现状行为);
   - 用户答"跳过"(归一化空串)→ 放弃补全,直接进 `docx_confirm`。

### LangGraph 机制说明

`interrupt()` 支持节点内多次调用:resume 时节点从头重跑,已答的 interrupt 按序重放返回值,跑到未答处再暂停 — while 循环天然成立,不改图结构。代价:每轮 resume 触发节点重跑,`_extract_doc_fields` 重复调用(3 轮上限最多约 6 次 flash LLM 抽取,接受)。重放正确性依赖:`state.docx_confirmed` 在 interrupt 期间仍为 False(现状 checkpoint 语义即如此,`docx_confirm` 同模式)。

### 配套改动

- `state.py`:新增 `doc_field_rounds: int = 0`;`ingest_node` 轮次重置(与 docx 三件套同批重置)。
- `config.py`:`max_doc_field_rounds: int = 3`(env 可覆盖)。
- interrupt 归一化(`FastAPI/utils.py` normalize_resume):`field_clarify` 归入文本型(空串=跳过补全,非空=补充文本)。
- `dialogue_log`:每轮落 `field_question` / `field_answer` 事件(对齐 round_question/round_answer 模式)。
- 前端 `InterruptPanel.vue`:TYPE_LABEL 加"关键信息补充";TEXT_KINDS 加 `field_clarify`(复用 MCQ+「其他」文本交互);PASS_VALUE 加 `field_clarify: ''`(跳过按钮文案"跳过补全")。

## 2. PDF 输出与下载端点

### 端点

新增 `GET /sessions/{session_id}/doc/{format}`,`format ∈ {docx, pdf}`。docx 分支逻辑 = 现有 `/sessions/{sid}/docx/latest`(`api.py:1080`,事件查路径 + 白名单 + FileResponse),旧端点保留为兼容别名,内部委托同一实现。

pdf 分支:

1. 同款查询拿 `docx_path`(docx_generated 事件)。
2. 缓存判定:`PDF_OUTPUT_DIR`(现有 `./pdf_outputs`)下同名 `.pdf` 存在且 `mtime(pdf) >= mtime(docx)` → 直接返回。
3. 否则转换:`docx2pdf`(Word COM)经 `asyncio.to_thread` + `asyncio.wait_for` 30 秒超时(COM 挂死防线);失败 → HTTP 502,detail="PDF 转换失败,请确认本机已安装 Microsoft Word"。
4. 转换成功落 `pdf_outputs/` 并 FileResponse(`application/pdf`)。

路径白名单:docx 路径仍须落在 `DOCX_OUTPUT_DIR` 内(复用现有校验);pdf 路径由后端拼接(不取自请求),无新增穿越面。

### 依赖

`docx2pdf` 加入 requirements.txt(纯 pywin32 COM 封装,无传递重依赖)。部署约束:服务端机器需装 Microsoft Word(本机已确认)。

## 3. 前端模态弹窗 DocReadyModal

- 新组件 `frontend/src/components/DocReadyModal.vue`(根 id=`doc-ready-modal`):居中模态,内容 = 文书文件名 + "下载 Word" / "下载 PDF" 两按钮(`href=/api/sessions/{sid}/doc/{format}`,download 属性)+ 关闭按钮 + 免责小字。
- 触发:ChatPage 事件路由中 `docx_done` 帧 → `state.showDocReady = true`(保留 `docxPath` 赋值,供终答区按钮)。
- `DocxDoneToast.vue` 移除(toast 形态由模态替换);`DocxGenModal`(生成中遮罩)不动。
- `ChatView.vue` 终答区下载按钮旁增"下载 PDF"按钮(同 URL,format=pdf)。
- store:`showDocReady` 进回合级状态,`resetTurn` 复位。

## 4. 答辩状模板(用户提供法院源表)

doc_templates.py 契约:模板目录三文件齐(fields.yaml + template.docx[, source.docx 留档])即 `template_available(doc_type)` 为 True,零代码改动。

流程:

1. 需求方提供法院《民事答辩状》源表 → 放 `data/doc_templates/defense/source.docx`。
2. 编写 `defense/fields.yaml`:按源表逐字段定义 key/label/type(text|date|choice)/critical/anchor 正则/occurrence(对齐 complaint 样板的字段结构,含 `_merge_into` 约定)。关键(critical)字段至少覆盖:双方当事人姓名、答辩请求、事实与理由主干。
3. 跑 `scripts/build_docx_template.py` 生成 `template.docx`(anchor 未命中即报错,保证字段与源表版式一一对应)。

配套去硬编码(代码侧):

- executor 直调分支文件名前缀 `起诉状_` → 按 doc_type 映射(`起诉状` / `答辩状`)(`LangGraph_lawApp.py:1406`)。
- `_extract_doc_fields` prompt 标题《民事起诉状》→ 按 doc_type 参数化(标题与可选选项参照同步适配答辩状字段)。
- `_TOOL_DESC_OVERRIDES` 中 generate_docx 描述"(起诉状)" → "(起诉状/答辩状)"。
- `PLANNER_ASSISTANT_DOCX_STEP` 文案如提及单一文书类型,同步通用化。

## 5. 案情长文折叠(前端纯改)

**现状**:用户气泡超长折叠已存在(ChatView.vue 硬截断 `text.slice(0,120) + '…'` + 展开全文按钮)。本次改造其呈现:

- 折叠态从"硬截断+省略号"改为**限高 + 模糊渐变隐没**:内容容器 `max-h` 限高 + `overflow-hidden`,底部叠 `mask-image: linear-gradient(...)` 渐变遮罩层(文字向下渐隐),"展开全文"按钮保留。
- 触发阈值沿用 `COLLAPSE_LEN = 120`(store.js),展开/收起交互不变。
- 应用点维持现状:对话流用户气泡(assistant 模式粘贴的案情长文走同一 user 气泡)。其余消息类型不动。

## 6. 监控页"触顶"列 → "运行时长"列(前端纯改)

- `MonitorView.vue` 运行列表表头"触顶"改"运行时长";每格取 `r.metrics.total_latency_ms` 格式化(`45s` / `3.2m`,零值/缺失显 —)。
- 触顶信息保留:`r.metrics.limit_hit` 行的时长数字标红,`title="触顶运行"` 悬停提示;总览卡"触顶运行(limit_hit)"计数不动。

## 7. 错误处理与降级汇总

| 故障 | 行为 |
|------|------|
| 字段抽取两连败 | 现状:步骤 failed 推进(不变) |
| field_clarify 用户跳过 | 直接 docx_confirm,缺失字段渲染"待补充"(现状兜底) |
| Word COM 转换失败/超时 | 502 + 明确 detail;docx 下载不受影响 |
| defense 模板缺失(未部署源表) | planner 不规划 docx 步,答辩状走纯文本终答(现状 D2 降级) |
| PDF 缓存目录不可写 | 同 502 路径,异常 detail 落日志 |

## 8. 测试计划

- 后端(pytest,全量回归):
  - `test_docx_generation` 扩:defense `template_available` True/文件名前缀按 doc_type。
  - 新增 `test_field_clarify`:补齐即停、轮尽即停、跳过直通 docx_confirm、多轮 resume interrupt 重放正确性、`doc_field_rounds` 状态推进、dialogue 事件落库。
  - 新增 `test_doc_download_endpoint`:format=docx 别名等价、pdf 缓存命中/过期重转、无 Word 环境标记 skipif、路径越界 404、无事件 404 三态。
- 前端:`vite build` 零错误;浏览器全链路实测(field_clarify 面板交互、DocReadyModal 双下载、案情折叠展开/收起、监控页时长列)。
- 集成冒烟:答辩状全流程(粘贴案情 → 问诊 → field_clarify → 确认 → docx/pdf 双下载)。

## 9. 不在本次范围

- 综合分数算法调整(第 7 项仅解释口径,score.py 不动)。
- 问诊问答/agent 思考链的折叠(折叠仅案情长文)。
- PDF 的非 Word-COM 转换路径(LibreOffice/pdfkit 备选不做)。
- 74 字段全量问询(仅关键缺口循环,非关键缺口维持确认面板展示)。

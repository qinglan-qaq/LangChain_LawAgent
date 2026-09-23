# 律师助理模式 Word 文书生成 — 设计(Brainstorm 产出)

- 日期: 2026-09-23
- 状态: 定稿(brainstorming 全流程: 探索 → 5+1 项澄清决策 → 3 方案比选 → 8 节设计逐节确认)
- 路径: architectural(新子系统: docx 渲染管线 + HITL 确认 + 模板机制 + 文件交付, 动图/工具层/API/前端四处接口)
- 下一步: 用户审阅本文档 → writing-plans 出实施计划

## Problem Statement

assistant(律师助理)模式现状: 律师粘贴案情 → 5 要素 HITL 问诊 → planner 规划(检索+起草) → finalize 以**纯文本**输出起诉状/答辩状, 前端 md 渲染展示。缺口:

1. 律师最终需要的是**可提交/可编辑的 Word 文档**, 纯文本终答要自己手动排版回 Word, 价值断在最后一公里。
2. 法院实际使用的是**表格式填空模板**(勾选+填空, 参见附件《离婚民事起诉状》), 系统没有模板机制, 也无法按模板结构填充。
3. 是否生成文档应由**律师本人判断**(HITL), 现有 interrupt 机制(risk/pdf/degrade/budget)没有文书生成这一类。

## 已确认决策(brainstorm 逐项)

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | docx 形态 | **表格式**(按附件法院模板复刻版式填充), 非叙述式 |
| D2 | 模板来源 | **通用 docx 模板机制**: 仓库 `data/doc_templates/` 放模板文件(docxtpl) + 结构定义(YAML)配对; 加新文书=加两个文件零代码; 本期只落离婚起诉状, 答辩状留接口 |
| D3 | 触发点 | **镜像 HITL-3**: planner 固定规划末步"生成 Word 文书", executor 执行前 interrupt(`docx_confirm`)由律师确认/跳过 |
| D4 | 字段填充 | **一次抽取 + 缺省待补充**: LLM 从案情一次性结构化抽取全部模板字段, HITL 只追问 critical 字段, 非关键缺失填"待补充"由律师在 Word 内自补 |
| D5 | 确认粒度 | **确认 + 字段预览卡**: interrupt 消息带抽取结果摘要(已填/待补充/critical 缺失清单), 选项仍二选(确认生成/跳过); 不做修正回路(YAGNI, Word 内自改最顺手) |
| D6 | AS 模式检索 | **留法条(fetch_laws), 去类案(search_case)**: law_vector 1526 条在库且模板"诉请依据"节依赖; law_cases 空表 + 文书起草用不上类案 |
| D7 | 实现路线 | **工具层挂载**(方案 1): 完全镜像 markdown_to_pdf 链, 图拓扑零改动; 否决新增图节点(方案 2, 动拓扑无必要)与图外端点(方案 3, 与 D3 冲突且断 dialogue_log/trace) |
| D8 | 生成期前端反馈 | 确认后**生成中弹窗**(全局 modal, spinner + 遮罩); 完成**左上角 toast**"生成完成"内嵌下载按钮(终答区下载按钮并存) |

## 方案比选(D7)

- **方案 1 工具层(选定)**: 新 LangChain 工具 `generate_docx`, executor 内该步骤前抽取+interrupt+渲染。复用 HITL-3 全套机制(interrupt 类型/落库/resume/abort 语义), 改动集中在 tools/prompts/api/前端。
- 方案 2 新增图节点: 语义"节点化"但动拓扑、动 checkpoint 兼容、动测试面, 收益仅概念洁癖。
- 方案 3 图外端点(仿 /ask/pdf 直连): HITL 只能前端自实现, 与 D3 冲突, dialogue_log/trace 全断。

## 设计

### §1 链路总览

assistant 模式 + doc_type=complaint(模板可用)时:

```
律师粘贴案情 → ask_element 问诊(critical 字段) → planner 规划
  → 法条检索(fetch_laws, 现有; 类案步骤不再规划)
  → 末步固定: "生成 Word 文书"(generate_docx)
      executor 执行前:
        ① flash LLM 结构化抽取全部模板字段 ← state(案情+补充+问诊答案+法条结果)
        ② raise docx_confirm interrupt(选项: 确认生成/跳过; message 带字段预览摘要)
        ③ 确认 → 工具 docxtpl 渲染落盘 → docx_path 进 state
        ④ 跳过 → 步骤标 skipped, finalize 正常收尾
  → finalize 终答(文本起诉状 + docx_path 下载信息)
```

非 assistant 模式、doc_type=defense(本期无模板)不规划 docx 步骤。

### §2 模板体系

`data/doc_templates/complaint/` 两文件配对:

- **template.docx**: 附件《离婚民事起诉状》转 docxtpl 版 — 填空位换 `{{ f.字段key }}` 标签; 勾选位换 `{{ c.选项key }}` 布尔符号对(每个选择项两个符号, 选中 ☑ 未选 ☐)。版式原样保留(法院表格结构、★提示、附页落款不动)。
- **fields.yaml**: 结构定义, 每字段 `{key, label, type: text|choice|date, options, critical, tag}`; 文件头 `doc_type: complaint, template: template.docx`。

新模块 `lawApp_LangGraph/doc_templates.py`: `load_template(doc_type) -> (docxtpl-Doc, 字段清单)`; 启动时 YAML key ↔ docx 标签双向对账, 缺一对记警告且该 doc_type 不启用 docx 步骤(退化纯文本, 现状行为)。

### §3 图内改动(全部镜像 pdf 链)

- **prompts.py**: `PLANNER_ASSISTANT_SUFFIX` 改写 — (2) 改"检索婚姻家事法条(不检索类案, 文书起草用不到)"; 追加"若文书模板可用, 末步固定规划 generate_docx 工具步骤"; `_tools_desc()` 注册新工具(带模板可用性动态标注)。
- **executor(LangGraph_lawApp.py)**: generate_docx 步骤未确认时:
  1. 调 flash LLM `_extract_doc_fields`(with_structured_output, schema 由 fields.yaml 动态生成, 输出 字段key → 值/勾选)
  2. raise `docx_confirm` interrupt: options `[确认生成, 跳过该步骤]`; message 摘要(已填 N / 待补充 M / critical 缺失清单 ⚠); payload 新键 `field_preview`(已填 key-value 列表, 前端渲染预览卡)
  3. 抽取结果存 state(`doc_fields`), resume 确认后直接传工具, 不二次抽取
- **tools/tools.py 新工具 `generate_docx`**: `@tool @traced`, 参数 `fields_json + doc_type + filename`; 文件名清洗复用 H9 白名单正则(basename 防穿越); 输出 `./docx_outputs/`(镜像 pdf_outputs); docxtpl 渲染放线程池(阻塞调用, 同 markdown 渲染先例); 返回 `{docx_path, filled, pending}`。

### §4 状态与交付 API

- **state.py**: `doc_fields: dict`、`docx_path: Optional[str]`、`docx_confirmed: bool`(镜像 pdf 三件套; ingest 重置)。
- **api.py**: interrupt 允许集加 `docx_confirm`(语义同 pdf_confirm: 拒绝=跳过该步骤, 不 abort 图); 新端点 `GET /sessions/{sid}/docx/latest` → FileResponse(从 checkpoint 恢复 docx_path; 路径白名单校验防穿越; 文件不存在 404)。
- **model.py QueryResponse**: 加 `docx_path: Optional[str] = None`。

### §5 前端

- **InterruptPanel**: `docx_confirm` 分支 — 复用单选卡 + 新增字段预览区(两列小表: 已填/待补充, critical 缺失 amber 警示行)。id 契约: `docx-preview-{n}`。
- **DocxGenModal.vue**(新, 全局): 律师点"确认生成"resume 后弹出 — 居中 spinner + "正在生成 Word 文书…" + 遮罩防误操作; 关闭时机: SSE progress 事件该步骤 done(成功→开 toast)或 failed(关 modal 转错误提示)。复用现有 progress 事件流, 不新加协议帧。
- **DocxDoneToast.vue**(新, 左上角): 生成完成后弹"Word 文书生成完成", 内嵌下载按钮(指向 `GET /sessions/{sid}/docx/latest`); 点击下载或 8 秒后自动消失。仿 DisclaimerToast 实现。
- **ChatView 终答区**: `docx_path` 非空时出下载按钮(`btn-download-docx`), 与 toast 双入口并存。
- **SessionDrawer**: 对话日志时间线 docx_confirm 显示为确认决策行(复用 interrupt_confirm 渲染); 终答节加下载按钮。

### §6 落库(session_dialogue_events)

镜像现有写入点, 加两类:

- `docx_confirm` — interrupt 消费路径, payload `{question, chosen: 确认生成|跳过, filled, pending, critical_missing}`(计数 + critical 缺失清单; 不落全量字段值, 与 interrupt payload 的完整 `field_preview` 区分)。
- `docx_generated` — 确认且渲染成功后写(final_answer 前), payload `{docx_path, filled, pending}`; 跳过或失败不写。

### §7 错误处理(全部不阻断图)

- 模板缺失/对账失败 → 启动警告, 该 doc_type 不规划 docx 步骤, 退化纯文本终答(现状)。
- 抽取 LLM 失败 → 步骤 failed 走 executor retry 一次, 再败 finalize 收尾(终答仍带文本稿)。
- docxtpl 渲染异常 → 工具返回错误, 步骤 failed, 终答带文本稿 + "Word 生成失败, 请用文本稿"提示。
- 文件名/路径穿越 → H9 正则 + 交付端点二次校验双防线。

### §8 依赖与测试

- **依赖**: `docxtpl`(纯 python, 带 python-docx; jinja2 项目已有)装 lawApp_LangGraph env + requirements.txt。
- **测试** `tests/test_docx_generation.py`(真实 PG, 镜像 test_dialogue_events 风格):
  1. 模板加载 + YAML↔docx 对账(缺标签/缺 YAML key 各一例)
  2. 工具: 全字段填充 / 部分→"待补充" / 勾选项 ☑☐ 正确 / 文件名清洗防穿越
  3. 抽取 schema 动态生成(fields.yaml → schema)
  4. 图内流: docx_confirm 触发与选项载荷 / 确认 resume → 落盘 + state / 跳过 resume → finalize 无 docx
  5. 落库: docx_confirm + docx_generated 事件 / 跳过无 docx_generated
  6. 交付端点: 200 FileResponse / 404 / 路径穿越拒绝
- **回归**: 现有 pytest 全量(14 用例)0 FAIL + `vite build` 通过。

## 明确不做(Out of Scope)

- 答辩状模板(留接口, 待用户提供模板后加两文件即启用)。
- 律师上传自定义模板的 UI(模板来自仓库文件)。
- 确认后修正回路(律师跳过→重新粘贴案情兜底; Word 内自改)。
- 非 assistant 模式的 docx 生成。

## 开放问题

无 — 5+1 项澄清全部确认, 设计 8 节逐节过完。

## 执行记录(2026-09-23, feat/docx-generation)

### 提交链

c67142d 计划 → 9c52504+1c1e8bb(T1 模板资产) → 4bb7ac6+f0e360e(T2 doc_templates) → c2c2f30+7be036c(T3 generate_docx 工具) → 9e9e556+b277814(T4 图内链路) → 2159c50(T5 API 层) → fce92cc+a90ed5d(T6 前端) → 本提交(T7 收官)。7 任务全部按 subagent-driven-development 实跑+任务审查,5 轮修复回环(T1/T2/T3/T4/M-1)。

### 落地明细

- **模板资产**: data/doc_templates/complaint/ 三件套(source/fields.yaml/template),74 字段(54 text/17 choice/3 date,13 个 `_merge_into` 融合型);scripts/build_docx_template.py 可重建,anchor 未命中构建期报错。
- **配置层**: doc_templates.py(load_fields/template_path/validate_template/template_available),YAML↔docx 标签双向对账,模板缺失整体降级 warning 不阻断(planner 末步随之消失,回退纯咨询)。
- **工具层**: generate_docx(fields_json, doc_type, filename) — docxtpl 线程池渲染,文本缺→"待补充"、勾选缺→全☐,文件名清洗防穿越,输出 DOCX_OUTPUT_DIR(默认 ./docx_outputs)。
- **图内链路**: state 三键(doc_fields/docx_path/docx_confirmed)+ _STATE_KEYS 透传;planner AS 后缀改"留法条检索、去类案检索"(D6),模板可用时末步固定 generate_docx;_extract_doc_fields flash 动态 74 键 schema 一次抽取,失败 retry 一次,再失败 failed 不出文书;executor docx_confirm interrupt(field_preview 74 行载荷,选项 确认/跳过),确认后 tool_calls 直注执行;merge_node 写 docx_generated 落库。抽离失败 replan 时守卫防绕过 HITL 渲染空白文书。
- **API 层**: docx_confirm 入两处归一化集合;SSE 新帧 `docx_done`(data {"path":...},GET 流+resume 流两处循环);GET /sessions/{sid}/docx/latest(FileResponse+DOCX_OUTPUT_DIR abspath 白名单+404 三态: 无事件/文件已删/路径越界);QueryResponse.docx_path;dialogue aggregate 新键 `docx`({path,filled,pending}|null)。对话日志新事件 docx_confirm(question/chosen/filled/pending/critical_missing)+ docx_generated(docx_path/filled/pending)。
- **前端**: store 三键+resetTurn 清理;InterruptPanel docx_confirm 分支(标签/跳过值/字段预览表两列+critical 缺失 amber 警示);DocxGenModal 全局生成中弹窗;DocxDoneToast 左上角完成通知(内嵌下载,watch 起算 8s 自消);ChatView/SessionDrawer 下载入口(后者用 detail.session_id 防历史会话串档);App 接线(resume 确认开弹窗,docx_done 收弹窗开 toast,流级 catch 兜底关弹窗)。

### 测试结果

全量回归 155 passed / 0 failed / 0 skipped(真实 PG,97s);test_docx_generation.py 26 用例(模板 4+配置 2+工具 6+图内 5+retry/守卫 2+API 6+聚合同步)。vite build 2.10s 零错误(js 415.48 kB gzip 151.30)。

### 偏差存档(实现与 spec 的出入)

1. **字段二次抽取**(架构性,存档不改): LangGraph interrupt() 在 resume 时从头重执行 executor 节点,_extract_doc_fields 会跑两次——预览载荷与最终渲染可能来自两次独立抽取,LLM 输出不确定时字段值有轻微出入概率。spec §3"不二次抽取"在不改图拓扑前提下无法达成(D7 已锁定工具层镜像方案),风险=预览与产物个别字段不同,兜底=律师下载后 Word 内自改。
2. **validate_template 返回 Tuple[bool, List[str]]** 而非计划 Interfaces 的 List[str](审查 I-1 裁定 Interfaces 为准后统一为 tuple 形态,调用方全同步)。
3. **`_merge_into` 13 字段**: 值进 f_ctx 参与渲染(缺→"待补充")但跳过 filled/pending 计数——与 D4"待补充"语义对齐(T3 I-1 修复,原实现完全丢弃值是错的)。
4. **模板 anchor 调整**(T1 修复): 5 个 choice 项 YAML 补手写 c 键,引入 `~` 跳段占位与文本模式 anchor(有/无 跨 run 可见文本定位),多段覆盖用尾段整段重写。
5. **owner 12 个委托权限勾选恒 ☐**: 表单无对应案情字段驱动(表格第一段"受托人权限"□____),填空文本框亦无字段;留待律师 Word 内自勾。
6. **docx_done 帧设计**: 事件从 merge 节点 state 更新流(updates)派生而非独立总线,与现有 SSE 架构一致。
7. **前端三处小偏差**(T6,审查认定合理): SessionDrawer 下载用 detail.session_id;resume catch 流级失败兜底关弹窗;DocxDoneToast 自消定时器从 onMounted 改 watch 起算(原实现 8s 自消失效,M-1 修复 a90ed5d)。
8. **跳过路径 state 残留**(终审 SF-1 修复): 跳过路径仅落 docx_confirmed=True,不落 doc_fields——同轮 replanner 再排 generate_docx 步时,直调分支 confirmed_fields 为空被 I-2 空守卫拦下按跳过处理,不会静默渲染;ingest 重置兜底续问场景。(修复前该 return 同时落 doc_fields,会导致直调分支绕过守卫不经 HITL 静默渲染——已按终审裁定去掉。)

### 收官后遗留 Minor(ledger 存档,不阻断)

T1: 段落重建丢 tab/br、数字字符引用、退出码 1 vs 2;T2: YAML 畸形无 try、warning 不缓存、标签空格敏感;T3: report 措辞、默认文件名前缀;T4: 二次抽取(=偏差1)、跳过路径 state 残留(=偏差8);T5: docx_done 帧无独立集成测试(图内+前端联调覆盖)、/home 端点清单未加新路由、白名单 normcase/CWD 相对路径运维项。

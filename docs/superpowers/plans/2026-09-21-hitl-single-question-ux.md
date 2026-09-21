# HITL 单问题选择题化 + 思考框整合 + 会话 id 时间格式

日期: 2026-09-21 | 分支: fix/boundary-audit-p0p1 | 状态: 执行中

## 需求来源

用户 6 项:
1. HITL 拆分为单个问题(不再一轮打包多问), 提示词改 JSON 输出带推荐选项(A/B/C + 其他用户输入), 前端呈现单问题选择题
2. 最终输出为结果; 其他内容(思考/调用过程)全部进思考框
3. 用户每一次选择有明确记录与展示(问题 + 用户选择)
4. 运行中动画; 会话 id 保持 模式-时间-编号 格式
5. 每次函数调用/选择都详细标明, 防长时间加载误以为暂停
6. 同会话 id 上下文持续对话 —— **仅方案, 不实施**(见「后续方案」章)

## 已确认决策

- clarify 每轮只问 1 个问题; `max_clarify_rounds` 默认 3 → **5**
- 思考框 = **合并为单一折叠框**(状态/计划/工具调用/CoT 分小节), 运行中展开带动画, done 自动折叠; 最终回答独占聊天区
- 会话 id 新格式 `AT-20260921-213045-001`(日期-HHMMSS-三位序号), 落在既有 `_SESSION_ID_OLD_RE` 内 → 校验零改动; uuid 格式保留兼容存量
- clarify answer 原样透传(点选项发 label 文本, 点其他发用户输入原文), normalize_resume 不改

## 现状探查结论(执行依据)

- `element_assess` 生成 ≤3 问(`ElementQuestion{key,question}` 无选项), `ask_element` 把 pending 问题 join 成一句发单个 interrupt(`LangGraph_lawApp.py:630-642`); risk/pdf/degrade/budget 已带 `options:[{value,label}]` 且前端已有单选渲染
- 前端 ThinkingPanel/PlanPanel/ToolTimeline/StatusBar 四独立面板; `progress`/`prompts_record`/`final_prompts` 事件后端发但前端没消费; `clarify_history` 不进任何 API 响应; messages 只有 `{role,text,collapsed,done}`
- 会话 id 现为 `AT-<uuid12>`(`utils.py:27-38`); M6 测试(test_boundary_medium.py:431-470)守卫 uuid 断言, 随规格变更修改(偏差存档, 同 M8 先例)
- LLM 结构化统一 `PromptTemplate | _structured(Schema)`(json_mode)

## 任务分解

### Task 1 — 后端: clarify 单问题 + 选项化 [需求 1/3]

- [ ] prompts.py `ELEMENT_ASSESS_PROMPT`: 「一次最多打包 2~3 个要素」→「每轮只生成 1 个反问(最关键 missing 要素)」+ 新增「生成 2~3 个具体互斥的推荐选项」; JSON 契约 `questions:[{key,question,options:[str]}]`(options 可空=退化纯文本)
- [ ] prompts.py `MID_CLARIFY_PROMPT` 同步加 options; `MidClarifySchema.options: List[str] = []`
- [ ] state.py `ElementQuestion.options: List[str] = []`; `ClarifyExchange.options: List[str] = []`
- [ ] `ask_element_node`: 只取 `pending_questions[0]`; payload 加 `options:[{value:"A",label:...}]` + `allow_other:true`(options 空时不带该键, 兼容 `_FakeVerdict`)
- [ ] `mid_clarify_node` payload 同样加 options/allow_other
- [ ] ask_element/mid_clarify resume: 本轮问题选项记进 `clarify_history`
- [ ] config.py `max_clarify_rounds: int = 5`
- [ ] 需求 3 后端侧: `build_response()` + `GET /sessions/{sid}` detail 增加 `clarify_history`(model.py 加 Optional 字段)

### Task 2 — 后端: 会话 id 模式-时间-编号 [需求 4]

- [ ] `new_session_id` 改 `{prefix}-%Y%m%d-%H%M%S-{seq:03d}`, 进程内锁+计数防同秒撞号; docstring 注明多 worker 残余风险(当前单 worker 部署)
- [ ] uuid 正则保留(存量会话可续聊); 新格式落 `_SESSION_ID_OLD_RE` 内, 校验零改动
- [ ] M6 测试断言随规格修改(偏差存档)

### Task 3 — 前端: 思考框整合 + 运行动画 + 步骤进度 [需求 2/5]

- [ ] 新组件 `ThinkingBox.vue` 替换 ThinkingPanel/PlanPanel/ToolTimeline/StatusBar; 单一 `<details>` 折叠框分小节: 运行状态(spinner+阶段文案+切换 pulse 动画)/执行进度(progress 事件 → ✓/◐/○ 步骤清单, 当前步 spinner)/计划/工具调用(带 spinner→落结果打勾)/CoT; 运行中默认展开, done 自动折叠
- [ ] App.vue `handleStreamEvent` 补 `progress`/`prompts_record`/`final_prompts` 分支; store.js 加 `steps`/`promptsLog`
- [ ] 最终回答仍走聊天气泡; 思考框只装过程内容

### Task 4 — 前端: 单问题选择题 + HITL 问答记录 [需求 1/3]

- [ ] InterruptPanel.vue: clarify/mid_clarify 且 payload 带 options → 问题 + A/B/C 字母徽标单选 + 「其他(请输入)」(选中出现文本框); 选选项 resume 发 label, 其他发原文; 无 options 退化现行文本框
- [ ] messages 加可选 `kind`: interrupt → push `{role:'assistant',kind:'hitl_question',text,options}`; 作答(含确认类) → push `{role:'user',kind:'hitl_answer',text,question}`
- [ ] ChatView: hitl_question 渲染问题卡(选项列表), hitl_answer 用户气泡头部标「回答: 原问题摘要」
- [ ] HistorySidebar 会话回看展示 `clarify_history`

### Task 5 — 需求 6 方案(仅文档)

- [ ] 多轮上下文方案章: chat_digest prompt 变量 / 新案 vs 追问判定 / langgraph store 要素档案, 各列改动面/风险/顺序(见「后续方案」)

## 约束(沿用 Global Constraints)

- 解释器 `F:/Anaconda_env/lawApp_langGraph/python.exe`; pytest `-q --timeout=120 --timeout-method=thread -rs`; 超时整文件记 SKIPPED
- 回归证据落 `tests_ipynb/09_hitl_ux_regression.ipynb`(run_all --save)
- interrupt `type` 字符串不改; .env 不进提交; commit 中文 `C:` 前缀
- 存量测试只新增不改(M6/smoke 随规格变更为用户决策偏差, 存档)
- 前端验证 `npx vite build`

## 后续方案: 同会话 id 上下文持续对话(需求 6, 仅思考题)

**现状**: 同 session_id = 同 checkpoint 线程, `messages` 跨轮保留(add_messages), 但 ingest 每轮 RESET case_elements/clarify_history/user_supplements, 且 messages **从不进任何 prompt** — LLM 看不到上一轮问答, 每次提问都是全新案件。

**方案 a — chat_digest 注入(最小改动)**:
- ingest 保留近 N 轮 messages, 压成摘要串(每轮「Q:.../A:...」截断, 总长 ~800 字), 存 `state.chat_digest`
- prompts.py 全部 prompt 模板加 `{chat_digest}` 占位(空则"无")
- 改动面: state.py + ingest_node + prompts.py 变量表; 风险低; 缺点: 摘要截断可能丢关键细节, token 预算 +200~400/轮

**方案 b — 新案 vs 追问判定(结构性)**:
- ingest 前置 LLM 分类(flash, `{"is_followup": bool, "reason": str}`): 追问轮不 RESET case_elements/clarify_history/user_supplements, 只追加; 新案轮照旧 RESET
- 追问轮 element_assess 拿到上轮要素档案直接深化, 澄清大幅减少
- 改动面: ingest_node + 新 schema + 路由; 风险: 要素陈旧(用户情况变了但档案没更新 → element_assess 仍可改写, 需 prompt 强调"以最新问答为准"); 分类错误时兜底(追问误判新案=丢上下文但可用, 新案误判追问=要素污染, 需保守阈值)

**方案 c — langgraph store 要素档案(跨进程持久)**:
- store.put(namespace=("case", sid), ...) 存要素档案 + 摘要; 图各节点 store.get 复用; 与 checkpoint 无关, 服务重启也不丢
- 改动面最大, 收益: 与 checkpointer 后端解耦(InMemory 降级时也保留)

**建议顺序**: a(立即) → b(验证追问体验后) → c(多 worker/持久化需求出现时)。

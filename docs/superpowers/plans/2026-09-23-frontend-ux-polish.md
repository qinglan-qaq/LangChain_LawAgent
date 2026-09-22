# 前端 UX 完善 + 会话 id 日计数 — 实施计划

- 日期：2026-09-23
- 分支：`feat/frontend-ux-polish`
- 执行范围：Task 1~5 实施；Task 6 思考题选定方案 c，仅存档不实施

## 已确认决策

- 会话编号改为**按天计数、零点重置**；跨重启用 PG 恢复基数（PG 掉线降级进程计数，HHMMSS 段兜底不撞号）
- 切换模式**彻底新建**会话（清消息 + 新会话号；旧会话留侧边栏抽屉回看）
- Task 6 采用**方案 c**（事件流表 + 聚合 API/视图），存档待后续需求

## 探查结论

- 前端 Vue3 + Vite + Tailwind v4 + motion-v，无组件库、无路由无 pinia（自写 store.js + localStorage 按模式分键）；全项目 0 处 id/data-testid
- 侧边栏 HistorySidebar：会话列表 + 内联展开详情，无「新建会话」入口
- ModeSwitch.pick() 切模式后 loadSession() 恢复旧 sid，messages 不清
- new_session_id（FastAPI/utils.py）按秒键重置计数，格式 `AT-YYYYMMDD-HHMMSS-NNN`；改日计数格式不变 → 存量测试（M6 `\d{3}`、唯一性）零破坏
- sessions 表有 created_at（首建时间，续聊不刷新）→ 可恢复「当天已建会话数」

---

## Task 1 — 全局 id 标签（id 契约表）

| 元素 | id |
|---|---|
| HistorySidebar 根 aside | `history` |
| 会话列表 ul | `history-list`，每项按钮 `history-item-{session_id}` |
| 新建会话按钮 | `btn-new-session` |
| DocComposer 当前激活输入框 | `input` |
| ChatView 根容器 | `chat` |
| 用户消息气泡（含 hitl_answer） | `ask-{n}`（n = messages 索引） |
| 助手终答气泡（done 且普通回答） | `result_llm-{n}`（done 后由 answer 切换） |
| 助手流式/其他气泡 | `answer-{n}` |
| HITL 问题卡（ChatView 内） | `choose-{n}` |
| InterruptPanel 根 | `choose`，选项按钮 `choose-opt-{value}` |
| ThinkingBox 根 details | `cot` |
| ElementPanel 根 | `elements` |
| App 头部会话号显示 | `session-id` |
| ModeSwitch 根 | `mode-switch` |

## Task 2 — 会话编号按天计数、零点重置（后端）

- `FastAPI/utils.py` `_SID_COUNTER` 键从秒级改**日级 `%Y%m%d`**；同日递增、日期变重置
- 新增 `_seed_daily_seq(prefix)`：每 (prefix, 日) 首次用 db.py `build_dsn()` 短连接查 `SELECT COUNT(*) FROM sessions WHERE session_id LIKE '{prefix}-%' AND created_at >= 当天零点` 为基数；异常降级进程计数；一天一前缀只查一次
- 格式保持 `AT-YYYYMMDD-HHMMSS-NNN`（HHMMSS 保时间信息 + 兜底唯一性）
- 新增 `tests/test_session_daily_counter.py`：日键变更重置 / 同日连续递增 / AT-AS 独立 / PG 降级（monkeypatch 抛异常仍正常发号）

## Task 3 — 侧边栏「新建会话」按钮

- HistorySidebar 顶部：lucide Plus + 「新建会话」，样式贴现有列表语言，id=`btn-new-session`
- store.js 新增 `newSession()`：清 sessionId + 当前模式 localStorage 键 + messages + resetTurn()

## Task 4 — 会话预览抽屉 SessionDrawer.vue

- Teleport to body，右侧滑出（`fixed right-0 h-full w-[420px] bg-white border-l shadow-xl`）+ 遮罩点击/Esc/X 关闭，motion-v `x:40→0`
- 内容分节（details/summary 折叠，ThinkingBox 同款）：头部 sid(font-mono)/终答/澄清记录/案件要素/引用(CitationList 嵌入)/工具调用
- HistorySidebar 内联详情块删除，改挂 `<SessionDrawer :detail :open>`

## Task 5 — 切换模式自动新建会话

- ModeSwitch.pick()：busy abort → 切 mode → `newSession()`（不再 loadSession 恢复旧 sid）
- 首次页面加载仍 loadSession()（刷新 ≠ 切换）

---

## Task 6 — 思考题：JSON 会话历史落 PG（仅存档，选定方案 c，不实施）

> 目标：每轮 问题/选项/用户选择/自由输入/最终结果 以 JSON 存 PG，查询友好可回溯。

**现状差距**：langgraph checkpoint 虽存全部图状态（含 clarify_history），但埋在序列化快照里需解图读取；sessions.meta JSONB 只存零星元数据。缺一张人类可读、按轮组织的会话日志表。

**方案 c（选定）= 事件流表 + 聚合 API/视图**：

1. 新表：
   ```sql
   CREATE TABLE session_dialogue_events (
       session_id  TEXT NOT NULL,
       seq         INT NOT NULL,
       event_type  TEXT NOT NULL,  -- round_question / round_answer / interrupt_confirm / final_answer
       payload     JSONB DEFAULT '{}'::jsonb,
       created_at  TIMESTAMPTZ DEFAULT NOW(),
       PRIMARY KEY (session_id, seq)
   );
   ```
2. 写入点（与图执行同步追加，幂等序号、无覆写竞争）：
   - `ask_element_node`/`mid_clarify_node` 发 interrupt 时 → `round_question`（问题 + 选项）
   - resume 消费时 → `round_answer`（选择/自由输入）
   - 风险确认类 interrupt → `interrupt_confirm`
   - finalize → `final_answer`（终答 + 引用 + token）
3. 聚合层：`GET /sessions/{sid}/dialogue`（或 PG 视图）拼成会话级文档，供前端抽屉直接消费：
   ```json
   {"rounds": [{"round": 2, "question": "是否有未成年子女？",
     "options": ["有一个孩子", "两个孩子", "无子女"],
     "selected": "有一个孩子", "selected_type": "option", "free_text": null,
     "element_keys": ["children"], "ts": "..."}],
    "final": {"answer": "...", "citations": [], "tokens": {}}}
   ```
4. 风险与注意：写日志失败降级 warning 不阻断主流程（`_safe_audit` 先例）；DDL 进 db.py 启动建表段；与 trace 表分工 —— trace 管观测（token/耗时），本表管业务回溯（问答内容）

## 测试与验证

- 后端：tests/test_session_daily_counter.py 新增 + 快速批回归（test_mcp 慢档单跑）；存量 M6/HITL 断言格式不变预计全绿
- 前端：`npx vite build` + 人工冒烟（id 逐个 DevTools 命中、新建按钮、抽屉开合/Esc、切模式清空、编号递增）
- 回归册：不新开 ipynb

## 执行方式

子代理两批并行（后端批 utils.py+新测试 / 前端批 5 组件+store.js，文件不相交）→ orchestrator 审查 + vite build + pytest + 分批提交 → 人工冒烟 → 合并 main 推送。

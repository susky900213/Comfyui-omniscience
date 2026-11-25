# ComfyUI Omniscience 编程插件设计文档

本文档描述 ComfyUI Omniscience 侧栏编程助手的独立架构设计。插件运行在 ComfyUI
进程内，由前端扩展、HTTP 路由、持久化层与 Agent 运行时共同组成。设计重点是明确
初始化、通信、任务与会话生命周期、事件回灌、工具执行、审批和取消机制之间的边界，
使交互体验、并发正确性与安全约束可以分别演进。

文档同时标注每一层的**当前实现**与**目标设计**。当前实现以模块和符号定位，不依赖
易随代码漂移的行号；目标设计说明拆分动机、职责与验收条件，作为后续迭代依据。

## 范围

- ComfyUI 前端扩展：`web/omniscience.js`、`web/omniscience.css`
- ComfyUI 后端路由与 Agent 运行时：`omniscience_server.py`
- 持久化与上下文原语：`omniscience_core.py`
- MCP 集成：`omniscience_mcp.py`
- 浏览器工具：`omniscience_browser.py`
- 下游边界：OpenAI-compatible LLM 接口、Playwright、MCP server、Shadow Git

本文档只描述 ComfyUI Omniscience 自身的模块、协议与运行时，不依赖其他编程助手或
Agent SDK 的实现。OpenAI-compatible 接口、Playwright、MCP server 与 Shadow Git
仅作为可替换的下游能力出现。

## 总体链路

```text
ComfyUI 前端
  │
  └─ __init__.py: register_routes(PromptServer.instance) + WEB_DIRECTORY
       │
       ├─ aiohttp 路由层（/omniscience/*）
       └─ web/omniscience.js: app.registerExtension()
            │
            ├─ 侧栏面板 UI（任务 / 对话 / 检查点 / 文件 / 工作流 / MCP / Skill / 设置）
            │
            └─ ApiClient（fetch）
                 │
                 └─ OmniscienceController（目标：收敛当前三个 Agent 类的入口）
                      │
                      ├─ TaskStartCoordinator      新建任务、构建 session input
                      ├─ TaskControlCoordinator    取消、清理、切换历史任务
                      ├─ FollowupCoordinator       审批回答、follow-up、队列、resume
                      ├─ SessionEventCoordinator   事件校验、翻译、turn 状态
                      ├─ MessageCoordinator        消息状态、保存、向 UI 转发
                      ├─ SessionLifecycle          session start / active ref / stop
                      └─ CheckpointCoordinator     快照、diff、恢复
                           │
                           └─ AgentRuntime（_run_loop：模型 → 工具 → 结果回灌）
                                │
                                ├─ Local host: 当前工作区（Workspace、终端、Playwright、MCP）
                                └─ LLM host:   OpenAI-compatible /chat/completions
```

当前主任务 Agent 同时承担了 Controller、Session 生命周期、Agent 循环、工具分发和
检查点五种职责。第 4 节按变化频率与故障边界拆分：路由协议、任务控制、会话生命周期、
模型循环、消息持久化和检查点分别演进，避免一次修改牵动整条执行链。

---

## 1. 插件注册与初始化

入口文件：

```text
__init__.py
```

后端激活阶段：

1. 声明 `WEB_DIRECTORY = "./web"`，由 ComfyUI 逐文件加载 `web/omniscience.js`。
2. `from server import PromptServer` 后调用 `register_routes(PromptServer.instance)`。
3. 注册失败仅打印告警，不阻断 ComfyUI 启动。
4. `NODE_CLASS_MAPPINGS` 为空——本插件是纯"后端路由 + 前端扩展"，不提供节点。

前端初始化在 `omniscience.js` 的 `registerExtension.setup()` 中完成：

- `ensureStyle()` 动态注入 CSS；
- `installLauncher()` 将 `AI 助手` 按钮注册到左侧竖向菜单栏；
- `buildPanel()` 构建侧栏 DOM；
- 启动事件轮询定时器。

### Host 抽象

ComfyUI 宿主依赖目前直接表现为 `PromptServer.instance` 与前端 `app` 对象。目标设计是
把二者收敛为两个薄 adapter，其余代码不直接引用：

- 后端 `ComfyUIHost`：画布序列化接口、custom_nodes 软链目录、输出目录；
- 前端 `ComfyUIApp`：`app.canvas` 序列化、toast 通知。

当前软链与工作流同步逻辑已分散在主任务 Agent 的 `_ensure_comfyui_link`
和 `web/omniscience.js` 中，是收敛的第一批候选。

---

## 2. 前后端通信通道

### 通信模型取舍

侧栏需要同时满足两类需求：用户提交动作后立即得到确认，多步 Agent 执行期间持续看到
文本与工具进度。仅使用阻塞请求会把 HTTP 连接生命周期与任务生命周期绑定；仅使用全量
快照则会增加带宽并放大刷新时的状态覆盖风险。因此目标模型区分动作、增量事件与完整
状态三条通道。

### 当前实现

```text
omniscience.js                          omniscience_server.py
  sendMessage()  ──POST /tasks/{id}/messages──▶  chat()：阻塞至整轮 agent 完成
  pollEvents()   ──GET /tasks/{id}/events?after=▶  events 表增量返回（1.5s 轮询）
  refreshTask()  ──GET /tasks/{id}────────────▶  task + messages 全量快照
```

问题：

1. `POST messages` 阻塞到整轮结束，模型多步循环期间 UI 只靠轮询感知进度；
2. 浏览器刷新或切换页面会取消该 HTTP 请求，aiohttp handler 收到
   `CancelledError`，虽然状态由 `finally` 兜底，但该轮响应内容丢失；
3. 事件 payload（`assistant_text`、`tool_result` 已带内容）在前端 `applyEvents`
   只被用来翻转 busy 状态，随后整仓重拉消息——事件流退化为心跳。

### 目标设计：请求与推送解耦

保留 REST 的简单性，同时建立“partial + state 双通道”模型：

| 通道 | 端点 | 目的 | 特性 |
| --- | --- | --- | --- |
| 动作通道 | `POST /tasks/{id}/messages` 等 | 提交用户输入/审批/取消 | **立即返回**（202 + 事件游标），不再阻塞整轮 |
| partial 通道 | `GET /tasks/{id}/events?after=`（SSE 化） | 低延迟显示流式文本与工具进度 | fire-and-forget、按 seq 收敛；轮询作为 SSE 不可用时的降级 |
| state 通道 | `GET /tasks/{id}` | 完整 task/messages/history/settings 快照 | 断线重连、页面刷新后的重新同步 |

关键原则：

- **消息发送不阻塞 agent turn**：agent 运行在后端独立任务中，HTTP 连接断开不影响运行；
- **持久事件日志即 partial 流**：`TaskStore.append_event`
  已经是带单调游标的事件表，天然满足"按 seq/epoch 收敛、丢弃 stale"的要求，只需前端
  真正消费 payload 并按 `task_id + seq` 合并；
- **state 快照负责最终一致**：任何 seq 缺口或客户端重启后，以 `GET /tasks/{id}` 收敛。

---

## 3. 事件模型与消息回灌

本项目的回路（已存在，需补齐语义）：

```text
Agent 循环阶段完成
  ↓
_event(task_id, kind, payload)
  ↓
TaskStore.append_event（SQLite, WAL）
  ↓
前端 pollEvents / SSE
  ↓
applyEvents：按 kind 更新本地消息副本 + turn 状态
  ↓
必要时 refreshTask 全量收敛
```

目标设计要点：

1. **事件种类对齐语义层**。当前事件 kinds：`task_created/task_started/user_message/
   assistant_text/tool_result/context_compacted/checkpoint_created/task_finished/
   task_failed/task_cancelled/approval_resolved/checkpoint_restored`。需要补充
   `turn_started`、`text_delta`（引入流式后）、`tool_started`（工具开始执行，区别于结果），
   使 UI 能显示"正在执行 write_file…"而不是一步结束后才出现结果。
2. **active session 过滤**。事件 payload 增加 `session_id`（见第 4 节），前端与后端
   都丢弃非当前 session 的迟到事件，防止旧运行覆盖当前任务状态；
3. **前端不做二次翻译**。消息在写入事件时即为最终展示形态；展示所需的模式标记
   剥离与格式化只发生在前端边界，不改变持久化事实。

---

## 4. Controller 与 Coordinator 拆分

任务协调应集中于 `OmniscienceController`，并按职责拆出多个 coordinator。这样路由
只关心协议，控制操作各有独立边界，模型循环不再承担会话与检查点管理。
本项目当前是三个并存的 Agent 类：

| 类 | 位置 | 现状职责 |
| --- | --- | --- |
| 兼容对话 Agent | `omniscience_server.py` | 旧版无状态 `/omniscience/chat` 兼容 |
| 全局对话 Agent | `omniscience_server.py` | 全局对话 + GitHub Skill 安装 |
| 主任务 Agent | `omniscience_server.py` | 主运行时：任务、循环、工具、检查点 |

目标设计（不要求一步到位，允许在单文件内先拆为私有类）：

| 组件 | 主要职责 | 现状来源 |
| --- | --- | --- |
| `OmniscienceController` | 路由 handler 的唯一入口；暴露 `initTask/askResponse/cancelTask/clearTask/showTaskWithId/getState/dispose` | `register_routes` 内的三处单例 |
| `TaskStartCoordinator` | 新建任务：项目目录、workflow 模板、custom_nodes 软链、session input、启动 | `create_task` 与 `chat` 前半 |
| `TaskControlCoordinator` | 取消、清理历史、切换任务 | `cancel_task`、`clear_history` |
| `FollowupCoordinator` | 后续消息：运行中入队 / idle 续跑 / 无 live session 时从 transcript 恢复 | 当前只有"串行排队"，无队列与 resume 分支 |
| `SessionLifecycle` | 每任务一个 session：start、active 引用、subscribe、stop、销毁 | 隐式散布在 `_running_tasks`/`_cancel_requested` |
| `AgentRuntime` | 模型 → 工具 → 结果回灌循环（≤ MAX_AGENT_STEPS） | `_run_loop` |
| `MessageCoordinator` | 消息持久化、上下文压缩触发、事件写出 | `_compact_messages`、`_event` |
| `CheckpointCoordinator` | 写类工具后快照、diff、恢复 | `_checkpoint_after_tool` |
| `InteractionCoordinator` | 工具审批（若恢复审批）、`ask_question` | 当前审批链路为兼容壳，见第 6 节 |

### Session 隔离（最重要的结构性改动）

每个任务需要独立 Session，事件也应按 session id 过滤。本项目当前用
**一把全局 `asyncio.Lock`**（主任务 Agent 的 `_lock`，由 `_run_loop` 持有）
串行化所有任务的 agent 运行：任务 A 编码时，任务 B 的对话完全排队。

目标设计：

```text
SessionLifecycle
  ├─ sessions: dict[task_id, Session]        # 每任务至多一个 live session
  ├─ Session { session_id, task_id, runtime_task, abort, pending, started_at }
  ├─ start(task_id, input) -> Session        # 同 task 已有 session 则先 stop
  ├─ send(task_id, text)                     # running → 入队；idle → 续跑
  ├─ abort(task_id)                          # 见第 7 节 cancel fence
  └─ stop(task_id)                           # 清 active ref → 取消订阅 → 等 stop（带超时）
```

- 不同 task 并行运行（各自持有 runtime task），同 task 内串行；
- LLM 与命令执行的并发上限用信号量控制，而不是全局互斥锁；
- 每个事件、每条消息都携带 `session_id`，为 stale 过滤提供依据。

---

## 5. 新建任务与一轮对话

### 5.1 新任务启动链路（目标）

新任务必须完成初始化并持久化用户消息，再异步启动 Session；HTTP 层不应等待模型
循环结束。目标链路如下：

```text
前端 sendMessage()（新任务先 createTask）
  ↓
POST /omniscience/tasks                       controller.create_task
  ↓
TaskStartCoordinator.init_task()
  ├─ _project_folder_name 校验（防穿越）；重名目录明确拒绝
  ├─ 创建目录 + 写 workflow.json 模板（exclusive-open）
  ├─ 软链到 ComfyUI custom_nodes（_ensure_comfyui_link），失败回滚
  ├─ SQLite 记录 task，写 task_created/task_started 事件
  ├─ 用户消息先入库并发事件，确保 UI 在模型运行前即可见
  └─ SessionLifecycle.start(task_id) → AgentRuntime 以 fire-and-forget 启动
       （HTTP 立即返回 202，进度走事件通道）
```

### 5.2 一轮对话的 Agent 循环

当前实现采用有步数上限的“模型—工具”循环：

```text
_run_loop (≤ MAX_AGENT_STEPS=8)
  ↓
_context_cache / _messages_for_model      # 压缩后的历史
  ↓
_tool_definitions                          # 静态工具 + Plan 过滤 + MCP list_tools
  ↓
_compact_messages                            # 超阈值调摘要模型，失败回退本地压缩
  ↓
_call_model                                  # OpenAI-compatible, aiohttp
  ↓
有 tool_calls？──否──> 置 idle、补链接/工作流、发 task_finished
  │是
  ↓
_process_tool_calls → _execute_tool
  ├─ Workspace: list/read/search/write/apply_patch
  ├─ _run_command: Popen(start_new_session) + BLOCKED_COMMANDS 黑名单
  ├─ browser_*: Playwright（每 task 独立 page）
  ├─ mcp__<server>__<tool>
  └─ get_current_workflow
  ↓
tool-result 以 role:"tool" 消息写 SQLite → 下一次迭代携带完整历史
```

目标增强：

1. **流式模型调用**：payload 加 `"stream": true`，解析
   `delta.content`/`delta.tool_calls`，产出 `text_delta`/`tool_call_started` 事件；
   这也让取消能在模型输出中途生效；
2. **follow-up 队列**：session 运行中收到新消息时入 Core 队列，在下一轮
   `prepareModelRequest` 边界消费，而不是等整轮结束或拒绝输入；
3. **resume**：任务有历史但无 live session 时，直接从 SQLite transcript 继续——
   当前已等价实现（每轮读全量消息），保留即可；
4. **MCP 工具目录缓存**：`list_tools` 不应每步 spawn（见第 8 节）。

### 5.3 文件写入执行路径

**模型只提出编辑意图，写盘永远发生在 host 层**。这条边界让文件结果可校验、可审计，
也避免模型输出被直接当作可信文件内容。本项目
三类写文件入口共走同一条路径：

```text
模型发出 write_file / apply_patch tool call
  ↓
_process_tool_calls
  ├─ schema 校验（参数缺失/类型错误 → 错误 tool result 回灌，不执行）
  ├─ Plan 模式：不在 PLAN_TOOL_NAMES → 直接返回错误 tool result
  ↓
_execute_tool → Workspace.write_file / apply_patch
  ├─ _safe_relative：拒绝绝对路径与 ../ 穿越
  ├─ stays_inside：解析 symlink 后仍在任务目录内
  └─ apply_unified_patch：纯 Python 应用 unified diff
  ↓
成功/失败都构造 tool result → role:"tool" 消息写入 SQLite
  ↓
_checkpoint_after_tool：shadow-git commit 或 tar 快照
  ↓
下一次模型请求携带"编辑结果 + 检查点"继续
```

文件编辑工具围绕三个目标设计：

1. **精确编辑优先于整写。** 长文件的小改动如果要求整文件覆盖，会消耗大量输出 token
   且易截断。工具应优先提供 `path + old_text/new_text`（或 `insert_line`）的精确编辑
   语义，复用 `apply_unified_patch` 的上下文匹配；整写只作为新建文件或明确替换的路径。
   当前 `write_file` 只支持全文覆盖，目标新增精确编辑工具；
2. **apply_patch 采用规范化文法并写进工具描述。** 输入可使用
   `*** Begin Patch / *** Update File: <path> / @@ / -old / +new /
   *** End Patch`，支持 Add/Delete/Move 与 `*** Move to:`，基于上下文匹配、
   不带行号，并在描述中写明"`@@` 用于消歧重复代码块、不要使用行号"。这比裸
   unified diff 对模型更友好，因为行号偏移与 hunk 计数错误是主要失败源。目标：
   把 `apply_unified_patch` 的输入描述升级为受控子集文法；
3. **写类工具不可自动重试。** 编辑是有状态操作，重试会重复插入。本项目禁止后端
   静默重试：失败必须把原因（哪一段上下文没匹配上）作为 tool result 回灌，让模型
   重读文件后再改。

失败路径语义：拒绝或校验失败时**不写盘**，失败原因进入历史，
模型必须基于"修改没有发生"重新选择方案。

### 5.4 命令执行路径

当前实现（`_run_command`）：`Popen(start_new_session=True)` 独立进程组 +
`BLOCKED_COMMANDS` 正则黑名单，取消时 SIGTERM 杀进程组。

```text
模型发出 run_commands tool call
  ↓
schema 校验 → ToolPolicy 检查（第 6 节）
  ↓
BLOCKED_COMMANDS 命中 → 不执行，返回错误 tool result（硬拦截）
  ↓
Popen(cwd=任务目录, start_new_session=True) → 登记 _processes[task_id]
  ↓
输出（截断）+ 退出码 → tool result 写 SQLite → tool_result 事件
  ↓
取消路径：cancel fence（第 7 节）→ os.killpg(SIGTERM) → 进程组退出
```

命令执行工具围绕四个目标设计：

1. **非交互约束写进工具描述。** 描述应明确要求命令使用无交互 flag 或参数
   （如 `git --no-pager`、`--non-interactive`），避免模型误跑 pager/REPL 导致挂死；
2. **双层防护：提示词第一道，command-guard 硬背托。** Plan 模式可以保留
   `run_commands` 做只读调查，但执行前必须硬拦截：先遮蔽引号内容、heredoc 体、
   转义与注释（防误报），再按 shell 分隔符切分，检查每段**首个命令
   词**是否命中写操作集合（rm/mv/cp/touch/chmod/patch/…，含 PowerShell 别名
   remove-item/ri 等）或 git 变更子命令（add/am/apply/…），并拒绝重定向到
   `/tmp` 之外的文件；命中返回 tool error 而不执行。这是常见编辑手段的
   黑名单，不是 shell 解释器。当前 `BLOCKED_COMMANDS` 对原始字符串跑正则，
   `bash -c`、管道、引号内文本既易漏也易误杀——目标采用"遮蔽 → 切分 → 首词匹配"
   三步法，Plan 模式改用该策略；
3. **输出中段截断，保头保尾。** 超长命令输出保留开头与结尾，因为测试失败信息
   通常在结尾；描述中写明截断行为，需要特定段落时先过滤输出。当前输出截断应对齐为
   中段截断；
4. **环境相关约束写进工具描述。** 描述应反映实际执行 shell 的引号规则、串联操作符
   与挂载路径，并在每次组装模型请求时重新求值。本项目固定 POSIX shell，场景简单，
   但"shell 方言、cwd、超时都写在该工具自己的 description 里，而不是只写
   system prompt"这一点直接采用。

### 5.5 浏览器访问路径

`BrowserManager`（`omniscience_browser.py`）已实现按 task 隔离的 Playwright
会话，五个 `browser_*` 工具走与其他工具相同的校验 → 执行 → 结果回灌路径：

```text
模型发出 browser_navigate / content / screenshot / click / fill
  ↓
_execute_tool → BrowserManager
  ├─ 未安装 Playwright → BrowserUnavailable 转错误 tool result（提示安装，不抛异常）
  ├─ navigate：仅允许 http/https
  ├─ content：body 或 selector inner_text，截断 10 万字符
  ├─ screenshot：full_page 保存到任务工作区 → tool result 给文件路径
  └─ per-task page（_pages[task_id]）；任务取消/清理时 close(task_id)
```

"截图落工作区、结果给路径"使模型在拿不到图片本身时，可以用路径 + `content`
文本继续验证。目标增强：
1. 跨任务浏览器全局 `asyncio.Lock` 改 per-task 锁（第 8 节）；
2. `content` 超限时在结果尾部附截断标记与截断位置，提示模型改用 selector 缩小
   范围，而不是重复拉取整页；
3. 工具描述补充 ComfyUI 场景闭环："开发节点后可用浏览器打开 ComfyUI、导入任务
   目录的 `workflow.json`、执行并截图验证节点图"——把**如何验证**写进描述，
   而不是等模型自己发明验证方式。

### 5.6 工具结果的统一回灌格式

工具结果需要统一结构，使任意成功或失败都能按相同顺序进入 runtime state，并在下一轮
请求中携带完整历史。本项目目标：把 `_execute_tool` 所有分支的返回值收敛为统一 schema，
再序列化为 `role:"tool"` 消息：

```json
{ "tool": "run_commands", "success": false,
  "output": "exit code 1\n…（中段截断）… FAILED tests/test_x.py" }
```

约束：

- 失败必须是**内容化的 tool result**（模型读到原因后能自愈），不允许只写事件
  不写消息，也不允许抛异常中断整轮；
- 每个 tool result 与触发它的 assistant tool_calls 消息保持相邻；上下文压缩时
  不拆散 assistant/tool 对（`ContextManager._split_messages` 已保证）；
- 回灌顺序固定为：`工具执行完成 → tool-result message → 写入消息 state
  → 发事件 → 下一次模型请求携带完整历史`。测试失败时，失败输出进入历史，模型
  据此再读取失败位置、修改文件、重跑测试——这就是"编码闭环"的全部机制。

### 5.7 custom_nodes 项目接管（`omniscience/agent/projects.py`）

任务历史不再只有工作区项目：`custom_node_projects()` 直接枚举设置里 ComfyUI 目录的
`custom_nodes`，让既有的节点仓库也能成为任务节点。三条硬规则：

- **软链接一律不列**：`custom_nodes/<name> -> 工作区/<name>` 是本插件为工作区项目建立的
  链接，其任务已经在历史里，再列一次就是重复；因此只收真正的目录，同时跳过隐藏目录、
  `__pycache__`、非目录条目，按 mtime 倒序并受 `MAX_CUSTOM_NODE_PROJECTS` 限制；
- **已被任务覆盖的目录不列**：以 `Path.resolve()` 比对 `tasks.workspace`，接管过一次就
  不再重复出现；`adopt_project()` 对同一路径幂等，直接返回既有任务；
- **接管不写任何文件**：不建目录、不建软链（`_comfyui_link_path` 在 `link == project` 时
  返回 `None` 已经覆盖这种自指情况）、不生成 `workflow.json`。元数据记
  `project_source = "comfyui_custom_nodes"`，`_ensure_task_workflow` 见到该标记只回读已有
  工作流，任务完成时也不会补写——别人的仓库由用户自己管。

前端把 `/omniscience/tasks` 与 `/omniscience/custom-nodes` 合并成一个节点流（按时间倒序），未接管的
条目用虚线卡片 + `ComfyUI` 角标 + "待接管"显示，点击即 `POST /omniscience/custom-nodes/adopt`
并直接打开该任务。`create_task` 的同名冲突提示改为引导点击接管，而不是只让用户改名。

安全边界随之放宽：接管后 `Workspace` 的根就是 `ComfyUI/custom_nodes/<name>`，文件与命令
工具在该目录内的读写是预期行为，路径仍受 `Workspace.resolve()` 约束不得越出该目录。

接管同时是一道安全门禁：`POST /omniscience/custom-nodes/adopt` 先扫描再落库，报告为 `blocked`
且请求没有 `acknowledge_security` 时返回 `409` + `security`，此时任务、软链、文件都未产生。
详见 §11.2。

### 5.8 节点安全检测路由（`omniscience/api/routes.py`）

| 路由 | 作用 | 是否可能调用模型 |
| --- | --- | --- |
| `POST /omniscience/custom-nodes/scan` | 检测单个未接管项目，结果写入 `node_scans` | 取决于 `skill_security_mode` |
| `POST /omniscience/custom-nodes/scan-all` | 批量静态检测，单次最多 `MAX_NODE_BATCH_SCANS` 个，返回 `remaining` | 否，强制 `static` |
| `POST /omniscience/custom-nodes/adopt` | 接管门禁 + 建任务，报告随元数据落库 | 取决于 `skill_security_mode` |
| `POST /omniscience/custom-nodes/install` | 暂存、检测并安装 GitHub 仓库或前端上传的节点压缩包；blocked 返回 `409` + `security` | GitHub 下载 + 取决于 `skill_security_mode`；静态上传不调用模型 |
| `POST /omniscience/tasks/{id}/scan` | 复检已接管任务的项目，并回写 `metadata.security` | 取决于 `skill_security_mode` |

扫描是同步的文件遍历，路由用 `asyncio.to_thread` 包裹，避免阻塞 aiohttp 事件循环。

---

## 6. 审批与工具策略

### 审批回路

工具审批需要形成完整回路：runtime 请求 approval → UI 展示 → `askResponse` →
协调层解决 pending approval → 工具继续/拒绝；拒绝结果回灌模型。

### 当前实现

本项目有意弱化逐项审批（README"Act 模式下……不再逐项弹出授权"）：

- `_requires_confirmation`（L1070、L2354）恒为 False；
- `TaskStore.create_approval` 生产路径零调用，但 `/omniscience/approvals` 路由、
  `confirm()`、旧 pending approval 自动批准（L1908-1913）整条链路仍在；
- 安全边界换成：workspace 沙箱 + `BLOCKED_COMMANDS`（L261）+ Plan 模式工具白名单
  `PLAN_TOOL_NAMES`（L55）。

### 目标设计：policy 层取代 if-else

不恢复逐工具弹窗，但把“什么工具能执行”收敛为声明式策略层，
供本地 host 构造：

```python
ToolPolicy = {
  "plan":  {"deny": [写文件, 命令, mcp:*, browser:*]},   # 现状 PLAN_TOOL_NAMES 的显式化
  "act":   {"deny": BLOCKED_COMMANDS 命中的 run_commands 参数,
            "allow": "*"},
  "auto":  {"approval_required": [可选：跨工作区写入、restore_checkpoint]},
}
```

并明确遗留审批链路的处置：**要么恢复为可用功能**（UI 加审批卡片、`askResponse`
打通），**要么删除** SQLite approvals 表、路由与自动批准逻辑。当前"半死代码 +
旧 pending 被静默批准"是最危险的状态——它让一个从未被 UI 消费的概念保留了改变
执行结果的权力。

Plan/Act 切换当前使用 `PATCH /tasks/{id}/mode`（路由注册处）。保持约束：模式必须进入下一轮
session 配置与工具集合，不能只当 UI 标签。

---

## 7. 取消、清理与历史任务

### 取消：先建 fence，再 abort

取消流程的固定次序：`cancel fence → host.abort() → 过滤 abort 期间迟到事件 →
发布取消后状态`。

当前实现（`cancel_task` L1756）已具备骨架：
`_cancel_requested.add → store.cancel_task → _terminate_process(SIGTERM 进程组) →
runtime.cancel()`，循环内有多个 `_check_cancelled` fence 点（L1722）。

需要修复的竞态：

1. `chat()` 用 `while ... : await asyncio.sleep(0)` 自旋等待旧任务释放槽位
   （L1904-1906）——CPU 忙等，且依赖被取消协程必然退出；改为 `SessionLifecycle`
   的 per-task stop 等待（带超时），同 task 的新消息进队列而非抢槽位；
2. 第二次 `chat()` 可能在第一次运行完全 unwind 前清除取消标记；
   `_cancel_requested.discard` 必须绑定 session 世代：每个 Session 有
   `session_id`（单调 generation），cancel 记录 `(task_id, session_id)`，
   运行时只检查自己 session 的标记，`finally` 只清理自己的标记；
3. `cancel_task` 要求 status ∈ {running, waiting_approval}，与 store 改 idle 之间存在
   no-op 窗口——以 Session 对象是否存在为准，而不是以 DB status 为准；
4. abort 之后到 runtime 真正退出之间产生的事件，打 `session_id` 后由
   SessionEventCoordinator 依 active session 丢弃，确保取消 fence 抑制迟到显示事件。

### 清空历史

`clear_history`（运行中拒绝）保留语义，并补齐以下清理：清 pending approval/question、
重置 translator 与 turn 状态、把完整 state 重新推送（前端 refreshTask 已覆盖）。

### 打开历史任务

前端切换任务时需要 generation fence，防止较早发起的历史加载覆盖较新的选择：
`showTask(taskId)` 记录请求世代，响应回来时若世代已过期则丢弃；后端切换是纯读操作，
无 active session 冲突，主要防护点在前端异步回调。

---

## 8. 工具层与外部 host

工具能力通过 host 抽象注入，Agent 循环只消费统一工具协议，不直接依赖 ComfyUI 宿主对象：

| 能力 | 当前实现 | 目标调整 |
| --- | --- | --- |
| 文件 | `Workspace`（L454）：resolve/list/read/search/write/apply_patch，路径防穿越 + symlink 逃逸检查 | `search_files` 全文件逐行读入；移植 ripgrep 或维护索引 |
| 命令 | `_run_command` Popen + 进程组取消 | 输出事件化：`tool_started` + 分块 `tool_output`；长任务采用 background + 轮询输出 |
| 编辑器/diff | 无 diff preview（ComfyUI 无编辑器宿主） | 以检查点 diff 替代：写后自动快照，UI 提供"查看本次修改" |
| 浏览器 | `BrowserManager`（browser.py:19）：每 task 独立 page，5 个 `browser_*` 工具 | 跨任务全局 `asyncio.Lock` 串行——改为 per-task lock |
| MCP | `MCPManager`（mcp.py:50）：手写 JSON-RPC，stdio + Streamable HTTP | **stdio 每次调用重新 spawn 子进程并 initialize**（L128、L224），且 `_tool_definitions` 每步都 `list_tools`（L1990）→ 每步 spawn 全部 server。目标：每 server 一个长驻连接 + 工具目录缓存（变更时失效），HTTP 复用 `Mcp-Session-Id` |
| Skill | SkillStore + 消息级选择注入 system prompt | 保持现有注入路径，并在请求边界统一组装 |

模型侧边界固定为：**模型只提出工具调用意图**；文件写入、命令执行、审批和 UI 更新
都由后端 host 完成，结果以 tool message 回灌。

---

## 9. Prompt 与上下文组装

每次模型请求由 session config 统一组装 system prompt、项目规则、Skills、模式、mention、
历史和工具定义。本项目对应关系：

| 部分 | 内容 | 当前来源 |
| --- | --- | --- |
| 基础 system prompt | 角色、工作方式、验证要求 | 主任务 Agent 内联提示词 |
| Plan/Act 约束 | 模式允许范围 + 工具白名单 | `PLAN_TOOL_NAMES` L55 |
| Skills | 选中且启用的 Skill 注入 | SkillStore（core L470）+ 任务元数据 |
| 会话历史 | 前面的 assistant/tool 消息 | SQLite messages 表（每轮全量读） |
| 上下文压缩 | ≥80% 阈值时摘要替换早期历史 | `ContextManager`（core L643）+ `_compact_messages` L2019 |
| mention 上下文 | `@file`、画布工作流 | `get_current_workflow` + 前端实时画布同步（js:92） |
| 工具定义 | 名称/描述/JSON schema | `TOOL_DEFINITIONS` L267 + MCP |

目标增强（按依赖关系与收益排序）：

1. **文件 mention 解析**：用户输入 `@src/foo.py` 在后端展开为真实文件内容再入库，
   而不是把 `@` 当普通文本；展开格式使用 `<file_content path="...">…</file_content>`，
   引用过的文件记入 FileContextTracker 等价物；
2. **请求边界组装**：把“system prompt + 模式 + skills + 压缩历史 + 工具定义”的组装
   收敛为独立的 `SessionConfigBuilder.build(task, mode, skills) -> ModelRequestSpec`，
   Agent 循环只消费该 spec，也为将来 `beforeModel` hook（运行时注入最新 follow-up）
   留出挂点；
3. **模式标记随消息持久化**。每条用户消息保存为
   `<user_input mode="plan|act">`，模式切换后的第一条消息前置
   `<mode_notice>The user switched from plan mode to act mode…</mode_notice>`；
   plan→act→plan 未发消息的空往返会互相抵消。system prompt 里配套
   `MODE_TAG_INSTRUCTIONS` 解释该机制：“以最新一条消息的 mode 为准，早期消息的
   约束不代表现在允许”。这解决了当前模式切换后的历史歧义：模型看到早期 Plan 对话里
   “不能改文件”，切到 Act 后可能继续拒绝执行。目标：`chat()` 入库时按当轮 mode 包装，
   `_messages_for_model` 送模型前保留包装、前端展示时剥离；
4. **工具描述按请求边界重建**。把与执行环境相关的描述做成 provider 函数，
   runtime 每次组装 `ModelRequestSpec` 时重新求值（shell 换了，在途请求不受影响，
   下一请求自动带上）。本项目对应点：Plan/Act 切换、MCP 服务器增删、Skill 选择
   变化都应体现在**下一次请求**的工具集合与描述里，而不是 session 级缓存；
5. **项目规则文件**：项目目录内 `OMNISCIENCE.md` 自动注入（ComfyUI 节点开发约定
   常与项目绑定，价值高于全局规则）。

**System prompt 本体**由本项目独立维护：Act 基础提示词、Plan 契约与组装顺序
（base → `{{OMNISCIENCE_RULES}}`（含模式说明）→ `{{OMNISCIENCE_METADATA}}` workspace
元数据 → preferred language 追加）。完整可用版本见**附录 A**。

压缩策略保持现状：先无工具摘要请求、失败回退本地截断、不拆散 assistant/tool 对。

---

## 10. 持久化

当前 SQLite 结构（WAL）把事件与消息都保存为持久事实：

| 表 | 用途 |
| --- | --- |
| `tasks` | 任务元数据、模式、状态、项目目录 |
| `messages` | canonical transcript（每轮模型请求的依据） |
| `events` | 带单调游标的事件日志（partial 通道数据源） |
| `approvals` | 兼容保留，按第 6 节处置 |
| `checkpoints` | shadow-git commit / tar 快照索引 |
| `current_workflows` | 实时画布缓存（≤2MB，不覆盖项目 workflow.json） |
| `skills.sqlite3`（独立库） | Skill CRUD、导入导出 |

本项目的持久化约束是：**内存态不是 canonical transcript**。
**内存态（`_running_tasks`、`_context_cache`、前端本地 messages）都只是缓存与
投影，随时可从 SQLite 重建**。这使页面刷新、ComfyUI 重启后任务可恢复成为架构保证
而非补丁。

---

## 11. 全局对话与 Skill 安装

`GlobalChatAgent`（omniscience/agent/global_chat.py）无任务、无项目文件夹，工作区是
仅供它使用的暂存目录 `paths.global_workspace()`（`<state>/global-workspace`）。可执行
工具分三类：安装类只有 `install_github_skill`；文件类是暂存工作区的只读
`list_files`/`read_file`/`search_files`；浏览器类（`browser_navigate`/
`browser_content`/`browser_screenshot`，无 click/fill）仅在设置的
`global_network_enabled` 打开时提供，与任务的 `metadata.network_enabled` 互不影响。
前端「上传文件」按钮在任务模式下写 `任务工作区/uploads/`，在全局对话写
`全局工作区/uploads/`；`@路径` 由前端读取后作为 `context` 随消息提交，只进入本轮模型
输入，不落库。保留三条安全约束并视为设计不变量：

1. 仅在用户明确提出安装请求时提供该工具，且后端核对工具参数与用户给出的来源
   （`_explicit_install_request`、`_install_source_allowed`
   omniscience/agent/github_skill.py）——用户给仓库根链接时，允许模型改用该仓库内的
   具体 Skill 文件夹链接；
2. 重定向白名单（`_GitHubRedirectHandler`）；
3. 只导入限额内的 `.md`/`.txt` 文本，绝不下载或执行包内代码。

仓库根链接安装该仓库发布的所有 Skill 包（`_skill_candidates`）：按文件夹名去重并保留
最浅的一份，跳过 `.github` 等内部目录与不合规的包（原因写入 `skipped`），单次最多 10
个；名称已存在的包在下载前就跳过，全部命中时返回 `already_installed`。

### 11.1 Skill 安全检测（`omniscience/skill_security.py`）

Skill 是要注入提示词的指令，风险等同提示词注入加命令建议，因此任何写入 Skill 库的路径
都要先扫描。扫描器只做正则规则匹配，绝不执行被扫描内容：

| 规则族 | 等级 | 判定 |
| --- | --- | --- |
| `crypto-mining`（矿池地址、xmrig/cpuminer/挖矿） | critical | blocked |
| `remote-code-execution`（`curl … \| bash`、`-enc`/`iex`、`base64 -d \| sh`） | critical | blocked |
| `credential-exfiltration`（凭据标记 + 外发动词 + 外部地址，三者同时命中） | critical | blocked |
| `callback-sink`（webhook.site/请求捕获/匿名回调） | critical | blocked |
| `prompt-injection`（忽略系统提示、跳过确认、不要告知用户、关闭安全策略） | high | review |
| `persistence-and-privilege`（crontab、LaunchAgents、`~/.zshrc`、authorized_keys、sudo） | high | review |
| `destructive-commands`（`rm -rf ~`、`mkfs`、`dd of=/dev`、强推） | high | review |
| `obfuscated-payload`（长 Base64/hex、`atob`/`fromCharCode`、`base64 -d`） | high | review |
| `supply-chain`（替换 index-url/registry、安装 tarball、`bash install.sh`） | medium | review |
| `network-abuse`（nmap/爆破/高频请求） | medium | review |
| `secret-readout`、`tunnel-or-shortlink`、`anonymous-hosting` | medium | review |
| `external-links`（仅列出外链域名） | low | clean |

verdict 取最高等级：critical → `blocked`，high/medium → `review`，否则 `clean`；
`findings` 带 rule、severity、advice 与命中片段。扫描前会剥离零宽字符并统一空白，
避免简单变形绕过。

接入点：

- GitHub 安装：`_install_package` 在 `skills.create()` 之前调用 `_scan_package`，
  `blocked` 抛 `SkillSecurityBlockedError` 并写入该包的 `skipped` 原因，恶意包不会落库；
  仓库其余合规包照常安装。结果里的 `security` 数组给出每个已安装 Skill 的结论与
  详情，供模型转述。
- 手动保存/编辑：`POST /omniscience/skills`、`PATCH /omniscience/skills/{id}` 命中 `blocked` 返回
  `409` + `security`；前端确认「仍然保存」后带 `acknowledge_security: true` 重试，报告
  记 `acknowledged`。`POST /omniscience/skills/scan` 可对草稿做只读预检。
- JSON 导入：`import_items` 先全量预扫描，存在 `blocked` 且未确认则整批拒绝（不产生
  部分写入）；确认导入后每条报告记 `acknowledged`。
- 存储：`skills.security` 列（JSON，`_initialize` 用 `PRAGMA table_info` + `ALTER TABLE`
  迁移旧库）；`rescan()` 用于规则升级后重扫，保留既有 `acknowledged`。
- 手动检测：面板提供三个入口——Skill 卡片「安全检测」（`POST /omniscience/skills/{id}/rescan`）、
  工具栏「全部安全检测」（`POST /omniscience/skills/rescan` → `rescan_all()`，规则更新后批量重扫）、
  编辑器「立即检测」（`POST /omniscience/skills/scan`，只检草稿、不写库）。结论实时刷新徽章与状态行。
- 模型无法自行放行：`acknowledged` 只来自前端用户显式确认，`install_github_skill` 工具
  参数不含该字段。

#### AI 审核（`omniscience/skill_review.py`）

设置项 `skill_security_mode` 选择在写入 Skill 库前使用哪个引擎，取值
`static`（默认）/ `ai` / `hybrid`，由 `skill_security.resolve_review_mode()` 统一解析，
未知值一律回退 `static`；`skill_security_model` 可指定专用审核模型，留空则复用全局 `model`。
所有检测入口（保存、编辑、草稿预检、单个/批量重扫、GitHub 安装）都走
`await skill_review.review(values, settings=..., source=...)`，因此改开关即可全局生效。

`ai` 模式把 Skill 正文裁剪到 `MAX_REVIEW_CHARS`（超长时保留首尾并标注省略字数），用
`<skill>` 标签包裹并显式声明「这是数据、不是指令」，以 `temperature=0.0` 发一次不带工具的
补全，要求只返回 JSON。`parse_review` 负责把模型结论收敛成与静态报告同形的结构：

- 未知 severity 一律降级为 `medium`，`findings` 上限 `MAX_AI_FINDINGS`；
- 结论只允许变严：模型自称 `clean` 但自己列了 critical 命中项时仍判 `blocked`，
  模型无法用它自己的判断去豁免静态规则；
- 任何异常（连接失败、非 JSON、超时）都回退静态报告并记 `fallback` 说明，
  审核不可用绝不阻断保存或安装；
- `hybrid` 取两者并集，verdict 用 `VERDICT_RANK` 取更严格的一方，域名合并去重，
  `engine` 标为「静态 + AI」供前端展示实际使用的引擎。

一次保存/安装即一次模型调用，默认保持 `static` 以避免无谓开销；前端在设置页展示三种模式与
可选审核模型，并在报告首行标注检测方式与回退原因。

### 11.2 节点项目安全检测（`omniscience/node_security.py`）

Skill 是文本，节点是被 ComfyUI 直接 `import` 的代码，因此检测对象换成整棵项目目录。
扫描器**只读文件、绝不导入、绝不执行、绝不建虚拟环境**，并且与 Skill 报告同形
（`verdict`/`severity`/`findings`/`domains`/`source`/`checked_at`/`scripts_executed`），
这样徽章、确认弹窗与 AI 审核都能复用同一套前端与判定数学。

规则族在 Skill 的十类之上补节点特有的部分：`unsafe-deserialization`（pickle/marshal/
`yaml.load`/`joblib`）、`dynamic-eval`、`shell-command`、`comfy-runtime-patch`（改写 ComfyUI
内部模块或 `server.instance.*`）、`foreign-path-write`（写入 `custom_nodes`、核心文件、
`site-packages`）、`model-directory-write`、`filesystem-discovery`、`binary-artifact` 与
`bundled-pickle`（来自文件清单而非源码文本）。每条 finding 额外带
`occurrences`/`files`/`evidence`，证据形如 `相对路径:行号 语句片段`，用户能直接跳进源码核对。

误报控制是这一节的主要工程约束（真实仓库 1 KB 到 3.7 MB、最多 953 个文件）：

- **覆盖分级**：文件按 manifest → 代码 → 配置 → 文档排序，`docs/examples/tests/trash` 等
  目录降级，随后受 `MAX_SCANNED_FILES`/`MAX_SCANNED_BYTES`/`MAX_FILE_BYTES` 截断；被截断或
  跳过的文件计入 `coverage-limit` 提示与 `files_scanned`/`files_total`/`files_skipped`，
  绝不静默丢证据。软链接与 `os.walk(followlinks=False)` 保证不跟着链接跑出项目。
- **同名变形**：零宽字符与软连字符直接删除（不改变行号），`data:...;base64,` 内联图片替换
  为等长占位，避免长 Base64 规则把图片本身当成载荷。
- **常见库惯用法不算风险**：`model.eval()`、`def evaluate()`、`original_images`、
  `api_config`、`OPENSSH` 的 PEM 正则、`env={**os.environ}` 传给构建子进程、
  `profiles.append(...)`、apt 缓存清理等均不命中；凭据外泄需要同时出现「命名凭据文件 +
  外发动词 + 外部地址」，只批量读环境变量降到 `environment-collection`（medium）。
- **文档/示例降到 review**：critical 命中的文件若全部落在文档或 `examples/tests/trash`
  这类不会被 import 的路径，降级为 medium 并在 advice 说明原因；出现在实际源码里的同类
  命中仍按 blocked 处理。

判定与开关完全沿用 Skill 那套：`skill_security_mode`（`static`/`ai`/`hybrid`）与
`skill_security_model` 由 `node_review.review_mode()` 解析，`hybrid` 取两个引擎的并集且
verdict 只会更严；AI 审核不喂整棵目录树，而是把静态报告压成证据摘要
（覆盖统计、依赖清单、第三方导入、外部域名、文件清单、静态命中与有限证据行），一次
`temperature=0.0`、不带工具的补全只读回 JSON；`MAX_DIGEST_EVIDENCE` 限制证据行数。模型
失败、无法解析或未配置审核模型时回退静态报告并记 `fallback`，接管不会被审核故障卡住。

缓存与门禁：`node_scans(path, report, updated_at)` 以项目绝对路径为主键，
`custom_node_projects()` 一次性带出缓存报告，因此打开面板不会重扫、也不会消耗模型调用。
接管走 `_node_gate()`：先扫描后写入，`blocked` 且未确认返回 `409`；`acknowledged`
只可能来自前端用户点击，模型不能自我放行；批量检测强制静态，单次最多
`MAX_NODE_BATCH_SCANS` 个项目并返回 `remaining` 供前端提示再点一次。

### 11.3 全局对话节点安装（`omniscience/agent/node_install.py`）

全局对话只在用户明确说“安装节点”并给出来源时暴露 `install_comfyui_node`。来源必须是用户消息中的公开 GitHub/Gitee HTTPS 仓库/tree 链接、`owner/repo` 简写，或用户上传到全局工作区 `uploads/` 的节点压缩包路径；后端会再次比较工具参数与原始消息，模型不能替换仓库、路径或安全确认参数。

GitHub 仓库通过 `codeload.github.com` 下载 tarball；Gitee 仓库通过 Gitee 仓库归档接口下载 zip，下载重定向仅允许停留在 Gitee HTTPS 主机。压缩包和上传的 zip/tar 包都先进入临时 staging 目录。`omniscience/archives.py` 逐项拒绝绝对路径、`..` 穿越、符号链接、硬链接、设备文件和超限包；当前上限为压缩包 20 MB、解压后 80 MB、2,000 个文件、单文件 20 MB。节点根目录必须包含 `__init__.py`，并使用仓库名或上传文件名推导 `custom_nodes/<项目名>`。

工作流缺失节点仓库搜索优先查询 GitHub；只有 GitHub 搜索请求失败时才回退 Gitee，GitHub 正常返回空候选时不触发回退。两端候选均须通过仓库主机和仓库根路径校验，用户只能显式选择本次搜索返回的来源。开始安装前从所选仓库解析默认分支并固定为 40 位 commit；归档暂存后仍按下述流程运行安全扫描，任何下载来源都不会绕过安装门禁。

安装顺序固定为：下载/解包 → 定位节点根 → 静态 `node_security.scan_project` → 按 `skill_security_mode` 运行 `node_review.review` → 只有报告完成且允许时才移动到 `custom_nodes` → 保存 `node_scans` 缓存。安装不执行 `git clone`、`pip install`、`setup.py`、`requirements.txt` 或包内脚本，不创建 `workflow.json`，不自动接管为任务；成功后项目会出现在待接管列表，需用户另行接管并重启 ComfyUI。

全局工具永远不传 `acknowledged`。blocked 安装通过普通工具结果返回给模型并保持拒绝；只有面板 `/omniscience/custom-nodes/install` 收到用户明确确认后才可带 `acknowledge_security` 重试。AI/hybrid 模式每次单独安装产生一次审核模型调用，静态模式与批量检测不产生调用；模型审核只能加严结论，不能放宽静态命中。

---

## 12. 生命周期结束与资源清理

生命周期清理入口为 `SessionLifecycle.stop_all()`。ComfyUI 不提供可靠的插件 unload 钩子，
因此清理点定义为：

- `SessionLifecycle.stop_all()`：结束所有 active session、杀进程组、关闭 Playwright、
  关闭 MCP 长驻连接；
- 触发点：ComfyUI 进程退出（`atexit`/信号处理）与控制器重建；
- 每个 stop 带超时并记录 warning，不阻塞整体退出；
- 前端 `setInterval` 轮询在面板隐藏时降频，页面卸载自然终止。

---

## 13. 关键设计约束

本项目最关键的设计约束如下：

1. **前端不直接访问 Agent Runtime。** 所有任务操作经 `/omniscience/*` 路由进入后端。
2. **`OmniscienceController` 是任务与 session 的真实协调边界**，路由 handler 只做
   参数转换与转发。
3. **内存态不是 canonical transcript。** SQLite messages/events 是唯一事实源，
   UI 与 runtime 内存态皆为可重建投影。
4. **partial 事件与完整 state 分离传输。** partial 追求低延迟、可丢；state 负责
   收敛与重新同步。
5. **消息发送不阻塞 agent turn，agent turn 不阻塞 HTTP 连接。** POST 立即返回，
   浏览器断开不影响运行。
6. **事件必须按 active session 过滤。** 旧 session 的迟到事件不能覆盖新 task 或
   新一轮运行的 UI 状态。
7. **取消必须先建立 fence（`(task_id, session_id)` 世代标记），再 abort。**
   abort 期间产生的显示事件同样被 fence 抑制。
8. **工具执行环境经 host 抽象。** 文件、命令、浏览器、MCP、画布工作流都通过
   可替换的 host 注入，Agent 循环不感知 ComfyUI 细节。
9. **所有异步 UI/状态突变防 stale。** 任务切换、历史加载、session resume 可能并发，
   较新的用户操作必须以世代号优先。

---

## 14. 关键文件索引

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 插件注册、路由挂载、WEB_DIRECTORY |
| `omniscience_server.py` | 路由层 + Agent 运行时（目标：拆出 Controller/Coordinators） |
| `omniscience_core.py` | TaskStore/SkillStore/ContextManager/CheckpointManager |
| `omniscience_mcp.py` | MCP stdio/HTTP 客户端与工具聚合 |
| `omniscience_browser.py` | Playwright 浏览器工具 |
| `web/omniscience.js` | 侧栏面板、ApiClient、事件消费 |
| `omniscience/skill_security.py` | Skill 静态规则扫描、verdict 数学与审核模式解析 |
| `omniscience/skill_review.py` | Skill 的 AI 审核、报告合并与回退 |
| `omniscience/node_security.py` | custom_nodes 项目静态扫描（只读遍历 + file:line 证据） |
| `omniscience/node_review.py` | 节点项目的 AI 审核（证据摘要 + 复用 Skill 开关） |
| `omniscience/agent/projects.py` | 项目枚举、接管门禁落库、任务安全元数据 |
| `web/omniscience.css` | 面板样式 |
| `tests/test_omniscience_server.py` | 沙箱、持久化、取消、压缩、Skill 安装行为回归 |

`omniscience_server.py` 内部锚点（改动前核对，行号随版本漂移）：

| 符号 | 行号 | 对应本文档章节 |
| --- | --- | --- |
| `register_routes` | 2486 | 2 |
| 主任务 Agent（内部类） | 1585 | 4 |
| 主任务 Agent 的 `_lock` | 1616 | 4（session 隔离） |
| `create_task` | 1847 | 5 |
| `chat` / `_run_loop` | 1896 / 2155 | 5 |
| `cancel_task` / `clear_history` | 1756 / 1771 | 7 |
| `_requires_confirmation` | 1070, 2354 | 6 |
| `_tool_definitions` / `_execute_tool` | 1980 / 2384 | 8 |
| `_compact_messages` / `_call_model` | 2019 / 2305 | 9 |
| `Workspace` / `_run_command` | 454 / 2266 | 8 |
| `TaskStore` / `ContextManager` / `CheckpointManager` | core 34 / 643 / 864 | 9, 10 |

---

## 15. 差距清单与实施顺序

按依赖关系排序的四个阶段，每阶段独立可发布：

### 阶段 A — 通信与并发正确性（结构地基）

1. `POST messages` 改 fire-and-forget + 202，前端全部进度走事件通道；
2. 引入 `SessionLifecycle`：per-task session、`session_id` 世代、废除全局 `_lock`；
3. 取消 fence 绑定 session 世代，删除 `sleep(0)` 自旋；
4. 事件增加 `session_id` 并按 active session 过滤（前后端）。

### 阶段 B — 流式体验

5. 模型调用 `stream: true`，产出 `text_delta`/`tool_started` 事件；
6. 事件通道 SSE 化（轮询保留为降级路径）；
7. 前端 `applyEvents` 真正消费 payload，增量渲染消息。

### 阶段 C — 工具与策略层

8. MCP 长驻连接 + 工具目录缓存（消除每步 spawn）；
9. `ToolPolicy` 声明层化；审批链路二选一（恢复或删除）；
10. Browser 全局锁改 per-task；`search_files` 接 ripgrep；
11. 命令执行对齐 5.4：command-guard"遮蔽→切分→首词"硬拦截、输出中段截断、
    非交互约束写进工具描述；
12. 文件写入对齐 5.3：新增 `editor` 精确编辑工具、`apply_patch` 升级文法描述、
    统一 `{tool, success, output/error}` 回灌格式（5.6）。

### 阶段 D — 上下文与协调器收尾

13. 启用附录 A 提示词：Act 基础 prompt + Plan 契约 + `<user_input mode>` /
    `<mode_notice>` 模式标记持久化（9 节目标增强 3）；
14. `SessionConfigBuilder` 独立化；工具描述按请求边界重建；`@file` mention 后端
    展开；项目 `OMNISCIENCE.md` 规则注入；
15. follow-up 队列与运行中插话；
16. 路由层收敛到 `OmniscienceController` 单一入口；
17. 清理 `omniscience_runtime/` 残留目录与旧 pyc。

### 验证

沿用现有测试栈并按阶段扩展：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile __init__.py omniscience_server.py
```

新增回归重点：并行两任务互不阻塞（阶段 A）、取消后无迟到事件覆盖（阶段 A）、
断连后 agent 继续且重连收敛（阶段 B）、MCP 单步不再 spawn N 进程（阶段 C）。

---

## 附录 A：提示词设计（可直接采用）

以下提示词由 ComfyUI Omniscience 独立维护，面向 ComfyUI 自定义节点开发场景。
占位符由 `SessionConfigBuilder`（第 9 节）在每次组装请求时填充。

### A.1 Act 模式基础 system prompt

```text
You are Omniscience, an AI coding agent embedded in ComfyUI. Your primary goal is
to assist users with coding tasks—especially ComfyUI custom node development—by
leveraging your knowledge and the tools at your disposal. Given the user's prompt,
use the available tools to complete the task.

Always gather all the necessary context before starting. For example, when writing
or fixing code, first understand the requirement, the naming conventions and
libraries already used in this codebase, and the commands used to run and test it.
Always validate new code at the end, running it if possible for live feedback.
If you need more information, use a tool or ask for clarification instead of
making assumptions or inventing results.

Environment you are running in:
<env>
1. Platform: {{PLATFORM_NAME}}
2. Date: {{CURRENT_DATE}}
3. Host: ComfyUI (project task workspace)
4. Working Directory: {{CWD}}
5. ComfyUI root: {{COMFYUI_DIR}} (custom_nodes/{{PROJECT_NAME}} is symlinked here when configured)
</env>

Remember:
- Always adhere to existing code conventions and patterns.
- Use only libraries and frameworks confirmed to be in use in the current codebase.
- Provide complete and functional code without omissions or placeholders.
- Be explicit about any assumptions or limitations in your solution.
- Show your planning process before executing a task.
- Always use absolute paths when referring to files, relative to the working directory.
- You can call multiple tools in a single response. Before using tools, identify
  every independent read, search, command, or edit needed for the next step and
  emit all of those tool calls now. Do not wait for one independent result before
  requesting another.
- Good parallelism examples: read all known relevant files in one call; run
  independent inspection commands in one run_commands call; emit independent
  reads, searches, and edits together in one response.
- Verify files you have edited or created at the end of the task.

Begin by analyzing the user's input and gathering necessary context. Present your
plan at the start of your response, along with the tool calls.

REMEMBER, be helpful and proactive! Do not indicate you will use a tool unless you
are actually calling it in this response.

IMPORTANT: Keep including tool calls until the task is completed. A response
without tool calls is treated as the final answer.

When the task is done, summarize what you did and what the user should know.
Always validate your result by checking code and running it where possible.

For ComfyUI node work specifically:
- Never invent a class_type, input or output spec. Read the actual node source or
  ask before referencing it in workflow.json.
- Test workflows by wiring the new node between LoadImage and PreviewImage in the
  project's workflow.json, then verify via the ComfyUI canvas or the browser tools.

If the user asked a simple question without coding context, answer it directly
without using tools.

{{MODE_SECTIONS}}
{{SKILLS_AND_RULES}}
{{WORKSPACE_METADATA}}
{{PREFERRED_LANGUAGE}}
```

最后一段 `# Preferred Language` 在非默认语言时追加 `Speak in <language>.`；
`{{SKILLS_AND_RULES}}` 依次注入项目 `OMNISCIENCE.md`、选中的启用 Skill；
`{{WORKSPACE_METADATA}}` 为任务项目目录、模式、最近 git 提交等结构化 JSON。

### A.2 Plan / Act 模式标记说明（`{{MODE_SECTIONS}}` 第一部分）

`MODE_TAG_INSTRUCTIONS` 解释持久化的 `<user_input mode="...">` 包装
（第 9 节目标增强 3）：

```text
# Plan / Act Modes

User messages arrive wrapped in a <user_input mode="..."> tag. The mode attribute
is the interaction mode the user was in when they sent that message: "plan" means
plan-mode constraints applied (explore, analyze, and align on a plan -- no edits
or state-changing commands), while "act" means implementation was allowed. If the
mode attribute changes between messages, the user switched modes -- the newest
message's mode governs right now, regardless of what earlier messages allowed.
A <mode_notice> block inside a message marks exactly when such a switch happened.
```

### A.3 Plan 模式契约（手动切换变体）

以下契约与 UI 一致：提供 Plan/Act 开关，不向模型开放模式切换工具：

```text
# Plan Mode

You are in Plan mode. Your role is to explore, analyze, and plan -- not to execute.

- Read files, search the codebase, and gather context to understand the problem
- Ask clarifying questions when requirements are ambiguous
- Present your plan as a structured outline with clear steps
- Explain tradeoffs between different approaches when they exist
- Do NOT edit files, write code, run destructive commands, or make any changes
- Do NOT implement anything -- focus on understanding and alignment first

The run_commands tool remains available in plan mode strictly for read-only
inspection -- listing files, searching (grep), reading configs, inspecting git
history and diffs, checking tool versions, and the like. Never use it to change
anything: no creating, modifying, or deleting files, no writing scripts that make
changes, no state-changing commands (installs, migrations, container commands
that mutate state). File-editing commands (rm/mv/cp, in-place edits like sed -i,
output redirection to files outside /tmp, git commands that change the working
tree, package installs) are hard-blocked in plan mode: they are not executed and
return a tool error instead, so do not attempt them.

Once you have presented your plan, end your turn and wait for the user's response.
You do NOT have the ability to switch to act mode yourself -- the user must do it
manually with the Plan/Act toggle. If the task requires act-mode tools, ask the
user to "toggle to Act mode" (use those words).
```

注意与现状的差异：当前 Plan 模式直接隐藏 `run_commands`（`PLAN_TOOL_NAMES`）。
目标设计是**保留只读调查能力 + command-guard 硬拦截写操作**（5.4 节），配合上面这段
契约，使模型在 Plan 模式能 grep、看 git diff，但不可能改任何东西。

### A.4 工具描述模板

各写进该工具自己的 `description`（对齐 5.3–5.5 的"环境约束放在工具描述"原则），
schema 保持 JSON Schema：

**editor（新增，精确编辑）**：

```text
An editor for controlled text edits on the file at `path`. Provide `insert_line`
to insert `new_text` at a line number; otherwise replace `old_text` with
`new_text`, or create the file with `new_text` if it does not exist. Use this
tool for small, precise edits over full-file writes or shell commands. If several
edits to different files or non-overlapping regions are already known, emit
multiple editor calls in the same response instead of serializing them across
turns.
```

**apply_patch（升级描述）** —— 使用受控 freeform 文法（内容行规则、`@@`
消歧、禁用行号）：

```text
Use `apply_patch` to edit files with the canonical patch grammar. Pass the patch
text directly as the `input` string:
*** Begin Patch
*** Update File: path/to/file.py
@@ optional disambiguating section marker
 [context before]
-[old line]
+[new line]
 [context after]
*** End Patch
Supported actions: *** Add File: <path> (every content line starts with +),
*** Update File, *** Delete File, and optional *** Move to: inside an Update.
Context lines locate the edit; do NOT use line numbers. Use @@ markers when the
same code appears more than once.
```

**run_commands** —— 借鉴 `RUN_COMMANDS_SHARED_INSTRUCTIONS` + 输出截断说明：

```text
Run non-interactive shell commands from the root of the task workspace.
Commands must be non-interactive: skip or de-interactive pagers and prompts with
flags/env (git --no-pager, --non-interactive). Output beyond ~30k characters is
middle-truncated (start and end preserved); filter output (grep, head, tail,
-q) when you need specific sections. State-changing commands (file edits,
installs, git mutations) are rejected in plan mode. Include multiple independent
commands in one call; run independent reads/edits via their own tools in the
same response.
```

**browser_navigate / browser_content / browser_screenshot** —— 补 ComfyUI 验证
闭环（5.5 节）：

```text
browser_navigate: Open an http/https page in this task's browser session. Use to
load the ComfyUI web UI and verify a node you are developing end-to-end.
browser_content: Extract visible text from the page body or a CSS selector;
results over 100k characters are truncated with a marker -- narrow with a
selector instead of re-fetching the whole page.
browser_screenshot: Capture the full page into the task workspace and return the
file path. Use after wiring a workflow (import the project workflow.json) to
verify node graph and outputs visually.
```

**工具层共同规则**（写进每个描述或公共说明，确保执行边界一致）：

- 写类工具（editor/apply_patch/write_file/run_commands 的变更命令）不可自动
  重试；失败原文必须作为 tool result 回灌；
- 所有工具的失败返回 `success:false + error` 内容化结果，而不是异常；
- 独立调用尽量在同一次响应中并行发出（读、搜索、检查、跨文件编辑）。

### A.5 组装顺序

`SessionConfigBuilder` 按以下顺序输出并填充模板：

```text
ACT_BASE
  .replace({{PLATFORM_NAME|CURRENT_DATE|CWD|COMFYUI_DIR|PROJECT_NAME}})
  + "\n\n" + MODE_TAG_INSTRUCTIONS          # 非 plan 也注入（历史可能含对侧模式消息）
  + "\n\n" + PLAN_MODE_INSTRUCTIONS_MANUAL_SWITCH   # 仅 plan 模式
  + "\n\n" + OMNISCIENCE.md（项目规则）
  + "\n\n" + 选中的启用 Skills
  + "\n\n" + workspace metadata JSON
  + "\n\n" + "# Preferred Language\nSpeak in <lang>."（非英文时）
```

override 规则：若未来支持用户自定义完整 system prompt（override），则跳过 base 与模式段，
仅按需在尾部补 workspace metadata。

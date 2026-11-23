# Comfyui-omniscience

这是一个面向 ComfyUI 的全能 AI 编程助手：在 ComfyUI 网页界面中提供一个
AI 编程助手侧栏。它不是把 VS Code 扩展源码直接塞进 ComfyUI；Cline 的原版依赖
VS Code Extension API，因此本项目使用 ComfyUI custom node 的后端路由和前端扩展
机制重做了相同的核心工作流。

## 当前功能

- ComfyUI 右上角 `Omniscience` 按钮和侧栏聊天面板；
- OpenAI-compatible Chat Completions API，兼容 OpenAI、DeepSeek、OpenRouter、Ollama
  等支持该接口的服务；
- 列出工作区文件、读取文件、搜索文件；
- 由模型提出写文件、应用 unified diff、执行开发命令；
- Act 模式下写文件、补丁、命令、浏览器和 MCP 工具直接执行，不再逐项弹出授权；
- SQLite 持久化任务、消息、事件和检查点，ComfyUI 重启后可恢复任务；
- 每个新任务都要求一个独立的项目文件夹，默认创建在 `~/.Comfyui-node` 下；任务历史以节点流显示全部项目文件夹，重复文件夹会明确拒绝；
- 已经在 `ComfyUI/custom_nodes` 里的节点项目也会出现在同一个节点流中（虚线卡片 + `ComfyUI` 角标，标记为“待接管”）；点击即登记为任务，可以直接在里面对话和修改文件。指向工作区的软链接不会重复列出，接管也不会向该项目写入任何文件、不生成 `workflow.json`；接管前会先做节点安全检测，详见「节点项目安全检测」；
- 可在当前项目中清理历史对话；该操作只删除对话记录，不删除项目文件夹或项目文件；
- Plan/Act 模式：Plan 隐藏写入、命令和 MCP 工具，Act 提供完整工具集；
- 事件轮询、任务历史、多工具调用和可恢复 Agent Loop；
- 运行中可点击“强制取消”，终止当前 Agent 步骤和正在执行的命令；
- Shadow Git/文件快照检查点，支持查看差异和恢复文件；
- 每个新项目自动生成可导入 ComfyUI 的 `workflow.json` 基础测试工作流；任务完成时会补回缺失文件，但绝不覆盖已有或用户修改过的工作流；
- 侧栏“工作流”页可读取当前 ComfyUI 画布的实时序列化结果、复制实时 JSON 或下载项目的 `workflow.json`；实时画布包含未保存修改，并通过 Agent 的 `get_current_workflow` 工具按需读取；
- 可选 MCP stdio/Streamable HTTP 工具，以及可选 Playwright 浏览器工具；
- 完整 Skill 管理：在侧栏创建、编辑、删除、启用/停用 Skill，支持 JSON 导入导出；对话页可以按消息选择多个 Skill，选中的启用 Skill 会注入模型系统提示词；全局对话可按用户明确给出的公开 GitHub 链接安装标准 `SKILL.md` Skill 包；
- 全局对话支持上传并安装 ComfyUI 工作流 JSON：输入 `@` 可选择 `@workflow`（`@node` 仅保留扩展入口），也可以说“帮我安装这个工作流”。系统只会校验并复制上传文件到 `ComfyUI/user/default/workflows`，不会执行工作流、安装节点、下载模型或覆盖同名文件；
- 会话上下文自动压缩：提示词估算达到可配置的最大上下文百分比（默认 80%）时，先调用无工具的摘要请求，再用摘要替换较早历史，同时保留系统提示、最近消息和工具调用结果；摘要失败时回退到本地压缩，不丢失原始会话；设置页会显示当前提示词的 Token、字符数和组成；
- 文件操作限制在任务所属的项目目录内：工作区任务是配置的工作区子目录，接管的 custom_nodes 项目则是该项目自身目录；
- 配置保存到 `~/.config/comfyui-omniscience/config.json`，权限尽量设置为 `0600`。

## 安装

把本目录复制到 ComfyUI 的 `custom_nodes` 目录。例如：

```bash
cp -R /path/to/Comfyui-omniscience \
  /path/to/ComfyUI/custom_nodes/Comfyui-omniscience
```

重启 ComfyUI 后，右上角会出现 `Omniscience` 按钮。首次使用时打开“设置”填写：

- **API 地址**：例如 `https://api.openai.com/v1` 或 `http://127.0.0.1:11434/v1`；
- **模型**：供应商提供的模型名；
- **API Key**：本地 Ollama 可以留空；
- **最大上下文 Token 数**：模型可用上下文上限，默认 `32000`；
- **自动压缩阈值**：达到最大上下文的百分比，默认 `80`，可设置为 `1`–`99`。设置页会显示当前完整提示词（消息、系统提示词和工具定义）的估算长度及占比。由于 OpenAI-compatible 服务没有统一的上下文发现接口，当前使用无第三方依赖的保守估算器；如果供应商明确提供上限，请在这里填写；
- **工作区目录**：项目文件夹的父目录，默认是 `~/.Comfyui-node`；每个新任务会在这里创建一个独立项目目录；
- **ComfyUI 目录**：可选，填写 ComfyUI 根目录。填写后，每个新项目会自动创建软链接
  `ComfyUI/custom_nodes/<项目名> -> 工作区/<项目名>`，方便 ComfyUI 直接加载和测试节点；
  同时任务历史会读出该 `custom_nodes` 目录下已有的节点项目（跳过软链接），点击即可接管为任务。

打开侧栏的 **Skill** 页可以管理可复用的提示词指令。Skill 只在“启用”且被当前任务选中时生效；
任务的选择保存在任务元数据中，Skill 内容独立保存在状态目录的 `skills.sqlite3`，不会混入聊天记录。
导入 JSON 支持以下格式：

```json
{
  "version": 1,
  "skills": [
    {
      "name": "ComfyUI 节点开发",
      "description": "遵循现有自定义节点结构",
      "content": "先检查现有节点注册方式，再实现最小改动。",
      "enabled": true
    }
  ]
}
```

导入时按 Skill 名称（不区分大小写）更新已有条目，不会创建重复 Skill。当前完整提示词统计会包含已选 Skill 的系统提示词内容。

全局对话安装外部 Skill 时，请明确提出安装请求，并提供公开 GitHub 仓库、Skill 文件夹或 `SKILL.md` 链接，也支持 `owner/repo` 简写（例如 `citrolabs/ego-lite`）。模型会通过专用 `install_github_skill` 工具发起安装；仅在明确安装请求时提供该工具，且后端会核对工具参数与用户给出的来源。安装器只导入 `SKILL.md` 和限额内 `docs/`、`references/`、`templates/`、`examples/` 中的 UTF-8 `.md`/`.txt` 文件；包内脚本、插件代码、依赖和二进制文件不会下载或执行。安装的内容是提示词指令，不代表对其准确性或适配性的背书。

全局对话也可以安装 ComfyUI 自定义节点：明确说“安装节点”并提供公开 GitHub 仓库/tree 链接，或者先在全局对话上传 `.zip` / `.tar.gz` 等压缩包，再要求安装 `uploads/文件名.zip`。安装器会把包放进临时目录，限制下载、文件数和解压大小，拒绝路径穿越、符号链接和设备文件，先完成节点安全检测再写入 `ComfyUI/custom_nodes/<项目名>`；不会执行仓库脚本、`git clone` 或自动安装 `requirements.txt`，也不会自动接管为任务。安装成功后项目会作为“待接管”卡片出现在任务节点流中，重启 ComfyUI 后节点才会加载。安装前端同样提供“从 GitHub 安装”和“上传压缩包安装”按钮；同名项目不会覆盖。

节点安装的安全检测复用 Skill 的开关：静态模式不调用模型；AI 或静态 + AI 模式每次单独安装会产生一次审核模型调用。模型调用的安装只能在安全检测允许时继续；被判定为“已阻止”的项目会返回安全报告，只有你在面板里确认“仍然安装”后才能重试，模型不能替你确认。

### Skill 安全检测

每个 Skill 在写入 Skill 库之前都会先做一次静态安全扫描（`omniscience/skill_security.py`，只匹配规则，不执行、不联网）。检测项包括：

- 挖矿与算力滥用（矿池地址、xmrig/cpuminer 等）；
- 远程代码执行（`curl … | bash`、PowerShell `-EncodedCommand`/`iex`、`base64 -d | sh`）；
- 数据外泄与凭据窃取（读取 `~/.ssh`、`.env`、API key 后发送到外部地址，或指向 webhook/回调捕获服务）；
- 外部链接（列出全部外链域名，另单独标记短链、粘贴站、裸 IP、非常规端口、内网穿透与匿名网络地址）；
- 混淆载荷（大段 Base64/十六进制、`atob`/`fromCharCode`）；
- 持久化与提权（crontab、LaunchAgents、`~/.zshrc`、`authorized_keys`、`sudo`、git hooks）；
- 破坏性命令（`rm -rf ~`、`mkfs`、`dd of=/dev/…`、强制推送）；
- 提示词注入与越权（“忽略之前的指令”“不要告诉用户”“跳过确认”“关闭安全策略”）；
- 供应链（替换软件源、安装来路不明的包或镜像）；
- 网络滥用（端口扫描、口令爆破、高频请求）。

结论分三级：`安全`、`需复核`、`已阻止`。GitHub 安装命中「已阻止」时该包会被拒绝，原因写入安装结果，同仓库其他合规 Skill 正常安装；Skill 卡片上会显示结论，点击可查看具体命中项与证据片段。手动新建/编辑与 JSON 导入同样先扫描，若你确认来源可信，可勾选「仍然保存 / 仍然导入」，此时报告会被标记为已人工确认。模型无法自行绕过该检测，覆盖只能由你在面板上确认。

也可以随时手动检测：Skill 卡片上的「安全检测」重扫单个 Skill，工具栏的「全部安全检测」按当前规则批量重扫整个库（规则升级后建议点一次），编辑器里的「立即检测」只检查尚未保存的草稿内容，不会写入 Skill 库。

除了静态规则，还可以让模型读一遍 Skill 再下结论。设置页「Skill 安全检测」提供三种方式：

- **静态规则检测**（默认）：本地正则匹配，不调用模型、不产生费用；
- **AI 模型审核**：把 Skill 正文交给模型判断意图，每次保存/安装消耗一次补全调用；
- **静态 + AI**：两者结论取更严格的一方。

AI 审核只把 Skill 当作数据（包裹在明确标注的标签内，绝不执行其中的指令），要求模型返回结构化结论；
无法解析或调用失败时自动回退静态检测并在报告里说明原因，因此审核服务不可用也不会阻断保存与安装。
模型只能把结论变严，不能用「我觉得没问题」豁免静态规则命中的高危项。可在「AI 审核模型」里为审核单独指定
一个更小的模型，留空则使用上方配置的模型。详情面板首行会标注本次实际使用的检测方式。

### 节点项目安全检测

接管 `ComfyUI/custom_nodes` 里的节点项目等于让别人代码在你本机被 ComfyUI 导入执行，因此
接管前会先扫描该项目源码（`omniscience/node_security.py`）。扫描只读文件，绝不导入、绝不
执行、也不建虚拟环境。检测项在 Skill 那套之外补充了节点特有的部分：反序列化
（`pickle`/`marshal`/`yaml.load`/`torch.load` 自定义反序列化）、动态 `eval`/`exec`、
`shell=True` 与 `os.system`、改写 ComfyUI 运行时（猴子补丁、`importlib.reload`）、写入
`custom_nodes`/核心文件/`site-packages`、下载写模型权重、遍历用户目录、批量读环境变量、
自带的 `.so`/`.pkl` 产物，以及与 Skill 共用的远程代码执行、凭据外泄、回调接收、持久化提权、
破坏性命令、混淆载荷、供应链、网络滥用、提示词注入、外链域名与埋点。

结论与 Skill 同样是「安全 / 需复核 / 已阻止」，每条命中都会给出
`文件:行号 + 语句片段`，以及扫描覆盖了多少文件（项目过大时会被截断，报告里会明确说明还有
多少文件没读）。徽章在两处显示：未接管的 ComfyUI 卡片上，以及已接管任务节点上，点击徽章
可展开详情。

怎么用：

- 卡片或任务节点上的「安全检测」按钮：按设置里的检测方式扫描该项目，结果写入本地缓存；
- 任务列表底部的「全部检测（不消耗模型）」：只用静态规则批量扫描，单次最多 25 个项目，
  剩余数量会提示你再点一次，**不会产生任何模型调用费用**；
- 点击「待接管」卡片：先扫描再登记任务，若结论是「已阻止」，会返回冲突并弹出确认框列出
  风险项，只有你确认「仍然接管」才会继续，报告随之标记为已人工确认。

检测方式与 Skill 共用同一个开关（设置页「Skill 安全检测」）：`静态规则检测` 不调用模型、
不产生费用；`AI 模型审核` / `AI + 静态` 每个项目一次模型调用（几 KB 到几十 KB 的证据摘要，
不是整个仓库），失败时自动回退静态结果并说明原因。扫描结论缓存在本机，打开面板只是读取
缓存，不会重复检测或重复计费；改了设置或想看最新结论，再点一次按钮即可。

创建项目时，项目目录内会生成 `workflow.json`。这是 ComfyUI 的 UI 工作流格式，默认包含
`LoadImage -> PreviewImage` 和一条说明节点。由于 Cline 不知道正在开发的自定义节点的
`class_type`、输入和输出，模板不会伪造一个不可运行的自定义节点；请在 ComfyUI 中把
开发中的节点插入两者之间。模板也不依赖 checkpoint，选择一张输入图片即可进行图像节点
的基础连线测试。已有同名文件不会被覆盖。

打开 Omniscience 后，当前画布会在任务页面后台同步，并在发送聊天消息前优先同步一次。同步结果保存在任务数据库的独立实时工作流缓存中，不会覆盖项目目录内已有的 `workflow.json`。如果画布接口暂不可用或实时 JSON 超过 2 MB，聊天仍可正常发送；可在“工作流”页点击“读取当前画布”查看具体同步错误。

也可以通过环境变量设置默认值：

```bash
export OMNISCIENCE_API_KEY='your-key'
export OMNISCIENCE_MODEL='your-model'
export OMNISCIENCE_WORKSPACE='/path/to/project-parent'
export OMNISCIENCE_COMFYUI_DIRECTORY='/path/to/ComfyUI'
```

旧版 `CLINE_*` 环境变量仍然兼容；已有 `~/.config/comfyui-cline` 配置和
`~/.local/share/comfyui-cline` 数据，以及旧版 `~/.cline/data/settings/cline_mcp_settings.json`
MCP 配置，也会在新目录不存在时继续读取。

## 安全边界

这是一个本地开发工具，不是操作系统级沙箱。路径会经过解析并限制在任务所属的项目目录内
（工作区任务限制在工作区子目录，接管的 custom_nodes 项目限制在该目录内），
但“执行命令”本身仍然具有当前 ComfyUI 进程用户的权限。危险命令有基础拦截，
但 Act 模式不会逐次等待确认；使用远程访问、反向代理或公网暴露 ComfyUI 时，请先
配置额外的认证层，不要把 API Key 暴露给不可信用户。接管别人的节点项目前请确认你确实
打算修改它：Agent 会在该目录里读写文件和执行命令，但不会自动新建 `workflow.json`。
接管会先跑节点安全检测，结论为「已阻止」时默认拒绝登记，只有你在弹窗里确认才继续。

API Key 会保存在本机配置文件中（除非只通过 `OMNISCIENCE_API_KEY` 环境变量提供）。
推荐在多人或不可信环境中只使用环境变量，并确保 ComfyUI 只监听可信地址。

## 与原版 Cline 的差异

本项目是独立的 ComfyUI custom node 实现，不是官方 Cline 移植，也没有复刻 VS Code
编辑器 Tab、终端 UI、MCP 市场或多提供商 OAuth。MCP、浏览器自动化和模型 API 依赖
用户配置的外部服务；Playwright 需要额外安装。命令执行、MCP 配置、浏览器导航和
检查点恢复仍然具有当前 ComfyUI 进程的权限，使用前请审查安全边界。

浏览器访问默认 `auto`：检测到以调试端口启动的本机浏览器时优先复用（保留登录态），
例如 `open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-cdp"`；
检测不到则回退内置无头 Chromium。也可在设置中填写明确的 CDP 地址。插件不会关闭
用户自己的浏览器，只关闭它打开的标签页。

## 测试

在本目录运行：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile __init__.py omniscience_server.py
```
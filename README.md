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
- 可在当前项目中清理历史对话；该操作只删除对话记录，不删除项目文件夹或项目文件；
- Plan/Act 模式：Plan 隐藏写入、命令和 MCP 工具，Act 提供完整工具集；
- 事件轮询、任务历史、多工具调用和可恢复 Agent Loop；
- 运行中可点击“强制取消”，终止当前 Agent 步骤和正在执行的命令；
- Shadow Git/文件快照检查点，支持查看差异和恢复文件；
- 每个新项目自动生成可导入 ComfyUI 的 `workflow.json` 基础测试工作流；任务完成时会补回缺失文件，但绝不覆盖已有或用户修改过的工作流；
- 侧栏“工作流”页可读取当前 ComfyUI 画布的实时序列化结果、复制实时 JSON 或下载项目的 `workflow.json`；实时画布包含未保存修改，并通过 Agent 的 `get_current_workflow` 工具按需读取；
- 可选 MCP stdio/Streamable HTTP 工具，以及可选 Playwright 浏览器工具；
- 完整 Skill 管理：在侧栏创建、编辑、删除、启用/停用 Skill，支持 JSON 导入导出；对话页可以按消息选择多个 Skill，选中的启用 Skill 会注入模型系统提示词；全局对话可按用户明确给出的公开 GitHub 链接安装标准 `SKILL.md` Skill 包；
- 会话上下文自动压缩：提示词估算达到可配置的最大上下文百分比（默认 80%）时，先调用无工具的摘要请求，再用摘要替换较早历史，同时保留系统提示、最近消息和工具调用结果；摘要失败时回退到本地压缩，不丢失原始会话；设置页会显示当前提示词的 Token、字符数和组成；
- 文件操作限制在配置的工作区目录内；
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
  `ComfyUI/custom_nodes/<项目名> -> 工作区/<项目名>`，方便 ComfyUI 直接加载和测试节点。

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

全局对话安装外部 Skill 时，请明确提出安装请求，并提供公开 GitHub 仓库、Skill 文件夹或 `SKILL.md` 链接，也支持 `owner/repo` 简写（例如 `citrolabs/ego-lite`）。模型会通过专用 `install_github_skill` 工具发起安装；仅在明确安装请求时提供该工具，且后端会核对工具参数与用户给出的来源。安装器只导入 `SKILL.md` 和限额内 `docs/`、`references/`、`templates/`、`examples/` 中的 UTF-8 `.md`/`.txt` 文件；包内脚本、插件代码、依赖和二进制文件不会下载或执行。安装的内容是提示词指令，不代表对其安全性或准确性的背书；使用前请检查 Skill 内容。

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

这是一个本地开发工具，不是操作系统级沙箱。路径会经过解析并限制在工作区内，
但“执行命令”本身仍然具有当前 ComfyUI 进程用户的权限。危险命令有基础拦截，
但 Act 模式不会逐次等待确认；使用远程访问、反向代理或公网暴露 ComfyUI 时，请先
配置额外的认证层，不要把 API Key 暴露给不可信用户。

API Key 会保存在本机配置文件中（除非只通过 `OMNISCIENCE_API_KEY` 环境变量提供）。
推荐在多人或不可信环境中只使用环境变量，并确保 ComfyUI 只监听可信地址。

## 与原版 Cline 的差异

本项目是独立的 ComfyUI custom node 实现，不是官方 Cline 移植，也没有复刻 VS Code
编辑器 Tab、终端 UI、MCP 市场或多提供商 OAuth。MCP、浏览器自动化和模型 API 依赖
用户配置的外部服务；Playwright 需要额外安装。命令执行、MCP 配置、浏览器导航和
检查点恢复仍然具有当前 ComfyUI 进程的权限，使用前请审查安全边界。

## 测试

在本目录运行：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile __init__.py omniscience_server.py
```
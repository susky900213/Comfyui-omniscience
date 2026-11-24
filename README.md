# ComfyUI Omniscience

**An AI coding assistant that lives inside ComfyUI.**

ComfyUI Omniscience is a custom-node extension that adds an Omniscience sidebar to the ComfyUI web interface. Use an OpenAI-compatible model to inspect and develop custom nodes, manage project tasks, work with the live workflow canvas, discover ComfyUI workflows, and prepare their dependencies.

It runs entirely inside ComfyUI and does not require VS Code or a separate editor extension.

**License:** [PolyForm Noncommercial 1.0.0](LICENSE) — source-available for noncommercial use only. Commercial use is prohibited without separate permission.

[中文说明](README_zh-CN.md)

## Features

- **In-ComfyUI assistant** — open the assistant from the **Omniscience** button in the upper-right corner.
- **OpenAI-compatible providers** — OpenAI, DeepSeek, OpenRouter, Ollama, LM Studio, and compatible custom endpoints.
- **Plan / Act modes** — plan without mutation tools, then switch to Act to apply reviewed changes.
- **Project-aware tools** — list, read, and search files; write files; apply unified diffs; and run development commands inside the task project.
- **Task recovery** — SQLite-backed tasks, messages, events, and file snapshots persist across ComfyUI restarts.
- **Checkpoints** — inspect file changes and restore project files from snapshots.
- **Existing node adoption** — detect projects under `custom_nodes`, review their security scan, and adopt them as editable tasks.
- **Live canvas integration** — read the current ComfyUI canvas, including unsaved changes, through the Workflow page or the `get_current_workflow` tool.
- **Workflow discovery and preparation** — search RunningHub, inspect a selected workflow, identify missing nodes and models, install or download only after explicit confirmation, and diagnose execution.
- **Safe workflow uploads** — install uploaded workflow JSON into ComfyUI's user workflow directory without executing it or overwriting an existing file.
- **Skill management** — create, enable, import, export, and reuse prompt-instruction packages with static or optional AI-assisted security review.
- **Optional integrations** — MCP over stdio or HTTP, plus Playwright browser automation when installed and enabled.
- **Context compaction** — summarize older conversation context when the configured token threshold is reached.

## Requirements

- A working [ComfyUI](https://github.com/comfyanonymous/ComfyUI) installation.
- Python 3.10 or newer.
- An OpenAI-compatible Chat Completions endpoint and a model name.
- An API key for hosted providers; local endpoints such as Ollama or LM Studio may not require one.
- Optional: Playwright and its Chromium browser for browser tools.
- Optional: trusted MCP servers for MCP tools.

The backend uses ComfyUI's existing `aiohttp` environment. The frontend is plain JavaScript and CSS, so there is no npm build step. The repository does not install Python dependencies automatically.

## Installation

### Manual installation

Copy this directory into ComfyUI's `custom_nodes` directory:

```bash
cp -R /path/to/Comfyui-omniscience \
  /path/to/ComfyUI/custom_nodes/Comfyui-omniscience
```

If you cloned the repository, run the command from its parent directory. Restart ComfyUI after installation, then look for **Omniscience** in the upper-right corner.

### Git installation

```bash
git clone <repository-url> Comfyui-omniscience
cp -R Comfyui-omniscience /path/to/ComfyUI/custom_nodes/
```

Replace `<repository-url>` with the URL of your fork or mirror. Do not rename the destination unless you intentionally want a different custom-node folder name.

### Optional Playwright support

Install Playwright in the Python environment used by ComfyUI, then install Chromium:

```bash
/path/to/ComfyUI/.venv/bin/python -m pip install playwright
/path/to/ComfyUI/.venv/bin/python -m playwright install chromium
```

Adjust the Python path for portable installations. Browser automation is optional; the rest of the assistant works without Playwright.

## Quick start

1. Install the extension and restart ComfyUI.
2. Click **Omniscience** in the upper-right corner.
3. Open **Settings** and configure:
   - **Provider** and **API base URL**, for example `https://api.openai.com/v1` or `http://127.0.0.1:11434/v1`.
   - **Model** and, when required, **API key**.
   - **Maximum context tokens** and the **automatic compaction threshold**.
   - **Workspace directory**, the parent directory for new task projects.
   - **ComfyUI directory**, optionally enabling workflow uploads and custom-node adoption.
4. Select **＋ New task**, enter a project name, and describe the task.
5. Keep **Plan** selected for investigation. Switch to **Act** only when you want the assistant to modify files or run commands.

For an existing custom-node project, use its **Pending adoption** card in the task list instead of creating a duplicate project. Adoption scans the project first; a blocked scan requires explicit confirmation in the panel.

## Example tasks

### Develop a custom node

```text
Inspect my custom node and explain how its inputs and outputs are registered.
Find the error in the forward method and propose a minimal fix.
```

Review the plan, then switch to Act:

```text
Apply the reviewed fix and run the project's tests.
```

### Inspect the live workflow

```text
Read my current ComfyUI canvas and summarize the connected nodes.
Do not change the workflow.
```

### Find and prepare a workflow

```text
Search RunningHub for a moss text-to-image workflow.
```

Select a result, inspect it, and review missing dependencies. Node installation, model download, and execution are separate steps and require your confirmation. Workflow search and downloads require network access.

### Install a workflow JSON safely

Upload a `.json` workflow and send:

```text
Install the uploaded workflow file.
```

You can also select `@workflow` in the composer. This action validates and copies the file into `ComfyUI/user/default/workflows`; it does **not** execute the workflow, install nodes, download models, or replace an existing file.

### Reuse a Skill

Open **Skill**, create a reusable instruction set, and select it for a conversation. Skills are injected as prompt instructions; imported or AI-generated instructions are untrusted content and should still be reviewed.

## Configuration

Settings are stored at:

```text
~/.config/comfyui-omniscience/config.json
```

The extension attempts to restrict this file to mode `0600`. API keys are not returned by the settings API. Local state, including task history and Skill data, is stored at `~/.local/share/comfyui-omniscience/` by default.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `OMNISCIENCE_API_KEY` | Model provider API key |
| `OMNISCIENCE_MODEL` | Default model name |
| `OMNISCIENCE_API_BASE_URL` | OpenAI-compatible API base URL |
| `OMNISCIENCE_WORKSPACE` | Parent directory for task projects |
| `OMNISCIENCE_COMFYUI_DIRECTORY` | ComfyUI root directory |
| `OMNISCIENCE_CONFIG_FILE` | Custom settings file path |
| `OMNISCIENCE_STATE_DIRECTORY` | Custom local state directory |

Example:

```bash
export OMNISCIENCE_API_KEY='your-key'
export OMNISCIENCE_MODEL='your-model'
export OMNISCIENCE_API_BASE_URL='https://api.openai.com/v1'
export OMNISCIENCE_WORKSPACE="$HOME/ComfyUI-projects"
export OMNISCIENCE_COMFYUI_DIRECTORY='/path/to/ComfyUI'
```

Environment variables take precedence over stored defaults. Provider and service behavior is configured in **Settings**.

## Security boundaries

Read this section before using Act mode or adopting third-party code.

- **This is not an operating-system sandbox.** File paths are restricted to the task project, but command execution still has the permissions of the ComfyUI process.
- **Act mode does not ask for approval before every tool call.** Review the task, provider, project, and enabled network access before use.
- **API keys and project code may be sent to your configured model provider.** Use a trusted endpoint and do not place secrets in prompts or project files.
- **Static security scans are review aids, not proof that code is safe.** A blocked result requires human confirmation, and that confirmation does not guarantee safety.
- **Third-party nodes, workflows, models, Skills, and MCP servers have their own licenses and risks.** This project's license does not grant rights to them.
- **Do not expose ComfyUI to untrusted users without an authentication layer.** Browser, command, MCP, and file tools can affect the host running ComfyUI.

Use environment variables for API keys on shared or untrusted machines, and keep ComfyUI bound to trusted addresses.

## Project structure

```text
Comfyui-omniscience/
├── __init__.py                 # ComfyUI extension entry point
├── omniscience_server.py       # HTTP route registration
├── omniscience/
│   ├── agent/                  # Agent and project workflows
│   ├── api/                    # REST endpoints
│   ├── tools/                  # Tool definitions and execution
│   └── workflow_manager*.py    # Workflow discovery and execution
├── web/
│   ├── omniscience.js          # Sidebar UI
│   └── omniscience.css         # Sidebar styles
├── tests/                      # Backend and frontend regression tests
├── DESIGN.md                   # Architecture and design notes
└── DESIGN_WORKFLOW_INSTALL.md  # Workflow installation design
```

## Testing

Run from the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile __init__.py omniscience_server.py
```

For integration testing, install the extension in ComfyUI, restart ComfyUI, and exercise the sidebar against a configured model endpoint.

## Contributing

Bug reports and focused pull requests are welcome.

- Reproduce the issue and include your OS, ComfyUI version, provider/model, and relevant logs.
- Remove API keys, tokens, cookies, payment information, and private project content from reports.
- Add regression tests for backend or frontend behavior changes.
- Keep pull requests focused and describe the verification performed.
- By contributing, you agree that your contribution is provided under this project's [PolyForm Noncommercial 1.0.0 license](LICENSE).

## License

Copyright (c) 2026 Comfyui-omniscience contributors.

This project is released under the [PolyForm Noncommercial License 1.0.0](LICENSE) (`PolyForm-Noncommercial-1.0.0`).

Noncommercial personal, research, hobby, educational, charitable, public-safety, environmental, and government uses are permitted as defined by the license. **Commercial use is prohibited without separate written permission from the copyright holder.**

PolyForm Noncommercial is a **source-available** license, not an OSI-approved open-source license, because it restricts commercial use. See the full license for definitions, patent terms, termination rules, and warranty disclaimers.

## Support the project

If ComfyUI Omniscience is useful to you, you can support its development with USDT on **BNB Smart Chain (BEP-20)**:

```text
0x31babbb634bb46df0298d0edb93c1f4eaac73f85
```

Verify the network and address before sending. Cryptocurrency transfers are irreversible.

For Alipay donations, see the [Chinese README](README_zh-CN.md#support-the-project).

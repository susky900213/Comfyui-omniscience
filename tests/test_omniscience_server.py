import tempfile
import unittest
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from omniscience_server import (
    GlobalChatAgent,
    PersistentClineAgent,
    ProjectFolderExistsError,
    SettingsStore,
    Workspace,
    _project_folder_name,
    apply_unified_patch,
)
from omniscience_core import CheckpointManager, ContextManager, SkillStore, TaskStore
from omniscience_mcp import MCPManager


class WorkspaceTests(unittest.TestCase):
    def test_resolve_rejects_traversal_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            with self.assertRaises(ValueError):
                workspace.resolve("../outside.txt")
            with self.assertRaises(ValueError):
                workspace.resolve("/tmp/outside.txt")

    def test_file_operations_stay_inside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            workspace.write_file("src/example.py", "print('hello')\n")
            self.assertEqual(workspace.read_file("src/example.py"), "print('hello')\n")
            self.assertEqual(len(workspace.search_files("hello")), 1)
            self.assertEqual(workspace.list_files("src")[0]["path"], "src/example.py")

    def test_listing_skips_symlink_that_escapes_workspace(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            workspace = Workspace(directory)
            outside_file = Path(outside) / "secret.txt"
            outside_file.write_text("outside\n", encoding="utf-8")
            link = Path(directory) / "external"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks are not available in this environment")
            entries = workspace.list_files(".", recursive=True)
            self.assertFalse(any(entry["path"].startswith("external") for entry in entries))
            self.assertEqual(workspace.search_files("outside"), [])

    def test_unified_patch(self):
        original = "one\ntwo\nthree\n"
        patch = "@@ -1,3 +1,3 @@\n" + " one\n" + "-two\n" + "+TWO\n" + " three\n"
        self.assertEqual(apply_unified_patch(original, patch), "one\nTWO\nthree\n")

    def test_patch_rejects_wrong_context(self):
        patch = "@@ -1,2 +1,2 @@\n" + "-one\n" + "+changed\n" + " two\n"
        with self.assertRaises(ValueError):
            apply_unified_patch("different\ntwo\n", patch)

    def test_unified_patch_can_create_a_file(self):
        patch = "@@ -0,0 +1,2 @@\n" + "+first\n" + "+second\n"
        self.assertEqual(apply_unified_patch("", patch), "first\nsecond\n")


class SettingsTests(unittest.TestCase):
    def test_default_workspace_is_comfyui_node_directory(self):
        import omniscience_server

        with tempfile.TemporaryDirectory() as directory:
            old_default = omniscience_server.DEFAULT_WORKSPACE
            try:
                omniscience_server.DEFAULT_WORKSPACE = Path(directory) / ".Comfyui-node"
                public = SettingsStore(Path(directory) / "config.json").public()
                self.assertEqual(Path(public["workspace"]), (Path(directory) / ".Comfyui-node").resolve())
                self.assertTrue(Path(public["workspace"]).is_dir())
            finally:
                omniscience_server.DEFAULT_WORKSPACE = old_default

    def test_settings_are_saved_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            workspace = Path(directory) / "workspace"
            comfyui = Path(directory) / "ComfyUI"
            workspace.mkdir()
            comfyui.mkdir()
            store = SettingsStore(config)
            public = store.update(
                {
                    "api_base_url": "http://localhost:11434/v1",
                    "model": "qwen",
                    "api_key": "secret",
                    "workspace": str(workspace),
                    "comfyui_directory": str(comfyui),
                }
            )
            self.assertTrue(public["api_key_configured"])
            self.assertNotIn("api_key", public)
            self.assertEqual(store.api_key(), "secret")
            self.assertEqual(Path(public["comfyui_directory"]), comfyui.resolve())

    def test_context_budget_is_saved_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            store = SettingsStore(config)
            public = store.update({"workspace": str(workspace), "max_context_tokens": 4096})
            self.assertEqual(public["max_context_tokens"], 4096)
            with self.assertRaises(ValueError):
                store.update({"max_context_tokens": 512})

    def test_compaction_threshold_is_saved_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            store = SettingsStore(config)
            public = store.update({"workspace": str(workspace), "compaction_threshold_percent": 65})
            self.assertEqual(public["compaction_threshold_percent"], 65)
            with self.assertRaises(ValueError):
                store.update({"compaction_threshold_percent": 100})


class SkillStoreTests(unittest.TestCase):
    def test_skill_store_supports_crud_import_export_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "skills.sqlite3"
            store = SkillStore(database)
            created = store.create(
                {
                    "name": "Node style",
                    "description": "Use the project style.",
                    "content": "Inspect existing nodes before editing.",
                }
            )
            self.assertTrue(created["enabled"])
            self.assertEqual(store.get(created["id"])["name"], "Node style")
            updated = store.update(created["id"], {"enabled": False, "description": "Updated"})
            self.assertFalse(updated["enabled"])
            self.assertEqual(updated["description"], "Updated")

            imported = store.import_items(
                {
                    "skills": [
                        {"name": "Workflow review", "description": "Review graphs", "content": "Read the live workflow first."},
                        {"name": "Node style", "description": "Replaced", "content": "Keep the existing style."},
                    ]
                }
            )
            self.assertEqual(len(imported), 2)
            self.assertEqual(len(store.list()), 2)
            self.assertEqual(store.get(created["id"])["content"], "Keep the existing style.")
            exported = store.export_items()
            self.assertEqual({item["name"] for item in exported}, {"Node style", "Workflow review"})

            reopened = SkillStore(database)
            self.assertEqual(len(reopened.list()), 2)
            with self.assertRaises(ValueError):
                reopened.create({"name": "Node style", "content": "duplicate"})


class PersistentCoreTests(unittest.TestCase):
    def test_project_folder_name_rejects_traversal(self):
        for value in ("", ".", "..", "../outside", "nested/project", "/tmp/project", "project\\child"):
            with self.assertRaises(ValueError):
                _project_folder_name(value)

    def test_create_task_creates_unique_project_folder_and_rejects_duplicate(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Project", "act", "project-one")
                self.assertEqual(task["folder"], "project-one")
                self.assertEqual(task["metadata"]["project_folder"], "project-one")
                self.assertEqual(Path(task["workspace"]), (workspace / "project-one").resolve())
                self.assertTrue((workspace / "project-one").is_dir())
                workflow = workspace / "project-one" / "workflow.json"
                self.assertTrue(workflow.is_file())
                workflow_data = json.loads(workflow.read_text(encoding="utf-8"))
                self.assertEqual(workflow_data["extra"]["cline"]["project"], "project-one")
                self.assertEqual(Path(task["metadata"]["workflow"]), workflow.resolve())
                with self.assertRaises(ProjectFolderExistsError):
                    agent.create_task("Duplicate", "act", "project-one")
                self.assertEqual(len(agent.store.list_tasks()), 1)
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    def test_workflow_generation_never_overwrites_existing_file(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Workflow project", "act", "workflow-project")
                workflow = Path(task["workspace"]) / "workflow.json"
                custom_content = '{"custom": true}\n'
                workflow.write_text(custom_content, encoding="utf-8")

                path, created = agent._ensure_task_workflow(task["id"])

                self.assertEqual(Path(path), workflow)
                self.assertFalse(created)
                self.assertEqual(workflow.read_text(encoding="utf-8"), custom_content)
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    def test_create_task_links_project_into_comfyui_custom_nodes(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            comfyui = root / "ComfyUI"
            workspace.mkdir()
            comfyui.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "comfyui_directory": str(comfyui)})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)

                task = agent.create_task("Linked project", "act", "linked-project")
                project = workspace / "linked-project"
                link = comfyui / "custom_nodes" / "linked-project"

                self.assertTrue(project.is_dir())
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), project.resolve())
                metadata_link = Path(task["metadata"]["comfyui_link"])
                self.assertTrue(metadata_link.is_symlink())
                self.assertEqual(metadata_link.resolve(), project.resolve())
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    def test_create_task_does_not_overwrite_existing_comfyui_node(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            comfyui = root / "ComfyUI"
            existing = comfyui / "custom_nodes" / "existing-node"
            workspace.mkdir()
            existing.mkdir(parents=True)
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "comfyui_directory": str(comfyui)})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)

                with self.assertRaises(ProjectFolderExistsError):
                    agent.create_task("Existing project", "act", "existing-node")

                self.assertFalse((workspace / "existing-node").exists())
                self.assertTrue(existing.is_dir())
                self.assertEqual(agent.store.list_tasks(), [])
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    def test_clear_history_keeps_task_and_project_folder(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Project", "act", "project-one")
                agent.store.append_message(task["id"], {"role": "user", "content": "hello"})
                agent.store.append_event(task["id"], "user_message", {"content": "hello"})
                agent.store.create_approval(task["id"], "run_command", {"command": "echo ok"}, {}, 0)
                cleared = agent.clear_history(task["id"])
                self.assertEqual(cleared["id"], task["id"])
                self.assertEqual(agent.store.messages(task["id"]), [])
                self.assertEqual(agent.store.events(task["id"]), [])
                self.assertIsNone(agent.store.pending_approval(task["id"]))
                self.assertTrue(Path(task["workspace"]).is_dir())
                self.assertEqual(len(agent.store.list_tasks()), 1)
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    def test_task_store_persists_messages_events_and_approvals(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tasks.sqlite3"
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            store = TaskStore(database)
            task = store.create_task(str(workspace), "Persistent", "plan")
            store.append_message(task["id"], {"role": "user", "content": "inspect"})
            store.append_event(task["id"], "user_message", {"ok": True})
            approval = store.create_approval(
                task["id"],
                "run_command",
                {"command": "echo ok"},
                {"id": "call-1", "function": {"name": "run_command"}},
                0,
            )
            reopened = TaskStore(database)
            self.assertEqual(reopened.get_task(task["id"])["mode"], "plan")
            self.assertEqual(reopened.messages(task["id"])[0]["content"], "inspect")
            self.assertEqual(reopened.events(task["id"], after=-1)[0]["kind"], "user_message")
            self.assertEqual(reopened.pending_approval(task["id"])["id"], approval["id"])
            self.assertEqual(reopened.resolve_approval(approval["id"], False)["status"], "rejected")

    def test_task_store_persists_global_messages_without_a_task(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tasks.sqlite3"
            store = TaskStore(database)
            store.append_global_message({"role": "user", "content": "global question"})
            store.append_global_message({"role": "assistant", "content": "global answer"})

            reopened = TaskStore(database)
            self.assertEqual(
                [message["content"] for message in reopened.global_messages()],
                ["global question", "global answer"],
            )
            self.assertEqual(reopened.list_tasks(), [])
            reopened.clear_global_messages()
            self.assertEqual(reopened.global_messages(), [])

    def test_global_chat_uses_independent_history_without_tools_or_tasks(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "global-test-model"})
            old_database = cline_server.TASK_DATABASE
            old_skill_database = cline_server.SKILL_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.SKILL_DATABASE = root / "skills.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = GlobalChatAgent(settings)
                skill = agent.skills.create({"name": "Global helper", "content": "Answer with a short checklist."})
                captured = []

                async def finish_model(messages, tools=None, system_prompt=None, **kwargs):
                    captured.append({
                        "messages": messages,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "tool_choice": kwargs.get("tool_choice"),
                    })
                    return {"choices": [{"message": {"content": "global answer"}}]}

                agent.model._call_model = finish_model
                result = asyncio.run(agent.chat("global question", [skill["id"]]))

                self.assertEqual(result["message"]["content"], "global answer")
                self.assertEqual(captured[0]["tools"], [])
                self.assertIsNone(captured[0]["tool_choice"])
                self.assertIn("independent of every task", captured[0]["system_prompt"])
                self.assertIn("call install_github_skill", captured[0]["system_prompt"])
                self.assertIn("Answer with a short checklist.", captured[0]["system_prompt"])
                self.assertEqual([message["role"] for message in captured[0]["messages"]], ["user"])
                self.assertEqual(agent.store.list_tasks(), [])
                self.assertEqual(
                    [message["content"] for message in agent.store.global_messages()],
                    ["global question", "global answer"],
                )
                self.assertEqual(agent.store.global_messages()[0]["skill_ids"], [skill["id"]])
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.SKILL_DATABASE = old_skill_database
                cline_server.STATE_DIRECTORY = old_state

    def test_global_skill_installer_requires_explicit_matching_public_github_source(self):
        self.assertFalse(GlobalChatAgent._explicit_install_request("Could you explain this skill repository?"))
        self.assertFalse(GlobalChatAgent._explicit_install_request("Install skill guidance from a non-GitHub source"))
        refusal = "我目前无法调用 Skill 安装工具，因此不能替你安装 citrolabs/ego-lite。"
        self.assertFalse(GlobalChatAgent._explicit_install_request(refusal))
        self.assertTrue(GlobalChatAgent._explicit_install_request("安装 citrolabs/ego-lite"))
        self.assertFalse(GlobalChatAgent._explicit_install_request("安装 GitHub 插件 citrolabs/ego-lite"))
        self.assertTrue(GlobalChatAgent._explicit_install_request("请安装skill https://github.com/example/skills"))
        self.assertTrue(GlobalChatAgent._explicit_install_request("请安装skill citrolabs/ego-lite"))
        self.assertFalse(GlobalChatAgent._explicit_install_request("请不要安装skill citrolabs/ego-lite"))
        self.assertFalse(GlobalChatAgent._explicit_install_request("请安装插件 example/plugin"))
        self.assertEqual(
            GlobalChatAgent._requested_skill_source("请安装 Skill：citrolabs/ego-lite"),
            "https://github.com/citrolabs/ego-lite",
        )
        self.assertTrue(
            GlobalChatAgent._explicit_install_request(
                "Install the Skill from https://github.com/example/skills/tree/main/my-skill"
            )
        )
        self.assertTrue(
            GlobalChatAgent._install_source_allowed(
                "Install the Skill from https://github.com/example/skills/tree/main/my-skill",
                "https://github.com/example/skills/tree/main/my-skill",
            )
        )
        self.assertTrue(
            GlobalChatAgent._install_source_allowed(
                "请安装 Skill：citrolabs/ego-lite",
                "https://github.com/citrolabs/ego-lite",
            )
        )
        self.assertTrue(
            GlobalChatAgent._install_source_allowed(
                "Install a Skill from https://github.com/example/skills",
                "https://github.com/example/skills/tree/main/my-skill",
            )
        )
        self.assertFalse(
            GlobalChatAgent._install_source_allowed(
                "Install the Skill from https://github.com/example/skills/tree/main/my-skill",
                "https://github.com/example/skills/tree/main/other-skill",
            )
        )
        self.assertFalse(
            GlobalChatAgent._install_source_allowed(
                "Install the Skill from https://github.com/example/skills/tree/main/my-skill",
                "https://github.com/example/skills/tree/other-branch/my-skill",
            )
        )
        self.assertFalse(
            GlobalChatAgent._install_source_allowed(
                "Can you explain this repository https://github.com/example/skills/tree/main/my-skill",
                "https://github.com/example/skills/tree/main/my-skill",
            )
        )
        self.assertFalse(
            GlobalChatAgent._install_source_allowed(
                "Install the Skill from https://github.com/example/skills/tree/main/my-skill",
                "https://github.com/other/skills/tree/main/my-skill",
            )
        )
        for source in (
            "http://github.com/example/skills",
            "https://github.com.evil.example/example/skills",
            "https://user@github.com/example/skills",
            "https://example.com/skills",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                GlobalChatAgent._github_skill_source(source)

    def test_global_skill_agent_tool_imports_text_package_and_returns_result_to_model(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "global-test-model"})
            old_database = cline_server.TASK_DATABASE
            old_skill_database = cline_server.SKILL_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.SKILL_DATABASE = root / "skills.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = GlobalChatAgent(settings)
                source = "https://github.com/cline/cline/tree/main/.agents/skills/cline-sdk"
                tree = {
                    "truncated": True,
                    "tree": [],
                }

                def github_response(url, accept="application/vnd.github+json", max_bytes=200_000):
                    if url == "https://api.github.com/repos/cline/cline/git/trees/main?recursive=1":
                        return tree
                    if url == "https://api.github.com/repos/cline/cline/contents/.agents/skills/cline-sdk?ref=main":
                        return [
                            {"name": "SKILL.md", "type": "file", "mode": "100644", "size": 100},
                            {"name": "references", "type": "dir", "mode": "040000", "size": 0},
                            {"name": "scripts", "type": "dir", "mode": "040000", "size": 0},
                        ]
                    if url == "https://api.github.com/repos/cline/cline/contents/.agents/skills/cline-sdk/references?ref=main":
                        return [
                            {"name": "agent", "type": "dir", "mode": "040000", "size": 0},
                            {"name": "ignored.pdf", "type": "file", "mode": "100644", "size": 15},
                        ]
                    if url == "https://api.github.com/repos/cline/cline/contents/.agents/skills/cline-sdk/references/agent?ref=main":
                        return [
                            {"name": "api.md", "type": "file", "mode": "100644", "size": 40},
                            {"name": "unsafe.sh", "type": "file", "mode": "100755", "size": 25},
                        ]
                    if url == "https://api.github.com/repos/cline/cline/contents/.agents/skills/cline-sdk/scripts?ref=main":
                        return [{"name": "install.sh", "type": "file", "mode": "100755", "size": 25}]
                    if url.endswith("/SKILL.md"):
                        return "---\nname: cline-sdk\ndescription: Test imported Skill\n---\n# Instructions\nUse this package safely."
                    if url.endswith("/api.md"):
                        return "Reference notes."
                    self.fail(f"Installer fetched an unexpected file: {url}")

                captured = []

                async def model_with_tool_roundtrip(messages, tools=None, tool_choice=None, **kwargs):
                    captured.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
                    self.assertEqual([tool["function"]["name"] for tool in tools], ["install_github_skill"])
                    if not any(message.get("role") == "tool" for message in messages):
                        self.assertEqual(tool_choice, "required")
                        return {"choices": [{"message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "install-cline-sdk",
                                "type": "function",
                                "function": {
                                    "name": "install_github_skill",
                                    "arguments": json.dumps({"github_url": source}),
                                },
                            }],
                        }}]}
                    self.assertTrue(any(message.get("role") == "tool" for message in messages))
                    tool_result = next(message for message in messages if message.get("role") == "tool")
                    self.assertEqual(json.loads(tool_result["content"])["status"], "installed")
                    return {"choices": [{"message": {"content": "Agent confirms Skill installed."}}]}

                agent.model._call_model = model_with_tool_roundtrip
                with patch.object(GlobalChatAgent, "_github_request", side_effect=github_response):
                    result = asyncio.run(agent.chat(f"Please install this Skill: {source}"))

                self.assertEqual(result["message"]["content"], "Agent confirms Skill installed.")
                self.assertEqual(len(captured), 2)
                self.assertEqual(agent.store.list_tasks(), [])
                installed = agent.skills.list()
                self.assertEqual([skill["name"] for skill in installed], ["cline-sdk"])
                self.assertIn("Use this package safely.", installed[0]["content"])
                self.assertIn("Reference notes.", installed[0]["content"])
                self.assertNotIn("install.sh", installed[0]["content"])
                transcript = agent.store.global_messages()
                self.assertEqual([message["role"] for message in transcript], ["user", "assistant", "tool", "assistant"])
                tool_result = json.loads(transcript[-2]["content"])
                self.assertEqual(tool_result["status"], "installed")
                self.assertFalse(tool_result["scripts_executed"])
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.SKILL_DATABASE = old_skill_database
                cline_server.STATE_DIRECTORY = old_state

    def test_global_chat_requires_agent_tool_call_for_explicit_install_request(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "global-test-model"})
            old_database = cline_server.TASK_DATABASE
            old_skill_database = cline_server.SKILL_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.SKILL_DATABASE = root / "skills.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = GlobalChatAgent(settings)
                installed_skill = {
                    "id": "ego-lite-id",
                    "name": "ego-lite",
                    "description": "Ego-lite skill",
                    "content": "Use the ego-lite process.",
                    "enabled": True,
                }
                agent.install_github_skill = AsyncMock(return_value={"skill": installed_skill, "files_imported": 1})
                agent.model._call_model = AsyncMock(return_value={"choices": [{"message": {
                    "content": "I cannot call the Skill install tool.",
                }}]})

                result = asyncio.run(agent.chat("请安装 Skill：citrolabs/ego-lite"))

                agent.install_github_skill.assert_not_awaited()
                agent.model._call_model.assert_awaited_once()
                call = agent.model._call_model.await_args
                self.assertEqual([tool["function"]["name"] for tool in call.kwargs["tools"]], ["install_github_skill"])
                self.assertEqual(call.kwargs["tool_choice"], "required")
                self.assertEqual(result["message"]["content"], "I cannot call the Skill install tool.")
                self.assertEqual(agent.skills.list(), [])
                self.assertEqual(agent.store.list_tasks(), [])
                self.assertEqual([message["role"] for message in agent.store.global_messages()], ["user", "assistant"])
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.SKILL_DATABASE = old_skill_database
                cline_server.STATE_DIRECTORY = old_state

    def test_task_store_persists_current_canvas_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tasks.sqlite3"
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            store = TaskStore(database)
            task = store.create_task(str(workspace), "Canvas")
            workflow = {"nodes": [{"id": 7, "type": "TestNode"}], "links": [], "extra": {"unsaved": True}}
            stored = store.set_current_workflow(task["id"], workflow)

            reopened = TaskStore(database)
            current = reopened.current_workflow(task["id"])
            self.assertEqual(current["workflow"], workflow)
            self.assertEqual(current["source"], "comfyui_canvas")
            self.assertEqual(stored["workflow"], workflow)

    def test_skills_are_global_and_selected_per_message(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            old_database = cline_server.TASK_DATABASE
            old_skill_database = cline_server.SKILL_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.SKILL_DATABASE = root / "skills.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Skill task", "act", "skill-task")
                other_task = agent.create_task("Other task", "act", "other-task")
                active = agent.skills.create({"name": "Active", "content": "Always inspect node registration."})
                disabled = agent.skills.create({"name": "Disabled", "content": "This must not be injected."})
                agent.skills.update(disabled["id"], {"enabled": False})

                selected = agent.selected_skills(task["id"], [active["id"]])
                self.assertEqual([skill["name"] for skill in selected], ["Active"])
                prompt = agent.system_prompt(task["id"], skill_ids=[active["id"]])
                self.assertIn("Always inspect node registration.", prompt)
                self.assertNotIn("This must not be injected.", prompt)
                self.assertNotIn("Always inspect node registration.", agent.system_prompt(other_task["id"], skill_ids=[]))
                with self.assertRaises(ValueError):
                    agent._validated_skill_ids([disabled["id"]])
                with self.assertRaises(ValueError):
                    agent._validated_skill_ids(["missing-skill"])

                captured = {}

                async def finish_model(*args, **kwargs):
                    captured.update(kwargs)
                    return {"choices": [{"message": {"content": "已检查。"}}]}

                agent._call_model = finish_model
                result = asyncio.run(agent.chat(task["id"], "检查节点", skill_ids=[active["id"]]))
                self.assertEqual(result["status"], "ok")
                self.assertIn("Always inspect node registration.", captured["system_prompt"])
                user_message = agent.store.messages(task["id"])[0]
                self.assertEqual(user_message["skill_ids"], [active["id"]])
                self.assertNotIn("skill_ids", agent.task(task["id"])["metadata"])

                status = asyncio.run(agent.context_status(task["id"], [active["id"]]))
                self.assertGreater(status["system_tokens"], ContextManager.estimate_tokens(cline_server.SYSTEM_PROMPT))

                agent.remove_skill(active["id"])
                self.assertNotIn("skill_ids", agent.task(task["id"])["metadata"])
                self.assertEqual(agent.selected_skills(task["id"], [active["id"]]), [])
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.SKILL_DATABASE = old_skill_database
                cline_server.STATE_DIRECTORY = old_state

    def test_removing_global_skill_does_not_update_tasks(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            old_database = cline_server.TASK_DATABASE
            old_skill_database = cline_server.SKILL_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.SKILL_DATABASE = root / "skills.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                skill = agent.skills.create({"name": "Shared", "content": "Use the shared instruction."})
                task = agent.store.create_task(
                    str(workspace / "legacy-task"),
                    "Legacy task",
                    "act",
                    {"skill_ids": [skill["id"]]},
                )
                agent.remove_skill(skill["id"])

                self.assertEqual(agent.task(task["id"])["metadata"]["skill_ids"], [skill["id"]])
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.SKILL_DATABASE = old_skill_database
                cline_server.STATE_DIRECTORY = old_state


    def test_cancel_task_rejects_pending_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tasks.sqlite3"
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            store = TaskStore(database)
            task = store.create_task(str(workspace), "Cancellable")
            approval = store.create_approval(task["id"], "run_command", {"command": "echo ok"}, {}, 0)

            cancelled = store.cancel_task(task["id"])

            self.assertEqual(cancelled["status"], "idle")
            self.assertIsNone(store.pending_approval(task["id"]))
            self.assertEqual(store.approval(approval["id"])["status"], "rejected")

    def test_context_manager_compacts_old_messages(self):
        manager = ContextManager(max_chars=50, keep_messages=2)
        messages, compacted = manager.prepare(
            [
                {"role": "user", "content": "a" * 30},
                {"role": "assistant", "content": "b" * 30},
                {"role": "user", "content": "c" * 30},
                {"role": "assistant", "content": "d" * 30},
            ]
        )
        self.assertTrue(compacted)
        self.assertLessEqual(sum(len(str(message.get("content") or "")) for message in messages), 50)
        self.assertTrue(any("Earlier context compacted" in message["content"] for message in messages))

    def test_context_manager_uses_eighty_percent_token_threshold(self):
        manager = ContextManager(max_tokens=100, keep_messages=2)
        below = [{"role": "user", "content": "甲" * 20}] * 3
        above = [{"role": "user", "content": "甲" * 24}] * 3
        self.assertFalse(manager.needs_compaction(below))
        self.assertTrue(manager.needs_compaction(above))
        self.assertFalse(manager.needs_compaction([]))

    def test_context_manager_uses_configured_threshold_and_full_prompt_size(self):
        messages = [{"role": "user", "content": "甲" * 85}]
        self.assertTrue(ContextManager(max_tokens=100, keep_messages=14).needs_compaction(messages))
        self.assertFalse(
            ContextManager(max_tokens=100, keep_messages=14, compaction_threshold=0.9).needs_compaction(messages)
        )
        self.assertTrue(ContextManager(max_tokens=100, keep_messages=14).needs_compaction(messages, reserved_tokens=10))

    def test_context_manager_does_not_duplicate_a_single_oversized_message(self):
        manager = ContextManager(max_tokens=100, keep_messages=14)
        messages = [{"role": "user", "content": "甲" * 100}]

        compacted, changed = manager.fallback(messages)

        self.assertTrue(changed)
        self.assertEqual(len(compacted), 1)
        self.assertLess(len(compacted[0]["content"]), len(messages[0]["content"]))

    def test_context_manager_keeps_recent_tool_call_pair(self):
        manager = ContextManager(max_tokens=200, keep_messages=2)
        messages = [
            {"role": "user", "content": "需求" * 40},
            {"role": "assistant", "content": "旧回复" * 30},
            {"role": "user", "content": "旧问题" * 30},
            {"role": "assistant", "content": "旧工具前回复" * 20},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "工具结果" * 30},
        ]
        compacted, changed = manager.with_summary(messages, "已保留需求、工具调用和工具结果。")
        self.assertTrue(changed)
        self.assertEqual(compacted[-2]["role"], "assistant")
        self.assertEqual(compacted[-1]["role"], "tool")
        self.assertEqual(compacted[-1]["tool_call_id"], "call-1")

    def test_checkpoint_restores_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            state = Path(directory) / "state"
            file_path = root / "example.txt"
            file_path.write_text("before\n", encoding="utf-8")
            manager = CheckpointManager(state)
            backend, reference, _ = manager.create("task", root, "before change")
            file_path.write_text("after\n", encoding="utf-8")
            manager.restore("task", root, backend, reference)
            self.assertEqual(file_path.read_text(encoding="utf-8"), "before\n")


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def _agent_for_context_test(self, directory, max_tokens=1024):
        import cline_server

        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        settings = SettingsStore(root / "settings.json")
        settings.update({"workspace": str(workspace), "model": "test-model", "max_context_tokens": max_tokens})
        old_database = cline_server.TASK_DATABASE
        old_state = cline_server.STATE_DIRECTORY
        cline_server.TASK_DATABASE = root / "tasks.sqlite3"
        cline_server.STATE_DIRECTORY = root / "state"
        agent = PersistentClineAgent(settings)
        agent.context = ContextManager(max_tokens=max_tokens, keep_messages=2)
        return agent, settings, old_database, old_state

    async def test_context_compaction_skips_short_history(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _settings, old_database, old_state = await self._agent_for_context_test(directory, 10_000)
            try:
                called = False

                async def unexpected_call(*args, **kwargs):
                    nonlocal called
                    called = True
                    return {"choices": [{"message": {"content": "unexpected"}}]}

                agent._call_model = unexpected_call
                messages, compacted = await agent._compact_messages(
                    [{"role": "user", "content": "short"}], [], "act"
                )
                self.assertFalse(compacted)
                self.assertEqual(messages[0]["content"], "short")
                self.assertFalse(called)
            finally:
                import cline_server
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_context_compaction_uses_model_summary_without_mutating_history(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            agent, _settings, old_database, old_state = await self._agent_for_context_test(directory)
            try:
                messages = [
                    {"role": "user", "content": "需求" * 60},
                    {"role": "assistant", "content": "旧回复" * 60},
                    {"role": "user", "content": "最近问题" * 40},
                    {"role": "assistant", "content": "最近回复" * 40},
                ]
                original = [dict(message) for message in messages]
                calls = []

                async def summarize(request_messages, tools, mode, **kwargs):
                    calls.append((request_messages, tools, mode, kwargs))
                    return {"choices": [{"message": {"content": "用户要开发节点；旧回复已完成初步检查。"}}]}

                agent._call_model = summarize
                compacted, changed = await agent._compact_messages(messages, [], "act")
                self.assertTrue(changed)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][1], [])
                self.assertEqual(calls[0][3]["system_prompt"], cline_server.COMPACTION_SYSTEM_PROMPT)
                self.assertTrue(any("summarized by Cline" in item["content"] for item in compacted))
                self.assertEqual(messages, original)
            finally:
                import cline_server
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_context_compaction_falls_back_when_summary_model_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _settings, old_database, old_state = await self._agent_for_context_test(directory)
            try:
                messages = [
                    {"role": "user", "content": "需求" * 60},
                    {"role": "assistant", "content": "旧回复" * 60},
                    {"role": "user", "content": "最近问题" * 40},
                    {"role": "assistant", "content": "最近回复" * 40},
                ]

                async def failing_summary(*args, **kwargs):
                    raise RuntimeError("summary unavailable")

                agent._call_model = failing_summary
                compacted, changed = await agent._compact_messages(messages, [], "act")
                self.assertTrue(changed)
                self.assertTrue(any("Earlier context compacted" in item["content"] for item in compacted))
            finally:
                import cline_server
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_context_compaction_falls_back_when_summary_is_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            agent, _settings, old_database, old_state = await self._agent_for_context_test(directory)
            try:
                messages = [
                    {"role": "user", "content": "需求" * 60},
                    {"role": "assistant", "content": "旧回复" * 60},
                    {"role": "user", "content": "最近问题" * 40},
                    {"role": "assistant", "content": "最近回复" * 40},
                ]

                async def whitespace_summary(*args, **kwargs):
                    return {"choices": [{"message": {"content": " \n\t "}}]}

                agent._call_model = whitespace_summary
                compacted, changed = await agent._compact_messages(messages, [], "act")
                self.assertTrue(changed)
                self.assertTrue(any("Earlier context compacted" in item["content"] for item in compacted))
            finally:
                import cline_server
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_context_status_reports_complete_prompt_length(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "compaction_threshold_percent": 70})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Prompt stats", "act", "prompt-stats")
                agent.store.append_message(task["id"], {"role": "user", "content": "需求" * 20})

                status = await agent.context_status(task["id"])

                self.assertEqual(status["compaction_threshold_percent"], 70)
                self.assertEqual(
                    status["prompt_tokens"],
                    status["message_tokens"] + status["system_tokens"] + status["tools_tokens"],
                )
                self.assertGreater(status["prompt_chars"], 0)
                self.assertGreaterEqual(status["prompt_percent"], 0)
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_tool_definitions_include_read_only_current_canvas_tool(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                act_tools = await agent._tool_definitions("act")
                plan_tools = await agent._tool_definitions("plan")

                self.assertIn("get_current_workflow", {item["function"]["name"] for item in act_tools})
                self.assertIn("get_current_workflow", {item["function"]["name"] for item in plan_tools})
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_current_canvas_tool_reports_when_no_workflow_is_synced(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Canvas unavailable", "act", "canvas-unavailable")

                result = await agent._execute_tool(task["id"], "get_current_workflow", {})

                self.assertFalse(result["available"])
                self.assertIn("尚未同步", result["message"])
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_agent_reads_current_canvas_workflow_without_touching_saved_workflow(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "test-model"})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Canvas tool", "act", "canvas-tool")
                saved = (workspace / "canvas-tool" / "workflow.json").read_text(encoding="utf-8")
                live = {"nodes": [{"id": 1, "type": "UnsavedCustomNode"}], "links": []}
                agent.update_current_workflow(task["id"], live)

                result = await agent._execute_tool(task["id"], "get_current_workflow", {})

                self.assertTrue(result["available"])
                self.assertEqual(result["workflow"], live)
                self.assertEqual((workspace / "canvas-tool" / "workflow.json").read_text(encoding="utf-8"), saved)
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_current_canvas_workflow_rejects_oversized_payload(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Large canvas", "act", "large-canvas")
                with self.assertRaisesRegex(ValueError, "过大"):
                    agent.update_current_workflow(task["id"], {"payload": "x" * (cline_server.MAX_CURRENT_WORKFLOW_BYTES + 1)})
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_current_canvas_workflow_rejects_non_json_values(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Invalid canvas", "act", "invalid-canvas")
                with self.assertRaisesRegex(ValueError, "有效的 JSON"):
                    agent.update_current_workflow(task["id"], {"not_json": float("nan")})
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_act_tools_execute_without_approval(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "test-model"})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("direct execution smoke test", "act", "direct-execution")
                executed = []

                async def execute_tool(task_id, name, arguments):
                    executed.append((task_id, name, arguments))
                    return {"ok": True}

                async def skip_checkpoint(task_id, tool_name):
                    return None

                agent._execute_tool = execute_tool
                agent._checkpoint_after_tool = skip_checkpoint
                calls = [{
                    "id": "call-1",
                    "function": {"name": "write_file", "arguments": '{"path":"example.txt","content":"ok"}'},
                }]

                result = await agent._process_tool_calls(task["id"], calls, 0, "act")

                self.assertIsNone(result)
                self.assertEqual(executed[0][1], "write_file")
                self.assertIsNone(agent.store.pending_approval(task["id"]))
                self.assertEqual(agent.store.events(task["id"])[-1]["kind"], "tool_result")
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_task_completion_repairs_missing_comfyui_link(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            comfyui = root / "ComfyUI"
            workspace.mkdir()
            comfyui.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "comfyui_directory": str(comfyui), "model": "test-model"})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Repair link", "act", "repair-link")
                link = comfyui / "custom_nodes" / "repair-link"
                link.unlink()

                async def finish_model(*args, **kwargs):
                    return {"choices": [{"message": {"content": "开发完成。"}}]}

                agent._call_model = finish_model
                result = await agent.chat(task["id"], "完成节点开发")

                self.assertEqual(result["status"], "ok")
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), (workspace / "repair-link").resolve())
                completed_task = agent.task(task["id"])
                self.assertEqual(Path(completed_task["metadata"]["comfyui_link"]).resolve(), link.resolve())
                workflow = workspace / "repair-link" / "workflow.json"
                self.assertTrue(workflow.is_file())
                self.assertEqual(result["workflow"], str(workflow.resolve()))
                self.assertEqual(agent.store.events(task["id"])[-1]["kind"], "task_finished")
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_task_completion_repairs_missing_workflow_without_overwriting_custom_workflow(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "test-model"})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Workflow repair", "act", "workflow-repair")
                workflow = workspace / "workflow-repair" / "workflow.json"
                workflow.unlink()

                async def finish_model(*args, **kwargs):
                    return {"choices": [{"message": {"content": "完成。"}}]}

                agent._call_model = finish_model
                result = await agent.chat(task["id"], "完成开发")

                self.assertTrue(workflow.is_file())
                self.assertTrue(result["workflow_created"])
                repaired_content = workflow.read_text(encoding="utf-8")
                self.assertEqual(json.loads(repaired_content)["extra"]["cline"]["project"], "workflow-repair")

                custom_content = '{"my_workflow": "keep"}\n'
                workflow.write_text(custom_content, encoding="utf-8")
                await agent.chat(task["id"], "再次完成")
                self.assertEqual(workflow.read_text(encoding="utf-8"), custom_content)
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_task_completion_links_project_when_comfyui_is_configured_later(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            comfyui = root / "ComfyUI"
            workspace.mkdir()
            comfyui.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "test-model"})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Late link", "act", "late-link")
                settings.update({"comfyui_directory": str(comfyui)})

                async def finish_model(*args, **kwargs):
                    return {"choices": [{"message": {"content": "节点开发完成。"}}]}

                agent._call_model = finish_model
                await agent.chat(task["id"], "完成开发")

                link = comfyui / "custom_nodes" / "late-link"
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), (workspace / "late-link").resolve())
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_plan_tool_calls_cannot_modify_workspace(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "test-model"})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("plan safety smoke test", "plan", "plan-safety")
                executed = []

                async def execute_tool(task_id, name, arguments):
                    executed.append(name)
                    return {"unexpected": True}

                async def skip_checkpoint(task_id, tool_name):
                    return None

                agent._execute_tool = execute_tool
                agent._checkpoint_after_tool = skip_checkpoint
                calls = [{
                    "id": "call-1",
                    "function": {"name": "write_file", "arguments": '{"path":"blocked.txt","content":"no"}'},
                }]

                await agent._process_tool_calls(task["id"], calls, 0, "plan")

                self.assertEqual(executed, [])
                self.assertFalse((workspace / "blocked.txt").exists())
                self.assertIn("PLAN 模式下不可用", agent.store.messages(task["id"])[-1]["content"])
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_model_failure_does_not_leave_task_running(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "test-model"})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("failure smoke test")

                async def fail_model(*args, **kwargs):
                    raise RuntimeError("model unavailable")

                agent._call_model = fail_model
                with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                    await agent.chat(task["id"], "hello")

                self.assertEqual(agent.task(task["id"])["status"], "idle")
                self.assertEqual(agent.store.events(task["id"])[-1]["kind"], "task_failed")
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_force_cancel_interrupts_model_wait_without_failure(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "test-model"})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("cancel smoke test")
                model_started = asyncio.Event()

                async def wait_model(*args, **kwargs):
                    model_started.set()
                    await asyncio.Future()

                agent._call_model = wait_model
                running = asyncio.create_task(agent.chat(task["id"], "hello"))
                await asyncio.wait_for(model_started.wait(), timeout=1)

                cancelled = agent.cancel_task(task["id"])
                result = await asyncio.wait_for(running, timeout=1)

                self.assertEqual(cancelled["status"], "idle")
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual(agent.task(task["id"])["status"], "idle")
                self.assertNotIn("task_failed", [event["kind"] for event in agent.store.events(task["id"])])
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_cancelled_run_can_continue_in_same_task_and_workspace(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "test-model"})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Continue after cancel", "act", "same-node")
                model_started = asyncio.Event()

                async def wait_model(*args, **kwargs):
                    model_started.set()
                    await asyncio.Future()

                agent._call_model = wait_model
                running = asyncio.create_task(agent.chat(task["id"], "第一次提问"))
                await asyncio.wait_for(model_started.wait(), timeout=1)

                cancelled = agent.cancel_task(task["id"])
                cancelled_result = await asyncio.wait_for(running, timeout=1)

                self.assertEqual(cancelled["status"], "idle")
                self.assertEqual(cancelled_result["status"], "cancelled")
                self.assertEqual(agent.task(task["id"])["status"], "idle")

                async def finish_model(*args, **kwargs):
                    return {"choices": [{"message": {"content": "继续处理完成。"}}]}

                agent._call_model = finish_model
                resumed = await agent.chat(task["id"], "取消后继续提问")

                self.assertEqual(resumed["status"], "ok")
                self.assertEqual(agent.task(task["id"])["id"], task["id"])
                self.assertEqual(agent.task(task["id"])["workspace"], task["workspace"])
                self.assertEqual(len(agent.store.list_tasks()), 1)
                self.assertEqual(
                    [message["content"] for message in agent.store.messages(task["id"]) if message["role"] == "user"],
                    ["第一次提问", "取消后继续提问"],
                )
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state

    async def test_force_cancel_terminates_running_command(self):
        import cline_server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace), "model": "test-model"})
            old_database = cline_server.TASK_DATABASE
            old_state = cline_server.STATE_DIRECTORY
            try:
                cline_server.TASK_DATABASE = root / "tasks.sqlite3"
                cline_server.STATE_DIRECTORY = root / "state"
                agent = PersistentClineAgent(settings)
                task = agent.create_task("command cancel smoke test")
                agent.store.update_task(task["id"], status="running")
                command = asyncio.create_task(
                    agent._run_command(task["id"], f'"{sys.executable}" -c "import time; time.sleep(30)"', 120, agent.workspace())
                )
                for _ in range(100):
                    if agent._processes:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(agent._processes)

                agent.cancel_task(task["id"])
                await asyncio.wait_for(command, timeout=2)

                self.assertEqual(agent.task(task["id"])["status"], "idle")
                self.assertFalse(agent._processes)
            finally:
                cline_server.TASK_DATABASE = old_database
                cline_server.STATE_DIRECTORY = old_state


class WebRegressionTests(unittest.TestCase):
    def test_cancelled_task_message_reuses_current_node_without_folder_prompt(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "cline.js").read_text(encoding="utf-8")
        send_start = source.index("async function sendMessage")
        send_end = source.index("async function cancelTask", send_start)
        send_message = source[send_start:send_end]

        self.assertNotIn('state.task?.status === "cancelled"', send_message)
        self.assertIn('if (!currentTaskId())', send_message)
        self.assertIn("requestProjectFolder()", send_message)
        self.assertIn('/messages', send_message)

    def test_current_task_displays_prompt_length(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "cline.js").read_text(encoding="utf-8")

        self.assertIn("function promptLengthText(stats)", source)
        self.assertIn('el("span", "cline-prompt-stats"', source)
        self.assertIn("promptLengthText(state.contextStats)", source)
        self.assertIn("await loadContextStats();", source)

    def test_skill_management_and_task_selector_are_present(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "cline.js").read_text(encoding="utf-8")

        self.assertIn('"skills", "Skill"', source)
        self.assertIn("function renderSkills()", source)
        self.assertIn("/cline/skills/import", source)
        self.assertIn("/cline/skills/", source)
        self.assertIn("function renderSkillSelector()", source)
        self.assertIn("function skillCommand(value)", source)
        self.assertIn("function matchingSkills()", source)
        self.assertIn("selectSkillFromMenu", source)
        self.assertIn("event.key === \"ArrowDown\"", source)
        self.assertIn("event.key === \"ArrowUp\"", source)
        self.assertIn("event.key === \"Escape\"", source)
        self.assertIn('skill_ids: [...state.selectedSkillIds]', source)
        self.assertNotIn("/cline/tasks/${encodeURIComponent(currentTaskId())}/skills", source)

    def test_global_chat_has_independent_header_button_and_send_route(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "cline.js").read_text(encoding="utf-8")

        self.assertIn('"/cline/global-chat"', source)
        self.assertIn('"/cline/global-chat/messages"', source)
        self.assertIn('"/cline/global-chat/history"', source)
        self.assertIn("cline-global-chat-toggle", source)
        self.assertIn("state.globalChatActive", source)
        self.assertIn("if (state.globalChatActive)", source)
        self.assertIn("requestProjectFolder()", source)
        global_branch = source[source.index("if (state.globalChatActive) {", source.index("async function sendMessage")):source.index("if (!currentTaskId())", source.index("async function sendMessage"))]
        self.assertNotIn("requestProjectFolder()", global_branch)


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_tools_list_handles_initialized_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "mcp.json"
            script = (
                "import sys,json\n"
                "for line in sys.stdin:\n"
                " m=json.loads(line)\n"
                " if 'id' not in m: continue\n"
                " r={'jsonrpc':'2.0','id':m['id'],'result':{}}\n"
                " if m['method']=='tools/list': r['result']={'tools':[{'name':'echo','description':'Echo','inputSchema':{'type':'object'}}]}\n"
                " print(json.dumps(r),flush=True)\n"
            )
            config.write_text(
                json.dumps({"mcpServers": {"mock": {"command": sys.executable, "args": ["-c", script]}}}),
                encoding="utf-8",
            )
            manager = MCPManager(config)
            groups = await asyncio.wait_for(manager.list_tools(), timeout=5)
            self.assertEqual(groups[0]["tools"][0]["name"], "mcp__mock__echo")


if __name__ == "__main__":
    unittest.main()
from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from omniscience import paths
from omniscience.agent.global_chat import GlobalChatAgent
from omniscience.agent.runtime import PersistentClineAgent
from omniscience.api.turns import TaskTurnController
from omniscience.i18n import normalize_locale
from omniscience.prompts import build_system_prompt
from omniscience.settings import SettingsStore


class LocaleTests(unittest.IsolatedAsyncioTestCase):
    def test_locale_normalization_uses_chinese_only_for_chinese_locales(self):
        self.assertEqual(normalize_locale("zh-CN"), "zh")
        self.assertEqual(normalize_locale("zh_TW"), "zh")
        self.assertEqual(normalize_locale("en-US"), "en")
        self.assertEqual(normalize_locale("ja-JP"), "en")

    def test_system_prompt_switches_language_and_reply_contract(self):
        english = build_system_prompt("act", locale="en")
        chinese = build_system_prompt("plan", locale="zh-CN")
        self.assertIn("Always write all user-facing", english)
        self.assertIn("You are in Plan mode", build_system_prompt("plan", locale="en"))
        self.assertIn("始终使用简体中文", chinese)
        self.assertIn("你处于 Plan 模式", chinese)
        self.assertNotIn("Always write all user-facing", chinese)

    def test_frontend_translator_handles_static_and_dynamic_copy(self):
        script = """
import { createTranslator, normalizeLocale } from './web/i18n.js';
let locale = 'en';
const translator = createTranslator(() => locale);
const checks = [
  normalizeLocale('zh-TW') === 'zh',
  translator.translate('全局对话') === 'Global chat',
  translator.translate('AI 助手') === 'AI Assistant',
  translator.translate('全能') === 'Omni',
  translator.translate('检测失败：网络不可用') === 'Scan failed: 网络不可用',
  translator.translate('共 2 种节点类型，3 个工作流实例。') === '2 node types and 3 workflow instances in total.',
  translator.translate('提示词：约 3,886 Token · 13,129 字符') === 'Prompt: about 3,886 tokens · 13,129 characters',
  translator.translate('约 3,886 Token · 13,129 字符') === 'About 3,886 tokens · 13,129 characters',
  translator.translate('当前提示词：约 3,886 Token，13,129 字符，占上下文上限 12.1%（消息 0，系统 1,399，工具 2,487）。达到 80% 时自动压缩。') === 'Current prompt: about 3,886 tokens, 13,129 characters, 12.1% of the context limit (messages 0, system 1,399, tools 2,487). Automatically compacts at 80%.',
];
locale = 'zh';
checks.push(translator.translate('Global chat') === '全局对话');
checks.push(translator.translate('AI Assistant') === 'AI 助手');
checks.push(translator.translate('Omni') === '全能');
checks.push(translator.translate('Prompt: about 3,886 tokens · 13,129 characters') === '提示词：约 3,886 Token · 13,129 字符');
checks.push(translator.translate('Scan failed: 网络不可用') === '检测失败：网络不可用');
const attributes = { 'aria-label': 'AI 助手', title: 'AI 助手' };
locale = 'en';
globalThis.document = { createTreeWalker: () => ({ nextNode: () => false }) };
globalThis.NodeFilter = { SHOW_TEXT: 4 };
const root = {
  matches: (selector) => selector.includes('[aria-label]'),
  querySelectorAll: () => [],
  hasAttribute: (name) => name in attributes,
  getAttribute: (name) => attributes[name],
  setAttribute: (name, value) => { attributes[name] = value; },
};
translator.localize(root);
checks.push(attributes['aria-label'] === 'AI Assistant' && attributes.title === 'AI Assistant');
locale = 'zh';
translator.localize(root);
checks.push(attributes['aria-label'] === 'AI 助手' && attributes.title === 'AI 助手');
if (checks.some((value) => !value)) process.exit(1);
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frontend_locale_listener_uses_setting_change_payload(self):
        script = """
import { readFileSync } from 'node:fs';
const source = readFileSync('./web/omniscience.js', 'utf8');
const start = source.indexOf('function installLocaleListener()');
const end = source.indexOf('function installTranslationObserver()', start);
if (start < 0 || end < 0) process.exit(1);
const settings = new EventTarget();
const listeners = new Map();
const window = { addEventListener: (name, handler) => listeners.set(name, handler) };
const document = { hidden: false, addEventListener: (name, handler) => listeners.set(name, handler) };
const applied = [];
const install = new Function(
  'app', 'window', 'document', 'configuredLocale', 'applyLocale', 'syncLocale',
  `${source.slice(start, end)}; return installLocaleListener;`
)({ ui: { settings } }, window, document, () => '', (value) => applied.push(value), () => {});
install();
settings.dispatchEvent(new CustomEvent('Comfy.Locale.change', { detail: { value: 'en', oldValue: 'zh' } }));
settings.dispatchEvent(new CustomEvent('Comfy.Locale.change', { detail: { value: 'zh-CN', oldValue: 'en' } }));
if (applied.join(',') !== 'en,zh-CN') process.exit(1);
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_task_prompt_uses_latest_message_locale(self):
        from omniscience import paths

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            previous = (paths.TASK_DATABASE, paths.SKILL_DATABASE, paths.STATE_DIRECTORY)
            paths.TASK_DATABASE = root / "tasks.sqlite3"
            paths.SKILL_DATABASE = root / "skills.sqlite3"
            paths.STATE_DIRECTORY = root / "state"
            try:
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Locale", "act", "locale")
                agent.store.append_message(task["id"], {"role": "user", "content": "hello", "locale": "en"})
                english = agent.system_prompt(task["id"])
                agent.store.append_message(task["id"], {"role": "user", "content": "你好", "locale": "zh"})
                chinese = agent.system_prompt(task["id"])
                self.assertIn("Network access is disabled", english)
                self.assertIn("当前任务未开启网络访问", chinese)
                self.assertNotIn("locale", agent._messages_for_model(agent.store.messages(task["id"]))[0])
            finally:
                paths.TASK_DATABASE, paths.SKILL_DATABASE, paths.STATE_DIRECTORY = previous

    async def test_global_chat_prompt_and_history_use_requested_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            previous = (paths.TASK_DATABASE, paths.SKILL_DATABASE, paths.STATE_DIRECTORY)
            paths.TASK_DATABASE = root / "tasks.sqlite3"
            paths.SKILL_DATABASE = root / "skills.sqlite3"
            paths.STATE_DIRECTORY = root / "state"
            try:
                agent = GlobalChatAgent(settings)
                prompts = []

                async def answer(messages, tools=None, system_prompt=None, **kwargs):
                    prompts.append(system_prompt)
                    return {"choices": [{"message": {"content": "Hello"}}]}

                agent.model._call_model = answer
                await agent.chat("Hello", locale="en")
                await agent.chat("你好", locale="zh-CN")
                self.assertIn("Always write all user-facing", prompts[0])
                self.assertIn("Tools Available This Turn", prompts[0])
                self.assertIn("始终使用简体中文", prompts[1])
                self.assertIn("本轮可用工具说明", prompts[1])
                self.assertEqual(agent.store.global_messages()[0]["locale"], "en")
                self.assertEqual(agent.store.global_messages()[2]["locale"], "zh")
            finally:
                paths.TASK_DATABASE, paths.SKILL_DATABASE, paths.STATE_DIRECTORY = previous

    async def test_legacy_pending_approval_uses_current_request_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            settings = SettingsStore(root / "settings.json")
            settings.update({"workspace": str(workspace)})
            previous = (paths.TASK_DATABASE, paths.SKILL_DATABASE, paths.STATE_DIRECTORY)
            paths.TASK_DATABASE = root / "tasks.sqlite3"
            paths.SKILL_DATABASE = root / "skills.sqlite3"
            paths.STATE_DIRECTORY = root / "state"
            try:
                agent = PersistentClineAgent(settings)
                task = agent.create_task("Legacy approval", "act", "locale")
                agent.store.append_message(task["id"], {"role": "user", "content": "你好", "locale": "zh"})
                agent.store.create_approval(
                    task["id"],
                    "write_file",
                    {"path": "example.txt", "content": "ok"},
                    {"id": "legacy-call", "function": {"name": "write_file"}},
                    0,
                )
                with patch.object(agent, "_execute_tool", AsyncMock(return_value={"written": True})), patch.object(
                    agent, "_checkpoint_after_tool", AsyncMock()
                ), patch.object(agent, "_run_loop", AsyncMock(return_value={"status": "ok"})) as run_loop:
                    await agent.chat(task["id"], "continue", locale="en")
                run_loop.assert_awaited_once_with(task["id"], locale="en")
            finally:
                paths.TASK_DATABASE, paths.SKILL_DATABASE, paths.STATE_DIRECTORY = previous

    async def test_turn_queue_preserves_each_message_locale(self):
        class FakeAgent:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.calls = []

            def task(self, task_id):
                return {"id": task_id}

            def _validated_skill_ids(self, values):
                return []

            @property
            def store(self):
                return self

            def last_event_seq(self, task_id):
                return 0

            async def chat(self, task_id, content, context, skill_ids, locale=""):
                self.calls.append(locale)
                self.started.set()
                await self.release.wait()

        agent = FakeAgent()
        controller = TaskTurnController(agent)
        controller.submit("task", "first", locale="en")
        await agent.started.wait()
        controller.submit("task", "second", locale="zh")
        self.assertEqual(controller._session("task").queue[0]["locale"], "zh")
        agent.release.set()
        for _ in range(20):
            if len(agent.calls) == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(agent.calls, ["en", "zh"])


if __name__ == "__main__":
    unittest.main()

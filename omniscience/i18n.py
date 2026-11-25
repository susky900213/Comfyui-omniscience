from __future__ import annotations

from typing import Any


def normalize_locale(value: Any) -> str:
    locale = str(value or "").strip().lower().replace("_", "-")
    return "zh" if locale == "zh" or locale.startswith("zh-") else "en"


def is_chinese(value: Any) -> bool:
    return normalize_locale(value) == "zh"


def text(locale: Any, chinese: str, english: str) -> str:
    return chinese if is_chinese(locale) else english


def language_instruction(locale: Any) -> str:
    if is_chinese(locale):
        return (
            "# 回复语言\n"
            "ComfyUI 当前配置为中文界面。请始终使用简体中文撰写所有面向用户的回复、状态说明、"
            "错误解释和总结。工具名称、代码、路径、命令及技术标识保留原样；即使用户用英文提问，"
            "除非用户明确要求改用其他语言，否则仍使用简体中文。"
        )
    return (
        "# Response Language\n"
        "ComfyUI is currently configured with a non-Chinese interface. Always write all user-facing "
        "replies, status updates, error explanations, and summaries in English. Keep tool names, code, "
        "paths, commands, and technical identifiers unchanged. Even if the user writes in Chinese, use "
        "English unless they explicitly request another language."
    )

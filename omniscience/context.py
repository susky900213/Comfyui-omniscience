"""Token-budget estimation and history compaction."""

from __future__ import annotations

import re
from typing import Any

from .util import json_dumps as _json


class ContextManager:
    """Estimate and compact model context without requiring a tokenizer package.

    OpenAI-compatible providers do not expose a common context-limit discovery
    endpoint.  The estimator is intentionally conservative and can be replaced
    by a provider-specific tokenizer later without changing the compaction
    boundary.
    """

    COMPACTION_THRESHOLD = 0.8
    _TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u4e00-\u9fff]", re.UNICODE)

    def __init__(
        self,
        max_tokens: int = 32_000,
        keep_messages: int = 14,
        max_chars: int | None = None,
        compaction_threshold: float = COMPACTION_THRESHOLD,
    ):
        # max_chars is retained for callers of the original ContextManager API.
        # A rough four-character/token conversion keeps old small-budget tests
        # useful while the production boundary is expressed in tokens.
        self.max_chars = max_chars
        if max_chars is not None:
            max_tokens = max(1, int(max_chars / 4))
        self.max_tokens = max(1, int(max_tokens))
        self.keep_messages = keep_messages
        self.compaction_threshold = min(1.0, max(0.01, float(compaction_threshold)))

    @classmethod
    def estimate_tokens(cls, value: Any) -> int:
        """Return a deterministic, dependency-free token estimate.

        CJK characters and punctuation are counted individually; runs of Latin
        letters/digits are counted as one token per four characters. JSON/tool
        payloads therefore receive a deliberately conservative estimate.
        """
        text = str(value or "")
        if not text:
            return 0
        total = 0
        for part in cls._TOKEN_PATTERN.findall(text):
            if len(part) == 1 and not part.isalnum() and not part == "_":
                total += 1
            elif re.fullmatch(r"[\u4e00-\u9fff]", part):
                total += 1
            else:
                total += max(1, (len(part) + 3) // 4)
        return total

    @classmethod
    def estimate_messages_tokens(cls, messages: list[dict[str, Any]]) -> int:
        # The small per-message overhead accounts for role and tool-call fields.
        total = 0
        for message in messages:
            total += cls.estimate_tokens(message.get("content")) + 4
            if message.get("tool_calls"):
                total += cls.estimate_tokens(_json(message["tool_calls"]))
            if message.get("tool_call_id"):
                total += cls.estimate_tokens(message["tool_call_id"])
        return total

    @classmethod
    def estimate_prompt_tokens(cls, messages: list[dict[str, Any]], reserved_tokens: int = 0) -> int:
        """Estimate the complete input prompt, including non-message fields."""
        return cls.estimate_messages_tokens(messages) + max(0, int(reserved_tokens))

    def needs_compaction(self, messages: list[dict[str, Any]], reserved_tokens: int = 0) -> bool:
        return bool(messages) and self.estimate_prompt_tokens(messages, reserved_tokens) > self.max_tokens * self.compaction_threshold

    def _split_messages(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        if not messages:
            return [], [], []
        # Preserve the first system-like/user instruction and the latest turns.
        # The current protocol normally puts system instructions outside this
        # list, but keeping the first message also protects older integrations.
        head = [dict(messages[0])] if messages else []
        tail_start = max(1, len(messages) - self.keep_messages)
        # Never leave a tool result without the assistant tool call that
        # introduced it. This may retain one or more extra recent messages.
        while tail_start > 1 and messages[tail_start].get("role") == "tool":
            tail_start -= 1
        return head, [dict(message) for message in messages[tail_start:]], [dict(message) for message in messages[1:tail_start]]

    def fallback(self, messages: list[dict[str, Any]], reserved_tokens: int = 0) -> tuple[list[dict[str, Any]], bool]:
        """Compact locally when the optional model summary request fails."""
        if not self.needs_compaction(messages, reserved_tokens):
            return messages, False
        head, tail, omitted = self._split_messages(messages)
        if not omitted:
            # There is no older turn to summarize (for example, one very large
            # user message). Keep the transcript shape intact and trim the
            # message in place instead of duplicating it with an empty summary.
            compacted = [dict(message) for message in messages]
            available_tokens = max(1, self.max_tokens - max(0, int(reserved_tokens)))
            over_hard_limit = self.estimate_messages_tokens(compacted) > available_tokens or (
                self.max_chars is not None
                and sum(len(str(message.get("content") or "")) for message in compacted) > self.max_chars
            )
            if not over_hard_limit:
                return messages, False
            changed = False
            while self.estimate_messages_tokens(compacted) > available_tokens or (
                self.max_chars is not None
                and sum(len(str(message.get("content") or "")) for message in compacted) > self.max_chars
            ):
                largest = max(range(len(compacted)), key=lambda index: len(str(compacted[index].get("content") or "")))
                content = str(compacted[largest].get("content") or "")
                if not content:
                    break
                excess = self.estimate_messages_tokens(compacted) - available_tokens
                target_length = max(1, len(content) - max(1, excess * 4))
                trimmed = content[: max(1, target_length - 1)] + "…"
                if trimmed == content:
                    # The message is at its minimum length; no further
                    # progress is possible for this budget.
                    break
                compacted[largest]["content"] = trimmed
                changed = True
            return compacted, changed
        summary_parts: list[str] = []
        for message in omitted:
            role = message.get("role", "unknown")
            content = str(message.get("content") or "").replace("\n", " ")
            if len(content) > 240:
                content = content[:240] + "…"
            if content:
                summary_parts.append(f"{role}: {content}")
        summary = {
            "role": "user",
            "content": "[Earlier context compacted by Cline]\n" + "\n".join(summary_parts[-80:]),
        }
        compacted = head + [summary] + tail
        # A small budget can be smaller than the preserved recent messages.
        # Trim content rather than returning an over-budget request. Keep the
        # marker itself so the model knows why earlier turns are absent.
        available_tokens = max(1, self.max_tokens - max(0, int(reserved_tokens)))
        while self.estimate_messages_tokens(compacted) > available_tokens or (
            self.max_chars is not None
            and sum(len(str(message.get("content") or "")) for message in compacted) > self.max_chars
        ):
            excess = self.estimate_messages_tokens(compacted) - available_tokens
            changed = False
            for index, message in enumerate(compacted):
                content = str(message.get("content") or "")
                minimum = len("[Earlier context compacted by Cline]") if index == 1 else 0
                removable = max(0, len(content) - minimum)
                if removable:
                    target_length = len(content) - min(removable, max(1, excess * 4))
                    if target_length and target_length < len(content):
                        message["content"] = content[: target_length - 1] + "…"
                    else:
                        message["content"] = content[:target_length]
                    changed = True
                    break
            if not changed:
                break
        return compacted, True

    @staticmethod
    def summary_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build a safe, tool-free prompt for a summarization model call."""
        result: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role not in {"user", "assistant", "tool"}:
                continue
            content = str(message.get("content") or "")
            if message.get("tool_calls"):
                content += "\nTool calls: " + _json(message["tool_calls"])
            result.append({"role": role, "content": content})
        return result

    def messages_to_summarize(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return only the old turns that will be replaced by a summary."""
        _, _, omitted = self._split_messages(messages)
        return self.summary_messages(omitted)

    def bounded_summary_messages(self, messages: list[dict[str, Any]], ratio: float = 0.65) -> list[dict[str, Any]]:
        """Keep the summary request itself below the configured context budget."""
        result = self.messages_to_summarize(messages)
        budget = max(128, int(self.max_tokens * max(0.2, min(0.8, ratio))))
        while result and self.estimate_messages_tokens(result) > budget:
            # Prefer retaining the most recent omitted turns; they are usually
            # the ones needed to connect the summary to the preserved tail.
            result.pop(0)
        return result

    def with_summary(
        self,
        messages: list[dict[str, Any]],
        summary_text: str,
        reserved_tokens: int = 0,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Replace omitted history with a model-generated summary."""
        if not self.needs_compaction(messages, reserved_tokens) or not str(summary_text).strip():
            return messages, False
        head, tail, omitted = self._split_messages(messages)
        if not omitted:
            # A single oversized message has no replaceable history. Use the
            # same safe local trimming path as a failed summary request rather
            # than inserting a duplicate summary beside the original message.
            return self.fallback(messages, reserved_tokens)
        summary = {
            "role": "user",
            "content": "[Earlier context summarized by Cline]\n" + str(summary_text).strip(),
        }
        compacted = head + [summary] + tail
        available_tokens = max(1, self.max_tokens - max(0, int(reserved_tokens)))
        if self.estimate_messages_tokens(compacted) > available_tokens:
            # Keep the successful summary but enforce the configured hard
            # budget. fallback() would summarize the original messages again.
            while self.estimate_messages_tokens(compacted) > available_tokens:
                largest = max(range(len(compacted)), key=lambda index: len(str(compacted[index].get("content") or "")))
                content = str(compacted[largest].get("content") or "")
                minimum = len("[Earlier context summarized by Cline]") if largest == 1 else 0
                if len(content) <= minimum:
                    break
                compacted[largest]["content"] = content[: max(minimum, len(content) - 256)] + "…"
        return compacted, True

    def prepare(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """Compatibility method: use the deterministic local fallback only."""
        return self.fallback(messages)

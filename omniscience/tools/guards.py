"""Command guard (DESIGN 5.4): prompt is the first line, this is the backstop.

Ported from Cline's ``command-guard.ts`` three-step approach: mask quoted
strings, heredoc bodies, escapes, and comments (to avoid false positives),
split on shell separators, then inspect the leading word of every segment
against write-operation sets.  This is deliberately a blacklist of common
mutation techniques, not a shell interpreter.
"""

from __future__ import annotations

import re
import tempfile

# Historical hard-deny regex, retained for ACT mode (same policy as before).
BLOCKED_COMMANDS = re.compile(
    r"(?:^|[;&|])\s*(?:sudo|shutdown|reboot|mkfs(?:\.|\s)|dd\s+if=)|"
    r"(?:rm\s+-[^\n]*r[^\n]*f|git\s+reset\s+--hard|git\s+clean\s+-fd)|"
    r"(?:^|[;&|])\s*(?:format(?:\.com)?|diskpart)\b|"
    r"(?:^|[;&|])\s*(?:del|erase)\b[^\n]*(?:/[a-z]*s[a-z]*|/[a-z]*q[a-z]*)|"
    r"(?:^|[;&|])\s*(?:rmdir|rd)\b[^\n]*/[a-z]*s[a-z]*|"
    r"\b(?:remove-item)\b[^\n]*-(?:recurse|force)\b",
    re.IGNORECASE,
)

# Head words that mutate the filesystem or install software.  Used to
# hard-block PLAN mode; ACT mode keeps only the historical BLOCKED_COMMANDS.
STATE_CHANGING_COMMANDS = {
    "rm", "rmdir", "mv", "cp", "install", "touch", "mkdir", "chmod", "chown",
    "chgrp", "ln", "truncate", "patch", "dd", "tee", "rsync",
    "del", "erase", "move", "copy", "mklink", "format", "diskpart", "powershell", "pwsh",
    "remove-item", "set-content", "out-file", "clear-content", "stop-computer", "restart-computer", "format-volume",
    "tar", "unzip", "gzip", "bzip2", "xz",
    "pip", "pip3", "npm", "pnpm", "yarn", "uv", "poetry", "conda", "brew",
    "apt", "apt-get", "yum", "dnf", "pacman",
    "docker", "podman", "kubectl", "systemctl", "launchctl",
    "git", "svn", "hg",
}
GIT_READ_ONLY_SUBCOMMANDS = {
    "status", "log", "diff", "show", "ls-files", "ls-tree", "ls-remote",
    "blame", "describe", "rev-parse", "config", "shortlog", "cat-file",
}
SED_INPLACE = re.compile(r"\bsed\b[^\n]*\s-i(\s|$)", re.IGNORECASE)
REDIRECTION = re.compile(r"(?<!\d)>>?\s*([^\s;|&]+)")
TMP_PREFIXES = ("/tmp/", "./tmp/", "../tmp/", "/dev/", "%temp%/", "%tmp%/")
WINDOWS_MUTATION = re.compile(
    r"\b(?:del|erase|rmdir|rd|move|copy|mklink|format|diskpart|remove-item|set-content|out-file|clear-content|stop-computer|restart-computer|format-volume)\b",
    re.IGNORECASE,
)
POWERSHELL_DESTRUCTIVE = re.compile(
    r"\b(?:remove-item|set-content|out-file|clear-content|stop-computer|restart-computer|format-volume)\b",
    re.IGNORECASE,
)

# Head words that reach the network. Tasks keep network tools closed until the
# user flips the per-task "网络访问" switch (runtime + system prompt enforce it).
NETWORK_COMMANDS = {
    "curl", "wget", "nc", "netcat", "ncat", "ssh", "scp", "sftp", "telnet",
    "ftp", "ping", "ping6", "dig", "nslookup", "host", "traceroute",
}
GIT_NETWORK_SUBCOMMANDS = {"clone", "fetch", "pull", "push", "submodule", "ls-remote"}

_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_ESCAPED = re.compile(r"\\.")
_COMMENT = re.compile(r"(?:^|(?<=\s))#[^\n]*")
_HEREDOC = re.compile(r"<<[-~]?\s*['\"]?(\w+)['\"]?\n.*?\n\1\b", re.DOTALL)
_SEPARATORS = re.compile(r"[;\n]|\|\||&&?|\|\|?")
_PREFIX_WORDS = {"env", "command", "time", "nohup", "exec", "sudo!"}


def mask_noise(command: str) -> str:
    """Blank out quoted text, heredoc bodies, escapes, and comments."""
    masked = _HEREDOC.sub(lambda m: " " * len(m.group(0)), command)
    masked = _QUOTED.sub(lambda m: " " * len(m.group(0)), masked)
    # Backslashes are path separators in cmd.exe, not shell escapes. Treating
    # ``C:\\work\\file`` as escaped text would hide both command and redirection
    # tokens from the guard on Windows.
    windows_path_syntax = re.search(
        r"(?i)(?:\b[A-Za-z]:[\\/]|%(?:temp|tmp)%[\\/]|(?:^|\s)\.{1,2}[\\/]|(?:^|\s)\\\\)",
        command,
    )
    if not windows_path_syntax and not command.lstrip().lower().startswith(("cmd", "powershell", "pwsh")):
        masked = _ESCAPED.sub("  ", masked)
    masked = _COMMENT.sub(lambda m: " " * len(m.group(0)), masked)
    return masked


def segments(command: str) -> list[str]:
    return [part.strip() for part in _SEPARATORS.split(mask_noise(command)) if part.strip()]


def tokenize(segment: str) -> list[str]:
    return segment.split()


_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)


def head_word(tokens: list[str]) -> str:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ENV_ASSIGNMENT.fullmatch(token) or token.lower().rstrip(":") in _PREFIX_WORDS:
            index += 1
            continue
        break
    if index >= len(tokens):
        return ""
    head = re.split(r"[/\\]", tokens[index].lower())[-1]
    return re.sub(r"\.(?:exe|com|cmd|bat)$", "", head)


def _nested_windows_command(tokens: list[str]) -> list[str]:
    """Return the command after ``cmd.exe /c`` or ``/k`` when present."""

    for index, token in enumerate(tokens[1:], start=1):
        if token.lower() in {"/c", "/k"}:
            return tokens[index + 1 :]
    return []


def _destructive_windows_tokens(tokens: list[str]) -> bool:
    """Recognize Windows deletion/volume commands that must never run."""

    head = head_word(tokens)
    if head in {"format", "diskpart", "shutdown", "reboot", "mkfs"}:
        return True
    if head in {"del", "erase", "rmdir", "rd"}:
        return any(token.casefold() in {"/s", "/q", "/s/q", "/q/s"} for token in tokens[1:])
    if head in {"powershell", "pwsh"}:
        return bool(POWERSHELL_DESTRUCTIVE.search(" ".join(tokens[1:])))
    return False


def _raw_windows_command_changes(command: str) -> bool:
    """Inspect quoted ``cmd /c`` and PowerShell scripts before masking them."""

    for segment in _SEPARATORS.split(command):
        tokens = tokenize(segment)
        if not tokens:
            continue
        head = head_word(tokens)
        if head in {"powershell", "pwsh"} and WINDOWS_MUTATION.search(segment):
            return True
        if head == "cmd":
            nested = _nested_windows_command(tokens)
            nested_command = " ".join(nested).strip().strip('"\'')
            if nested_command and _is_state_changing_segment(nested_command):
                return True
    return False


def _raw_windows_command_is_destructive(command: str) -> bool:
    """Inspect quoted nested commands for the ACT-mode hard deny list."""

    for segment in _SEPARATORS.split(command):
        tokens = tokenize(segment)
        if not tokens:
            continue
        head = head_word(tokens)
        if head == "cmd":
            nested = _nested_windows_command(tokens)
            nested_command = " ".join(nested).strip().strip('"\'')
            if nested_command and _destructive_windows_tokens(tokenize(nested_command)):
                return True
        elif head in {"powershell", "pwsh"} and POWERSHELL_DESTRUCTIVE.search(segment):
            return True
    return False


def _is_state_changing_segment(segment: str) -> bool:
    tokens = tokenize(segment)
    head = head_word(tokens)
    if not head:
        return False
    if head == "cmd":
        nested = _nested_windows_command(tokens)
        return bool(nested) and _is_state_changing_segment(" ".join(nested))
    if head in {"powershell", "pwsh"}:
        return bool(WINDOWS_MUTATION.search(segment))
    if head == "git":
        sub = next((t for t in tokens[1:] if not t.startswith("-")), "")
        return sub.lower() not in GIT_READ_ONLY_SUBCOMMANDS
    if head == "sed":
        return False  # in-place sed is handled by SED_INPLACE over the full command
    return head in STATE_CHANGING_COMMANDS


def redirects_outside_tmp(command: str) -> bool:
    for match in REDIRECTION.finditer(mask_noise(command)):
        target = match.group(1).strip('"\'')
        normalized = target.replace("\\", "/").casefold()
        temporary_roots = {
            prefix.replace("\\", "/").casefold().rstrip("/")
            for prefix in TMP_PREFIXES
        }
        try:
            temporary_roots.update(
                str(tempfile.gettempdir()).replace("\\", "/").casefold().rstrip("/")
                for _ in (0,)
            )
        except OSError:
            pass
        if target and not any(
            normalized == root or normalized.startswith(f"{root}/")
            for root in temporary_roots
        ):
            return True
    return False


def state_changing(command: str) -> bool:
    """True when any segment's head word mutates filesystem/state."""
    if _raw_windows_command_changes(command):
        return True
    masked = mask_noise(command)
    if SED_INPLACE.search(masked):
        return True
    if any(_is_state_changing_segment(segment) for segment in segments(command)):
        return True
    return redirects_outside_tmp(command)


def network_accessed(command: str) -> bool:
    """True when any segment would open a network connection."""
    for segment in segments(command or ""):
        tokens = tokenize(segment)
        head = head_word(tokens)
        if head in NETWORK_COMMANDS:
            return True
        if head == "git":
            sub = next((token for token in tokens[1:] if not token.startswith("-")), "")
            if sub.lower() in GIT_NETWORK_SUBCOMMANDS:
                return True
    return False


def guard_error(command: str, mode: str) -> str | None:
    """Return an error message when the command must not run, else None."""
    if not command or "\x00" in command or BLOCKED_COMMANDS.search(command):
        return "This command is blocked by the local safety policy."
    if _raw_windows_command_is_destructive(command):
        return "This command is blocked by the local safety policy."
    for segment in segments(command):
        tokens = tokenize(segment)
        head = head_word(tokens)
        nested = _nested_windows_command(tokens) if head == "cmd" else tokens
        if _destructive_windows_tokens(nested):
            return "This command is blocked by the local safety policy."
    if mode == "plan" and state_changing(command):
        return (
            "该命令会修改文件或系统状态，在 PLAN 模式下被 command-guard 拒绝执行。"
            "Plan 模式只允许只读检查命令；如需变更请让用户切换到 Act 模式。"
        )
    return None

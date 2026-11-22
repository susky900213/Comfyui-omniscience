"""Filesystem locations for Comfyui-omniscience state.

Every module must read these attributes at call time (never bind them at
import) so deployments and tests can retarget storage by patching this
module, e.g. ``omniscience.paths.TASK_DATABASE = tmp / "tasks.sqlite3"``.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_STATE_DIRECTORY = Path.home() / ".local" / "share" / "comfyui-omniscience"
LEGACY_STATE_DIRECTORY = Path.home() / ".local" / "share" / "comfyui-cline"
STATE_DIRECTORY = Path(
    os.environ.get(
        "OMNISCIENCE_STATE_DIRECTORY",
        os.environ.get(
            "CLINE_STATE_DIRECTORY",
            str(
                LEGACY_STATE_DIRECTORY
                if LEGACY_STATE_DIRECTORY.exists() and not DEFAULT_STATE_DIRECTORY.exists()
                else DEFAULT_STATE_DIRECTORY
            ),
        ),
    )
).expanduser()
TASK_DATABASE = Path(
    os.environ.get(
        "OMNISCIENCE_TASK_DATABASE", os.environ.get("CLINE_TASK_DATABASE", str(STATE_DIRECTORY / "tasks.sqlite3"))
    )
).expanduser()
SKILL_DATABASE = Path(
    os.environ.get(
        "OMNISCIENCE_SKILL_DATABASE", os.environ.get("CLINE_SKILL_DATABASE", str(STATE_DIRECTORY / "skills.sqlite3"))
    )
).expanduser()

DEFAULT_MCP_SETTINGS_PATH = Path.home() / ".config" / "comfyui-omniscience" / "mcp_settings.json"
LEGACY_MCP_SETTINGS_PATH = Path.home() / ".cline" / "data" / "settings" / "cline_mcp_settings.json"
MCP_SETTINGS_PATH = Path(
    os.environ.get(
        "OMNISCIENCE_MCP_SETTINGS_PATH",
        os.environ.get(
            "CLINE_MCP_SETTINGS_PATH",
            str(
                LEGACY_MCP_SETTINGS_PATH
                if LEGACY_MCP_SETTINGS_PATH.exists() and not DEFAULT_MCP_SETTINGS_PATH.exists()
                else DEFAULT_MCP_SETTINGS_PATH
            ),
        ),
    )
).expanduser()

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "comfyui-omniscience" / "config.json"
LEGACY_CONFIG_PATH = Path.home() / ".config" / "comfyui-cline" / "config.json"
CONFIG_PATH = Path(
    os.environ.get("OMNISCIENCE_CONFIG_FILE", os.environ.get("CLINE_CONFIG_FILE", str(DEFAULT_CONFIG_PATH)))
).expanduser()

# Optional local-only repository token file.  The file is intentionally kept
# next to this checkout so setup-page entry is not required; deployments can
# point it elsewhere with OMNISCIENCE_TOKEN_FILE.
LOCAL_TOKEN_CONFIG_PATH = Path(
    os.environ.get(
        "OMNISCIENCE_TOKEN_FILE",
        str(Path(__file__).resolve().parent.parent / ".omniscience-tokens.json"),
    )
).expanduser()

DEFAULT_MODEL = os.environ.get("OMNISCIENCE_MODEL", os.environ.get("CLINE_MODEL", ""))
DEFAULT_API_BASE_URL = os.environ.get("OMNISCIENCE_API_BASE_URL", os.environ.get("CLINE_API_BASE_URL", "https://api.openai.com/v1"))
DEFAULT_WORKSPACE = Path.home() / ".Comfyui-node"
DEFAULT_COMFYUI_DIRECTORY = os.environ.get("OMNISCIENCE_COMFYUI_DIRECTORY", os.environ.get("CLINE_COMFYUI_DIRECTORY", "")).strip()


def env(name: str, legacy: str) -> str:
    """Read an ``OMNISCIENCE_*`` override with the historical ``CLINE_*`` fallback."""
    return os.environ.get(name) or os.environ.get(legacy) or ""


def global_workspace() -> Path:
    """Scratch workspace backing the task-independent global conversation."""
    return Path(
        os.environ.get("OMNISCIENCE_GLOBAL_WORKSPACE", str(STATE_DIRECTORY / "global-workspace"))
    ).expanduser()

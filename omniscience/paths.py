"""Filesystem locations for Comfyui-omniscience state.

Every module must read these attributes at call time (never bind them at
import) so deployments and tests can retarget storage by patching this
module, e.g. ``omniscience.paths.TASK_DATABASE = tmp / "tasks.sqlite3"``.
"""

from __future__ import annotations

import os
from pathlib import Path

def _default_platform_directories(
    platform_name: str | None = None,
    path_type: type[Path] = Path,
) -> tuple[Path, Path, Path, Path]:
    """Return state/config defaults for the current OS and user profile."""

    is_windows = platform_name or os.name
    home = path_type(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    if is_windows == "nt":
        user_profile = path_type(os.environ.get("USERPROFILE") or home)
        local_data = path_type(os.environ.get("LOCALAPPDATA") or user_profile / "AppData" / "Local")
        config_directory = path_type(os.environ.get("APPDATA") or user_profile / "AppData" / "Roaming")
        return (
            local_data / "comfyui-omniscience",
            home / ".local" / "share" / "comfyui-omniscience",
            config_directory / "comfyui-omniscience" / "mcp_settings.json",
            config_directory / "comfyui-omniscience" / "config.json",
        )
    default_state = home / ".local" / "share" / "comfyui-omniscience"
    default_config = home / ".config" / "comfyui-omniscience"
    return (
        default_state,
        default_state,
        default_config / "mcp_settings.json",
        default_config / "config.json",
    )


(
    DEFAULT_STATE_DIRECTORY,
    LEGACY_STATE_DIRECTORY,
    DEFAULT_MCP_SETTINGS_PATH,
    DEFAULT_CONFIG_PATH,
) = _default_platform_directories()

if os.name == "nt":
    LEGACY_CONFIG_PATH = Path.home() / ".config" / "comfyui-omniscience" / "config.json"
else:
    LEGACY_CONFIG_PATH = DEFAULT_CONFIG_PATH

STATE_DIRECTORY = Path(
    os.environ.get(
        "OMNISCIENCE_STATE_DIRECTORY",
        os.environ.get(
            "OMNISCIENCE_STATE_DIRECTORY",
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
        "OMNISCIENCE_TASK_DATABASE", os.environ.get("OMNISCIENCE_TASK_DATABASE", str(STATE_DIRECTORY / "tasks.sqlite3"))
    )
).expanduser()
SKILL_DATABASE = Path(
    os.environ.get(
        "OMNISCIENCE_SKILL_DATABASE", os.environ.get("OMNISCIENCE_SKILL_DATABASE", str(STATE_DIRECTORY / "skills.sqlite3"))
    )
).expanduser()

LEGACY_MCP_SETTINGS_PATH = Path.home() / ".omniscience" / "data" / "settings" / "omniscience_mcp_settings.json"
MCP_SETTINGS_PATH = Path(
    os.environ.get(
        "OMNISCIENCE_MCP_SETTINGS_PATH",
        os.environ.get(
            "OMNISCIENCE_MCP_SETTINGS_PATH",
            str(
                LEGACY_MCP_SETTINGS_PATH
                if LEGACY_MCP_SETTINGS_PATH.exists() and not DEFAULT_MCP_SETTINGS_PATH.exists()
                else DEFAULT_MCP_SETTINGS_PATH
            ),
        ),
    )
).expanduser()

CONFIG_PATH = Path(
    os.environ.get("OMNISCIENCE_CONFIG_FILE", os.environ.get("OMNISCIENCE_CONFIG_FILE", str(DEFAULT_CONFIG_PATH)))
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

DEFAULT_MODEL = os.environ.get("OMNISCIENCE_MODEL", os.environ.get("OMNISCIENCE_MODEL", ""))
DEFAULT_API_BASE_URL = os.environ.get("OMNISCIENCE_API_BASE_URL", os.environ.get("OMNISCIENCE_API_BASE_URL", "https://api.openai.com/v1"))
DEFAULT_WORKSPACE = Path.home() / ".Comfyui-node"
DEFAULT_COMFYUI_DIRECTORY = os.environ.get("OMNISCIENCE_COMFYUI_DIRECTORY", os.environ.get("OMNISCIENCE_COMFYUI_DIRECTORY", "")).strip()


def env(name: str, legacy: str) -> str:
    """Read an ``OMNISCIENCE_*`` override with the historical ``OMNISCIENCE_*`` fallback."""
    return os.environ.get(name) or os.environ.get(legacy) or ""


def global_workspace() -> Path:
    """Scratch workspace backing the task-independent global conversation."""
    return Path(
        os.environ.get("OMNISCIENCE_GLOBAL_WORKSPACE", str(STATE_DIRECTORY / "global-workspace"))
    ).expanduser()

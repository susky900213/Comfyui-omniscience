"""GitCode access-token loading and URL authentication.

The repository APIs used by this project accept ``access_token`` as a query
parameter.  This module deliberately does not create an Authorization header.
Token values are never returned by the settings API; callers should also avoid
including authenticated URLs in user-visible messages or logs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any

from . import paths


_GITCODE_TOKEN_NAMES = ("GITCODE_TOKEN", "CLINE_GITCODE_TOKEN", "gitcode_access_token", "gitcode_token")


def _local_values() -> Mapping[str, Any]:
    """Read the optional ignored token file without surfacing its contents."""
    try:
        value = json.loads(Path(paths.LOCAL_TOKEN_CONFIG_PATH).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _legacy_value(provider: str) -> str:
    """Read a legacy configured token for callers that do not own SettingsStore."""
    if str(provider).strip().lower() != "gitcode":
        return ""
    names = _GITCODE_TOKEN_NAMES
    paths_to_try = [Path(paths.CONFIG_PATH)]
    legacy_path = Path(paths.LEGACY_CONFIG_PATH)
    if legacy_path not in paths_to_try:
        paths_to_try.append(legacy_path)
    for path in paths_to_try:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        for key in names[2:]:
            token = str(value.get(key) or "").strip()
            if token:
                return token
    return ""


def _environment_value(names: tuple[str, ...]) -> str:
    for name in names[:2]:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def resolve(provider: str, configured: Any = "") -> str:
    """Resolve the GitCode token with environment/local/settings precedence.

    Environment variables override the ignored local file.  The local file
    overrides the legacy settings JSON value passed as ``configured``.  This
    lets setup-page entry remain optional while preserving existing installs.
    """
    if str(provider).strip().lower() != "gitcode":
        # GitHub is public in this application and must never receive a token,
        # even if a legacy setting or GITHUB_TOKEN is still present.
        return ""
    names = _GITCODE_TOKEN_NAMES
    environment = _environment_value(names)
    if environment:
        return environment
    local = _local_values()
    for key in names[2:]:
        value = str(local.get(key) or "").strip()
        if value:
            return value
    return str(configured or _legacy_value(provider) or "").strip()


def with_access_token(url: str, provider: str, token: Any = "") -> str:
    """Return a GitCode URL with its token in the ``access_token`` query.

    Existing query parameters are retained.  The token is added only when a
    token is configured, so public requests remain unchanged by default.
    """
    if str(provider).strip().lower() != "gitcode":
        return str(url)
    value = resolve("gitcode", token)
    if not value:
        return str(url)
    parsed = urlsplit(str(url))
    query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key != "access_token"]
    query.append(("access_token", value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def local_config_template() -> dict[str, str]:
    """Return the safe, empty shape used by the local configuration example."""
    return {"gitcode_access_token": ""}
"""Host resource assessment for workflow recommendation and execution gates."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResourceSnapshot:
    disk_free_bytes: int | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    vram_total_bytes: int | None
    vram_free_bytes: int | None
    gpu_name: str | None
    source: str


@dataclass(frozen=True)
class ResourceAssessment:
    feasible: bool | None
    disk_ok: bool | None
    memory_ok: bool | None
    vram_ok: bool | None
    local_supported: bool | None
    recommend_remote: bool
    reasons: tuple[str, ...]
    recommendation: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"reasons": list(self.reasons)}


def _memory_info() -> tuple[int | None, int | None]:
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = pages * page_size
        available = None
        if hasattr(os, "sysconf_names") and "SC_AVPHYS_PAGES" in os.sysconf_names:
            available = int(os.sysconf("SC_AVPHYS_PAGES")) * page_size
        return total, available
    except (AttributeError, OSError, ValueError):
        return None, None


def local_snapshot(*, storage_root: Path | str | None = None, comfy_stats: dict[str, Any] | None = None) -> ResourceSnapshot:
    disk = None
    if storage_root is not None:
        try:
            disk = shutil.disk_usage(Path(storage_root).expanduser()).free
        except OSError:
            pass
    total, available = _memory_info()
    vram_total = vram_free = None
    gpu_name = None
    if isinstance(comfy_stats, dict):
        devices = comfy_stats.get("devices") or []
        device = devices[0] if isinstance(devices, list) and devices and isinstance(devices[0], dict) else comfy_stats
        vram_total = _number(device.get("vram_total") or device.get("vram_total_bytes"))
        vram_free = _number(device.get("vram_free") or device.get("vram_free_bytes"))
        gpu_name = str(device.get("name") or device.get("device") or "").strip() or None
    return ResourceSnapshot(disk, total, available, vram_total, vram_free, gpu_name, "local+comfyui" if comfy_stats else "local")


def _number(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def assess(snapshot: ResourceSnapshot, requirement: dict[str, Any] | None = None) -> ResourceAssessment:
    requirement = requirement or {}
    disk_required = _gb(requirement.get("disk_gb") or requirement.get("disk_bytes"))
    memory_required = _gb(requirement.get("memory_gb") or requirement.get("ram_gb") or requirement.get("memory_bytes"))
    vram_required = _gb(requirement.get("vram_gb") or requirement.get("vram_bytes"))
    checks: list[tuple[str, bool | None, int | None, int | None]] = [
        ("磁盘空间", None if disk_required is None or snapshot.disk_free_bytes is None else snapshot.disk_free_bytes >= disk_required, snapshot.disk_free_bytes, disk_required),
        ("内存", None if memory_required is None or snapshot.memory_total_bytes is None else snapshot.memory_total_bytes >= memory_required, snapshot.memory_total_bytes, memory_required),
        ("显存", None if vram_required is None or snapshot.vram_total_bytes is None else snapshot.vram_total_bytes >= vram_required, snapshot.vram_total_bytes, vram_required),
    ]
    reasons = []
    for label, ok, actual, needed in checks:
        if ok is False:
            reasons.append(f"{label}不足：可用/总量 {actual or 0}，需求 {needed} 字节。")
        elif ok is None and needed is not None:
            reasons.append(f"无法确认{label}是否满足需求。")
    known = [ok for _, ok, _, _ in checks if ok is not None]
    unknown_required = any(ok is None and needed is not None for _, ok, _, needed in checks)
    feasible = False if any(ok is False for ok in known) else None if unknown_required else True
    if feasible is False:
        recommendation = "不建议在本机运行。优先推荐使用非本地模型/云端推理工作流；如果必须本地运行，请选择更低显存、内存和磁盘需求的工作流。"
    elif feasible is None:
        recommendation = "本机资源信息不完整。优先选择声明可使用非本地模型或云端推理的工作流，并先确认资源。"
    else:
        recommendation = "当前资源满足声明的最低需求，但实际显存仍可能受节点和分辨率影响。"
    local_supported = feasible
    recommend_remote = feasible is not True and bool(requirement.get("remote_available", True))
    return ResourceAssessment(local_supported, checks[0][1], checks[1][1], checks[2][1], local_supported, recommend_remote, tuple(reasons), recommendation)


def _gb(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # Values below one million are interpreted as GiB; larger values are bytes.
    return int(number * 1024**3) if number < 1_000_000 else int(number)


def rank_resource(candidate: dict[str, Any], assessment: ResourceAssessment) -> float:
    score = float(candidate.get("score") or 0)
    execution_mode = str(candidate.get("execution_mode") or "local").lower()
    is_remote = execution_mode in {"remote", "cloud", "api", "hosted"} or bool(candidate.get("remote_endpoint"))
    if assessment.recommend_remote and is_remote:
        return score + 35
    if assessment.feasible is True and not is_remote:
        return score + 20
    if assessment.feasible is False and not is_remote:
        return score - 30
    if assessment.feasible is None and is_remote:
        return score + 15
    return score - 5

"""Read-only ComfyUI workflow validation and dependency extraction."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MAX_WORKFLOW_BYTES = 2_000_000
MAX_WORKFLOW_NODES = 10_000
NON_EXECUTION_NODES = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute"}
MODEL_FIELD = re.compile(r"(?:model|checkpoint|ckpt|vae|lora|controlnet|clip|unet|diffusion|upscale|embedding|filename|file_name)", re.I)
MODEL_SUFFIXES = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".pkl")


class WorkflowParseError(ValueError):
    pass


def load_workflow(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, Path)):
        path = Path(value)
        if path.is_file():
            if path.stat().st_size > MAX_WORKFLOW_BYTES:
                raise WorkflowParseError("工作流文件超过 2 MB 限制。")
            try:
                value = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise WorkflowParseError(f"无法读取工作流文件：{exc}") from exc
    if isinstance(value, bytes):
        if len(value) > MAX_WORKFLOW_BYTES:
            raise WorkflowParseError("工作流内容超过 2 MB 限制。")
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowParseError("工作流必须是 UTF-8 JSON。") from exc
    if not isinstance(value, str):
        raise WorkflowParseError("工作流必须是 JSON 对象。")
    if len(value.encode("utf-8")) > MAX_WORKFLOW_BYTES:
        raise WorkflowParseError("工作流内容超过 2 MB 限制。")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise WorkflowParseError(f"工作流 JSON 无效：{exc.msg}。") from exc
    if not isinstance(parsed, dict):
        raise WorkflowParseError("工作流 JSON 顶层必须是对象。")
    return parsed


def inspect_workflow(value: Any, *, loaded_nodes: set[str] | list[str] | None = None, loaded_nodes_error: str = "") -> dict[str, Any]:
    workflow = load_workflow(value)
    nodes = _nodes(workflow)
    if len(nodes) > MAX_WORKFLOW_NODES:
        raise WorkflowParseError(f"工作流节点数超过 {MAX_WORKFLOW_NODES} 个限制。")
    loaded = {str(item) for item in (loaded_nodes or [])}
    class_types = sorted({node["class_type"] for node in nodes if node.get("class_type")})
    missing = sorted(class_type for class_type in class_types if loaded and class_type not in loaded and class_type not in NON_EXECUTION_NODES)
    unknown = [class_type for class_type in class_types if not loaded_nodes and loaded_nodes_error and class_type not in NON_EXECUTION_NODES]
    models = _models(nodes)
    return {
        "format": "api" if isinstance(workflow.get("prompt"), dict) else "ui" if isinstance(workflow.get("nodes"), list) else "unknown",
        "nodes": nodes,
        "node_count": len(nodes),
        "class_types": class_types,
        "loaded_nodes_checked": bool(loaded),
        "loaded_nodes_count": len(loaded),
        "loaded_nodes_error": loaded_nodes_error,
        "missing_nodes": missing,
        "unverified_nodes": unknown,
        "models": models,
        "valid": bool(nodes),
    }


def _nodes(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    source = workflow.get("prompt") if isinstance(workflow.get("prompt"), dict) else workflow.get("nodes")
    if isinstance(source, dict):
        result = []
        for node_id, item in source.items():
            if not isinstance(item, dict):
                continue
            class_type = str(item.get("class_type") or item.get("type") or "").strip()
            if class_type:
                result.append({"id": str(node_id), "class_type": class_type, "inputs": item.get("inputs") or {}})
        return result
    if isinstance(source, list):
        result = []
        for index, item in enumerate(source):
            if not isinstance(item, dict):
                continue
            class_type = str(item.get("type") or item.get("class_type") or "").strip()
            if class_type:
                result.append({"id": str(item.get("id") or index), "class_type": class_type, "inputs": item.get("inputs") or {}, "widgets_values": item.get("widgets_values") or []})
        return result
    raise WorkflowParseError("工作流不是可识别的 ComfyUI API 或前端图格式。")


def _models(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for node in nodes:
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        values = list(inputs.items()) + [(f"widget_{index}", value) for index, value in enumerate(node.get("widgets_values") or [])]
        for field, value in values:
            if not isinstance(value, str):
                continue
            value = value.strip()
            if not value or not (MODEL_FIELD.search(str(field)) or value.lower().endswith(MODEL_SUFFIXES)):
                continue
            key = (str(field), value)
            found.setdefault(key, {"field": str(field), "name": value, "nodes": []})["nodes"].append(node["id"])
    return list(found.values())

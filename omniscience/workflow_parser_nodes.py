from .workflow_parser_common import *
from .workflow_parser_conversion import *


def _nodes(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    source, _source_format = _workflow_payload(workflow)
    if _source_format == "api":
        source = source
    else:
        source = source.get("nodes") if isinstance(source, dict) else None
    if isinstance(source, dict):
        result = []
        for node_id, item in source.items():
            if not isinstance(item, dict):
                continue
            class_type = str(item.get("class_type") or item.get("type") or "").strip()
            if class_type:
                properties = deepcopy(item.get("properties")) if isinstance(item.get("properties"), dict) else {}
                aux_id = str(item.get("aux_id") or properties.get("aux_id") or "").strip()
                result.append(
                    {
                        "id": str(node_id),
                        "class_type": class_type,
                        "inputs": item.get("inputs") or {},
                        "widgets_values": item.get("widgets_values") or [],
                        "widgets_values_named": (
                            deepcopy(item.get("widgets_values_named"))
                            if isinstance(item.get("widgets_values_named"), dict)
                            else {}
                        ),
                        "mode": item.get("mode"),
                        "properties": properties,
                        "aux_id": aux_id,
                    }
                )
        return result
    if isinstance(source, list):
        result = []
        for index, item in enumerate(source):
            if not isinstance(item, dict):
                continue
            class_type = str(item.get("type") or item.get("class_type") or "").strip()
            if class_type:
                properties = deepcopy(item.get("properties")) if isinstance(item.get("properties"), dict) else {}
                aux_id = str(item.get("aux_id") or properties.get("aux_id") or "").strip()
                result.append(
                    {
                        "id": str(item.get("id") or index),
                        "class_type": class_type,
                        "inputs": item.get("inputs") or {},
                        "widgets_values": item.get("widgets_values") or [],
                        "widgets_values_named": (
                            deepcopy(item.get("widgets_values_named"))
                            if isinstance(item.get("widgets_values_named"), dict)
                            else {}
                        ),
                        "mode": item.get("mode"),
                        "properties": properties,
                        "aux_id": aux_id,
                    }
                )
        return result
    raise WorkflowParseError("工作流不是可识别的 ComfyUI API 或前端图格式。")


def _node_type_details(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group node occurrences by exact execution ``class_type``."""
    details: dict[str, dict[str, Any]] = {}
    for node in nodes:
        class_type = str(node.get("class_type") or "")
        if not class_type:
            continue
        detail = details.setdefault(class_type, {"class_type": class_type, "count": 0, "node_ids": []})
        detail["count"] += 1
        detail["node_ids"].append(str(node.get("id") or ""))
    return [details[class_type] for class_type in sorted(details)]


def _node_instance_details(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return source-aware details for every node occurrence.

    A canvas ``type`` is not sufficient to identify the extension that owns a
    node.  ComfyUI extensions can register the same class name, while the
    frontend stores an optional ``properties.aux_id`` repository identifier.
    Keep that information attached to the exact occurrence instead of reducing
    it to a type-level guess.
    """
    result = []
    for node in nodes:
        item = {
            "id": str(node.get("id") or ""),
            "class_type": str(node.get("class_type") or ""),
            "mode": node.get("mode"),
            "aux_id": str(node.get("aux_id") or "").strip(),
            "properties": deepcopy(node.get("properties")) if isinstance(node.get("properties"), dict) else {},
        }
        if item["class_type"]:
            result.append(item)
    return result


def _normalise_loader_type(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _is_legacy_loader_node(node: dict[str, Any]) -> bool:
    node_type = _normalise_loader_type(node.get("class_type"))
    return node_type in LEGACY_LOADER_TYPES or "loader" in node_type

__all__ = [name for name in globals() if not name.startswith("__")]

"""Read-only ComfyUI workflow validation and dependency extraction."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

MAX_WORKFLOW_BYTES = 2_000_000
MAX_WORKFLOW_NODES = 10_000
NON_EXECUTION_NODES = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute"}
MODEL_FIELD = re.compile(r"(?:model|checkpoint|ckpt|vae|lora|controlnet|clip|unet|diffusion|upscale|embedding|filename|file_name)", re.I)
MODEL_SUFFIXES = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".pkl")
SOCKET_INPUT_TYPES = {
    "AUDIO",
    "CLIP",
    "CONDITIONING",
    "CONTROL_NET",
    "CROSS_ATTENTION_CONTROL",
    "GLIGEN",
    "IMAGE",
    "LATENT",
    "MASK",
    "MODEL",
    "NOISE",
    "SIGMAS",
    "STYLE_MODEL",
    "VAE",
}
BYPASS_NODE_MODE = 4


class WorkflowParseError(ValueError):
    def __init__(self, message: str, *, diagnostic: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}


def to_api_workflow(value: Any, *, node_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a ComfyUI workflow envelope containing an executable prompt.

    ComfyUI has two JSON representations that are commonly called a workflow:
    the editor's canvas export (``nodes``/``links``) and the API prompt (node
    IDs mapped to ``class_type``/``inputs``).  The execution endpoint accepts
    only the latter.  RunningHub and older saved jobs can contain either one,
    so normalize both forms at the boundary instead of making callers export a
    second file manually.

    The returned object keeps the canvas fields when they are available.  This
    lets the web panel still load the original graph while ``prompt`` is used
    for ``POST /prompt``.  Conversion is deliberately conservative: widget
    values are mapped using explicit canvas metadata plus the live schema, and
    a missing link or widget raises a useful error rather than submitting a
    silently corrupted prompt.
    """
    workflow = load_workflow(value)
    payload, source_format = _workflow_payload(workflow)
    if source_format == "api":
        prompt = deepcopy(payload)
        # A direct API prompt has no useful canvas metadata.  An envelope such
        # as {"prompt": {...}, "extra": ...} should retain that metadata.
        if isinstance(workflow.get("prompt"), dict) and payload is workflow.get("prompt"):
            result = deepcopy(workflow)
            result["prompt"] = prompt
            return result
        return {"prompt": prompt}
    if source_format != "ui":
        raise WorkflowParseError("工作流不是可识别的 ComfyUI API 或前端图格式。")

    prompt, conversion_diagnostics = _ui_to_api_prompt(payload, node_info=node_info)
    # For a normal UI export, retain nodes/links for the optional canvas
    # preview.  For a nested provider response, retain the actual workflow
    # object rather than its transport wrapper.
    result = deepcopy(payload)
    result["prompt"] = prompt
    result["conversion_diagnostics"] = conversion_diagnostics
    return result


def _workflow_payload(workflow: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Find an API prompt or UI graph inside common provider wrappers."""
    if _looks_like_api_prompt(workflow):
        return workflow, "api"
    # A converted envelope intentionally keeps both the original canvas and a
    # generated prompt.  The canvas is the source of truth: it lets us rebuild
    # the prompt with the current /object_info schema instead of reusing a
    # stale prompt produced by an older or incomplete conversion.
    if isinstance(workflow.get("nodes"), list):
        return workflow, "ui"
    if isinstance(workflow.get("prompt"), dict):
        prompt = workflow["prompt"]
        if _looks_like_api_prompt(prompt):
            return prompt, "api"
        if isinstance(prompt.get("nodes"), list):
            return prompt, "ui"
    for key in ("workflowJson", "workflowJSON", "workflowData", "workflowContent", "workflow", "data", "result"):
        nested = workflow.get(key)
        if isinstance(nested, str):
            try:
                nested = json.loads(nested)
            except json.JSONDecodeError:
                continue
        if isinstance(nested, dict):
            try:
                return _workflow_payload(nested)
            except WorkflowParseError:
                continue
    raise WorkflowParseError("工作流不是可识别的 ComfyUI API 或前端图格式。")


def _looks_like_api_prompt(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    return all(
        isinstance(node, dict)
        and isinstance(node.get("class_type"), str)
        and bool(node.get("class_type").strip())
        and isinstance(node.get("inputs"), dict)
        for node in value.values()
    )


def _schema_input_is_widget(definition: Any) -> bool:
    """Return whether an /object_info input normally has a canvas widget.

    ComfyUI's schema describes both socket inputs and scalar/combo controls in
    the same mapping.  ``forceInput`` is the authoritative opt-out for scalar
    controls; known graph types are treated as sockets.  Unknown scalar types
    are deliberately treated as widgets because custom nodes commonly expose
    controls using INT/FLOAT/STRING aliases.
    """
    if not isinstance(definition, (list, tuple)) or not definition:
        return False
    input_type = definition[0]
    options = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
    # ComfyUI can expose frontend-only controls in /object_info.  In
    # particular, ResolutionSelector.preview is a socketless
    # RESOLUTION_PREVIEW input: it renders a live preview in the canvas but is
    # not an executable API input and is commonly absent from saved workflows.
    if options.get("forceInput") is True or options.get("socketless") is True:
        return False
    if isinstance(input_type, (list, tuple)):
        return True
    normalized_type = str(input_type or "").strip().upper()
    if normalized_type == "RESOLUTION_PREVIEW":
        return False
    return normalized_type not in SOCKET_INPUT_TYPES


def _schema_fields(input_schema: Any) -> list[tuple[str, Any, bool]]:
    fields: list[tuple[str, Any, bool]] = []
    if not isinstance(input_schema, dict):
        return fields
    for section_name, required in (("required", True), ("optional", False)):
        section = input_schema.get(section_name)
        if isinstance(section, dict):
            fields.extend(
                (str(name), definition, required)
                for name, definition in section.items()
                if str(name).strip()
            )
    return fields


def _is_link_reference(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[1], int) and isinstance(value[0], (str, int))


def _schema_scalar_type_valid(input_type: Any, value: Any) -> bool | None:
    """Validate only scalar types whose ComfyUI contract is unambiguous."""
    if not isinstance(input_type, str):
        return None
    normalized = input_type.strip().upper()
    if normalized == "INT":
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized in {"FLOAT", "NUMBER"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if normalized in {"BOOLEAN", "BOOL"}:
        return isinstance(value, bool)
    if normalized == "STRING":
        return isinstance(value, str)
    return None


def _validate_api_prompt(prompt: dict[str, Any], node_info: dict[str, Any]) -> None:
    """Validate values that can be checked safely against /object_info."""
    for node_id, node in prompt.items():
        node_type = str(node.get("class_type") or "").strip() if isinstance(node, dict) else ""
        inputs = node.get("inputs") if isinstance(node, dict) else None
        schema = node_info.get(node_type) if isinstance(node_info, dict) else None
        input_schema = schema.get("input") if isinstance(schema, dict) else None
        fields = _schema_fields(input_schema)
        if not fields:
            continue
        definitions = {name: (definition, required) for name, definition, required in fields}
        if not isinstance(inputs, dict):
            raise WorkflowParseError(
                f"节点 {node_id}（{node_type}）转换后 inputs 不是对象。",
                diagnostic={"node_id": str(node_id), "node_type": node_type, "reason": "invalid_inputs"},
            )
        for name, (definition, required) in definitions.items():
            if required and name not in inputs:
                raise WorkflowParseError(
                    f"节点 {node_id}（{node_type}）缺少必需输入 {name}；不能提交未完成的 prompt。",
                    diagnostic={
                        "node_id": str(node_id),
                        "node_type": node_type,
                        "field": name,
                        "reason": "missing_required_input",
                    },
                )
            if name not in inputs or _is_link_reference(inputs[name]):
                continue
            if not isinstance(definition, (list, tuple)) or not definition:
                continue
            input_type = definition[0]
            if isinstance(input_type, (list, tuple)):
                choices = list(input_type)
                if inputs[name] not in choices:
                    raise WorkflowParseError(
                        f"节点 {node_id}（{node_type}）输入 {name} 的值 {inputs[name]!r} 不在 schema 允许范围内。",
                        diagnostic={
                            "node_id": str(node_id),
                            "node_type": node_type,
                            "field": name,
                            "value": deepcopy(inputs[name]),
                            "allowed_values": deepcopy(choices),
                            "reason": "invalid_enum_value",
                        },
                    )
                continue
            type_valid = _schema_scalar_type_valid(input_type, inputs[name])
            if type_valid is False:
                raise WorkflowParseError(
                    f"节点 {node_id}（{node_type}）输入 {name} 的值类型不符合 schema。",
                    diagnostic={
                        "node_id": str(node_id),
                        "node_type": node_type,
                        "field": name,
                        "value": deepcopy(inputs[name]),
                        "expected_type": str(input_type),
                        "reason": "invalid_input_type",
                    },
                )


def _ui_to_api_prompt(
    workflow: dict[str, Any], *, node_info: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw_nodes = workflow.get("nodes")
    raw_links = workflow.get("links")
    if not isinstance(raw_nodes, list):
        raise WorkflowParseError("ComfyUI 画布工作流缺少 nodes 数组。")
    if not isinstance(raw_links, list):
        raw_links = []

    nodes: dict[str, dict[str, Any]] = {}
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            continue
        node_id = str(raw_node.get("id") if raw_node.get("id") is not None else index).strip()
        node_type = str(raw_node.get("type") or raw_node.get("class_type") or "").strip()
        if not node_id or not node_type:
            continue
        nodes[node_id] = raw_node

    links: dict[str, tuple[str, int]] = {}
    for link in raw_links:
        if not isinstance(link, (list, tuple)) or len(link) < 5:
            continue
        link_id, source_id, source_slot = link[0], link[1], link[2]
        if link_id is not None and source_id is not None:
            try:
                links[str(link_id)] = (str(source_id), int(source_slot))
            except (TypeError, ValueError) as exc:
                raise WorkflowParseError(f"工作流连线 {link_id} 的源插槽无效。") from exc

    def input_links(node: dict[str, Any]) -> list[Any]:
        values = node.get("inputs")
        return values if isinstance(values, list) else []

    def resolve_source(
        source_id: str, source_slot: int, seen: set[tuple[str, int]] | None = None
    ) -> tuple[str, int] | None:
        seen = set() if seen is None else seen
        marker = (source_id, source_slot)
        if marker in seen:
            raise WorkflowParseError(f"工作流连线在节点 {source_id} 处形成循环。")
        seen.add(marker)
        source_node = nodes.get(source_id)
        if source_node is None:
            raise WorkflowParseError(f"工作流连线引用了不存在的源节点 {source_id}。")
        if source_node.get("mode") == BYPASS_NODE_MODE:
            return None
        source_type = str(source_node.get("type") or source_node.get("class_type") or "").strip()
        if source_type == "Reroute":
            incoming = [item for item in input_links(source_node) if item.get("link") is not None]
            if not incoming:
                raise WorkflowParseError(f"Reroute 节点 {source_id} 没有可解析的输入连线。")
            incoming_link = links.get(str(incoming[0].get("link")))
            if incoming_link is None:
                raise WorkflowParseError(f"Reroute 节点 {source_id} 的输入连线不存在。")
            return resolve_source(*incoming_link, seen=seen)
        if source_type in NON_EXECUTION_NODES:
            raise WorkflowParseError(f"节点 {source_id}（{source_type}）不能直接转换为 API 输入。")
        return source_id, source_slot

    prompt: dict[str, Any] = {}
    conversion_diagnostics: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        node_type = str(node.get("type") or node.get("class_type") or "").strip()
        if node_type in NON_EXECUTION_NODES:
            continue
        if node.get("mode") == BYPASS_NODE_MODE:
            conversion_diagnostics.append(
                {
                    "node_id": node_id,
                    "node_type": node_type,
                    "source": "canvas",
                    "reason": "bypassed_node",
                }
            )
            continue
        raw_inputs = node.get("inputs")
        if isinstance(raw_inputs, dict):
            # Some exporters already put API inputs inside an otherwise UI
            # shaped node.  Preserve those values and only normalize links
            # when they are represented by the canvas link ID.
            inputs = deepcopy(raw_inputs)
        elif isinstance(raw_inputs, list):
            inputs = {}
            widgets = node.get("widgets_values")
            if widgets is None:
                widgets = []
            if not isinstance(widgets, list):
                raise WorkflowParseError(f"节点 {node_id}（{node_type}）的 widgets_values 不是数组。")
            schema = node_info.get(node_type) if isinstance(node_info, dict) else None
            input_schema = schema.get("input") if isinstance(schema, dict) else None
            schema_fields = _schema_fields(input_schema)

            linked_names: set[str] = set()
            explicit_widget_names: list[str] = []
            for input_def in raw_inputs:
                if not isinstance(input_def, dict):
                    continue
                name = str(input_def.get("name") or "").strip()
                if not name:
                    continue
                link_id = input_def.get("link")
                if link_id is not None:
                    source = links.get(str(link_id))
                    if source is None:
                        # A few exports store [source_id, slot] directly.
                        if isinstance(link_id, (list, tuple)) and len(link_id) >= 2:
                            source = (str(link_id[0]), int(link_id[1]))
                        else:
                            raise WorkflowParseError(
                                f"节点 {node_id} 的输入 {name} 引用了不存在的连线 {link_id}。",
                                diagnostic={"node_id": node_id, "field": name, "source": "link", "link_id": link_id},
                            )
                    resolved_source = resolve_source(*source)
                    if resolved_source is None:
                        conversion_diagnostics.append(
                            {
                                "node_id": node_id,
                                "node_type": node_type,
                                "field": name,
                                "source": "link",
                                "link_id": link_id,
                                "reason": "bypassed_source",
                            }
                        )
                    else:
                        linked_names.add(name)
                        inputs[name] = list(resolved_source)
                        conversion_diagnostics.append(
                            {"node_id": node_id, "node_type": node_type, "field": name, "source": "link", "link_id": link_id}
                        )
                elif input_def.get("widget") is not None:
                    # Some ComfyUI frontend-only inputs still carry a widget
                    # descriptor in the canvas node.  Do not let that explicit
                    # metadata override the live schema's socketless marker.
                    definition = next(
                        (item_definition for item_name, item_definition, _required in schema_fields if item_name == name),
                        None,
                    )
                    if definition is None or _schema_input_is_widget(definition):
                        explicit_widget_names.append(name)

            named_widgets = node.get("widgets_values_named")
            use_named_widgets = isinstance(named_widgets, dict) and bool(named_widgets)
            if use_named_widgets or widgets or explicit_widget_names:
                if not schema_fields:
                    raise WorkflowParseError(
                        f"节点 {node_id}（{node_type}）包含控件值，但缺少 ComfyUI /object_info schema，无法安全映射。",
                        diagnostic={"node_id": node_id, "node_type": node_type, "source": "widgets_values"},
                    )

                # widgets_values is a flat list in the node's widget order;
                # input socket order is not a safe substitute.  Use object_info
                # order, skipping connected sockets, and use explicit canvas
                # widget metadata as an additional signal for older schemas.
                widget_fields: list[tuple[str, Any, bool]] = []
                for name, definition, required in schema_fields:
                    if name in linked_names:
                        continue
                    if name in explicit_widget_names or _schema_input_is_widget(definition):
                        widget_fields.append((name, definition, required))
                for name in explicit_widget_names:
                    if name not in {item[0] for item in widget_fields} and name not in linked_names:
                        widget_fields.append((name, None, False))

                if use_named_widgets:
                    for name, _definition, required in widget_fields:
                        if name not in named_widgets:
                            if name in explicit_widget_names or required:
                                raise WorkflowParseError(
                                    f"节点 {node_id}（{node_type}）缺少控件输入 {name} 的值（widgets_values_named）。",
                                    diagnostic={
                                        "node_id": node_id,
                                        "node_type": node_type,
                                        "field": name,
                                        "source": "widgets_values_named",
                                        "reason": "missing_widget_value",
                                    },
                                )
                            continue
                        inputs[name] = deepcopy(named_widgets[name])
                        conversion_diagnostics.append(
                            {
                                "node_id": node_id,
                                "node_type": node_type,
                                "field": name,
                                "source": "widgets_values_named",
                            }
                        )
                else:
                    for index, (name, definition, required) in enumerate(widget_fields):
                        if index >= len(widgets):
                            if name in explicit_widget_names or required:
                                raise WorkflowParseError(
                                    f"节点 {node_id}（{node_type}）缺少控件输入 {name} 的值（widgets_values[{index}]）。",
                                    diagnostic={
                                        "node_id": node_id,
                                        "node_type": node_type,
                                        "field": name,
                                        "source": "widgets_values",
                                        "source_index": index,
                                        "reason": "missing_widget_value",
                                    },
                                )
                            continue
                        inputs[name] = deepcopy(widgets[index])
                        conversion_diagnostics.append(
                            {
                                "node_id": node_id,
                                "node_type": node_type,
                                "field": name,
                                "source": "widgets_values",
                                "source_index": index,
                            }
                        )

                if not use_named_widgets and len(widgets) > len(widget_fields):
                    # LoadImage and similar file-picker widgets historically
                    # serialize an extra upload-state value that is not an API
                    # input.  Preserve compatibility for that explicit schema
                    # shape, but record it instead of silently discarding it.
                    image_picker = any(
                        isinstance(definition, (list, tuple))
                        and len(definition) > 1
                        and isinstance(definition[1], dict)
                        and "image_folder" in definition[1]
                        for _name, definition, _required in schema_fields
                    )
                    if not image_picker:
                        index = len(widget_fields)
                        raise WorkflowParseError(
                            f"节点 {node_id}（{node_type}）的 widgets_values[{index}:] 没有对应的 schema 控件，无法安全转换。",
                            diagnostic={
                                "node_id": node_id,
                                "node_type": node_type,
                                "source": "widgets_values",
                                "source_index": index,
                                "reason": "unmapped_widget_values",
                                "count": len(widgets) - index,
                            },
                        )
                    conversion_diagnostics.append(
                        {
                            "node_id": node_id,
                            "node_type": node_type,
                            "source": "widgets_values",
                            "source_index": len(widget_fields),
                            "reason": "canvas_only_value",
                            "count": len(widgets) - len(widget_fields),
                        }
                    )

            if isinstance(input_schema, dict):
                for name, _definition, required in schema_fields:
                    if required and name not in inputs and name not in linked_names:
                        raise WorkflowParseError(
                            f"节点 {node_id}（{node_type}）缺少必需输入 {name}；不能猜测其默认值。",
                            diagnostic={
                                "node_id": node_id,
                                "node_type": node_type,
                                "field": name,
                                "reason": "missing_required_input",
                            },
                        )
        else:
            raise WorkflowParseError(f"节点 {node_id}（{node_type}）缺少 inputs 定义，无法安全转换。")
        prompt[node_id] = {"class_type": node_type, "inputs": inputs}

    if not prompt:
        raise WorkflowParseError("画布工作流没有可执行节点。")
    if isinstance(node_info, dict):
        _validate_api_prompt(prompt, node_info)
    return prompt, conversion_diagnostics


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
    _payload, source_format = _workflow_payload(workflow)
    nodes = _nodes(workflow)
    if len(nodes) > MAX_WORKFLOW_NODES:
        raise WorkflowParseError(f"工作流节点数超过 {MAX_WORKFLOW_NODES} 个限制。")
    loaded = {str(item) for item in (loaded_nodes or [])}
    class_types = sorted({node["class_type"] for node in nodes if node.get("class_type")})
    missing = sorted(class_type for class_type in class_types if loaded and class_type not in loaded and class_type not in NON_EXECUTION_NODES)
    unknown = [class_type for class_type in class_types if not loaded_nodes and loaded_nodes_error and class_type not in NON_EXECUTION_NODES]
    models = _models(nodes)
    return {
        "format": source_format,
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

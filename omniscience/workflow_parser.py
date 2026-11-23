"""Read-only ComfyUI workflow validation and dependency extraction."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

MAX_WORKFLOW_BYTES = 2_000_000
MAX_WORKFLOW_NODES = 10_000
WORKFLOW_INSPECTION_VERSION = 6
# These are canvas-only nodes in the ComfyUI UI format.  They are not part of
# the API prompt sent to the execution server, so they must not become missing
# registered node types during inspection.
NON_EXECUTION_NODES = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute"}
# KJNodes implements GetNode and SetNode as frontend registrations.  They can
# be present in a canvas without a corresponding backend entry in /object_info,
# so treating them as installable custom nodes would produce a false repair
# requirement.  Keep this separate from NON_EXECUTION_NODES because these nodes
# are real canvas nodes and should remain visible in canvas_class_types.
FRONTEND_ONLY_NODE_TYPES = {"GetNode", "SetNode"}
MODEL_FIELD = re.compile(r"(?:model|checkpoint|ckpt|vae|lora|controlnet|clip|unet|diffusion|upscale|embedding|filename|file_name)", re.I)
MODEL_SUFFIXES = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".pkl")
# These are the model selectors used by ComfyUI's built-in loaders and by the
# loader nodes commonly found in exported workflows.  This is intentionally a
# whitelist: old UI exports do not carry ``properties.models``, but scanning
# every string in ``inputs``/``widgets_values`` makes prompts, previews and
# arbitrary text look like model dependencies.
LEGACY_MODEL_FIELDS = {
    "checkpoint",
    "checkpoint_name",
    "ckpt_name",
    "clip_name",
    "clip_name1",
    "clip_name2",
    "control_net_name",
    "controlnet_name",
    "diffusion_model",
    "diffusion_model_name",
    "embedding_name",
    "file_name",
    "filename",
    "lora_name",
    "model_name",
    "model",
    "text_encoder_name",
    "unet_name",
    "upscale_model",
    "upscale_model_name",
    "vae_name",
}
LEGACY_LOADER_TYPES = {
    "checkpointloader",
    "checkpointloadersimple",
    "cliploader",
    "controlnetloader",
    "dualcliploader",
    "embeddingloader",
    "loraloader",
    "loraloadermodelonly",
    "stylemodelloader",
    "triplecliploader",
    "unetloader",
    "upscalemodelloader",
    "vaeloader",
}
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


def inspect_workflow(
    value: Any,
    *,
    loaded_nodes: set[str] | list[str] | None = None,
    loaded_nodes_error: str = "",
    loaded_nodes_checked: bool | None = None,
    loader_choices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect a workflow using ComfyUI's registered execution node names.

    ComfyUI resolves an API prompt by looking up each node's exact
    ``class_type`` in ``NODE_CLASS_MAPPINGS`` (exposed by ``/object_info``).
    Display names, Manager mappings and case-insensitive aliases are not part
    of that lookup and therefore are intentionally not accepted here either.

    ``loaded_nodes`` used to use an empty set as both "the endpoint returned no
    nodes" and "the endpoint was not checked".  ``loaded_nodes_checked`` keeps
    those states separate while retaining the old argument for callers and
    stored reports.
    """
    workflow = load_workflow(value)
    _payload, source_format = _workflow_payload(workflow)
    nodes = _nodes(workflow)
    if len(nodes) > MAX_WORKFLOW_NODES:
        raise WorkflowParseError(f"工作流节点数超过 {MAX_WORKFLOW_NODES} 个限制。")
    loaded = {str(item) for item in (loaded_nodes or []) if str(item)}
    checked = bool(loaded_nodes_checked) if loaded_nodes_checked is not None else bool(loaded_nodes is not None and not loaded_nodes_error)
    frontend_only_nodes = [
        node
        for node in nodes
        if source_format == "ui" and node.get("class_type") in FRONTEND_ONLY_NODE_TYPES
    ]
    frontend_only_type_details = _node_type_details(frontend_only_nodes)
    frontend_only_node_instances = _node_instance_details(frontend_only_nodes)
    execution_nodes = [
        node
        for node in nodes
        if source_format == "api"
        or (
            node.get("class_type") not in NON_EXECUTION_NODES
            and node.get("class_type") not in FRONTEND_ONLY_NODE_TYPES
            and node.get("mode") != BYPASS_NODE_MODE
        )
    ]
    class_types = sorted({node["class_type"] for node in execution_nodes if node.get("class_type")})
    matched_nodes = [node for node in execution_nodes if checked and node["class_type"] in loaded]
    missing_nodes = [node for node in execution_nodes if checked and node["class_type"] not in loaded]
    missing_type_details = _node_type_details(missing_nodes)
    unknown_nodes = [node for node in execution_nodes if not checked]
    unknown_type_details = _node_type_details(unknown_nodes)
    models = _models(nodes, workflow)
    _annotate_loader_status(models, loader_choices)
    node_instances = _node_instance_details(nodes)
    execution_node_instances = _node_instance_details(execution_nodes)
    missing_node_instances = _node_instance_details(missing_nodes)
    unknown_node_instances = _node_instance_details(unknown_nodes)
    canvas_dependency_nodes = [
        node
        for node in nodes
        if source_format != "ui"
        or (
            node.get("class_type") not in NON_EXECUTION_NODES
            and node.get("class_type") not in FRONTEND_ONLY_NODE_TYPES
        )
    ]
    canvas_missing_nodes = [
        node for node in canvas_dependency_nodes if checked and node["class_type"] not in loaded
    ]
    canvas_missing_type_details = _node_type_details(canvas_missing_nodes)
    canvas_missing_node_instances = _node_instance_details(canvas_missing_nodes)
    # The installation card must match the nodes ComfyUI has to construct when
    # it loads the canvas.  A bypassed node is not part of the execution graph,
    # but it is still a real canvas node and becomes a missing placeholder when
    # its type is not registered.  API prompts have no canvas-only state, so the
    # historical executable set is already the correct source there.
    installable_missing_nodes = canvas_missing_nodes if source_format == "ui" else missing_nodes
    installable_missing_type_details = (
        canvas_missing_type_details if source_format == "ui" else missing_type_details
    )
    installable_missing_node_instances = (
        canvas_missing_node_instances if source_format == "ui" else missing_node_instances
    )
    return {
        "inspection_version": WORKFLOW_INSPECTION_VERSION,
        "format": source_format,
        "nodes": nodes,
        "node_count": len(nodes),
        "execution_node_count": len(execution_nodes),
        "canvas_node_count": len(nodes) if source_format == "ui" else len(execution_nodes),
        "canvas_class_types": sorted({node["class_type"] for node in nodes if node.get("class_type")}),
        "class_types": class_types,
        "loaded_nodes_checked": checked,
        # Keep this field as a compatibility alias, but make it describe the
        # current workflow rather than every type registered by ComfyUI.
        "loaded_nodes_count": len(matched_nodes),
        "matched_node_count": len(matched_nodes),
        "matched_type_count": len({node["class_type"] for node in matched_nodes}),
        "canvas_matched_node_count": (
            len(canvas_dependency_nodes) - len(canvas_missing_nodes) if checked else 0
        ),
        "loaded_nodes_error": loaded_nodes_error,
        # Keep source metadata separate from the historical type summaries.
        # ``nodes`` also contains these fields, but the flat instance list is
        # easier for dependency attribution and does not require callers to
        # reconstruct node IDs from a grouped report.
        "node_instances": node_instances,
        "execution_node_instances": execution_node_instances,
        # ``missing_nodes`` remains a de-duplicated type list because node
        # dependency installation is type-based.  The detail fields preserve
        # every occurrence and its node IDs for diagnostics/UI display.
        "missing_nodes": sorted({node["class_type"] for node in missing_nodes}),
        "missing_node_count": len(missing_nodes),
        "missing_type_count": len(missing_type_details),
        "missing_node_details": missing_type_details,
        "missing_node_instances": missing_node_instances,
        "canvas_missing_nodes": sorted({node["class_type"] for node in canvas_missing_nodes}),
        "canvas_missing_node_count": len(canvas_missing_nodes),
        "canvas_missing_type_count": len(canvas_missing_type_details),
        "canvas_missing_node_details": canvas_missing_type_details,
        "canvas_missing_node_instances": canvas_missing_node_instances,
        "frontend_only_nodes": sorted({node["class_type"] for node in frontend_only_nodes}),
        "frontend_only_node_count": len(frontend_only_nodes),
        "frontend_only_type_count": len(frontend_only_type_details),
        "frontend_only_node_details": frontend_only_type_details,
        "frontend_only_node_instances": frontend_only_node_instances,
        "installable_missing_nodes": sorted({node["class_type"] for node in installable_missing_nodes}),
        "installable_missing_node_count": len(installable_missing_nodes),
        "installable_missing_type_count": len(installable_missing_type_details),
        "installable_missing_node_details": installable_missing_type_details,
        "installable_missing_node_instances": installable_missing_node_instances,
        "unverified_nodes": sorted({node["class_type"] for node in unknown_nodes}),
        "unverified_node_count": len(unknown_nodes),
        "unverified_type_count": len(unknown_type_details),
        "unverified_node_details": unknown_type_details,
        "unverified_node_instances": unknown_node_instances,
        "models": models,
        "loader_choices_checked": isinstance(loader_choices, dict),
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


def _normalise_model_metadata_key(directory: Any, name: Any) -> tuple[str, str]:
    """Return a stable key for equivalent frontend model metadata entries."""
    directory_value = str(directory or "").strip().replace("\\", "/").strip("/")
    parts = [part for part in directory_value.split("/") if part]
    if parts and parts[0].casefold() in {"model", "models"}:
        parts = parts[1:]
    normalized_directory = "/".join(parts).casefold()
    normalized_name = str(name or "").strip().replace("\\", "/")
    basename = Path(normalized_name).name.casefold()
    return normalized_directory, basename


def _metadata_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _append_model_metadata(
    found: dict[tuple[str, str], dict[str, Any]],
    entry: Any,
    *,
    node_id: Any,
    node_type: Any,
) -> bool:
    """Add one ComfyUI ``properties.models`` entry and merge its provenance."""
    if not isinstance(entry, dict):
        return False
    name = str(entry.get("name") or "").strip()
    if not name:
        return False
    directory = str(entry.get("directory") or "").strip()
    key = _normalise_model_metadata_key(directory, name)
    model = found.setdefault(key, {"name": name, "directory": directory, "nodes": [], "node_types": []})
    # Preserve frontend metadata (especially url and directory_invalid), while
    # keeping the stable fields used by older dependency-plan consumers.
    for field, value in entry.items():
        if field in {"nodes", "node_types"}:
            continue
        if field == "directory_invalid":
            # One invalid occurrence is enough to prevent an unsafe download;
            # duplicate metadata must not downgrade it back to valid.
            model[field] = _metadata_bool(model.get(field)) or _metadata_bool(value)
            continue
        if value not in (None, "", [], {}):
            model[field] = deepcopy(value)
    model.setdefault("url", str(entry.get("url") or "").strip())
    model["directory_invalid"] = _metadata_bool(model.get("directory_invalid")) or _metadata_bool(
        entry.get("directory_invalid", False)
    )
    identifier = str(node_id or "").strip()
    if identifier and identifier not in model["nodes"]:
        model["nodes"].append(identifier)
    node_name = str(node_type or "").strip()
    if node_name and node_name not in model["node_types"]:
        model["node_types"].append(node_name)
    return True


def _collect_nested_model_metadata(value: Any, found: dict[tuple[str, str], dict[str, Any]]) -> int:
    """Recursively collect node ``properties.models`` from canvas definitions.

    MiniMax-H3 and newer ComfyUI templates put executable nodes inside
    ``definitions.subgraphs[*].nodes``.  Walking the JSON shape rather than only
    the top-level ``nodes`` list supports both that format and future nested
    canvas containers without treating arbitrary strings as model names.
    """
    count = 0
    if isinstance(value, dict):
        properties = value.get("properties")
        models = properties.get("models") if isinstance(properties, dict) else None
        if isinstance(models, list):
            node_id = value.get("id")
            node_type = value.get("type") or value.get("class_type")
            for entry in models:
                count += _append_model_metadata(found, entry, node_id=node_id, node_type=node_type)
        for child in value.values():
            count += _collect_nested_model_metadata(child, found)
    elif isinstance(value, list):
        for child in value:
            count += _collect_nested_model_metadata(child, found)
    return count


def _collect_nested_loader_nodes(value: Any, result: list[dict[str, Any]]) -> None:
    """Collect loader-shaped nodes from nested canvas definitions."""
    if isinstance(value, dict):
        node_type = str(value.get("type") or value.get("class_type") or "").strip()
        if node_type and _is_legacy_loader_node({"class_type": node_type}):
            result.append(
                {
                    "id": str(value.get("id") or "").strip(),
                    "class_type": node_type,
                    "inputs": value.get("inputs") or {},
                    "widgets_values": value.get("widgets_values") or [],
                    "widgets_values_named": (
                        deepcopy(value.get("widgets_values_named"))
                        if isinstance(value.get("widgets_values_named"), dict)
                        else {}
                    ),
                }
            )
        for child in value.values():
            _collect_nested_loader_nodes(child, result)
    elif isinstance(value, list):
        for child in value:
            _collect_nested_loader_nodes(child, result)


def _metadata_models(nodes: list[dict[str, Any]], workflow: dict[str, Any] | None) -> list[dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    # Use normalized nodes first so API prompts and canvas nodes have identical
    # provenance.  The recursive raw walk adds subgraph nodes not returned by
    # ``_nodes`` and merges duplicate top-level entries.
    for node in nodes:
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        models = properties.get("models")
        if isinstance(models, list):
            for entry in models:
                _append_model_metadata(
                    found,
                    entry,
                    node_id=node.get("id"),
                    node_type=node.get("class_type"),
                )
    if isinstance(workflow, dict):
        _collect_nested_model_metadata(workflow, found)
        nested_loader_nodes: list[dict[str, Any]] = []
        _collect_nested_loader_nodes(workflow, nested_loader_nodes)
    else:
        nested_loader_nodes = []
    _merge_loader_widget_models(nodes + nested_loader_nodes, found)
    return list(found.values())


def _model_field_directory_matches(field: str, directory: Any) -> bool:
    """Return whether a loader field is compatible with a metadata directory."""
    normalized_field = str(field or "").casefold()
    normalized_directory = str(directory or "").replace("\\", "/").strip("/").casefold()
    directory_name = normalized_directory.rsplit("/", 1)[-1]
    expected_directories = {
        "checkpoint": {"checkpoints", "checkpoint"},
        "checkpoint_name": {"checkpoints", "checkpoint"},
        "ckpt_name": {"checkpoints", "checkpoint"},
        "clip_name": {"clip", "clip_gguf", "text_encoder", "text_encoders"},
        "clip_name1": {"clip", "clip_gguf", "text_encoder", "text_encoders"},
        "clip_name2": {"clip", "clip_gguf", "text_encoder", "text_encoders"},
        "control_net_name": {"controlnet", "controlnets"},
        "controlnet_name": {"controlnet", "controlnets"},
        "diffusion_model": {"diffusion_model", "diffusion_models", "unet", "unet_gguf"},
        "diffusion_model_name": {"diffusion_model", "diffusion_models", "unet", "unet_gguf"},
        "lora_name": {"lora", "loras"},
        "model_name": {"checkpoints", "checkpoint", "diffusion_model", "diffusion_models", "unet", "unet_gguf"},
        "unet_name": {"diffusion_model", "diffusion_models", "unet", "unet_gguf"},
        "upscale_model": {"upscale_model", "upscale_models"},
        "upscale_model_name": {"upscale_model", "upscale_models"},
        "vae_name": {"vae"},
    }
    return not normalized_directory or directory_name in expected_directories.get(normalized_field, set())


def _loader_widget_model_references(nodes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Extract the values currently selected by ComfyUI loader widgets.

    ``properties.models`` is useful download metadata, but it is generated by
    the frontend and can be stale after a model is renamed.  Loader widgets are
    the value ComfyUI will actually submit to the execution API, so they are
    collected independently and reconciled with metadata below.
    """
    references: list[dict[str, str]] = []
    for node in nodes:
        if not _is_legacy_loader_node(node):
            continue
        node_id = str(node.get("id") or "").strip()
        node_type = str(node.get("class_type") or "").strip()
        named = node.get("widgets_values_named")
        values: list[tuple[str, Any]] = []
        if isinstance(named, dict) and named:
            values.extend((str(field), value) for field, value in named.items())
        else:
            inputs = node.get("inputs")
            if isinstance(inputs, dict):
                values.extend((str(field), value) for field, value in inputs.items())
            elif isinstance(inputs, list):
                widget_fields = [
                    str(item.get("name") or "").strip()
                    for item in inputs
                    if isinstance(item, dict) and item.get("link") is None and str(item.get("name") or "").strip()
                ]
                widgets = node.get("widgets_values") if isinstance(node.get("widgets_values"), list) else []
                values.extend(zip(widget_fields, widgets))
        for field, value in values:
            if field.casefold() not in LEGACY_MODEL_FIELDS or not isinstance(value, str):
                continue
            name = value.strip()
            if not name:
                continue
            references.append(
                {
                    "node_id": node_id,
                    "node_type": node_type,
                    "field": field,
                    "name": name,
                }
            )
    return references


def _merge_loader_widget_models(nodes: list[dict[str, Any]], found: dict[tuple[str, str], dict[str, Any]]) -> None:
    """Make loader selections authoritative while retaining metadata provenance."""
    for reference in _loader_widget_model_references(nodes):
        node_id = reference["node_id"]
        field = reference["field"]
        name = reference["name"]
        basename = Path(name.replace("\\", "/")).name.casefold()
        candidates = [
            (key, model)
            for key, model in found.items()
            if node_id in {str(item) for item in model.get("nodes") or []}
            and _model_field_directory_matches(field, model.get("directory"))
        ]
        exact = [
            (key, model)
            for key, model in candidates
            if _normalise_model_metadata_key(model.get("directory"), model.get("name"))[1] == basename
        ]
        if exact:
            _record_loader_field(exact[0][1], field)
            continue
        if len(candidates) == 1:
            old_key, model = candidates[0]
            old_name = str(model.get("name") or "").strip()
            old_url = str(model.get("url") or "").strip()
            _record_loader_field(model, field)
            model["name"] = name
            model["metadata_name"] = old_name
            model["metadata_mismatch"] = True
            if old_url:
                # Never use a URL for the stale basename to download the newly
                # selected file.  Keep it only as an inspection diagnostic.
                model["metadata_url"] = old_url
                model.pop("url", None)
            new_key = _normalise_model_metadata_key(model.get("directory"), name)
            if new_key != old_key:
                found.pop(old_key, None)
                found[new_key] = model
            continue

        # A loader without frontend metadata (for example LoraLoaderModelOnly
        # in older exports) still has a concrete dependency in its widget.
        _append_model_metadata(
            found,
            {"name": name},
            node_id=node_id,
            node_type=reference["node_type"],
        )
        key = _normalise_model_metadata_key("", name)
        model = found[key]
        _record_loader_field(model, field)


def _record_loader_field(model: dict[str, Any], field: str) -> None:
    """Keep the selector field used to obtain a model's current widget value."""
    current = str(model.get("field") or "").strip()
    if not current or current == field:
        model["field"] = field
        return
    fields = list(model.get("loader_fields") or [])
    for value in (current, field):
        if value and value not in fields:
            fields.append(value)
    model["loader_fields"] = fields


def _loader_choice_values(loader_choices: dict[str, Any] | None, node_type: str, field: str) -> list[str] | None:
    """Return the live ComfyUI enum values for one loader selector."""
    if not isinstance(loader_choices, dict):
        return None
    schema = loader_choices.get(node_type)
    input_schema = schema.get("input") if isinstance(schema, dict) else None
    if not isinstance(input_schema, dict):
        return None
    for section_name in ("required", "optional"):
        section = input_schema.get(section_name)
        if not isinstance(section, dict) or field not in section:
            continue
        definition = section[field]
        if not isinstance(definition, (list, tuple)) or not definition:
            return None
        choices = definition[0]
        if isinstance(choices, (list, tuple)):
            return [str(value).replace("\\", "/") for value in choices]
        return None
    return None


def _annotate_loader_status(models: list[dict[str, Any]], loader_choices: dict[str, Any] | None) -> None:
    """Compare actual loader selections with the current /object_info choices."""
    if loader_choices is None:
        return
    for model in models:
        fields = list(model.get("loader_fields") or [])
        if not fields and model.get("field"):
            fields = [str(model["field"])]
        node_types = [str(value) for value in model.get("node_types") or [] if str(value).strip()]
        if not loader_choices:
            model["loader_status"] = "unknown"
            continue
        matched_choice_list = False
        known_choice_list = False
        normalized_name = str(model.get("name") or "").replace("\\", "/")
        for node_type in node_types:
            for field in fields:
                choices = _loader_choice_values(loader_choices, node_type, field)
                if choices is None:
                    continue
                known_choice_list = True
                if normalized_name in choices or normalized_name.casefold() in {choice.casefold() for choice in choices}:
                    matched_choice_list = True
                    break
            if matched_choice_list:
                break
        if matched_choice_list:
            model["loader_status"] = "recognized"
        elif known_choice_list:
            model["loader_status"] = "unrecognized"
        else:
            model["loader_status"] = "unknown"


def _legacy_models(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract model values from old exports only when they are loader inputs."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for node in nodes:
        if not _is_legacy_loader_node(node):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        values = list(inputs.items()) + [
            (f"widget_{index}", value) for index, value in enumerate(node.get("widgets_values") or [])
        ]
        for field, value in values:
            field_name = str(field)
            field_key = field_name.casefold()
            is_explicit_field = field_key in LEGACY_MODEL_FIELDS
            is_model_filename = isinstance(value, str) and value.strip().lower().endswith(MODEL_SUFFIXES)
            # Some older/custom canvas exports use a semantic input name such
            # as ``weights`` instead of ComfyUI's standard model field names.
            # The loader-node boundary plus a known model suffix is sufficient
            # evidence for compatibility, without scanning arbitrary nodes or
            # accepting free-form prompt text as a dependency.
            if not is_explicit_field and not is_model_filename:
                continue
            if not isinstance(value, str):
                continue
            name = value.strip()
            if not name:
                continue
            key = (field_name, name)
            model = found.setdefault(key, {"field": field_name, "name": name, "directory": "", "nodes": [], "node_types": []})
            node_id = str(node.get("id") or "")
            node_type = str(node.get("class_type") or "")
            if node_id and node_id not in model["nodes"]:
                model["nodes"].append(node_id)
            if node_type and node_type not in model["node_types"]:
                model["node_types"].append(node_type)
    return list(found.values())


def _models(nodes: list[dict[str, Any]], workflow: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    metadata = _metadata_models(nodes, workflow)
    # A valid frontend metadata list is authoritative.  Do not add guesses from
    # widgets/inputs to it, even if an older node happens to contain a filename.
    return metadata if metadata else _legacy_models(nodes)

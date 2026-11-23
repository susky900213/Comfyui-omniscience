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

__all__ = [name for name in globals() if not name.startswith("__")]


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

__all__ = [name for name in globals() if not name.startswith("__")]

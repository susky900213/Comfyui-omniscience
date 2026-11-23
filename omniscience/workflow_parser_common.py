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

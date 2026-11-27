from .workflow_parser_common import *
from .workflow_parser_nodes import *


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
    node_types_by_id = {
        str(node.get("id") or "").strip(): str(node.get("class_type") or "").strip()
        for node in nodes
        if str(node.get("id") or "").strip()
    }
    references = _loader_widget_model_references(nodes)
    references_by_node: dict[str, list[dict[str, str]]] = {}
    for reference in references:
        node_reference_id = reference.get("node_id")
        if node_reference_id:
            references_by_node.setdefault(node_reference_id, []).append(reference)
    for reference in references:
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
            model_node_ids = [str(item) for item in model.get("nodes") or [] if str(item).strip()]
            # ComfyUI can copy the same stale ``properties.models`` entry to
            # several loader instances.  If those instances select different
            # files, mutating the shared record in place makes the last widget
            # overwrite the earlier dependency (and loses its node
            # attribution).  Detach the current instance before applying the
            # authoritative widget value.
            other_references = [
                other_reference
                for other_node_id in model_node_ids
                if other_node_id != node_id
                for other_reference in references_by_node.get(other_node_id, [])
            ]
            has_different_selection = any(
                str(other.get("name") or "").strip().casefold() != name.casefold()
                for other in other_references
            )
            if len(model_node_ids) > 1 and node_id in model_node_ids and has_different_selection:
                remaining_node_ids = [item for item in model_node_ids if item != node_id]
                old_name = str(model.get("name") or "").strip()
                old_url = str(model.get("url") or "").strip()
                selected_model = deepcopy(model)
                selected_model["name"] = name
                selected_model["metadata_name"] = old_name
                selected_model["metadata_mismatch"] = True
                if old_url:
                    # The stale URL belongs to the metadata basename, not to
                    # the file selected by this loader widget.
                    selected_model["metadata_url"] = old_url
                    selected_model.pop("url", None)
                selected_model["nodes"] = [node_id]
                selected_model["node_types"] = list(
                    dict.fromkeys(
                        node_types_by_id.get(node_id, "")
                        or item
                        for item in model.get("node_types") or []
                        if str(item).strip()
                    )
                )
                model["nodes"] = remaining_node_ids
                model["node_types"] = list(
                    dict.fromkeys(
                        node_types_by_id.get(item, "")
                        for item in remaining_node_ids
                        if node_types_by_id.get(item, "").strip()
                    )
                ) or list(model.get("node_types") or [])
                new_key = _normalise_model_metadata_key(model.get("directory"), name)
                existing = found.get(new_key)
                if existing is not None and existing is not model:
                    existing_nodes = [str(item) for item in existing.get("nodes") or []]
                    if node_id not in existing_nodes:
                        existing["nodes"] = [*existing_nodes, node_id]
                    existing_types = [str(item) for item in existing.get("node_types") or []]
                    selected_type = node_types_by_id.get(node_id, "").strip()
                    if selected_type and selected_type not in existing_types:
                        existing_types.append(selected_type)
                    existing["node_types"] = existing_types
                    model = existing
                else:
                    found[new_key] = selected_model
                    model = selected_model
                _record_loader_field(model, field)
                # The detached record already represents this widget value, so
                # it must not fall through and be renamed a second time.
                continue
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

__all__ = [name for name in globals() if not name.startswith("__")]

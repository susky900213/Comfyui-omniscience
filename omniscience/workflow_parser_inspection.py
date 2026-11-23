from .workflow_parser_common import *
from .workflow_parser_conversion import *
from .workflow_parser_nodes import *
from .workflow_parser_models import *


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

__all__ = [name for name in globals() if not name.startswith("__")]

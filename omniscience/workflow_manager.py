"""Workflow discovery orchestration for the global conversation.

The implementation is split by responsibility into focused mixins. This module
keeps the historical ``WorkflowManagerMixin`` import path stable for callers.
"""

from .workflow_manager_common import *
from .workflow_manager_dependencies import WorkflowDependencyMixin
from .workflow_manager_execution import WorkflowExecutionMixin
from .workflow_manager_inspection import WorkflowInspectionMixin
from .workflow_manager_model_download import WorkflowModelDownloadMixin
from .workflow_manager_model_files import WorkflowModelFilesMixin
from .workflow_manager_model_inventory import WorkflowModelInventoryMixin
from .workflow_manager_model_sources import WorkflowModelSourcesMixin
from .workflow_manager_node_install import WorkflowNodeInstallMixin
from .workflow_manager_node_progress import WorkflowNodeProgressMixin
from .workflow_manager_node_sources import WorkflowNodeSourcesMixin
from .workflow_manager_nodes import WorkflowNodeCandidatesMixin
from .workflow_manager_search import WorkflowSearchMixin


class WorkflowManagerMixin(
    WorkflowNodeCandidatesMixin,
    WorkflowSearchMixin,
    WorkflowInspectionMixin,
    WorkflowDependencyMixin,
    WorkflowModelInventoryMixin,
    WorkflowModelFilesMixin,
    WorkflowModelSourcesMixin,
    WorkflowNodeSourcesMixin,
    WorkflowNodeProgressMixin,
    WorkflowNodeInstallMixin,
    WorkflowModelDownloadMixin,
    WorkflowExecutionMixin,
):
    """Compatibility facade combining the workflow manager responsibilities."""

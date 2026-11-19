"""Compatibility facade re-exporting the persistence package.

The implementations now live in the ``omniscience`` package (DESIGN.md
section 4).  This module keeps ``from omniscience_core import ...`` working
for existing imports and tests.
"""

from __future__ import annotations

try:  # ComfyUI loads custom nodes as packages; direct unittest does not.
    from .omniscience.checkpointing import CheckpointManager
    from .omniscience.context import ContextManager
    from .omniscience.skills import SkillStore
    from .omniscience.storage import TaskStore
except ImportError:
    from omniscience.checkpointing import CheckpointManager
    from omniscience.context import ContextManager
    from omniscience.skills import SkillStore
    from omniscience.storage import TaskStore

__all__ = [
    "CheckpointManager",
    "ContextManager",
    "SkillStore",
    "TaskStore",
]

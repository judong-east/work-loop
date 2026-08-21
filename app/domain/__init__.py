"""Domain primitives for the Workloop multi-agent workbench.

The domain package intentionally has no HTTP, subprocess, or vendor SDK
dependencies.  It is the stable contract shared by the resource, orchestration,
and interaction layers.
"""

from .models import (
    ContextState,
    ModelAlias,
    ModelProvider,
    NodeDefinition,
    NodeRun,
    Project,
    Session,
    SessionMessage,
    SessionMode,
    WorkflowDefinition,
    WorkflowNode,
)
from .node_registry import NodeRegistry, built_in_nodes
from .node_catalog import NodeCatalog

__all__ = [
    "ContextState",
    "ModelAlias",
    "ModelProvider",
    "NodeDefinition",
    "NodeCatalog",
    "NodeRegistry",
    "NodeRun",
    "Project",
    "Session",
    "SessionMessage",
    "SessionMode",
    "WorkflowDefinition",
    "WorkflowNode",
    "built_in_nodes",
]

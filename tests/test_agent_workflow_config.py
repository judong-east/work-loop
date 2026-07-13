from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agents.workflow_config import (
    BUILTIN_WORKFLOWS,
    WorkflowCatalog,
    WorkflowNodeKind,
    workflow_from_dict,
)


def custom_workflow(workflow_id: str = "personal") -> dict:
    return {
        "workflow_id": workflow_id,
        "label": "Personal flow",
        "nodes": [
            {
                "node_id": "plan",
                "kind": "planner",
                "label": "Plan",
                "instructions": "Inspect public APIs first.",
            },
            {"node_id": "execute", "kind": "executor", "label": "Execute"},
            {"node_id": "validate", "kind": "validation", "label": "Validate"},
            {"node_id": "review", "kind": "reviewer", "label": "Review"},
            {"node_id": "deliver", "kind": "delivery", "label": "Deliver"},
        ],
    }


class WorkflowDefinitionTest(unittest.TestCase):
    def test_builtins_offer_guarded_and_autopilot_topologies(self) -> None:
        self.assertTrue(BUILTIN_WORKFLOWS["guarded"].requires_plan_approval)
        self.assertFalse(BUILTIN_WORKFLOWS["autopilot"].requires_plan_approval)
        self.assertEqual(
            BUILTIN_WORKFLOWS["autopilot"].node(WorkflowNodeKind.EXECUTOR).label,
            "Codex 执行",
        )

    def test_rejects_unsafe_or_out_of_order_nodes(self) -> None:
        data = custom_workflow()
        data["nodes"][1], data["nodes"][2] = data["nodes"][2], data["nodes"][1]
        with self.assertRaisesRegex(ValueError, "节点顺序"):
            workflow_from_dict(data)

        data = custom_workflow()
        data["nodes"][2]["instructions"] = "run arbitrary command"
        with self.assertRaisesRegex(ValueError, "只有 Agent 节点"):
            workflow_from_dict(data)

    def test_catalog_persists_custom_workflow_without_overriding_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = WorkflowCatalog(Path(tmp) / "workflows.json")
            saved = catalog.save(workflow_from_dict(custom_workflow()))

            self.assertEqual(saved.workflow_id, "personal")
            self.assertEqual(catalog.get("personal").instructions_for(WorkflowNodeKind.PLANNER), "Inspect public APIs first.")
            self.assertEqual(
                {item.workflow_id for item in catalog.list_all()},
                {"guarded", "autopilot", "personal"},
            )

            data = custom_workflow("guarded")
            with self.assertRaisesRegex(ValueError, "内置工作流"):
                catalog.save(workflow_from_dict(data))

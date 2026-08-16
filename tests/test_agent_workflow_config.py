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
    def test_builtins_offer_guarded_and_quick_topologies(self) -> None:
        self.assertTrue(BUILTIN_WORKFLOWS["guarded"].requires_plan_approval)
        self.assertFalse(BUILTIN_WORKFLOWS["quick"].requires_plan_approval)
        self.assertEqual(
            BUILTIN_WORKFLOWS["quick"].node(WorkflowNodeKind.EXECUTOR).label,
            "Codex 执行",
        )

    def test_legacy_autopilot_id_resolves_to_quick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = WorkflowCatalog(Path(tmp) / "workflows.json")
            self.assertEqual(catalog.get("autopilot").workflow_id, "quick")

    def test_accepts_reordered_and_repeated_middle_nodes(self) -> None:
        data = custom_workflow()
        data["nodes"] = [
            data["nodes"][0],
            {
                "node_id": "preflight",
                "kind": "validation",
                "label": "Preflight",
                "instructions": "Record the baseline evidence.",
            },
            data["nodes"][1],
            {"node_id": "review-early", "kind": "reviewer", "label": "Early review"},
            {"node_id": "execute-final", "kind": "executor", "label": "Final execute"},
            data["nodes"][2],
            data["nodes"][3],
            data["nodes"][4],
        ]

        workflow = workflow_from_dict(data)

        self.assertEqual(
            [node.kind.value for node in workflow.nodes],
            [
                "planner",
                "validation",
                "executor",
                "reviewer",
                "executor",
                "validation",
                "reviewer",
                "delivery",
            ],
        )
        self.assertEqual(workflow.nodes[1].instructions, "Record the baseline evidence.")

    def test_rejects_a_workflow_without_fresh_delivery_evidence(self) -> None:
        data = custom_workflow()
        data["nodes"][1], data["nodes"][2] = data["nodes"][2], data["nodes"][1]
        with self.assertRaisesRegex(ValueError, "最后一个 executor.*validation"):
            workflow_from_dict(data)

        data = custom_workflow()
        data["nodes"][2], data["nodes"][3] = data["nodes"][3], data["nodes"][2]
        with self.assertRaisesRegex(ValueError, "最后一个 validation.*reviewer"):
            workflow_from_dict(data)

    def test_catalog_persists_custom_workflow_without_overriding_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = WorkflowCatalog(Path(tmp) / "workflows.json")
            saved = catalog.save(workflow_from_dict(custom_workflow()))

            self.assertEqual(saved.workflow_id, "personal")
            self.assertEqual(catalog.get("personal").instructions_for(WorkflowNodeKind.PLANNER), "Inspect public APIs first.")
            self.assertEqual(
                {item.workflow_id for item in catalog.list_all()},
                {"guarded", "quick", "personal"},
            )

            data = custom_workflow("guarded")
            with self.assertRaisesRegex(ValueError, "内置工作流"):
                catalog.save(workflow_from_dict(data))

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

import yaml

ROADMAP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROADMAP / "scripts"))

import validate  # noqa: E402


class EpicDefinitionValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with (ROADMAP / "schemas" / "epic-definition.schema.json").open(
            encoding="utf-8"
        ) as handle:
            cls.schema = json.load(handle)

    def _base(self) -> dict:
        return {
            "apiVersion": "roadmap.mctl.ai/v1alpha1",
            "kind": "EpicDefinition",
            "metadata": {"name": "example", "owner": "mctl-agents"},
            "spec": {
                "title": "Example",
                "goal": "Exercise validation.",
                "lifecycle": "active",
                "github": {
                    "issue": {"repository": "mctlhq/.github", "number": 1}
                },
                "phases": [{"id": "build", "title": "Build"}],
                "workItems": [
                    {
                        "id": "a",
                        "phase": "build",
                        "required": True,
                        "issue": {"repository": "mctlhq/a", "number": 10},
                    },
                    {
                        "id": "b",
                        "phase": "build",
                        "required": True,
                        "issue": {"repository": "mctlhq/b", "number": 20},
                        "dependsOn": ["a"],
                    },
                ],
                "completion": {"mode": "allRequired"},
                "successCriteria": ["It works."],
            },
        }

    def test_human_input_pilot_is_valid(self) -> None:
        with (ROADMAP / "epics" / "human-input.yaml").open(encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        self.assertEqual([], validate.validate_document(document, self.schema))

    def test_dependency_cycle_fails(self) -> None:
        document = self._base()
        document["spec"]["workItems"][0]["dependsOn"] = ["b"]
        errors = validate.validate_document(document, self.schema)
        self.assertIn("dependency graph contains cycle: a -> b -> a", errors)

    def test_parent_cycle_fails(self) -> None:
        document = self._base()
        document["spec"]["workItems"][0]["parent"] = "b"
        document["spec"]["workItems"][1]["parent"] = "a"
        errors = validate.validate_document(document, self.schema)
        self.assertIn("parent graph contains cycle: a -> b -> a", errors)

    def test_duplicate_issue_binding_fails(self) -> None:
        document = self._base()
        document["spec"]["workItems"][1]["issue"] = {
            "repository": "mctlhq/a",
            "number": 10,
        }
        errors = validate.validate_document(document, self.schema)
        self.assertIn(
            "GitHub issue mctlhq/a#10 is bound more than once: a, b", errors
        )

    def test_unknown_dependency_fails(self) -> None:
        document = self._base()
        document["spec"]["workItems"][1]["dependsOn"] = ["missing"]
        errors = validate.validate_document(document, self.schema)
        self.assertIn("work item b: unknown dependsOn target 'missing'", errors)

    def test_unbound_item_requires_title_and_owner(self) -> None:
        document = self._base()
        item = document["spec"]["workItems"][0]
        item.pop("issue")
        errors = validate.validate_document(document, self.schema)
        self.assertIn("work item a: unbound item requires owner", errors)
        self.assertIn("work item a: unbound item requires title", errors)

    def test_reverse_relation_is_not_authored(self) -> None:
        document = self._base()
        document["spec"]["workItems"][1]["blocks"] = ["a"]
        errors = validate.validate_document(document, self.schema)
        self.assertTrue(any("Additional properties are not allowed" in error for error in errors))

    def test_root_issue_cannot_be_reused_by_child(self) -> None:
        document = self._base()
        document["spec"]["workItems"][0]["issue"] = {
            "repository": "mctlhq/.github",
            "number": 1,
        }
        errors = validate.validate_document(document, self.schema)
        self.assertIn(
            "GitHub issue mctlhq/.github#1 is bound more than once: epic, a", errors
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
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

    def _write_yaml(self, path: Path, document: dict) -> None:
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def test_all_pilot_manifests_are_valid(self) -> None:
        manifests = sorted((ROADMAP / "epics").glob("*.yaml"))
        manifests += sorted((ROADMAP / "epics").glob("*.yml"))
        self.assertTrue(manifests)
        for path in manifests:
            with self.subTest(manifest=path.name):
                with path.open(encoding="utf-8") as handle:
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

    def test_external_dependency_cannot_point_to_local_binding(self) -> None:
        document = self._base()
        document["spec"]["workItems"][1]["externalDependsOn"] = [
            {"repository": "mctlhq/a", "number": 10}
        ]
        errors = validate.validate_document(document, self.schema)
        self.assertIn(
            "work item b: externalDependsOn mctlhq/a#10 is locally bound by a; use dependsOn instead",
            errors,
        )

    def test_external_dependency_cannot_point_to_epic_root(self) -> None:
        document = self._base()
        document["spec"]["workItems"][1]["externalDependsOn"] = [
            {"repository": "mctlhq/.github", "number": 1}
        ]
        errors = validate.validate_document(document, self.schema)
        self.assertIn(
            "work item b: externalDependsOn mctlhq/.github#1 is locally bound by epic; use dependsOn instead",
            errors,
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

    def test_repository_ref_rejects_shell_like_or_spacey_values(self) -> None:
        for repository in ("--foo/$(id)", "owner/repo --json x", "owner/repo\nx"):
            with self.subTest(repository=repository):
                document = self._base()
                document["spec"]["github"]["issue"]["repository"] = repository
                errors = validate.validate_document(document, self.schema)
                self.assertTrue(any("$.spec.github.issue.repository" in error for error in errors))

    def test_cycle_detector_handles_deep_acyclic_graph(self) -> None:
        size = 1500
        nodes = [f"n{i}" for i in range(size)]
        edges = {nodes[i]: [nodes[i - 1]] for i in range(1, size)}
        edges[nodes[0]] = []
        self.assertIsNone(validate._cycle(nodes, edges))

    def test_corpus_rejects_duplicate_issue_bindings_across_manifests(self) -> None:
        first = self._base()
        second = self._base()
        second["metadata"]["name"] = "second"
        second["spec"]["github"]["issue"] = {
            "repository": "mctlhq/.github",
            "number": 2,
        }
        second["spec"]["workItems"][0]["issue"] = {
            "repository": "mctlhq/a",
            "number": 10,
        }
        second["spec"]["workItems"][1]["issue"] = {
            "repository": "mctlhq/c",
            "number": 30,
        }

        path_a = Path("a.yaml")
        path_b = Path("b.yaml")
        failures = validate.corpus_errors([(path_a, first), (path_b, second)])
        expected = (
            "GitHub issue mctlhq/a#10 is bound across manifests: "
            "a.yaml (a) and b.yaml (a)"
        )
        self.assertIn(expected, failures[path_a])
        self.assertIn(expected, failures[path_b])

    def test_corpus_rejects_duplicate_epic_names(self) -> None:
        first = self._base()
        second = self._base()
        second["spec"]["github"]["issue"]["number"] = 2
        second["spec"]["workItems"][0]["issue"]["number"] = 11
        second["spec"]["workItems"][1]["issue"]["number"] = 21

        path_a = Path("a.yaml")
        path_b = Path("b.yaml")
        failures = validate.corpus_errors([(path_a, first), (path_b, second)])
        expected = "epic metadata.name 'example' is duplicated across a.yaml and b.yaml"
        self.assertIn(expected, failures[path_a])
        self.assertIn(expected, failures[path_b])

    def test_repository_case_variant_collides_with_epic_root_binding(self) -> None:
        """T0a: root and a work item naming one issue in different case."""

        document = self._base()
        document["spec"]["github"]["issue"] = {
            "repository": "mctlhq/example",
            "number": 10,
        }
        document["spec"]["workItems"][0]["issue"] = {
            "repository": "MCTLHQ/EXAMPLE",
            "number": 10,
        }
        errors = validate.validate_document(document, self.schema)
        self.assertIn(
            "GitHub issue MCTLHQ/EXAMPLE#10 is bound more than once: epic, a",
            errors,
        )

    def test_repository_case_variant_collides_between_work_items(self) -> None:
        document = self._base()
        document["spec"]["workItems"][1]["issue"] = {
            "repository": "MCTLHQ/A",
            "number": 10,
        }
        errors = validate.validate_document(document, self.schema)
        self.assertIn(
            "GitHub issue MCTLHQ/A#10 is bound more than once: a, b", errors
        )

    def test_corpus_rejects_case_variant_across_root_and_work_item(self) -> None:
        """T0b: ownership crosses manifests and binding classes."""

        first = self._base()
        first["spec"]["github"]["issue"] = {
            "repository": "mctlhq/example",
            "number": 10,
        }

        second = self._base()
        second["metadata"]["name"] = "second"
        second["spec"]["github"]["issue"] = {
            "repository": "mctlhq/.github",
            "number": 2,
        }
        second["spec"]["workItems"][0]["issue"] = {
            "repository": "MCTLHQ/EXAMPLE",
            "number": 10,
        }
        second["spec"]["workItems"][1]["issue"] = {
            "repository": "mctlhq/c",
            "number": 30,
        }

        path_a = Path("a.yaml")
        path_b = Path("b.yaml")
        failures = validate.corpus_errors([(path_a, first), (path_b, second)])
        expected = (
            "GitHub issue MCTLHQ/EXAMPLE#10 is bound across manifests: "
            "a.yaml (epic) and b.yaml (a)"
        )
        self.assertIn(expected, failures[path_a])
        self.assertIn(expected, failures[path_b])

    def test_external_dependency_case_variant_of_local_binding_fails(self) -> None:
        """T0c: a case variant does not escape the local-binding rule."""

        document = self._base()
        document["spec"]["workItems"][1]["externalDependsOn"] = [
            {"repository": "MCTLHQ/A", "number": 10}
        ]
        errors = validate.validate_document(document, self.schema)
        self.assertIn(
            "work item b: externalDependsOn MCTLHQ/A#10 is locally bound by a; "
            "use dependsOn instead",
            errors,
        )

    def test_issue_key_canonicalizes_repository_case(self) -> None:
        self.assertEqual(
            validate.issue_key({"repository": "MCTLHQ/Mctl-API", "number": 261}),
            validate.issue_key({"repository": "mctlhq/mctl-api", "number": 261}),
        )

    def test_main_returns_zero_for_valid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "valid.yaml"
            self._write_yaml(path, self._base())
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(0, validate.main([str(path)]))

    def test_main_returns_one_for_invalid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "invalid.yaml"
            document = self._base()
            document["spec"]["workItems"][0]["dependsOn"] = ["b"]
            self._write_yaml(path, document)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(1, validate.main([str(path)]))

    def test_main_returns_two_for_invalid_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manifest = Path(raw) / "valid.yaml"
            schema = Path(raw) / "broken.json"
            self._write_yaml(manifest, self._base())
            schema.write_text("{not-json", encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    2,
                    validate.main([str(manifest), "--schema", str(schema)]),
                )

    def test_main_returns_two_when_directory_has_no_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(2, validate.main([raw]))

    def test_manifest_paths_include_yaml_and_yml_and_deduplicate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            yaml_path = directory / "a.yaml"
            yml_path = directory / "b.yml"
            yaml_path.write_text("{}", encoding="utf-8")
            yml_path.write_text("{}", encoding="utf-8")
            paths = validate._manifest_paths(
                [str(directory), str(yaml_path), str(yml_path)]
            )
            self.assertEqual([yaml_path, yml_path], paths)


if __name__ == "__main__":
    unittest.main()

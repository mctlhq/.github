from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROADMAP = Path(__file__).resolve().parents[1]
REPO = ROADMAP.parent
sys.path.insert(0, str(ROADMAP / "scripts"))
sys.path.insert(0, str(ROADMAP / "tests"))

import lifecycle_guard as guard  # noqa: E402
import mutations  # noqa: E402

FINOPS = yaml.safe_load((ROADMAP / "epics" / "agent-finops.yaml").read_text(encoding="utf-8"))
PATH = "roadmap/epics/agent-finops.yaml"


def state(document, path=PATH):
    return guard.epic_state(path, document)


def codes(violations):
    return sorted(v.code for v in violations)


class CheckTest(unittest.TestCase):
    def test_unchanged(self):
        self.assertEqual(guard.check([state(FINOPS)], [state(FINOPS)]), [])

    def test_issue_152_mutation(self):
        head = mutations.add_required_item(FINOPS, "trajectory-economics")
        found = guard.check([state(FINOPS)], [state(head)])
        self.assertEqual(codes(found), ["RequiredItemAdded"])
        self.assertIn("trajectory-economics", str(found[0]))

    def test_optional_addition_allowed(self):
        head = mutations.add_required_item(FINOPS, "extra", required=False)
        self.assertEqual(guard.check([state(FINOPS)], [state(head)]), [])

    def test_optional_raised_to_required(self):
        base = mutations.add_required_item(FINOPS, "extra", required=False)
        head = mutations.set_required(base, "extra", True)
        self.assertEqual(codes(guard.check([state(base)], [state(head)])), ["RequiredFlagRaised"])

    def test_reopen_opts_out(self):
        head = mutations.add_required_item(FINOPS, "trajectory-economics")
        for lifecycle in ("active", "paused"):
            reopened = mutations.set_lifecycle(head, lifecycle)
            self.assertEqual(guard.check([state(FINOPS)], [state(reopened)]), [])

    def test_stale_base(self):
        base = mutations.set_lifecycle(FINOPS, "active")
        head = mutations.add_required_item(FINOPS, "trajectory-economics")
        self.assertEqual(codes(guard.check([state(base)], [state(head)])), ["RequiredItemAdded"])

    def test_born_completed(self):
        new = {**FINOPS, "metadata": {**FINOPS["metadata"], "name": "brand-new"}}
        self.assertEqual(codes(guard.check([], [state(new, "roadmap/epics/new.yaml")])), ["BornCompletedUnverified"])
        bound = mutations.add_required_item(new, "y", issue="mctlhq/.github#9999")
        self.assertEqual(codes(guard.check([], [state(bound, "roadmap/epics/new.yaml")])), ["BornCompletedUnverified"])
        for lifecycle in ("active", "paused"):
            self.assertEqual(guard.check([], [state(mutations.set_lifecycle(new, lifecycle))]), [])

    def test_deleted_and_shrunk(self):
        self.assertEqual(guard.check([state(FINOPS)], []), [])
        self.assertEqual(
            guard.check([state(FINOPS)], [state(mutations.set_required(FINOPS, "usage-ledger", False))]), []
        )
        shrunk = yaml.safe_load(yaml.safe_dump(FINOPS))
        shrunk["spec"]["workItems"].pop()
        self.assertEqual(guard.check([state(FINOPS)], [state(shrunk)]), [])

    def test_moved_file_keeps_identity(self):
        head = mutations.add_required_item(FINOPS, "trajectory-economics")
        found = guard.check([state(FINOPS)], [state(head, "roadmap/epics/moved.yaml")])
        self.assertEqual(codes(found), ["RequiredItemAdded"])

    def test_load_bearing(self):
        head = mutations.add_required_item(FINOPS, "trajectory-economics")
        with mock.patch.object(guard, "check", lambda base, head: []):
            with self.assertRaises(AssertionError):
                self.assertEqual(
                    codes(guard.check([state(FINOPS)], [state(head)])), ["RequiredItemAdded"]
                )


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=repo, check=True, capture_output=True)


class GitBaseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q")
        (self.repo / "roadmap/epics").mkdir(parents=True)

    def commit(self, files):
        for rel, text in files.items():
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c")

    def test_enumerates_recursively_and_ignores_non_manifests(self):
        other = mutations.set_lifecycle({**FINOPS, "metadata": {**FINOPS["metadata"], "name": "other"}}, "active")
        self.commit({
            PATH: yaml.safe_dump(FINOPS),
            "roadmap/epics/other.yaml": yaml.safe_dump(other),
            "roadmap/epics/README": "not a manifest",
        })
        states = guard.load_base_from_git(self.repo, "HEAD")
        self.assertEqual(sorted(s.name for s in states), ["agent-finops", "other"])

    def test_requires_recursive_listing(self):
        self.commit({PATH: yaml.safe_dump(FINOPS)})
        real = guard._git

        def no_recursion(repo, *args):
            return real(repo, *("-z" if a == "-rz" else a for a in args))

        with mock.patch.object(guard, "_git", no_recursion):
            self.assertNotEqual(
                [s.name for s in guard.load_base_from_git(self.repo, "HEAD")], ["agent-finops"]
            )

    def test_empty_base(self):
        self.commit({"roadmap/epics/README": "x"})
        self.assertEqual(guard.load_base_from_git(self.repo, "HEAD"), [])

    def test_unresolvable(self):
        self.commit({PATH: yaml.safe_dump(FINOPS)})
        with self.assertRaisesRegex(guard.BaseError, "^base-unresolvable:"):
            guard.load_base_from_git(self.repo, "deadbeef" * 5)

    def test_shallow_hint(self):
        self.commit({PATH: yaml.safe_dump(FINOPS)})
        real = guard._git

        def fake(repo, *args):
            if args[:1] == ("rev-parse",) and "--is-shallow-repository" in args:
                return subprocess.CompletedProcess(args, 0, b"true\n", b"")
            return real(repo, *args)

        with mock.patch.object(guard, "_git", fake):
            with self.assertRaisesRegex(guard.BaseError, "base-unresolvable:.*shallow"):
                guard.load_base_from_git(self.repo, "deadbeef" * 5)

    def test_malformed(self):
        for text in ("- a\n", "a: [", "metadata: {name: x}\nspec: {lifecycle: active}\n",
                     "metadata: {name: x}\nspec: {lifecycle: active, workItems: [{id: a}]}\n"):
            with self.subTest(text=text):
                self.commit({PATH: text})
                with self.assertRaisesRegex(guard.BaseError, "^base-malformed:.*agent-finops.yaml"):
                    guard.load_base_from_git(self.repo, "HEAD")

    def test_unreadable_blob_and_tree(self):
        self.commit({PATH: yaml.safe_dump(FINOPS)})
        real = guard._git

        def failing(command):
            def fake(repo, *args):
                if args[0] == command:
                    return subprocess.CompletedProcess(args, 128, b"", b"boom")
                return real(repo, *args)
            return fake

        with mock.patch.object(guard, "_git", failing("show")):
            with self.assertRaisesRegex(guard.BaseError, "^base-unreadable:.*agent-finops.yaml"):
                guard.load_base_from_git(self.repo, "HEAD")
        with mock.patch.object(guard, "_git", failing("ls-tree")):
            with self.assertRaisesRegex(guard.BaseError, "^base-unreadable:.*roadmap/epics"):
                guard.load_base_from_git(self.repo, "HEAD")


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = guard.main(list(argv))
        return code, err.getvalue()

    def test_converged_tree_is_clean(self):
        epics = str(ROADMAP / "epics")
        self.assertEqual(self.run_cli("--base-dir", epics, "--head-dir", epics)[0], 0)

    def test_violation_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = Path(tmp)
            for path in (ROADMAP / "epics").glob("*.yaml"):
                (head / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            document = yaml.safe_load((head / "agent-finops.yaml").read_text(encoding="utf-8"))
            mutated = mutations.add_required_item(document, "trajectory-economics")
            (head / "agent-finops.yaml").write_text(yaml.safe_dump(mutated), encoding="utf-8")
            code, _ = self.run_cli("--base-dir", str(ROADMAP / "epics"), "--head-dir", str(head))
            self.assertEqual(code, 1)

    def test_head_invalid_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = Path(tmp)
            text = (ROADMAP / "epics" / "agent-finops.yaml").read_text(encoding="utf-8")
            (head / "a.yaml").write_text(text, encoding="utf-8")
            (head / "b.yaml").write_text(text, encoding="utf-8")
            code, err = self.run_cli("--base-dir", str(ROADMAP / "epics"), "--head-dir", str(head))
            self.assertEqual((code, err.startswith("head-invalid:")), (2, True))
            (head / "b.yaml").write_text("metadata: {}\n", encoding="utf-8")
            code, err = self.run_cli("--base-dir", str(ROADMAP / "epics"), "--head-dir", str(head))
            self.assertEqual((code, err.startswith("head-invalid:")), (2, True))

    def test_base_errors_exit_2(self):
        epics = str(ROADMAP / "epics")
        code, err = self.run_cli("--base-ref", "no-such-ref", "--repo", str(REPO), "--head-dir", epics)
        self.assertEqual((code, err.startswith("base-unresolvable:")), (2, True))


class WorkflowTest(unittest.TestCase):
    def test_zero_base_warns_and_skips_guard(self):
        workflow = yaml.safe_load(
            (REPO / ".github/workflows/roadmap-validate.yml").read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["validate"]["steps"]
        self.assertEqual(workflow["permissions"], {})
        checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        warn = next(s for s in steps if "guard NOT evaluated" in s.get("run", ""))
        self.assertIn("::warning::", warn["run"])
        self.assertIn("0000000000000000000000000000000000000000", warn["if"])
        guard_step = next(
            s for s in steps
            if "lifecycle_guard.py" in s.get("run", "") and "push" in s.get("if", "")
            and "!=" in s["if"]
        )
        self.assertIn("!=", guard_step["if"])
        self.assertIn("0000000000000000000000000000000000000000", guard_step["if"])
        self.assertNotIn("exit 0", guard_step["run"])


if __name__ == "__main__":
    unittest.main()

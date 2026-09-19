"""The canonical envelope schema accepts every valid example and rejects every invalid one."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator

EVENTS = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((EVENTS / "schemas" / "event-envelope.v1.schema.json").read_text())


class EnvelopeSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Draft202012Validator.check_schema(SCHEMA)
        cls.validator = Draft202012Validator(SCHEMA)

    def examples(self, kind: str) -> list[Path]:
        paths = sorted((EVENTS / "examples" / kind).glob("*.json"))
        self.assertTrue(paths, f"no {kind} examples")
        return paths

    def test_valid_examples(self) -> None:
        for path in self.examples("valid"):
            with self.subTest(path.name):
                errors = [e.message for e in self.validator.iter_errors(json.loads(path.read_text()))]
                self.assertEqual([], errors)
                self.assertLessEqual(len(path.read_bytes()), 4096)

    def test_invalid_examples_are_rejected_by_the_schema_itself(self) -> None:
        for path in self.examples("invalid"):
            with self.subTest(path.name):
                self.assertFalse(self.validator.is_valid(json.loads(path.read_text())))

    def test_calendar_invalid_examples_pass_the_schema_but_not_a_parse(self) -> None:
        """What only a parsing consumer can catch: the pattern cannot know that
        February has no 30th. Kept apart so the schema assertion above is never
        satisfied by the parse instead."""

        for path in self.examples("calendar-invalid"):
            with self.subTest(path.name):
                doc = json.loads(path.read_text())
                self.assertTrue(self.validator.is_valid(doc))
                with self.assertRaises(ValueError):
                    datetime.fromisoformat(doc["occurred_at"].replace("Z", "+00:00"))


if __name__ == "__main__":
    unittest.main()

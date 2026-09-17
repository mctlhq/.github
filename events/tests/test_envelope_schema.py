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

    def test_invalid_examples(self) -> None:
        for path in self.examples("invalid"):
            with self.subTest(path.name):
                doc = json.loads(path.read_text())
                self.assertFalse(
                    self.validator.is_valid(doc) and self.timestamp_parses(doc),
                    "accepted by the schema and by the consumer-side timestamp parse",
                )

    @staticmethod
    def timestamp_parses(doc: dict) -> bool:
        """What every consumer does after the schema: parse occurred_at for real.

        The schema pattern bounds each field's range but cannot know that
        February has no 30th; a consumer that parses the value can.
        """

        try:
            datetime.fromisoformat(str(doc.get("occurred_at", "")).replace("Z", "+00:00"))
        except ValueError:
            return False
        return True


if __name__ == "__main__":
    unittest.main()

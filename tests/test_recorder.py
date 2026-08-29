import unittest

from scripts.record_real_demo import redact


class RecorderTests(unittest.TestCase):
    def test_redact_handles_nested_values(self):
        value = {"token": "prefix secret suffix", "items": ["secret", 1]}
        self.assertEqual(
            redact(value, "secret"),
            {"token": "prefix [REDACTED_API_KEY] suffix", "items": ["[REDACTED_API_KEY]", 1]},
        )


if __name__ == "__main__":
    unittest.main()

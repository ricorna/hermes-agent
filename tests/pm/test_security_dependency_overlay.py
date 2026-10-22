"""Metadata-only guards for the draft v0.22.4 security overlay.

The full lock and official PM generation remain pending certifi quarantine.
"""
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
HARNESS_SHA = "5068d73cdac0f281b3a26d94644623fd144b486a"


class SecurityDependencyOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())

    def test_core_pins_keep_runtime_markers(self):
        deps = self.metadata["project"]["dependencies"]
        for pin in ("Pillow==13.0.0", "certifi==2026.10.19"):
            self.assertIn(pin + "; python_version >= '3.14'", deps)
        self.assertIn(
            "browser-harness @ git+https://github.com/ricorna/browser-harness.git@"
            + HARNESS_SHA + " ; python_version >= '3.14'", deps
        )

    def test_aiohttp_extras_and_linux_gate(self):
        extras = self.metadata["project"]["optional-dependencies"]
        for name in ("messaging", "slack", "sms", "teams"):
            self.assertIn("aiohttp==3.14.4", extras[name])
        self.assertIn("aiohttp==3.14.4; sys_platform == 'linux'", extras["matrix"])
        self.assertIn("oauthlib==4.0.0", extras["google"])

    def test_quarantine_and_existing_overrides_preserved(self):
        uv = self.metadata["tool"]["uv"]
        self.assertEqual(uv["exclude-newer"], "3 days")
        self.assertIn("yarl==1.25.1", uv["constraint-dependencies"])
        self.assertEqual(uv["override-dependencies"], [
            "pynacl>=1.6,<1.7", "cryptography==50.0.1",
            "spacy-curated-transformers; sys_platform == 'never'",
        ])


if __name__ == "__main__":
    unittest.main()

"""Exercise the actual YAML subprocess, never a mocked parser result."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/maintenance"))
import jimmy_v0231_activate as controller


class YamlRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "config.yaml"
        self.pythons = json.loads(os.environ.get("JIMMY_YAML_PYTHONS", "null")) or [sys.executable]

    def read(self, text, python):
        self.path.write_text(text, encoding="utf-8-sig")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        env.update(HOME=str(self.root), HERMES_HOME=str(self.root))
        def command(argv):
            # A dev environment's PyYAML must not hide a production dependency bug.
            argv = list(argv)
            argv[2] = (
                "import sys; sys.modules['yaml'] = None; " + argv[2]
            )
            return subprocess.check_output(argv, cwd=self.root, env=env, text=True,
                                           stderr=subprocess.PIPE, timeout=15)
        return controller.yaml_value(self.path, command, python)

    def test_bom_yaml11_and_nested_values(self):
        for python in self.pythons:
            with self.subTest(python=python):
                self.assertEqual(self.read("enabled: on\nflag: n\nlabel: 'off'\nitems: [1, null, café]\n", python),
                                 {"enabled": True, "flag": False, "label": "off", "items": [1, None, "café"]})

    def test_empty_document(self):
        for python in self.pythons:
            with self.subTest(python=python):
                self.assertIsNone(self.read("", python))

    def test_duplicate_keys_fail_closed(self):
        for python in self.pythons:
            with self.subTest(python=python):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.read("key: 1\nkey: 2\n", python)
                self.assertIn("DuplicateKeyError", caught.exception.stderr)

    def test_malformed_yaml_fails_closed(self):
        for python in self.pythons:
            with self.subTest(python=python):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.read("key: [\n", python)
                self.assertIn("ParserError", caught.exception.stderr)

    def test_python_tags_rejected(self):
        for python in self.pythons:
            with self.subTest(python=python):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.read("!!python/object:builtins.object {}", python)
                self.assertIn("ConstructorError", caught.exception.stderr)

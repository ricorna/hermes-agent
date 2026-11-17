"""Frozen v0.23.2 target and source fence; never invoke production preflight."""

import ast
import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts/maintenance"
sys.path.insert(0, str(SCRIPTS))
import jimmy_v0231_activate as c

ROOT = pathlib.Path("/home/jimmy/.local/state/hermes-fleet/jimmy-v0232-reconcile-t_1e63040a")
SHA = "e850d48d9ac707f01c619d1d4ddd9afcc9eeaace"
BASE = "ae2a81ccc00e9b7c06301523e5bb09608397149c"
PLUGIN = "32e4000c6e58133f7877309f8e79db8b12d1e1f3"


class ReleaseTargetTests(unittest.TestCase):
    def test_frozen_release_and_plugin_bindings(self):
        self.assertEqual(c.ROOT, ROOT)
        self.assertEqual(c.SRC, ROOT / "source")
        self.assertEqual(c.REVIEWED_PLUGIN, ROOT / "reviewed-plugin")
        self.assertEqual(c.VENV, ROOT / "source/.venv")
        self.assertEqual((c.SHA, c.BASE, c.PLUGIN_SHA), (SHA, BASE, PLUGIN))
        self.assertEqual(c.OLD_SHA, "f924a08357f95c24f826d595c48bb8da06c228e2")
        self.assertEqual(c.OLD_PLUGIN_SHA, "c92c27c9f919178a58974a72333b473c6cb2e71d")

    def source_fence(self, head=SHA, release=BASE):
        # Isolate only the real preflight identity assertion, as in the existing
        # baseline regression harness. No Controller construction or side effects.
        tree = ast.parse(pathlib.Path(c.__file__).read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Controller")
        preflight = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "preflight")
        fence = next(n for n in preflight.body if isinstance(n, ast.Assert) and "rev-parse" in ast.unparse(n))
        calls = []

        class Probe:
            def cmd(self, args):
                calls.append(args)
                return {"HEAD": head, "HEAD^": "2dbdb78e2ccc3f05cd6d6acca0cfbcfd71cb0359", "HEAD^2": release}[args[-1]]

        code = compile(ast.Module(body=[fence], type_ignores=[]), c.__file__, "exec")
        exec(code, {**vars(c), "self": Probe()})
        self.assertEqual([args[-1] for args in calls], ["HEAD", "HEAD^2"])

    def test_accepts_exact_combined_head_and_release_parent(self):
        self.source_fence()

    def test_rejects_wrong_combined_head(self):
        with self.assertRaises(AssertionError):
            self.source_fence(head="0" * 40)

    def test_rejects_wrong_release_parent(self):
        with self.assertRaises(AssertionError):
            self.source_fence(release="0" * 40)


if __name__ == "__main__":
    unittest.main()

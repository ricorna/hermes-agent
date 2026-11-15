"""Opt-in ONLY on a disposable Linux user systemd manager.

Run: JIMMY_DISPOSABLE_SYSTEMD_TEST=t-02dfc300 python3 -m unittest discover \
    -s tests/maintenance -p test_jimmy_systemd_disposable.py -v

Uses private source-local files and exact owned runtime links/units. Does not import
or execute production scripts, access a gateway, use ~/.hermes, or invoke SSH.
"""

import copy
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/maintenance"))
import jimmy_v0231_activate as c
import jimmy_v0231_watchdog as w

PREFIX = "hermes-jimmy-test-t-02dfc300-"
OPT_IN = os.getenv("JIMMY_DISPOSABLE_SYSTEMD_TEST") == "t-02dfc300"


@unittest.skipUnless(OPT_IN, "requires explicit disposable Linux systemd opt-in")
class DisposableSystemd(unittest.TestCase):
    def setUp(self):
        self.assertEqual(sys.platform, "linux", "run on the disposable Linux host only")
        self.temp = tempfile.TemporaryDirectory(
            prefix=PREFIX, dir=Path(__file__).parent
        )
        self.root = Path(self.temp.name).resolve()
        self.assertNotEqual(self.root, c.ROOT)
        self.deadline = c.Deadline(90)
        self.name = PREFIX + secrets.token_hex(6) + ".service"
        self.runtime = Path("/run/user") / str(os.getuid()) / "systemd/user"
        self.assertTrue(self.runtime.parent.exists())
        self.links = []
        self.addCleanup(self.cleanup)

    def cmd(self, args, timeout=20):
        for arg in map(str, args):
            if arg.endswith(".service"):
                self.assertTrue(Path(arg).name.startswith(PREFIX), arg)
        return c.command(args, self.deadline, timeout)

    def cleanup(self):
        # Fresh teardown deadline is separate from the operation under test.
        self.deadline = c.Deadline(30)
        try:
            self.cmd(["systemctl", "--user", "stop", self.name])
            self.cmd(["systemctl", "--user", "reset-failed", self.name])
        finally:
            for path in reversed(self.links):
                self.assertTrue(path.name.startswith(PREFIX))
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    (path / "zz-owned.conf").unlink(missing_ok=True)
                    path.rmdir()
            self.cmd(["systemctl", "--user", "daemon-reload"])
            self.temp.cleanup()

    def link_unit(self, body):
        fragment = self.root / self.name
        c.atomic(fragment, body.encode())
        self.runtime.mkdir(parents=True, exist_ok=True)
        link = self.runtime / self.name
        self.assertFalse(link.exists() or link.is_symlink())
        link.symlink_to(fragment)
        self.links.append(link)
        self.cmd(["systemctl", "--user", "daemon-reload"])
        return fragment

    def owner(self, prepared):
        self.work = self.root / "activate-test"
        (self.work / "backup").mkdir(parents=True)
        self.prepared = {
            "schema": c.SCHEMA,
            "attempt": self.work.name,
            "nonce": secrets.token_hex(32),
            "activation_unit": self.name,
            **prepared,
        }
        c.write_json(self.work / "backup/prepared.json", self.prepared)
        c.write_json(
            self.root / "attempt-owner.json",
            {
                **self.prepared,
                "prepared_sha256": c.digest(
                    (self.work / "backup/prepared.json").read_bytes()
                ),
            },
        )

    def test_drop_add_remove_before_and_after_reload(self):
        self.exercise_drop(active=True)

    def test_inactive_drop_reload_on_garbage_collection(self):
        self.exercise_drop(active=False)

    def exercise_drop(self, active):
        self.link_unit("[Service]\nType=simple\nExecStart=/usr/bin/sleep infinity\n")
        if active:
            self.cmd(["systemctl", "--user", "start", self.name])
        link = self.runtime / (self.name + ".d")
        self.assertFalse(link.exists() or link.is_symlink())
        link.mkdir()
        self.links.append(link)
        drop = link / "zz-owned.conf"

        def snapshot():
            state = c.unit_state(self.cmd, self.name)
            state["physical"][str(drop)] = c.file_identity(drop)
            return state

        original = snapshot()
        self.owner({})
        journal = c.Journal(self.root, self.work, snapshot)
        journal.allow("initial", [original])
        venv = self.root / "candidate"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").symlink_to(sys.executable)
        environment = {
            "HERMES_HOME": str(self.root / "private-home"),
            "VIRTUAL_ENV": str(venv),
            "PATH": "/usr/bin:/bin",
            "HERMES_RUNTIME_DIR": str(self.root / "runtime"),
        }
        body = c.drop_body(venv, environment)
        expected = c.expected_unit(original, drop, body, venv, environment)
        physical_only = copy.deepcopy(original)
        physical_only["physical"] = expected["physical"]
        journal.change(
            "add-before-reload",
            physical_only,
            lambda: c.atomic(drop, body),
            alternatives=[expected],
        )
        if active:
            self.assertEqual(
                journal.check()["loaded"]["ExecStart"], original["loaded"]["ExecStart"]
            )
        journal.change(
            "add-after-reload",
            expected,
            lambda: self.cmd(["systemctl", "--user", "daemon-reload"]),
        )
        self.assertEqual(
            journal.check()["loaded"]["ExecStart"], expected["loaded"]["ExecStart"]
        )
        removed = copy.deepcopy(expected)
        removed["physical"][str(drop)] = None
        journal.change(
            "remove-before-reload", removed, drop.unlink, alternatives=[original]
        )
        if active:
            self.assertEqual(
                journal.check()["loaded"]["ExecStart"], expected["loaded"]["ExecStart"]
            )
        journal.change(
            "remove-after-reload",
            original,
            lambda: self.cmd(["systemctl", "--user", "daemon-reload"]),
        )
        self.assertEqual(journal.check(), original)

    def start_worker(self):
        worker = self.root / "worker.py"
        child_ready = self.root / "child.json"
        worker.write_text(
            "\n".join(
                [
                    "import os,signal,json,pathlib",
                    "child=os.fork()",
                    "if child == 0:",
                    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                    "    pathlib.Path("
                    + repr(str(child_ready))
                    + ').write_text(json.dumps({"pid":os.getpid()}))',
                    "signal.pause()",
                ]
            )
        )
        self.link_unit(
            "[Service]\nType=simple\nKillMode=control-group\nTimeoutStopSec=2\n"
            "ExecStart=" + c.quoted(sys.executable) + " " + c.quoted(str(worker)) + "\n"
        )
        self.cmd(["systemctl", "--user", "start", self.name])
        while not child_ready.exists():
            self.deadline.pause(0.05)
        child = c.read_json(child_ready)["pid"]
        child_start = c.proc_start(child)
        pid = int(
            self.cmd(
                ["systemctl", "--user", "show", self.name, "-p", "MainPID", "--value"]
            )
        )
        identity = w.worker_identity(self.cmd, self.name)
        self.owner(
            {
                "worker_identity": identity,
                "worker_pid": pid,
                "worker_start": c.proc_start(pid),
            }
        )
        return pid, child, child_start, identity["unit"]["loaded"]["ControlGroup"]

    def test_watchdog_fences_worker_and_sigterm_resistant_child(self):
        _, child, _, group = self.start_worker()
        # fence_worker is the same function supervise uses before invoking controller recovery.
        self.assertTrue(
            w.fence_worker(self.root, self.work, self.name, self.cmd, self.deadline)
        )
        self.assertTrue(c.cgroup_empty(group))
        self.assertEqual(
            int(
                self.cmd(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        self.name,
                        "-p",
                        "MainPID",
                        "--value",
                    ]
                )
            ),
            0,
        )
        if Path("/proc", str(child), "stat").exists():
            self.assertEqual(
                Path("/proc", str(child), "stat")
                .read_text()
                .rsplit(")", 1)[1]
                .split()[0],
                "Z",
            )

    def test_committed_watchdog_leaves_worker_and_child_alive(self):
        pid, child, child_start, group = self.start_worker()
        c.commit_receipt(
            self.root,
            self.work,
            {"attempt": self.work.name, "nonce": self.prepared["nonce"]},
        )
        self.assertFalse(
            w.fence_worker(self.root, self.work, self.name, self.cmd, self.deadline)
        )
        self.assertEqual(c.proc_start(pid), self.prepared["worker_start"])
        self.assertEqual(c.proc_start(child), child_start)
        self.assertFalse(c.cgroup_empty(group))


if __name__ == "__main__":
    unittest.main()

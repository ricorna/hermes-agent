"""Actual snapshot/transition composition, isolated from production state."""

import copy
from unittest import mock
import test_jimmy_controller as t
import test_jimmy_systemd_disposable as remote

c, w = t.c, t.w


def setup_controller(case, cmd, unit):
    ctl = object.__new__(c.Controller)
    ctl.work = case.work
    ctl.backup = case.work / "backup"
    ctl.budget = c.Deadline(30)
    ctl.unit = unit
    ctl.cmd = cmd
    ctl.drop = case.root / "drop.conf"
    ctl.live_plugin = case.root / "plugin"
    ctl.launcher = case.root / "launcher"
    ctl.original_cgroup = ""
    ctl.recovery_started = True
    ctl.rolled = case.work / "rollback-performed.json"
    ctl.attempt = case.work.name
    ctl.clear_owned_drain = lambda: None
    ctl.disarm = lambda: None
    ctl.guard = c.Journal(case.root, case.work, ctl.snapshot)
    return ctl


def exercise(case, ctl, save_owner):
    def status():
        pid = int(
            ctl.cmd(
                ["systemctl", "--user", "show", ctl.unit, "-p", "MainPID", "--value"]
            )
        )
        return {
            "pid": pid,
            "start_time": c.proc_start(pid) if pid else 0,
            "code_sha": c.OLD_SHA,
            "answered_at": c.time.time(),
        }

    ctl.ctl = lambda verb: status()
    original = ctl.snapshot()
    case.prepared.update(
        original=original,
        gateway_cgroup=ctl.original_cgroup,
        candidate_plugin=None,
        platforms={},
    )
    save_owner()
    c.write_json(ctl.backup / "launcher.json", {"exists": False, "symlink": False})
    ctl.guard.allow("initial", [original])
    ctl.stop_gateway()
    ctl.reload_to(original["unit"])
    if isinstance(case, remote.DisposableSystemd):
        venv = case.root / "candidate"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").symlink_to(remote.sys.executable)
        environment = {
            "HERMES_HOME": str(case.root / "private-home"),
            "VIRTUAL_ENV": str(venv),
            "PATH": "/usr/bin:/bin",
            "HERMES_RUNTIME_DIR": str(case.root / "runtime"),
        }
        body = c.drop_body(venv, environment)
        expected = c.expected_unit(original["unit"], ctl.drop, body, venv, environment)
        current = ctl.guard.check()
        physical_only = copy.deepcopy(current)
        physical_only["unit"]["physical"] = expected["physical"]
        reloaded = copy.deepcopy(physical_only)
        reloaded["unit"] = expected
        ctl.guard.change(
            "drop-add",
            physical_only,
            lambda: c.atomic(ctl.drop, body),
            alternatives=[reloaded],
        )
        ctl.reload_to(expected)
        case.assertEqual(
            ctl.snapshot()["unit"]["loaded"]["ExecStart"],
            expected["loaded"]["ExecStart"],
        )
    # Readiness transport is a fixture; snapshot/journal/systemd transitions are real.
    ctl.ready = lambda *args, **kwargs: (status(), {})
    ctl.ctl = lambda verb: status()

    with mock.patch.object(c, "ROOT", case.root):
        ctl.rollback()
        case.assertTrue(ctl.rolled.exists())
        ctl.rollback()


class SnapshotComposition(t.Workspace):
    def test_snapshot_reload_and_standalone_rollback(self):
        pid = [0]

        def cmd(args):
            if "start" in args:
                pid[0] = 123
            if "MainPID" in args:
                return str(pid[0])
            return ""

        ctl = setup_controller(self, cmd, "owned.service")
        baseline = {"loaded": {"ControlGroup": ""}, "physical": {}}
        with (
            mock.patch.object(
                c, "unit_state", side_effect=lambda *args: copy.deepcopy(baseline)
            ),
            mock.patch.object(c, "cgroup_empty", return_value=True),
            mock.patch.object(c, "proc_start", return_value=123),
        ):
            exercise(self, ctl, self.save_owner)


class RealSnapshotComposition(remote.DisposableSystemd):
    def test_real_snapshot_reload_and_standalone_rollback(self):
        self.link_unit("[Service]\nType=simple\nExecStart=/usr/bin/sleep infinity\n")
        self.cmd(["systemctl", "--user", "start", self.name])
        self.owner({})
        ctl = setup_controller(self, self.cmd, self.name)
        drop_dir = self.runtime / (self.name + ".d")
        drop_dir.mkdir()
        self.links.append(drop_dir)
        ctl.drop = drop_dir / "zz-owned.conf"
        ctl.original_cgroup = c.unit_state(self.cmd, self.name)["loaded"][
            "ControlGroup"
        ]

        def save_owner():
            c.write_json(ctl.backup / "prepared.json", self.prepared)
            c.write_json(
                self.root / "attempt-owner.json",
                {
                    **self.prepared,
                    "prepared_sha256": c.digest(
                        (ctl.backup / "prepared.json").read_bytes()
                    ),
                },
            )

        exercise(self, ctl, save_owner)

    def recovery_script(self, pid, child):
        script = self.root / "recover.py"
        marker = self.root / "recovered"
        script.write_text(
            "import pathlib\n"
            + f"assert not pathlib.Path('/proc/{pid}').exists()\nassert not pathlib.Path('/proc/{child}').exists()\n"
            + "pathlib.Path("
            + repr(str(marker))
            + ").touch()\n"
        )
        return script, marker

    def test_already_exited_worker(self):
        pid, child, _, group = self.start_worker()
        self.cmd(
            [
                "systemctl",
                "--user",
                "kill",
                "--signal=SIGKILL",
                "--kill-who=main",
                self.name,
            ]
        )
        while not c.cgroup_empty(group):
            self.deadline.pause(0.05)
        script, marker = self.recovery_script(pid, child)
        self.assertTrue(
            w.supervise(
                self.root, self.work, self.name, self.cmd, self.deadline, script
            )
        )
        self.assertTrue(marker.exists())

    def test_retry_after_interrupted_fencing(self):
        pid, child, _, _ = self.start_worker()
        original_cmd = self.cmd

        def interrupted(args, timeout=20):
            result = original_cmd(args, timeout)
            if "stop" in args:
                raise InterruptedError("watchdog interrupted after stop")
            return result

        with self.assertRaises(InterruptedError):
            w.fence_worker(self.root, self.work, self.name, interrupted, self.deadline)
        script, marker = self.recovery_script(pid, child)
        self.assertTrue(
            w.supervise(
                self.root, self.work, self.name, self.cmd, self.deadline, script
            )
        )
        self.assertTrue(marker.exists())

    def test_replacement_invocation_refused(self):
        self.start_worker()
        self.cmd(["systemctl", "--user", "restart", self.name])
        with self.assertRaisesRegex(AssertionError, "foreign activation"):
            w.fence_worker(self.root, self.work, self.name, self.cmd, self.deadline)
        self.assertNotEqual(
            self.prepared["worker_identity"]["invocation"],
            w.worker_identity(self.cmd, self.name)["invocation"],
        )

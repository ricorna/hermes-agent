"""Fence the exact owned activation unit before lock-protected recovery."""

import importlib.util
import os
import pathlib
import re
import socket
import sys

# Load the exact sibling, never a similarly named module from PYTHONPATH.
# Canonical source names map onto the existing deployed hyphenated pair.
controller_path = pathlib.Path(__file__).with_name(
    "jimmy-activate.py"
    if pathlib.Path(__file__).name == "jimmy-watchdog.py"
    else "jimmy_v0231_activate.py"
)
controller = sys.modules.get("jimmy_v0231_activate")
if (
    controller is None
    or pathlib.Path(controller.__file__).resolve() != controller_path.resolve()
):
    spec = importlib.util.spec_from_file_location(
        "jimmy_activation_controller", controller_path
    )
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)

SUPERVISOR_SECONDS = (
    1380  # RuntimeMaxSec=1500 leaves 120 seconds for stop/receipt delivery.
)
CHILD_SECONDS = 1080  # Controller gets 1050, leaving termination/receipt headroom.


def worker_identity(cmd, unit):
    state = controller.unit_state(cmd, unit)
    invocation = cmd(
        ["systemctl", "--user", "show", unit, "-p", "InvocationID", "--value"]
    ).strip()
    assert re.fullmatch("[0-9a-f]{32}", invocation), (
        "missing worker invocation identity"
    )
    return {"unit": state, "invocation": invocation}


def fence_worker(root, work, unit, cmd, deadline):
    owner, prepared = controller.validate_owner(root, work)
    assert unit == owner["activation_unit"]
    if controller.committed_owned(root, work):
        return False
    assert controller.decision(work) != "commit-" + owner["nonce"], (
        "commit receipt requires reconciliation"
    )
    actual = worker_identity(cmd, unit)
    saved = prepared["worker_identity"]
    assert actual.keys() == saved.keys(), "foreign activation unit generation"
    group = saved["unit"]["loaded"]["ControlGroup"]
    assert group, "uncaptured activation cgroup"
    pid = int(cmd(["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"]))
    if not pid and not actual["unit"]["loaded"]["ControlGroup"]:
        # systemd clears this runtime field on exit; static identities and the
        # InvocationID must still match. Never normalize a populated foreign group.
        assert controller.cgroup_empty(group), "activation descendants survived exit"
        actual["unit"]["loaded"]["ControlGroup"] = group
    assert actual == saved, "foreign activation unit generation"
    if pid:
        assert pid == prepared["worker_pid"]
        assert controller.proc_start(pid) == prepared["worker_start"]
    # Recheck immediately before stop: a completed activation is irreversible.
    if controller.committed_owned(root, work):
        return False
    controller.claim_decision(work, owner["nonce"], "recover")
    deadline.remaining()
    cmd(["systemctl", "--user", "stop", unit], timeout=180)
    assert (
        int(cmd(["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"])) == 0
    )
    assert controller.cgroup_empty(group), "activation descendants survived stop"
    controller.validate_owner(root, work)
    return not controller.committed_owned(root, work)


def supervise(root, work, unit, cmd, deadline, recovery_script):
    if not fence_worker(root, work, unit, cmd, deadline):
        return False
    cmd(
        ["/usr/bin/python3", recovery_script, work.name, "--rollback"],
        timeout=min(CHILD_SECONDS, deadline.remaining() - 30),
    )
    controller.write_json(
        work / "watchdog-recovery.json", {"attempt": work.name, "completed": True}
    )
    return True


def main():
    assert len(sys.argv) == 3
    attempt, unit = sys.argv[1:]
    assert re.fullmatch(r"(?:rollback|activate)[a-z0-9-]{1,24}", attempt)
    assert unit == "hermes-jimmy-activate-t-1e63040a-" + attempt + ".service"
    assert (
        pathlib.Path.home() == pathlib.Path("/home/jimmy")
        and socket.gethostname() == "jimmy"
    )
    assert not os.getenv("_HERMES_GATEWAY")
    os.umask(0o077)
    root = controller.ROOT
    work = root / "activation-attempts" / attempt
    deadline = controller.Deadline(SUPERVISOR_SECONDS)

    def cmd(args, timeout=180):
        return controller.command(args, deadline, timeout)

    supervise(
        root,
        work,
        unit,
        cmd,
        deadline,
        controller_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

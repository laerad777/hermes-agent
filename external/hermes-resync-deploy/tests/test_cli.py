from __future__ import annotations

import json
import os
from pathlib import Path
import time
import tomllib
import hashlib
from dataclasses import asdict

import pytest

from hermes_resync_deploy.backup import create_active_release_snapshot
from hermes_resync_deploy.cli import _SyntheticSlotAdapter, _load_slot, _parser, main
from hermes_resync_deploy.controller import DeploymentBlocked, GateBReceiptVerifier, GateBRequired
from hermes_resync_deploy.launchd import LongRunningLaunchdAdapter
from hermes_resync_deploy.manifest import build_composition, candidate_digest
from hermes_resync_deploy.model import CandidateComposition, SlotIdentity, TransactionRecord, TransactionState


def composition(marker: str = "a") -> CandidateComposition:
    return CandidateComposition(*([marker] * 13))


def test_distribution_and_console_entry_use_the_resync_identity() -> None:
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source)

    assert project["project"]["name"] == "hermes-resync-deploy"
    assert project["project"]["scripts"] == {
        "hermes-resync-deploy": "hermes_resync_deploy.cli:main"
    }
    assert project["tool"]["setuptools"]["packages"]["find"]["include"] == ["hermes_resync_deploy*"]


def test_parser_uses_deploy_identity_and_requires_an_explicit_adapter() -> None:
    parser = _parser()

    assert parser.prog == "hermes-resync-deploy"
    with pytest.raises(SystemExit):
        parser.parse_args(["recover"])


def test_artifact_only_preflight_does_not_require_a_controller(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    manifest = tmp_path / "composition.json"
    digest = build_composition(composition(), manifest)

    assert main(["preflight", "--manifest", str(manifest), "--digest", digest]) == 0

    assert json.loads(capsys.readouterr().out) == {"candidate_source_sha": "a", "verified": True}


def test_snapshot_binds_the_verified_composition_digest(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    source = tmp_path / "source"; source.mkdir(); (source / "app.py").write_text("x = 1\n", encoding="utf-8")
    manifest = tmp_path / "composition.json"
    digest = build_composition(composition(), manifest)

    assert main(["snapshot", "--kind", "active-release", "--source", str(source), "--cas", str(tmp_path / "cas"), "--allow", "app.py", "--composition-manifest", str(manifest), "--composition-digest", digest]) == 0

    assert json.loads(capsys.readouterr().out)["composition_digest"] == digest


def _default_factory_args(tmp_path: Path, receipt: str = "", *, adapter: str = "synthetic", selector_value: str = "unknown", phase: str = "selector_committed") -> list[str]:
    disposable, approved = tmp_path / "disposable", tmp_path / "approved"
    disposable.mkdir(); approved.mkdir()
    current, prior = composition("a"), composition("b")
    current_path, prior_path = approved / "current.json", approved / "prior.json"
    current_digest, prior_digest = build_composition(current, current_path), build_composition(prior, prior_path)
    source = tmp_path / "source"; source.mkdir(); (source / "release.py").write_text("release\n", encoding="utf-8")
    snapshot = create_active_release_snapshot(source, approved / "cas", ["release.py"], composition_digest=prior_digest)
    blue, green = disposable / "blue", disposable / "green"; blue.mkdir(); green.mkdir()
    (disposable / "selector").write_text(selector_value + "\n", encoding="utf-8")
    fake = disposable / "fake-service"
    fake.write_text("""#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path
if len(sys.argv) == 1:
    root = Path(os.environ["HERMES_RESYNC_SLOT_ROOT"])
    root.joinpath("health.json").write_text(json.dumps({
        "ready": True, "slot": os.environ["HERMES_RESYNC_SLOT"],
        "revision": os.environ["HERMES_RESYNC_REVISION"],
        "candidate_digest": os.environ["HERMES_RESYNC_CANDIDATE_DIGEST"],
        "plugins": ["hermes-resync"], "sidecar_ready": True,
        "writer_count": 1, "kanban": False, "kanban_processes": [],
        "forbidden_writes": [], "pid": os.getpid(),
        "socket": str(root / "service.sock"), "port": int(os.environ["HERMES_RESYNC_PORT"]),
        "port_owner": os.environ["HERMES_RESYNC_SLOT"],
        "health_nonce": os.environ["HERMES_RESYNC_HEALTH_NONCE"],
        "timestamp_ns": time.time_ns(),
    }))
""", encoding="utf-8")
    fake.chmod(0o700)
    runtime: dict[str, dict[str, str]] = {}
    for name, slot_root in (("blue", blue), ("green", green)):
        executable, interpreter = slot_root / "service", slot_root / "python"
        executable.write_text("service " + name, encoding="utf-8")
        interpreter.write_text("python " + name, encoding="utf-8")
        runtime[name] = {"executable": str(executable), "interpreter": str(interpreter),
                         "executable_digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
                         "interpreter_digest": hashlib.sha256(interpreter.read_bytes()).hexdigest()}
    key = b"disposable-test-key"; (approved / "gate-b.key").write_bytes(key)
    record = {
        "transaction_id": "tx", "state": "healthy", "candidate_digest": current_digest,
        "blue": {"name": "blue", "root": str(blue), "revision": "prior", "candidate_digest": prior_digest, "pid": None, **runtime["blue"]},
        "green": {"name": "green", "root": str(green), "revision": "candidate", "candidate_digest": current_digest, "pid": None, **runtime["green"]},
        "prior_snapshot": {"manifest_digest": snapshot.manifest_digest, "tree_digest": snapshot.tree_digest, "cas_path": str(snapshot.cas_path), "kind": snapshot.kind, "entry_count": snapshot.entry_count, "composition_digest": prior_digest},
        "selector_expected": "blue", "selector_observed": selector_value, "detail": {"phase": phase},
    }
    record_path = tmp_path / "record.json"; record_path.write_text(json.dumps(record), encoding="utf-8")
    return ["recover", "--adapter", adapter, "--record", str(record_path), "--disposable-root", str(disposable), "--approved-root", str(approved), "--selector-path", str(disposable / "selector"), "--journal", str(disposable / "journal" / "transaction.json"), "--lock", str(disposable / "journal" / "transaction.lock"), "--slot-program", str(fake), "--service-executable", str(fake), "--blue-port", "34121", "--green-port", "34122", "--current-composition", str(current_path), "--current-digest", current_digest, "--prior-composition", str(prior_path), "--prior-digest", prior_digest, "--gate-b-key", str(approved / "gate-b.key"), "--gate-b-receipt", receipt]


def test_installed_default_factory_rejects_arbitrary_receipt_before_service_or_selector_mutation(tmp_path: Path):
    args = _default_factory_args(tmp_path, "arbitrary", adapter="production", selector_value="blue", phase="green_healthy")
    with pytest.raises(GateBRequired):
        main(args)
    assert (tmp_path / "disposable" / "selector").read_text(encoding="utf-8") == "blue\n"


def test_installed_default_factory_recovers_ambiguous_record_with_bound_receipt(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    current_digest = candidate_digest(composition("a"))
    receipt = GateBReceiptVerifier(b"disposable-test-key").issue_for_test(current_digest)
    assert main(_default_factory_args(tmp_path, receipt)) == 0
    assert json.loads(capsys.readouterr().out) == {"phase": "manual_blocked", "state": "blocked"}


def test_installed_default_factory_recovers_ambiguous_record_without_gate_b(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(_default_factory_args(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out) == {"phase": "manual_blocked", "state": "blocked"}


def test_load_slot_converts_paths_and_rejects_strict_schema_variants() -> None:
    value = {
        "name": "blue", "root": "/disposable/blue", "revision": "prior",
        "candidate_digest": "a" * 64, "pid": None, "executable": None,
        "interpreter": None, "executable_digest": "", "interpreter_digest": "",
    }

    slot = _load_slot(value)

    assert slot is not None and slot.root == Path("/disposable/blue")
    assert slot.executable is None and slot.interpreter is None
    malformed = dict(value); malformed["pid"] = 0
    extra = {**value, "unexpected": "field"}
    partial = dict(value); partial["executable"] = "/disposable/blue/runtime/service"
    for invalid in (malformed, extra, partial):
        with pytest.raises(ValueError):
            _load_slot(invalid)


def _serialized_cli_transaction(tmp_path: Path, command: str, phase: str, *, fail_green: bool = False) -> tuple[list[str], Path, Path]:
    disposable, approved = tmp_path / "disposable", tmp_path / "approved"
    disposable.mkdir(); approved.mkdir()
    current, prior = composition("a"), composition("b")
    current_path, prior_path = approved / "current.json", approved / "prior.json"
    current_digest, prior_digest = build_composition(current, current_path), build_composition(prior, prior_path)
    source = tmp_path / "source"; source.mkdir()
    (source / "release.py").write_text("release\n", encoding="utf-8")
    source_runtime = source / "runtime"; source_runtime.mkdir()
    (source_runtime / "service").write_text("prior service\n", encoding="utf-8")
    (source_runtime / "python").write_text("prior python\n", encoding="utf-8")
    snapshot = create_active_release_snapshot(source, approved / "cas", ["release.py", "runtime/service", "runtime/python"], composition_digest=prior_digest)
    blue, green = disposable / "blue", disposable / "green"; blue.mkdir(); green.mkdir()
    runtime: dict[str, dict[str, str]] = {}
    for name, root in (("blue", blue), ("green", green)):
        runtime_dir = root / "runtime"; runtime_dir.mkdir()
        executable, interpreter = runtime_dir / "service", runtime_dir / "python"
        executable.write_text(f"{name} service\n", encoding="utf-8")
        interpreter.write_text(f"{name} python\n", encoding="utf-8")
        runtime[name] = {
            "executable": str(executable), "interpreter": str(interpreter),
            "executable_digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "interpreter_digest": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        }
    selector, events = disposable / "selector", disposable / "actions.log"
    selector.write_text("blue\n", encoding="utf-8")
    harness = disposable / "harness.py"
    harness.write_text(f'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
root = Path(os.environ["HERMES_RESYNC_SLOT_ROOT"])
action = "start" if len(sys.argv) == 1 else sys.argv[1]
Path(os.environ["HERMES_RESYNC_ACTION_LOG"]).open("a", encoding="utf-8").write(f"{{action}}:{{os.environ['HERMES_RESYNC_SLOT']}}\\n")
if action == "start" and os.environ["HERMES_RESYNC_SLOT"] == "green" and {fail_green!r}:
    raise SystemExit(23)
selector = Path(os.environ["HERMES_RESYNC_SELECTOR"])
if action == "retire" and selector.read_text(encoding="utf-8").strip() != "green":
    raise SystemExit(24)
if action == "start" and os.environ["HERMES_RESYNC_SLOT"].startswith("blue-rollback-") and selector.read_text(encoding="utf-8").strip() != "blue":
    raise SystemExit(25)
if action == "start":
    root.joinpath("health.json").write_text(json.dumps({{
        "ready": True, "slot": os.environ["HERMES_RESYNC_SLOT"], "revision": os.environ["HERMES_RESYNC_REVISION"],
        "candidate_digest": os.environ["HERMES_RESYNC_CANDIDATE_DIGEST"], "plugins": ["hermes-resync"],
        "sidecar_ready": True, "writer_count": 1, "kanban": False, "kanban_processes": [], "forbidden_writes": [],
        "pid": os.getpid(), "socket": str(root / "service.sock"), "port": int(os.environ["HERMES_RESYNC_PORT"]),
        "port_owner": os.environ["HERMES_RESYNC_SLOT"], "health_nonce": os.environ["HERMES_RESYNC_HEALTH_NONCE"], "timestamp_ns": time.time_ns(),
    }}), encoding="utf-8")
''', encoding="utf-8")
    harness.chmod(0o700)
    record = TransactionRecord(
        transaction_id="serialized-transaction", state=TransactionState.PREPARED, candidate_digest=current_digest,
        blue=SlotIdentity("blue", blue, "prior", prior_digest, **runtime["blue"]),
        green=SlotIdentity("green", green, "candidate", current_digest, **runtime["green"]),
        prior_snapshot=snapshot, selector_expected="blue", selector_observed="blue", detail={"phase": phase},
    )
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(asdict(record), default=str), encoding="utf-8")
    key = approved / "gate-b.key"; key.write_bytes(b"disposable-test-key")
    receipt = GateBReceiptVerifier(key.read_bytes()).issue_for_test(current_digest)
    args = [command, "--adapter", "synthetic", "--record", str(record_path), "--disposable-root", str(disposable),
            "--approved-root", str(approved), "--selector-path", str(selector), "--journal", str(disposable / "journal" / "transaction.json"),
            "--lock", str(disposable / "journal" / "transaction.lock"), "--slot-program", str(harness), "--service-executable", str(harness),
            "--blue-port", "34121", "--green-port", "34122", "--current-composition", str(current_path), "--current-digest", current_digest,
            "--prior-composition", str(prior_path), "--prior-digest", prior_digest, "--gate-b-key", str(key), "--gate-b-receipt", receipt]
    return args, selector, events


def test_cli_serialized_transaction_activates_with_candidate_bound_gate_b(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    args, selector, events = _serialized_cli_transaction(tmp_path, "activate", "prepared")
    monkeypatch.setenv("HERMES_RESYNC_ACTION_LOG", str(events))

    assert main(args) == 0

    assert json.loads(capsys.readouterr().out) == {"phase": "blue_retired", "state": "published"}
    assert selector.read_text(encoding="utf-8") == "green\n"
    assert events.read_text(encoding="utf-8").splitlines() == ["fence:blue", "start:green", "retire:blue"]


def test_cli_serialized_transaction_rolls_back_to_fresh_blue_runtime(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    args, selector, events = _serialized_cli_transaction(tmp_path, "rollback", "green_healthy")
    monkeypatch.setenv("HERMES_RESYNC_ACTION_LOG", str(events))

    assert main(args) == 0

    result = json.loads(capsys.readouterr().out)
    rollback_blue = selector.read_text(encoding="utf-8").strip()
    journal = json.loads((tmp_path / "disposable" / "journal" / "transaction.json").read_text(encoding="utf-8"))
    assert result == {"phase": "rollback_verified", "state": "rolled_back"}
    assert rollback_blue.startswith("blue-rollback-serialized-transaction-")
    assert events.read_text(encoding="utf-8").splitlines() == ["fence:green", f"start:{rollback_blue}"]
    assert Path(journal["blue"]["executable"]).is_relative_to(tmp_path / "disposable" / rollback_blue)


def test_cli_activation_failure_automatically_rolls_back_with_fresh_blue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args, selector, events = _serialized_cli_transaction(tmp_path, "activate", "prepared", fail_green=True)
    monkeypatch.setenv("HERMES_RESYNC_ACTION_LOG", str(events))

    with pytest.raises(DeploymentBlocked, match="activation failed and rollback completed"):
        main(args)

    rollback_blue = selector.read_text(encoding="utf-8").strip()
    assert rollback_blue.startswith("blue-rollback-serialized-transaction-")
    assert events.read_text(encoding="utf-8").splitlines() == ["fence:blue", "start:green", "fence:green", f"start:{rollback_blue}"]


def test_cli_recover_rolls_back_non_ambiguous_serialized_phase(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    args, selector, events = _serialized_cli_transaction(tmp_path, "recover", "green_healthy")
    monkeypatch.setenv("HERMES_RESYNC_ACTION_LOG", str(events))

    assert main(args) == 0

    rollback_blue = selector.read_text(encoding="utf-8").strip()
    assert json.loads(capsys.readouterr().out) == {"phase": "rollback_verified", "state": "rolled_back"}
    assert events.read_text(encoding="utf-8").splitlines() == ["fence:green", f"start:{rollback_blue}"]


def test_disposable_adapter_rejects_stale_health_evidence(tmp_path: Path) -> None:
    root = tmp_path / "disposable"; root.mkdir()
    slot_root = root / "green"; slot_root.mkdir()
    stale = slot_root / "health.json"; stale.write_text("{}", encoding="utf-8")
    executable = root / "no-op"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8"); executable.chmod(0o700)
    slot = SlotIdentity("green", slot_root, "candidate", "a" * 64)
    adapter = _SyntheticSlotAdapter(root, executable, executable, root / "selector", {"green": 34122})

    with pytest.raises(ValueError, match="fresh health evidence"):
        adapter.start(slot)


def test_synthetic_adapter_requires_nonce_bound_health_evidence(tmp_path: Path) -> None:
    root = tmp_path / "disposable"; root.mkdir()
    slot_root = root / "green"; slot_root.mkdir()
    executable = root / "no-nonce"
    executable.write_text("""#!/usr/bin/env python3
import json
import os
from pathlib import Path
root = Path(os.environ["HERMES_RESYNC_SLOT_ROOT"])
root.joinpath("health.json").write_text(json.dumps({
    "pid": os.getpid(), "socket": str(root / "service.sock"),
    "port": int(os.environ["HERMES_RESYNC_PORT"]), "port_owner": os.environ["HERMES_RESYNC_SLOT"],
}))
""", encoding="utf-8")
    executable.chmod(0o700)
    adapter = _SyntheticSlotAdapter(root, executable, executable, root / "selector", {"green": 34122})
    slot = SlotIdentity("green", slot_root, "candidate", "a" * 64)

    with pytest.raises(ValueError, match="not owned"):
        adapter.start(slot)


def test_synthetic_adapter_rejects_missing_pid_health_evidence(tmp_path: Path) -> None:
    root = tmp_path / "disposable"; root.mkdir()
    slot_root = root / "green"; slot_root.mkdir()
    executable = root / "missing-pid"
    executable.write_text("""#!/usr/bin/env python3
import json, os, time
from pathlib import Path
root = Path(os.environ["HERMES_RESYNC_SLOT_ROOT"])
root.joinpath("health.json").write_text(json.dumps({
    "slot": os.environ["HERMES_RESYNC_SLOT"], "revision": os.environ["HERMES_RESYNC_REVISION"],
    "candidate_digest": os.environ["HERMES_RESYNC_CANDIDATE_DIGEST"],
    "socket": str(root / "service.sock"), "port": int(os.environ["HERMES_RESYNC_PORT"]),
    "port_owner": os.environ["HERMES_RESYNC_SLOT"], "health_nonce": os.environ["HERMES_RESYNC_HEALTH_NONCE"],
    "timestamp_ns": time.time_ns(),
}))
""", encoding="utf-8")
    executable.chmod(0o700)
    adapter = _SyntheticSlotAdapter(root, executable, executable, root / "selector", {"green": 34122})

    with pytest.raises(ValueError, match="not owned"):
        adapter.start(SlotIdentity("green", slot_root, "candidate", "a" * 64))


def test_synthetic_adapter_rejects_cross_launch_nonce(tmp_path: Path) -> None:
    root = tmp_path / "disposable"; root.mkdir()
    slot_root = root / "green"; slot_root.mkdir()
    executable = root / "cross-nonce"
    executable.write_text("""#!/usr/bin/env python3
import json, os, time
from pathlib import Path
root = Path(os.environ["HERMES_RESYNC_SLOT_ROOT"])
root.joinpath("health.json").write_text(json.dumps({
    "pid": os.getpid(), "slot": os.environ["HERMES_RESYNC_SLOT"], "revision": os.environ["HERMES_RESYNC_REVISION"],
    "candidate_digest": os.environ["HERMES_RESYNC_CANDIDATE_DIGEST"], "socket": str(root / "service.sock"),
    "port": int(os.environ["HERMES_RESYNC_PORT"]), "port_owner": os.environ["HERMES_RESYNC_SLOT"],
    "health_nonce": "other-launch", "timestamp_ns": time.time_ns(),
}))
""", encoding="utf-8")
    executable.chmod(0o700)
    adapter = _SyntheticSlotAdapter(root, executable, executable, root / "selector", {"green": 34122})

    with pytest.raises(ValueError, match="not owned"):
        adapter.start(SlotIdentity("green", slot_root, "candidate", "a" * 64))


def test_production_launchd_adapter_uses_an_injected_disposable_service_seam(tmp_path: Path) -> None:
    root = tmp_path / "green"; root.mkdir()
    program = root / "service"; interpreter = root / "python"
    program.write_text("#!/bin/sh\n", encoding="utf-8"); interpreter.write_text("python", encoding="utf-8")
    commands: list[tuple[str, ...]] = []
    slot = SlotIdentity("green", root, "candidate", "a" * 64, executable=program, interpreter=interpreter,
                        executable_digest=hashlib.sha256(program.read_bytes()).hexdigest(),
                        interpreter_digest=hashlib.sha256(interpreter.read_bytes()).hexdigest())
    def execute(command: tuple[str, ...]) -> object:
        commands.append(command)
        if command[1] == "bootstrap":
            profile = __import__("plistlib").loads(root.joinpath("launchd.plist").read_bytes())
            env = profile["EnvironmentVariables"]
            root.joinpath("health.json").write_text(json.dumps({
                "pid": 742, "slot": env["HERMES_RESYNC_SLOT"], "revision": env["HERMES_RESYNC_REVISION"],
                "candidate_digest": env["HERMES_RESYNC_CANDIDATE_DIGEST"], "socket": str(root / "service.sock"),
                "port": int(env["HERMES_RESYNC_PORT"]), "port_owner": env["HERMES_RESYNC_SLOT"],
                "health_nonce": env["HERMES_RESYNC_HEALTH_NONCE"], "timestamp_ns": time.time_ns(),
            }), encoding="utf-8")
            return None
        if command[1] == "print":
            return {"pid": 742, "alive": True}
        return None
    adapter = LongRunningLaunchdAdapter(
        selector=tmp_path / "selector", ports={"green": 34122}, executor=execute, resolver=lambda _: slot,
    )

    assert adapter.start(slot).pid == 742
    adapter.fence(slot)
    adapter.retire(slot)

    assert commands == [
        ("/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(root / "launchd.plist")),
        ("/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/org.hermes.resync.slot.green"),
        ("/bin/launchctl", "print", f"gui/{os.getuid()}/org.hermes.resync.slot.green"),
        ("/bin/launchctl", "bootout", f"gui/{os.getuid()}/org.hermes.resync.slot.green"),
        ("/bin/launchctl", "bootout", f"gui/{os.getuid()}/org.hermes.resync.slot.green"),
    ]

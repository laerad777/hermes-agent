from __future__ import annotations

import plistlib
import stat
import tempfile
import hashlib
import json
import os
import time
import unittest
from pathlib import Path

from hermes_resync_deploy.launchd import LongRunningLaunchdAdapter, SlotProfile, parse_launchd_plist, render_launchd_plist
from hermes_resync_deploy.model import SlotIdentity


class LaunchdProfileTests(unittest.TestCase):
    def test_rendered_profile_binds_one_disposable_slot_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            slot = SlotIdentity("green", root / "slot", "candidate-revision", "candidate-digest", pid=412)
            profile = SlotProfile(
                slot=slot,
                label="dev.hermes.resync.green",
                program_arguments=("/isolated/python", "-m", "hermes"),
                home=root / "home",
                hermes_home=root / "hermes-home",
                selector_path=root / "selector",
                selector_owner="resync-controller",
                port=34120,
                health_nonce="nonce-bound-to-launch",
            )
            rendered = render_launchd_plist(profile)
            decoded = plistlib.loads(rendered)
            parsed = parse_launchd_plist(rendered)

            self.assertEqual(decoded["Label"], "dev.hermes.resync.green")
            self.assertEqual(decoded["ProgramArguments"], ["/isolated/python", "-m", "hermes"])
            self.assertEqual(decoded["EnvironmentVariables"]["HOME"], str(root / "home"))
            self.assertEqual(decoded["EnvironmentVariables"]["HERMES_HOME"], str(root / "hermes-home"))
            self.assertEqual(decoded["EnvironmentVariables"]["HERMES_RESYNC_SELECTOR"], str(root / "selector"))
            self.assertEqual(decoded["EnvironmentVariables"]["HERMES_RESYNC_SELECTOR_OWNER"], "resync-controller")
            self.assertEqual(decoded["EnvironmentVariables"]["HERMES_RESYNC_SLOT"], "green")
            self.assertEqual(decoded["EnvironmentVariables"]["HERMES_RESYNC_HEALTH_NONCE"], "nonce-bound-to-launch")
            self.assertEqual(parsed, profile)

    def test_two_slot_profiles_have_distinct_0700_homes_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profiles = []
            for name, port in (("blue", 34121), ("green", 34122)):
                home, hermes_home = root / name / "home", root / name / "hermes"
                profile = SlotProfile(
                    slot=SlotIdentity(name, root / name / "slot", f"{name}-rev", f"{name}-digest"),
                    label=f"dev.hermes.resync.{name}", program_arguments=("/isolated/python", "-m", "hermes"),
                    home=home, hermes_home=hermes_home, selector_path=root / "selector",
                    selector_owner="resync-controller", port=port,
                )
                render_launchd_plist(profile)
                profiles.append(profile)
                self.assertTrue(home.is_dir())
                self.assertTrue(hermes_home.is_dir())
                self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(hermes_home.stat().st_mode), 0o700)

            blue, green = profiles
            self.assertNotEqual(blue.home, green.home)
            self.assertNotEqual(blue.hermes_home, green.hermes_home)
            self.assertNotEqual(blue.port, green.port)
            self.assertEqual(blue.selector_owner, green.selector_owner)

    def test_resolver_launches_distinct_verified_blue_and_green_runtimes_after_delayed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtimes: dict[str, SlotIdentity] = {}
            for name in ("blue", "green"):
                slot_root = root / name; slot_root.mkdir()
                executable = slot_root / "service.py"; interpreter = slot_root / "python"
                executable.write_text(name, encoding="utf-8"); interpreter.write_text(name + " interpreter", encoding="utf-8")
                runtimes[name] = SlotIdentity(name, root / name, f"{name}-rev", f"{name}-digest", executable=executable, interpreter=interpreter,
                    executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(), interpreter_digest=hashlib.sha256(interpreter.read_bytes()).hexdigest())
            pending: list[Path] = []
            def execute(command: tuple[str, ...]) -> object:
                return {"pid": 123, "alive": True} if command[1] == "print" else None
            def sleep(_: float) -> None:
                slot = pending.pop()
                profile = plistlib.loads((slot.root / "launchd.plist").read_bytes())
                env = profile["EnvironmentVariables"]
                (slot.root / "health.json").write_text(json.dumps({"pid": 123, "slot": slot.name, "revision": slot.revision,
                    "candidate_digest": slot.candidate_digest, "socket": str(slot.root / "service.sock"), "port": 34121 if slot.name == "blue" else 34122,
                    "port_owner": slot.name, "health_nonce": env["HERMES_RESYNC_HEALTH_NONCE"], "timestamp_ns": time.time_ns()}), encoding="utf-8")
            adapter = LongRunningLaunchdAdapter(selector=root / "selector", ports={"blue": 34121, "green": 34122}, executor=execute, resolver=lambda slot: runtimes[slot.name], sleeper=sleep)
            for slot in runtimes.values():
                pending.append(slot)
                launched = adapter.start(SlotIdentity(slot.name, slot.root, slot.revision, slot.candidate_digest))
                self.assertEqual(launched.executable, slot.executable.resolve())
                self.assertEqual(launched.interpreter, slot.interpreter.resolve())
                arguments = plistlib.loads((slot.root / "launchd.plist").read_bytes())["ProgramArguments"]
                self.assertEqual(
                    arguments,
                    [str(slot.interpreter.resolve()), str(slot.executable.resolve())],
                )

    def test_start_timeout_fences_slot_and_rejects_bad_runtime_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); root.mkdir(exist_ok=True)
            slot_root = root / "green"; slot_root.mkdir()
            executable = slot_root / "service.py"; interpreter = slot_root / "python"
            executable.write_text("service", encoding="utf-8"); interpreter.write_text("python", encoding="utf-8")
            slot = SlotIdentity("green", root / "green", "rev", "digest", executable=executable, interpreter=interpreter,
                executable_digest="wrong", interpreter_digest=hashlib.sha256(interpreter.read_bytes()).hexdigest())
            commands: list[tuple[str, ...]] = []
            adapter = LongRunningLaunchdAdapter(selector=root / "selector", ports={"green": 34122}, executor=lambda command: commands.append(command), resolver=lambda _: slot, poll_attempts=1)
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                adapter.start(SlotIdentity("green", slot.root, "rev", "digest"))
            self.assertEqual(commands, [])
            valid = SlotIdentity("green", slot.root, "rev", "digest", executable=executable, interpreter=interpreter,
                executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(), interpreter_digest=hashlib.sha256(interpreter.read_bytes()).hexdigest())
            adapter = LongRunningLaunchdAdapter(selector=root / "selector", ports={"green": 34122}, executor=lambda command: commands.append(command), resolver=lambda _: valid, poll_attempts=1)
            with self.assertRaisesRegex(ValueError, "timed out"):
                adapter.start(SlotIdentity("green", slot.root, "rev", "digest"))
            self.assertEqual(commands[-3][1], "bootstrap")
            self.assertEqual(commands[-2][1], "kickstart")
            self.assertEqual(commands[-1][1], "bootout")

    def test_resolver_rejects_missing_digest_and_dead_or_mismatched_live_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); slot_root = root / "green"; slot_root.mkdir()
            executable, interpreter = slot_root / "service", slot_root / "python"
            executable.write_text("service", encoding="utf-8"); interpreter.write_text("python", encoding="utf-8")
            missing = SlotIdentity("green", slot_root, "rev", "digest", executable=executable, interpreter=interpreter)
            adapter = LongRunningLaunchdAdapter(selector=root / "selector", ports={"green": 34122}, executor=lambda _: None, resolver=lambda _: missing)
            with self.assertRaisesRegex(ValueError, "require digests"):
                adapter.start(SlotIdentity("green", slot_root, "rev", "digest"))

            valid = SlotIdentity("green", slot_root, "rev", "digest", executable=executable, interpreter=interpreter,
                executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(), interpreter_digest=hashlib.sha256(interpreter.read_bytes()).hexdigest())
            def dead(command: tuple[str, ...]) -> object:
                if command[1] == "bootstrap":
                    env = plistlib.loads((slot_root / "launchd.plist").read_bytes())["EnvironmentVariables"]
                    (slot_root / "health.json").write_text(json.dumps({"pid": 123, "slot": "green", "revision": "rev", "candidate_digest": "digest", "socket": str(slot_root / "service.sock"), "port": 34122, "port_owner": "green", "health_nonce": env["HERMES_RESYNC_HEALTH_NONCE"], "timestamp_ns": time.time_ns()}), encoding="utf-8")
                return {"pid": 123, "alive": False} if command[1] == "print" else None
            adapter = LongRunningLaunchdAdapter(selector=root / "selector", ports={"green": 34122}, executor=dead, resolver=lambda _: valid)
            with self.assertRaisesRegex(ValueError, "exited"):
                adapter.start(SlotIdentity("green", slot_root, "rev", "digest"))

            adapter = LongRunningLaunchdAdapter(selector=root / "selector", ports={"green": 34122}, executor=lambda command: {"pid": 999, "alive": True} if command[1] == "print" else dead(command), resolver=lambda _: valid)
            with self.assertRaisesRegex(ValueError, "PID differs"):
                adapter.start(SlotIdentity("green", slot_root, "rev", "digest"))


if __name__ == "__main__":
    unittest.main()

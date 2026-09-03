from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile

from hermes_resync import register
from hermes_resync import lifecycle
from hermes_resync.continuity import ContinuityStore
from hermes_resync.skills import HimalayaPreflight, MailMessage


class SyntheticMailAdapter:
    def __init__(self, *, sent: list[MailMessage] | None = None, outbox: list[MailMessage] | None = None) -> None:
        self._mailboxes = {"Sent": sent, "Outbox": outbox}

    def messages(self, mailbox: str):
        return self._mailboxes[mailbox]


class SkillContext:
    def __init__(self) -> None:
        self.hooks: list[str] = []
        self.skills: dict[str, tuple[Path, str]] = {}
        self.tools: dict[str, dict[str, object]] = {}

    def register_hook(self, name: str, _callback: object) -> None:
        self.hooks.append(name)

    def register_skill(self, name: str, path: Path, description: str) -> None:
        self.skills[name] = (path, description)

    def register_tool(self, **kwargs: object) -> None:
        self.tools[str(kwargs["name"])] = kwargs


def test_register_exposes_packaged_assets_hooks_and_bounded_exact_recall_tool(tmp_path: Path, monkeypatch):
    context = SkillContext()
    register(context)

    assert context.hooks == ["on_session_boundary", "on_agent_turn_origin", "on_delivery_result"]
    recall = context.tools["hermes_resync_exact_recall"]
    assert recall["toolset"] == "hermes_resync"
    assert recall["schema"]["parameters"]["additionalProperties"] is False
    store = ContinuityStore(tmp_path / "continuity.json")
    anchor = {
        "profile": "profile", "platform": "platform", "chat_id": "chat", "chat_type": "channel",
        "thread_id": "thread", "parent_chat_id": "parent", "previous_session_id": "old", "current_session_id": "new",
    }
    store.record_boundary({**anchor, "new_session_id": anchor["current_session_id"], "event_id": "boundary"})
    assert store.apply_origin({**anchor, "new_session_id": anchor["current_session_id"], "lineage_parent_session_id": "old", "event_id": "origin"})
    monkeypatch.setattr(lifecycle, "_STORE", store)
    handler = recall["handler"]
    assert handler(**anchor) == {"exact": True}
    assert handler(**{**anchor, "profile": "other"}) == {"exact": False}
    assert handler(**{key: value for key, value in anchor.items() if key != "current_session_id"}) == {"exact": False}
    assert handler(**{**anchor, "session_search": "old"}) == {"exact": False}
    comfy, _ = context.skills["comfyui-setup"]
    himalaya, _ = context.skills["himalaya-preflight"]
    assert stat.S_IMODE(comfy.stat().st_mode) == 0o755
    assert comfy.name == "comfyui_setup.sh"
    assert himalaya.name == "himalaya_preflight.md"
    assert (comfy.parent / "mode-manifest.json").is_file()


def test_himalaya_preflight_loader_with_synthetic_mail_adapter_covers_sent_outbox_uncertain_and_duplicate():
    preflight = HimalayaPreflight.load()
    draft = MailMessage(("recipient@example.test",), "Status", "body", ("report.pdf",))
    matching = MailMessage(("RECIPIENT@example.test",), " status ", "body", ("report.pdf",))

    assert len(preflight.steps) == 5
    assert preflight.evaluate(draft, SyntheticMailAdapter(sent=[], outbox=[])) == "sent"
    assert preflight.evaluate(draft, SyntheticMailAdapter(sent=[], outbox=[matching])) == "outbox"
    assert preflight.evaluate(draft, SyntheticMailAdapter(sent=None, outbox=[])) == "uncertain"
    assert preflight.evaluate(draft, SyntheticMailAdapter(sent=[matching], outbox=[])) == "duplicate"


def test_comfyui_asset_runs_in_fresh_private_homes_without_credentials():
    context = SkillContext()
    register(context)
    script = context.skills["comfyui-setup"][0]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        hermes_home = root / "hermes-home"
        home.mkdir(mode=0o700)
        hermes_home.mkdir(mode=0o700)
        environment = {"HOME": str(home), "HERMES_HOME": str(hermes_home), "PATH": os.environ["PATH"], "PORT": "9010"}
        assert not {"ANTHROPIC_API_KEY", "HERMES_API_KEY", "OPENAI_API_KEY", "TOKEN"}.intersection(environment)
        result = subprocess.run(
            ["bash", str(script), ""],
            cwd=root,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        )
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        assert stat.S_IMODE(hermes_home.stat().st_mode) == 0o700

    assert f"workspace={root.resolve()}" in result.stdout
    assert "PORT=9010" in result.stdout

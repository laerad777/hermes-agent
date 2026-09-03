from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from hermes_resync_deploy.backup import (
    create_active_release_snapshot,
    create_dirty_source_snapshot,
    restore_snapshot_to_slot,
    verify_snapshot,
)


class BackupRestoreTests(unittest.TestCase):
    def test_code_only_allowlist_preserves_content_and_mode_in_cas_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, cas, slot = root / "source", root / "cas", root / "blue"
            source.mkdir()
            code = source / "src" / "run.py"
            code.parent.mkdir()
            code.write_text("print('isolated')\n", encoding="utf-8")
            os.chmod(code, 0o755)
            (source / "unlisted.py").write_text("not in the snapshot", encoding="utf-8")

            snapshot = create_dirty_source_snapshot(source, cas, ["src/run.py"])
            verify_snapshot(snapshot)
            restored = restore_snapshot_to_slot(snapshot, slot)

            self.assertEqual(snapshot.kind, "dirty-source")
            self.assertEqual(snapshot.entry_count, 1)
            self.assertEqual((restored / "src/run.py").read_text(encoding="utf-8"), "print('isolated')\n")
            self.assertEqual(stat.S_IMODE((restored / "src/run.py").stat().st_mode), 0o755)
            self.assertFalse((restored / "unlisted.py").exists())
            self.assertEqual(stat.S_IMODE(snapshot.cas_path.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((snapshot.cas_path / "manifest.json").stat().st_mode), 0o600)

    def test_forbidden_state_unknown_paths_and_live_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, cas = root / "source", root / "cas"
            source.mkdir()
            for relative in (".env", "auth-token.txt", "config.yaml", "state.db", "sessions/data.json"):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("secret", encoding="utf-8")
                with self.subTest(relative=relative):
                    with self.assertRaises(ValueError):
                        create_dirty_source_snapshot(source, cas, [relative])

            # Live roots are permitted only as explicit read-only snapshot inputs;
            # this synthetic nonexistent member must fail before any output is made.
            with self.assertRaises((ValueError, FileNotFoundError)):
                create_active_release_snapshot(Path("/Users/jerome/.hermes/current"), cas, ["code.py"])

    def test_cas_symlink_to_live_root_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, cas = root / "source", root / "innocent-cas"
            source.mkdir()
            (source / "app.py").write_text("safe", encoding="utf-8")
            cas.symlink_to("/current")

            with self.assertRaises(ValueError):
                create_dirty_source_snapshot(source, cas, ["app.py"])

            self.assertTrue(cas.is_symlink())

    def test_restore_nested_symlink_to_live_root_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, cas = root / "source", root / "cas"
            source.mkdir()
            (source / "app.py").write_text("safe", encoding="utf-8")
            snapshot = create_dirty_source_snapshot(source, cas, ["app.py"])
            nested = root / "slots" / "candidate"
            nested.parent.mkdir()
            nested.symlink_to("/current")
            slot = nested / "blue"

            with self.assertRaises(ValueError):
                restore_snapshot_to_slot(snapshot, slot)

            self.assertFalse(slot.exists())

    def test_tampered_cas_and_nonfresh_restore_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, cas = root / "source", root / "cas"
            source.mkdir()
            (source / "app.py").write_text("original", encoding="utf-8")
            snapshot = create_active_release_snapshot(source, cas, ["app.py"])

            (snapshot.cas_path / "payload" / "app.py").write_text("tampered", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_snapshot(snapshot)
            with self.assertRaises(ValueError):
                restore_snapshot_to_slot(snapshot, root / "new-blue")

            # A valid snapshot must never overwrite a pre-existing slot.
            second_cas = root / "second-cas"
            snapshot = create_active_release_snapshot(source, second_cas, ["app.py"])
            occupied = root / "occupied-blue"
            occupied.mkdir()
            with self.assertRaises(ValueError):
                restore_snapshot_to_slot(snapshot, occupied)


if __name__ == "__main__":
    unittest.main()

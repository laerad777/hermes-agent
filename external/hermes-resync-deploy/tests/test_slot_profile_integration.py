from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from hermes_resync_deploy.launchd import SlotProfile, parse_launchd_plist, render_launchd_plist
from hermes_resync_deploy.model import SlotIdentity


class SlotProfileIntegrationTests(unittest.TestCase):
    def test_disposable_blue_green_profiles_isolate_homes_and_share_only_controller_owned_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selector = root / "controller" / "selector"
            profiles: list[SlotProfile] = []
            for name, port in (("blue", 35101), ("green", 35102)):
                slot_root = root / "slots" / name
                profile = SlotProfile(
                    slot=SlotIdentity(name, slot_root, f"{name}-revision", f"{name}-digest"),
                    label=f"dev.hermes.resync.{name}",
                    program_arguments=("/test/python", "-m", "hermes.gateway"),
                    home=root / "homes" / name,
                    hermes_home=root / "hermes-homes" / name,
                    selector_path=selector,
                    selector_owner="deployment-controller",
                    port=port,
                )
                rendered = render_launchd_plist(profile)
                parsed = parse_launchd_plist(rendered)
                profiles.append(parsed)

            blue, green = profiles
            self.assertEqual(blue.selector_path, selector)
            self.assertEqual(green.selector_path, selector)
            self.assertEqual(blue.selector_owner, "deployment-controller")
            self.assertEqual(green.selector_owner, "deployment-controller")
            self.assertNotEqual(blue.slot.root, green.slot.root)
            self.assertNotEqual(blue.home, green.home)
            self.assertNotEqual(blue.hermes_home, green.hermes_home)
            self.assertNotEqual(blue.port, green.port)
            for profile in profiles:
                self.assertEqual(stat.S_IMODE(profile.home.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(profile.hermes_home.stat().st_mode), 0o700)
                self.assertTrue(profile.home.is_relative_to(root))
                self.assertTrue(profile.hermes_home.is_relative_to(root))


if __name__ == "__main__":
    unittest.main()

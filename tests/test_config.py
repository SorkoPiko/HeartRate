import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakeSource, TempEnv, hr, install_fake_source, make_daemon


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv(self)

    def test_missing_file_is_empty_config(self):
        self.assertEqual(hr.load_config(), {})

    def test_missing_file_defaults_to_ble_with_no_filter(self):
        created = []

        class Recorder(FakeSource):
            def __init__(self, config, on_reading, on_status):
                created.append(config)

        previous = sys.modules.get("sources.ble")
        self.addCleanup(lambda: sys.modules.__setitem__("sources.ble", previous) if previous else sys.modules.pop("sources.ble", None))
        install_fake_source("ble").Source = Recorder
        daemon = make_daemon(self, hr.load_config())
        self.assertEqual(daemon.source_name, "ble")
        self.assertEqual(created, [{}])

    def test_env_overrides_path(self):
        self.assertEqual(hr.config_path(), self.env.config)
        self.env.config.write_text('source = "fake"\n[fake]\nname_prefix = "Polar"\n')
        self.assertEqual(hr.load_config(), {"source": "fake", "fake": {"name_prefix": "Polar"}})

    def test_source_table_passed_to_source(self):
        install_fake_source()
        FakeSource.instances.clear()
        self.env.config.write_text('source = "fake"\n[fake]\nname_prefix = "Polar"\n[ble]\nname_prefix = "COROS"\n')
        make_daemon(self, hr.load_config())
        self.assertEqual(FakeSource.instances[-1].config, {"name_prefix": "Polar"})

    def test_default_paths(self):
        home = Path.home()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1234"}):
            for key in ("HEARTRATE_CONFIG", "HEARTRATE_DB"):
                os.environ.pop(key, None)
            self.assertEqual(hr.config_path(), home / ".config/heartrate/config.toml")
            self.assertEqual(hr.db_path(), home / ".local/share/heartrate/heartrate.db")
            self.assertEqual(hr.state_path(), Path("/run/user/1234/heartrate/state.json"))


if __name__ == "__main__":
    unittest.main()

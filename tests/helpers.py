"""Shared test helpers: load the `heartrate` script as a module, fake clocks and sources."""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import time as real_time
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_heartrate():
    loader = importlib.machinery.SourceFileLoader("heartrate", str(ROOT / "heartrate"))
    spec = importlib.util.spec_from_loader("heartrate", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


hr = load_heartrate()


class StopFollow(Exception):
    pass


class FakeClock:
    """Stands in for the `time` module inside the heartrate script."""

    def __init__(self, now=1_792_190_003.0):
        self.now = now
        self.mono = 1000.0
        self.sleeps = 0
        self.max_sleeps = None

    def time(self):
        return self.now

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.now += seconds
        self.mono += seconds

    def sleep(self, seconds):
        self.sleeps += 1
        if self.max_sleeps is not None and self.sleeps >= self.max_sleeps:
            raise StopFollow
        self.advance(seconds)

    strftime = staticmethod(real_time.strftime)
    localtime = staticmethod(real_time.localtime)


class FakeSource:
    instances = []

    def __init__(self, config, on_reading, on_status):
        self.config = config
        self.on_reading = on_reading
        self.on_status = on_status
        self.calls = []
        FakeSource.instances.append(self)

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


def install_fake_source(name="fake"):
    module = types.ModuleType(f"sources.{name}")
    module.Source = FakeSource
    sys.modules[f"sources.{name}"] = module
    return module


def make_daemon(test, config):
    daemon = hr.Daemon(config)
    test.addCleanup(daemon.store.db.close)
    return daemon


class TempEnv:
    """Points the database, runtime dir and config at a temp dir and patches the clock."""

    def __init__(self, test):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "data" / "heartrate.db"
        self.config = self.dir / "config.toml"
        self.runtime = self.dir / "run"
        self.runtime.mkdir()
        env = mock.patch.dict(
            os.environ,
            {"HEARTRATE_DB": str(self.db), "HEARTRATE_CONFIG": str(self.config), "XDG_RUNTIME_DIR": str(self.runtime)},
        )
        env.start()
        test.addCleanup(env.stop)
        self.clock = FakeClock()
        patcher = mock.patch.object(hr, "time", self.clock)
        patcher.start()
        test.addCleanup(patcher.stop)

    @property
    def state_file(self):
        return self.runtime / "heartrate" / "state.json"

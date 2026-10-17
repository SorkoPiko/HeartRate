import json
import sqlite3
import unittest
from unittest import mock

from helpers import FakeSource, TempEnv, hr, install_fake_source, make_daemon


class FakeLoop:
    quit_called = False

    def quit(self):
        self.quit_called = True


class DaemonTestCase(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv(self)
        install_fake_source()
        FakeSource.instances.clear()
        # 1792190003 is 3 s into a bucket starting at 1792190000
        self.env.clock.now = 1_792_190_003.0
        self.daemon = make_daemon(self, {"source": "fake", "fake": {"device_id": "x"}})
        self.source = FakeSource.instances[-1]

    def rows(self):
        db = sqlite3.connect(self.env.db)
        try:
            return db.execute("SELECT ts, date, min, avg, max, samples, source FROM buckets ORDER BY ts").fetchall()
        finally:
            db.close()

    def state(self):
        return json.loads(self.env.state_file.read_text())

    def go_live(self, device="Strap 1"):
        self.source.on_status("live", device, "")


class SourceLoadingTest(DaemonTestCase):
    def test_loads_module_by_name_and_passes_its_table(self):
        self.assertEqual(self.daemon.source_name, "fake")
        self.assertIsInstance(self.daemon.source, FakeSource)
        self.assertEqual(self.source.config, {"device_id": "x"})

    def test_missing_table_passes_empty_config(self):
        make_daemon(self, {"source": "fake"})
        self.assertEqual(FakeSource.instances[-1].config, {})

    def test_unknown_source_fails(self):
        with self.assertRaises(ModuleNotFoundError):
            hr.Daemon({"source": "does_not_exist"})

    def test_database_created_in_wal_mode(self):
        self.assertTrue(self.env.db.exists())
        db = sqlite3.connect(self.env.db)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        db.close()


class BucketTest(DaemonTestCase):
    def test_readings_share_a_clock_aligned_bucket(self):
        self.go_live()
        for bpm in (70, 80, 75):
            self.source.on_reading(bpm)
            self.env.clock.advance(2)
        self.assertEqual(self.daemon.bucket.ts, 1_792_190_000)
        self.assertEqual(self.rows(), [])  # window still open

        self.env.clock.advance(1)  # now 1792190010: window closed
        self.daemon.tick()
        [row] = self.rows()
        ts, date, lo, avg, hi, samples, source = row
        self.assertEqual(ts, 1_792_190_000)
        self.assertEqual(ts % 10, 0)
        self.assertEqual(date, hr.time.strftime("%Y-%m-%d", hr.time.localtime(ts)))
        self.assertEqual((lo, hi, samples), (70, 80, 3))
        self.assertAlmostEqual(avg, 75.0)
        self.assertEqual(source, "fake:Strap 1")
        self.assertIsNone(self.daemon.bucket)

    def test_tick_leaves_open_bucket_alone(self):
        self.go_live()
        self.source.on_reading(70)
        self.env.clock.advance(6)  # 1792190009
        self.daemon.tick()
        self.assertEqual(self.rows(), [])

    def test_reading_in_new_window_flushes_previous_bucket(self):
        self.go_live()
        self.source.on_reading(70)
        self.env.clock.advance(8)  # 1792190011
        self.source.on_reading(90)
        rows = self.rows()
        self.assertEqual([r[0] for r in rows], [1_792_190_000])
        self.assertEqual(self.daemon.bucket.ts, 1_792_190_010)

    def test_disconnect_flushes_partial_bucket(self):
        self.go_live()
        self.source.on_reading(70)
        self.source.on_reading(72)
        self.source.on_status("disconnected", "Strap 1", "gone")
        [row] = self.rows()
        self.assertEqual((row[0], row[2], row[4], row[5]), (1_792_190_000, 70, 72, 2))
        self.assertIsNone(self.daemon.bucket)

    def test_waiting_does_not_flush(self):
        self.go_live()
        self.source.on_reading(70)
        self.source.on_status("waiting", "Strap 1", "waiting for first reading")
        self.assertEqual(self.rows(), [])

    def test_shutdown_flushes_and_removes_state_file(self):
        self.go_live()
        self.source.on_reading(70)
        self.assertTrue(self.env.state_file.exists())
        loop = FakeLoop()
        self.assertFalse(self.daemon.shutdown(loop))
        self.assertTrue(loop.quit_called)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.source.calls, ["stop"])
        self.assertFalse(self.env.state_file.exists())

    def test_reflush_of_same_ts_merges_with_existing_row(self):
        self.go_live()
        self.source.on_reading(60)
        self.source.on_reading(70)
        self.source.on_status("disconnected", "Strap 1", "gone")
        self.go_live()
        self.env.clock.advance(2)
        self.source.on_reading(100)
        self.source.on_status("disconnected", "Strap 1", "gone")
        [row] = self.rows()
        ts, _date, lo, avg, hi, samples, _source = row
        self.assertEqual((ts, lo, hi, samples), (1_792_190_000, 60, 100, 3))
        self.assertAlmostEqual(avg, (60 + 70 + 100) / 3)

    def test_source_column_uses_device_at_bucket_start(self):
        self.go_live("COROS PACE 3 8B7D1F")
        self.source.on_reading(70)
        self.daemon.flush()
        self.assertEqual(self.rows()[0][6], "fake:COROS PACE 3 8B7D1F")


class StateFileTest(DaemonTestCase):
    def test_reading_writes_live_state(self):
        self.go_live("Strap 1")
        self.source.on_reading(72)
        self.assertEqual(
            self.state(),
            {
                "status": "live",
                "bpm": 72,
                "device": "Strap 1",
                "source": "fake",
                "reason": "",
                "updated": 1_792_190_003.0,
            },
        )

    def test_bpm_null_unless_live(self):
        self.go_live()
        self.source.on_reading(72)
        for status in ("waiting", "disconnected"):
            self.source.on_status(status, "Strap 1", "why")
            state = self.state()
            self.assertEqual(state["status"], status)
            self.assertIsNone(state["bpm"])
            self.assertEqual(state["reason"], "why")

    def test_write_is_atomic_rename(self):
        replaced = []
        real_replace = type(self.env.state_file).replace

        def spy(path, target):
            replaced.append((path, target))
            return real_replace(path, target)

        with mock.patch.object(type(self.env.state_file), "replace", spy):
            self.daemon.write_state()
        [(tmp, target)] = replaced
        self.assertEqual(target, self.env.state_file)
        self.assertEqual(tmp.parent, self.env.state_file.parent)
        self.assertNotEqual(tmp, self.env.state_file)
        self.assertFalse(tmp.exists())
        self.assertEqual(self.state()["source"], "fake")

    def test_heartbeat_rewrites_state(self):
        self.daemon.write_state()
        self.env.clock.advance(3)
        self.daemon.tick()
        self.assertEqual(self.state()["updated"], 1_792_190_003.0)
        self.env.clock.advance(2)
        self.daemon.tick()
        self.assertEqual(self.state()["updated"], 1_792_190_008.0)


class WatchdogTest(DaemonTestCase):
    def test_restarts_live_source_that_goes_silent(self):
        self.go_live()
        self.source.on_reading(70)
        self.env.clock.advance(10)
        self.daemon.tick()
        self.assertEqual(self.source.calls, [])

        self.env.clock.advance(1)
        self.daemon.tick()
        self.assertEqual(self.source.calls, ["stop", "start"])
        state = self.state()
        self.assertEqual(state["status"], "disconnected")
        self.assertIsNone(state["bpm"])
        self.assertEqual(len(self.rows()), 1)

    def test_live_status_without_readings_also_times_out(self):
        self.go_live()
        self.env.clock.advance(11)
        self.daemon.tick()
        self.assertEqual(self.source.calls, ["stop", "start"])

    def test_ignores_sources_that_are_not_live(self):
        self.source.on_status("waiting", "Strap 1", "waiting for first reading")
        self.env.clock.advance(60)
        self.daemon.tick()
        self.assertEqual(self.source.calls, [])


class TickResilienceTest(DaemonTestCase):
    def test_tick_survives_errors_and_keeps_heartbeating(self):
        self.go_live()
        self.source.on_reading(70)
        self.env.clock.advance(8)
        with mock.patch.object(self.daemon.store, "write", side_effect=sqlite3.OperationalError("locked")), \
                mock.patch("sys.stderr"):
            self.assertTrue(self.daemon.tick())
        self.daemon.write_state = mock.Mock(wraps=self.daemon.write_state)
        self.env.clock.advance(5)
        self.assertTrue(self.daemon.tick())
        self.daemon.write_state.assert_called()

    def test_tick_survives_source_restart_errors(self):
        self.go_live()
        self.source.start = mock.Mock(side_effect=RuntimeError("boom"))
        self.env.clock.advance(11)
        with mock.patch("sys.stderr"):
            self.assertTrue(self.daemon.tick())
        self.assertEqual(self.state()["status"], "disconnected")


class LiveWithoutReadingTest(DaemonTestCase):
    def test_live_status_before_first_reading_publishes_waiting(self):
        self.go_live("Strap 1")
        state = self.state()
        self.assertEqual(state["status"], "waiting")
        self.assertIsNone(state["bpm"])
        self.assertEqual(state["reason"], "waiting for first reading")
        self.source.on_reading(72)
        self.assertEqual((self.state()["status"], self.state()["bpm"]), ("live", 72))

    def test_live_again_after_disconnect_waits_for_new_reading(self):
        self.go_live()
        self.source.on_reading(72)
        self.source.on_status("disconnected", "Strap 1", "gone")
        self.go_live()
        self.assertEqual(self.state()["status"], "waiting")


class ShutdownErrorTest(DaemonTestCase):
    def test_shutdown_quits_and_cleans_up_when_flush_fails(self):
        self.go_live()
        self.source.on_reading(70)
        loop = FakeLoop()
        with mock.patch.object(self.daemon.store, "write", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                self.daemon.shutdown(loop)
        self.assertTrue(loop.quit_called)
        self.assertEqual(self.source.calls, ["stop"])
        self.assertFalse(self.env.state_file.exists())

    def test_shutdown_quits_when_source_stop_fails(self):
        self.source.stop = mock.Mock(side_effect=RuntimeError("boom"))
        self.daemon.write_state()
        loop = FakeLoop()
        with self.assertRaises(RuntimeError):
            self.daemon.shutdown(loop)
        self.assertTrue(loop.quit_called)
        self.assertFalse(self.env.state_file.exists())


class ZeroReadingTest(DaemonTestCase):
    def test_zero_bpm_is_ignored(self):
        self.source.on_status("waiting", "Strap 1", "waiting for first reading")
        self.source.on_reading(0)
        self.assertIsNone(self.daemon.bucket)
        state = self.state()
        self.assertEqual(state["status"], "waiting")
        self.assertIsNone(state["bpm"])
        self.daemon.flush()
        self.assertEqual(self.rows(), [])

    def test_zero_while_live_keeps_last_real_value_out_of_buckets(self):
        self.go_live()
        self.source.on_reading(72)
        self.source.on_reading(0)
        self.assertEqual((self.daemon.bucket.min, self.daemon.bucket.samples), (72, 1))
        self.assertEqual(self.state()["bpm"], 72)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest import mock

import dbus

import helpers  # noqa: F401  (puts the repo root on sys.path)
from sources import ble

ADAPTER_PATH = "/org/bluez/hci0"
WATCH = "/org/bluez/hci0/dev_F7_AF_3F_0F_0A_93"
STRAP = "/org/bluez/hci0/dev_11_22_33_44_55_66"
OTHER = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


def device(name, connected=False, rssi=None, hr=True):
    props = {"Name": name, "Alias": name, "Connected": connected, "UUIDs": [ble.HR_SERVICE] if hr else []}
    if rssi is not None:
        props["RSSI"] = rssi
    return {ble.DEVICE: props}


def hr_char(device_path):
    return {f"{device_path}/service0010/char0011": {ble.CHAR: {"UUID": ble.HR_MEASUREMENT}}}


class FakeObject:
    def __init__(self, bluez, path):
        self.bluez, self.path = bluez, path

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.bluez.calls.append((method, self.path))
            if method == "GetManagedObjects":
                return self.bluez.objects
            if method == "Get":
                return False
            if method == "Connect":
                self.bluez.connects.append((kwargs["reply_handler"], kwargs["error_handler"]))
            return None

        return call


class FakeBluez:
    """A system bus with just enough of BlueZ for the ble source."""

    def __init__(self, objects=None):
        self.objects = {ADAPTER_PATH: {ble.ADAPTER: {}}, **(objects or {})}
        self.calls = []
        self.connects = []
        self.down = False

    def get_object(self, _name, path):
        if self.down:
            raise dbus.DBusException("The name is not activatable", name="org.freedesktop.DBus.Error.ServiceUnknown")
        return FakeObject(self, path)

    def add_signal_receiver(self, *args, **kwargs):
        return mock.Mock()

    def count(self, method):
        return sum(1 for m, _ in self.calls if m == method)


class BleTestCase(unittest.TestCase):
    def setUp(self):
        self.bluez = FakeBluez()
        self.glib = mock.Mock()
        self.timeouts = []
        self.glib.timeout_add.side_effect = lambda ms, fn, *args: self.timeouts.append((fn, args))
        for patcher in (
            mock.patch.object(ble.dbus, "SystemBus", return_value=self.bluez),
            mock.patch.object(ble.dbus, "Interface", side_effect=lambda obj, _iface: obj),
            mock.patch.object(ble, "GLib", self.glib),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.statuses = []
        self.readings = []

    def make_source(self, config=None):
        return ble.Source(config or {}, self.readings.append, lambda *s: self.statuses.append(s))


class ParseTest(unittest.TestCase):
    def test_uint8(self):
        self.assertEqual(ble.parse([0x00, 72]), 72)

    def test_uint8_ignores_trailing_fields(self):
        # Flags with RR intervals present (bit 4), value still uint8
        self.assertEqual(ble.parse([0x10, 65, 0x00, 0x04]), 65)

    def test_uint16(self):
        self.assertEqual(ble.parse([0x01, 0x2C, 0x01]), 300)

    def test_uint16_low_value(self):
        self.assertEqual(ble.parse([0x01, 72, 0x00]), 72)


class ManagedObjectsTest(BleTestCase):
    def test_one_lookup_when_scanning(self):
        source = self.make_source()
        source.housekeeping()
        self.assertEqual(self.bluez.count("GetManagedObjects"), 1)
        self.assertEqual(self.bluez.count("StartDiscovery"), 1)

    def test_one_lookup_when_connecting(self):
        self.bluez.objects[WATCH] = device("COROS PACE 3", connected=False)
        source = self.make_source()
        source.housekeeping()
        self.assertEqual(self.bluez.count("GetManagedObjects"), 1)
        self.assertEqual(self.bluez.count("Connect"), 1)

    def test_one_lookup_when_subscribing_to_connected_device(self):
        self.bluez.objects[WATCH] = device("COROS PACE 3", connected=True)
        self.bluez.objects.update(hr_char(WATCH))
        source = self.make_source()
        source.scanning = True
        source.housekeeping()
        self.assertEqual(self.bluez.count("GetManagedObjects"), 1)
        self.assertEqual(self.bluez.count("StopDiscovery"), 1)
        self.assertEqual(self.bluez.count("StartNotify"), 1)
        self.assertEqual(self.statuses[-1][0], "waiting")


class BluetoothDownTest(BleTestCase):
    def test_starts_without_bluetoothd_and_recovers(self):
        self.bluez.down = True
        source = self.make_source()
        source.start()
        status, _device, reason = self.statuses[-1]
        self.assertEqual(status, "disconnected")
        self.assertTrue(reason.startswith("bluetooth unavailable"), reason)

        self.bluez.down = False
        source.housekeeping()
        self.assertEqual(self.bluez.count("GetManagedObjects"), 1)
        self.assertEqual(self.bluez.count("StartDiscovery"), 1)
        source.stop()


class FindDeviceTest(BleTestCase):
    def find(self, objects, config=None):
        return self.make_source(config).find_device(objects)[0]

    def test_needs_heart_rate_service(self):
        self.assertIsNone(self.find({OTHER: device("Speaker", hr=False)}))

    def test_name_prefix_filters(self):
        objects = {STRAP: device("Polar H10", rssi=-60), WATCH: device("COROS PACE 3")}
        self.assertEqual(self.find(objects, {"name_prefix": "COROS"}), WATCH)
        self.assertIsNone(self.find({STRAP: device("Polar H10")}, {"name_prefix": "COROS"}))

    def test_prefers_connected(self):
        objects = {STRAP: device("Polar H10", rssi=-40), WATCH: device("COROS PACE 3", connected=True)}
        self.assertEqual(self.find(objects), WATCH)

    def test_prefers_recently_seen_over_stale(self):
        objects = {STRAP: device("Polar H10"), WATCH: device("COROS PACE 3", rssi=-70)}
        self.assertEqual(self.find(objects), WATCH)
        objects = {WATCH: device("COROS PACE 3", rssi=-70), STRAP: device("Polar H10")}
        self.assertEqual(self.find(objects), WATCH)


class RestartTest(BleTestCase):
    def setUp(self):
        super().setUp()
        self.bluez.objects[WATCH] = device("COROS PACE 3", connected=False)
        self.bluez.objects.update(hr_char(WATCH))
        self.source = self.make_source()
        self.source.start()

    def restart(self):
        self.source.stop()
        self.source.start()

    def test_stale_connect_reply_is_ignored(self):
        old_reply, _ = self.bluez.connects[0]
        self.restart()
        self.assertTrue(self.source.connecting)  # new session's Connect is in flight
        old_reply()
        self.assertTrue(self.source.connecting)
        self.assertEqual(self.timeouts, [])

    def test_stale_connect_error_is_ignored(self):
        _, old_error = self.bluez.connects[0]
        self.restart()
        before = list(self.statuses)
        old_error(dbus.DBusException("failed", name="org.bluez.Error.Failed"))
        self.assertEqual(self.statuses, before)
        self.assertTrue(self.source.connecting)

    def test_stale_pending_subscribe_is_ignored(self):
        reply, _ = self.bluez.connects[0]
        reply()
        [(fn, args)] = self.timeouts
        self.restart()
        self.assertFalse(fn(*args))
        self.assertEqual(self.bluez.count("StartNotify"), 0)

    def test_current_session_subscribes(self):
        reply, _ = self.bluez.connects[0]
        reply()
        [(fn, args)] = self.timeouts
        fn(*args)
        self.assertEqual(self.bluez.count("StartNotify"), 1)
        self.assertEqual(self.statuses[-1][0], "waiting")


if __name__ == "__main__":
    unittest.main()

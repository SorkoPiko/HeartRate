"""Bluetooth LE heart rate source.

Connects through BlueZ over D-Bus to any device advertising the Heart Rate
Service, subscribes to the Heart Rate Measurement characteristic and reports
each reading. Reconnects on its own whenever the device drops out or stops
broadcasting.

Config (`[ble]`):
  name_prefix   only use devices whose name starts with this (optional)
"""
import time
import traceback

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
STALE_AFTER = 10  # seconds without a reading before the device counts as gone
RETRY_EVERY = 5

BLUEZ = "org.bluez"
DEVICE = "org.bluez.Device1"
ADAPTER = "org.bluez.Adapter1"
CHAR = "org.bluez.GattCharacteristic1"
PROPS = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)


def parse(data):
    # Flags bit 0: heart rate value is uint16 instead of uint8
    data = bytes(data)
    return int.from_bytes(data[1:3], "little") if data[0] & 1 else data[1]


class Source:
    def __init__(self, config, on_reading, on_status):
        self.name_prefix = config.get("name_prefix", "")
        self.on_reading = on_reading
        self.on_status = on_status
        self.bus = dbus.SystemBus()
        self.manager = dbus.Interface(self.bus.get_object(BLUEZ, "/"), OBJECT_MANAGER)
        self.device_path = None
        self.char_path = None
        self.name = ""
        self.live = False
        self.last_seen = 0
        self.connecting = False
        self.scanning = False
        self.matches = []
        self.timer = None

    def start(self):
        self.matches = [
            self.bus.add_signal_receiver(
                self.on_props, dbus_interface=PROPS, signal_name="PropertiesChanged", path_keyword="path"
            ),
            # BlueZ drops unpaired devices soon after they disconnect, without a Connected change
            self.bus.add_signal_receiver(self.on_removed, dbus_interface=OBJECT_MANAGER, signal_name="InterfacesRemoved"),
        ]
        self.offline("looking for a heart rate device")
        self.tick()
        self.timer = GLib.timeout_add_seconds(RETRY_EVERY, self.tick)

    def stop(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        for match in self.matches:
            match.remove()
        self.matches = []
        try:
            self.stop_scan()
        except dbus.DBusException:
            pass
        # Leave the connection up for any other client; just drop our notify session
        if self.char_path is not None:
            try:
                dbus.Interface(self.bus.get_object(BLUEZ, self.char_path), CHAR).StopNotify()
            except dbus.DBusException:
                pass
        self.char_path = None
        self.device_path = None
        self.connecting = False

    def objects(self):
        return self.manager.GetManagedObjects()

    def adapter(self):
        for path, ifaces in self.objects().items():
            if ADAPTER in ifaces:
                return path
        return None

    def find_device(self):
        for path, ifaces in self.objects().items():
            dev = ifaces.get(DEVICE)
            if not dev or HR_SERVICE not in [str(u).lower() for u in dev.get("UUIDs", [])]:
                continue
            if str(dev.get("Name", "")).startswith(self.name_prefix):
                return path, dev
        return None, None

    def find_char(self):
        for path, ifaces in self.objects().items():
            ch = ifaces.get(CHAR)
            if ch and path.startswith(self.device_path) and str(ch["UUID"]).lower() == HR_MEASUREMENT:
                return path
        return None

    def offline(self, why):
        self.char_path = None
        self.live = False
        self.on_status("disconnected", self.name, why)

    def tick(self):
        """Periodic housekeeping: (re)connect and expire stale readings."""
        # Any exception would make GLib drop this timer and stop reconnecting for good
        try:
            self.housekeeping()
        except Exception:
            traceback.print_exc()
        return True

    def housekeeping(self):
        # Covers both a stalled stream and a subscription that never delivered
        if self.char_path is not None and time.monotonic() - self.last_seen > STALE_AFTER:
            self.offline("no reading for a while")
        if self.char_path is None and not self.connecting:
            try:
                self.try_connect()
            except dbus.DBusException as err:
                # bluetoothd is down or restarting; keep ticking until it's back
                self.offline(f"bluetooth unavailable: {err.get_dbus_message()}")

    def try_connect(self):
        path, dev = self.find_device()
        if path is None:
            self.scan()
            return
        self.device_path = path
        self.name = str(dev.get("Alias") or dev.get("Name") or dev.get("Address"))
        if not dev.get("Connected"):
            self.connecting = True
            dbus.Interface(self.bus.get_object(BLUEZ, path), DEVICE).Connect(
                reply_handler=self.on_connected, error_handler=self.on_connect_error, timeout=20
            )
            return
        self.stop_scan()
        self.subscribe()

    def scan(self):
        adapter = self.adapter()
        if adapter is None:
            self.offline("no bluetooth adapter")
            return
        props = dbus.Interface(self.bus.get_object(BLUEZ, adapter), PROPS)
        if props.Get(ADAPTER, "Discovering"):
            return
        ad = dbus.Interface(self.bus.get_object(BLUEZ, adapter), ADAPTER)
        try:
            ad.SetDiscoveryFilter({"Transport": "le"})
            ad.StartDiscovery()
            self.scanning = True
        except dbus.DBusException:
            pass

    def stop_scan(self):
        if not self.scanning:
            return
        self.scanning = False
        adapter = self.adapter()
        if adapter is None:
            return
        try:
            dbus.Interface(self.bus.get_object(BLUEZ, adapter), ADAPTER).StopDiscovery()
        except dbus.DBusException:
            pass

    def on_connected(self):
        self.connecting = False
        self.stop_scan()
        # GATT services resolve shortly after Connect returns
        GLib.timeout_add(1500, self.subscribe)

    def on_connect_error(self, err):
        self.connecting = False
        # Another client is already connecting; the next tick picks up the connection
        if "InProgress" in err.get_dbus_name():
            return
        self.offline(f"connect failed: {err.get_dbus_message()}")

    def subscribe(self):
        if self.device_path is None:
            return False
        self.char_path = self.find_char()
        if self.char_path is None:
            return False  # tick retries once services resolve
        try:
            dbus.Interface(self.bus.get_object(BLUEZ, self.char_path), CHAR).StartNotify()
        except dbus.DBusException as err:
            if "InProgress" not in err.get_dbus_name():
                self.offline(f"subscribe failed: {err.get_dbus_message()}")
                return False
        self.last_seen = time.monotonic()
        self.on_status("waiting", self.name, "waiting for first reading")
        return False

    def on_removed(self, path, _ifaces):
        if self.device_path and str(path).startswith(self.device_path):
            self.device_path = None
            if self.char_path is not None:
                self.offline("device not connected")

    def on_props(self, iface, changed, _invalidated, path=None):
        if iface == CHAR and path == self.char_path and "Value" in changed:
            self.last_seen = time.monotonic()
            if not self.live:
                self.live = True
                self.on_status("live", self.name, "")
            self.on_reading(parse(changed["Value"]))
        elif iface == DEVICE and path == self.device_path and changed.get("Connected") is False:
            self.offline("device not connected")

# HeartRate

Records heart rate history from a pluggable source. One daemon owns the
source connection, a SQLite database and a live state file; waybar and other
readers only read what it writes.

## Install

Needs Python 3.11+, `python-dbus` and `python-gobject`.

```sh
systemctl --user enable --now ~/code/HeartRate/heartrate.service
```

Then point waybar at the follower, which never opens Bluetooth itself:

```jsonc
"custom/heartrate": {
  "exec": "$HOME/code/HeartRate/heartrate waybar --follow",
  "return-type": "json"
}
```

Commands:

- `heartrate daemon`: connect to the configured source and record readings.
- `heartrate waybar --follow`: print one waybar JSON line on start and on
  every state change. Shows `hr –` when the daemon isn't running.

## Config

`~/.config/heartrate/config.toml` (`HEARTRATE_CONFIG` overrides the path):

```toml
source = "ble"

[ble]
name_prefix = "COROS"
```

The `[<source>]` table is passed to the source. Without a config file the
daemon uses `ble` with no name filter, which picks the first device
advertising the Heart Rate Service.

## Sources

A source is a module in `sources/` defining:

```python
class Source:
    def __init__(self, config: dict, on_reading, on_status): ...
    def start(self): ...
    def stop(self): ...
```

- `on_reading(bpm: int)` for each reading.
- `on_status(status, device, reason)`, where `status` is `live`, `waiting`
  or `disconnected` and `device` is a human label.

Sources run on the daemon's GLib main loop and never touch the database or
the state file. Adding one means adding a module and setting
`source = "<module>"` in the config. The daemon restarts a source that reports
`live` but sends no reading for 10 s.

`ble` connects through BlueZ to a device advertising the Heart Rate Service
(on a COROS watch, turn on Broadcast HR), optionally filtered by
`name_prefix`, and reconnects whenever it drops out.

## Database

`~/.local/share/heartrate/heartrate.db` (`HEARTRATE_DB` overrides it), in WAL
mode. Readings are grouped into clock-aligned 10 s buckets:

```sql
CREATE TABLE IF NOT EXISTS buckets (
  ts      INTEGER PRIMARY KEY,  -- bucket start, unix seconds, multiple of 10
  date    TEXT    NOT NULL,     -- local YYYY-MM-DD of ts
  min     INTEGER NOT NULL,
  avg     REAL    NOT NULL,
  max     INTEGER NOT NULL,
  samples INTEGER NOT NULL,
  source  TEXT    NOT NULL      -- "<source>:<device label>", e.g. "ble:COROS PACE 3 8B7D1F"
);
CREATE INDEX IF NOT EXISTS idx_buckets_date ON buckets(date);
```

A bucket is written once its window closes, and a partial bucket is written
when the source disconnects or the daemon stops. Re-writing the same `ts`
merges with the existing row.

## State file

`$XDG_RUNTIME_DIR/heartrate/state.json`, replaced atomically on every reading,
every status change and at least every 5 s. It is removed on clean exit.
Readers should treat an `updated` older than 15 s as a dead daemon.

```json
{"status": "live", "bpm": 72, "device": "COROS PACE 3 8B7D1F", "source": "ble", "reason": "", "updated": 1792190000.5}
```

`bpm` is null unless `status` is `live`.

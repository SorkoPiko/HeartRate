"""Heart rate sources.

Each module defines `Source(config, on_reading, on_status)` with `.start()`
and `.stop()`, and runs on the daemon's GLib main loop. `config` is the
`[<source>]` table from the config file. Sources report through the two
callbacks and never touch the database or the state file:

  on_reading(bpm)                  one heart rate reading
  on_status(status, device, reason) status is "live", "waiting" or "disconnected";
                                    device is a human label for the sensor
"""

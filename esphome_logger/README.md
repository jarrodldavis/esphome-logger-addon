# ESPHome Logger (Home Assistant local add-on)

A Home Assistant OS add-on that discovers ESPHome devices on your LAN via
mDNS, then persists logs from a **user-selected subset** of them to
`/share/esphome-logs/<device-name>/<device-name>.log` with **per-device**
rotation and retention.

Why this instead of a `systemd` service? Home Assistant OS doesn't expose
systemd to users. The HA-OS-native way to run "a service that starts on boot"
is a local add-on managed by the Supervisor — this folder is that add-on.

## Features

- mDNS discovery on startup: the add-on log lists every ESPHome device it
  sees, with hostname and IPv4 address. You then opt-in by adding entries to
  the `devices:` list in the Configuration tab.
- Per-device settings: each device can have its own rotation frequency
  (`hourly` / `daily` / `weekly` / `size`), retention count, log level, and
  (for size-based rotation) a max file size.
- Concurrent: each device runs in its own asyncio task. One device dropping or
  failing auth doesn't affect the others.
- Plain-text output: ANSI color codes are stripped, every line is prefixed
  with an ISO-8601 timestamp.
- Anomaly notifications: the add-on watches the live log stream for errors,
  warnings, reboots, brownouts, watchdog/crash signatures, Wi-Fi disconnects,
  and sensor failures. When configured thresholds are crossed within a rolling
  window, it posts a `persistent_notification` to Home Assistant (visible in
  the HA bell icon and on the mobile app). Notifications are debounced to at
  most one per device per `debounce_minutes`. (True HA "Repair" cards require
  a custom integration and are not provided by this add-on.)
- Daily summary: at a configured time each day (default 08:00 local), the
  add-on posts a single consolidated `persistent_notification` covering every
  device's last 24h of activity with a ✅ / ⚠️ / 🚨 verdict — useful as a
  heartbeat on quiet days and an early warning on bad ones.

## Output layout

```
/share/esphome-logs/
    crowpanel/
        crowpanel.log              <- current
        crowpanel.log.2026-05-10   <- rotated
        crowpanel.log.2026-05-09
        ...
    thermostat/
        thermostat.log
        thermostat.log.2026-05-11_14
        ...
```

Browse `/share/` from the Samba share, File editor, Studio Code Server, or
Advanced SSH & Web Terminal add-ons.

## Installation

### 1. Copy the folder onto your HA host

The folder must end up at `/addons/esphome_logger/` on the host. Two ways:

- **Samba**: install the *Samba share* add-on, drag the folder into the
  `addons` share.
- **SSH**: install the *Advanced SSH & Web Terminal* add-on, then from your
  Mac: `scp -r esphome_logger root@homeassistant.local:/addons/`

Final layout:

```
/addons/esphome_logger/config.yaml
/addons/esphome_logger/Dockerfile
/addons/esphome_logger/main.py
/addons/esphome_logger/run.sh
/addons/esphome_logger/README.md
```

### 2. Discover the add-on

Settings → Add-ons → **Add-on Store** → ⋮ → **Check for updates**. The add-on
appears at the bottom under **Local add-ons** as "ESPHome Logger".

### 3. Install, then let it discover devices first

Install (build takes ~2 minutes on a Pi). On the **Info** tab turn on **Start
on boot** and **Watchdog**, then **Start**. Open the **Log** tab — you should
see:

```
[2026-05-11T...] ESPHome Logger starting
[2026-05-11T...] discovery: browsing _esphomelib._tcp.local. for 6s ...
[2026-05-11T...] discovery: 3 device(s) found:
  - crowpanel     address=192.168.1.42  hostname=crowpanel.local
  - thermostat    address=192.168.1.51  hostname=thermostat.local
  - garage-door   address=192.168.1.77  hostname=garage-door.local
```

(if you don't see any devices, mDNS may be blocked between VLANs — verify
`host_network: true` in `config.yaml` and that the add-on actually has
host-network access in your HA setup.)

### 4. Pick which devices to log, configure rotation per device

Edit the **Configuration** tab:

```yaml
discover_on_start: true
discovery_timeout: 6
log_subdir: esphome-logs

defaults:
  rotation: daily
  retention: 30
  max_size_mb: 10
  log_level: DEBUG

devices:
  - name: crowpanel
    address: crowpanel.local
    noise_psk: "BASE64-PSK-FROM-crowpanel.yaml"
    rotation: daily
    retention: 30
    log_level: DEBUG
    notify: true

  - name: thermostat
    address: 192.168.1.51
    noise_psk: "BASE64-PSK-FROM-thermostat.yaml"
    rotation: hourly
    retention: 48        # 48 hourly files = last 2 days
    log_level: INFO
    notify: true

  - name: garage-door
    address: garage-door.local
    rotation: size
    max_size_mb: 5
    retention: 10        # keep 10 backup files = ~50 MB total
    log_level: WARN
    notify: false        # noisy device, opted out of notifications

notifications:
  enabled: true
  debounce_minutes: 60
  window_hours: 24
  thresholds:
    error: 1             # any single error fires
    warning: 10
    reboot: 2
    brownout: 1
    wifi_disconnect: 20
    watchdog: 1
    sensor_failure: 5

daily_summary:
  enabled: true
  hour: 8                # local time
  minute: 0
  post_when_clean: true  # also post when everything is healthy (a heartbeat)
```

Save, restart the add-on.

### Configuration reference

`discover_on_start` *(bool, default true)*: run mDNS browse on startup and
print results to the add-on log.

`discovery_timeout` *(int, default 6)*: how long to listen for mDNS responses.

`log_subdir` *(str, default `esphome-logs`)*: subdirectory under `/share/`.

`defaults` *(map)*: applied to any device whose own field is missing.

`devices` *(list)*: one entry per device to capture.

Per-device fields:

| Field         | Required | Notes |
| ------------- | -------- | ----- |
| `name`        | yes      | Used for the subdirectory and filename. Must be unique. |
| `address`     | yes      | Hostname (`foo.local`) or IPv4. |
| `noise_psk`   | no       | The base64 key from `api: encryption: key:` in the device's YAML. Required if encryption is enabled (almost always). |
| `password`    | no       | Legacy API password if used instead of noise PSK. |
| `rotation`    | no       | `hourly`, `daily`, `weekly`, or `size`. Default `daily`. |
| `retention`   | no       | For time-based rotation: number of past periods to keep (30 daily = 30 days). For `size`: number of backup files to keep. Default 30. |
| `max_size_mb` | no       | Only used when `rotation: size`. Default 10 MB. |
| `log_level`   | no       | `NONE`/`ERROR`/`WARN`/`INFO`/`DEBUG`/`VERBOSE`/`VERY_VERBOSE`. Default `DEBUG`. Beware `VERY_VERBOSE` can destabilize the device. |
| `notify`      | no       | Whether anomaly notifications fire for this device. Default `true`. Set to `false` to silence a chatty device while still capturing its logs. |

### Notifications

The add-on classifies every log line into one of these categories:

| Category          | Matches |
| ----------------- | ------- |
| `error`           | `[E]` log severity |
| `warning`         | `[W]` log severity |
| `reboot`          | `ESPHome version`, `Running through setup()`, `rst:0x`, `esp_image:`, `ESP-ROM:` |
| `brownout`        | `Brownout` (any case) |
| `watchdog`        | `Task watchdog`, `wdt:`, `Guru Meditation`, `Exception(`, `abort()` |
| `wifi_disconnect` | `WiFi … disconnect`, `Disconnected from WiFi`, `Lost connection … WiFi` |
| `sensor_failure`  | `Failed to read`, `Sensor … invalid`, `checksum … invalid`, `CRC check failed` |

It keeps a rolling `window_hours` deque of events per device. After every new
event it re-evaluates: if **any** category's count is `>=` its threshold, a
notification is posted — at most one per device every `debounce_minutes`.

| Field              | Default | Notes |
| ------------------ | ------- | ----- |
| `enabled`          | `true`  | Global on/off for the notifier. |
| `debounce_minutes` | 60      | Minimum time between two notifications for the same device. |
| `window_hours`     | 24      | Size of the rolling window used to count events. |
| `thresholds.error` | 1       | Any single error fires. |
| `thresholds.warning` | 10    | |
| `thresholds.reboot` | 2      | More than 2 reboots a day usually indicates a problem. |
| `thresholds.brownout` | 1    | Any brownout fires - they almost always mean a bad power supply. |
| `thresholds.watchdog` | 1    | |
| `thresholds.wifi_disconnect` | 20 | Occasional Wi-Fi blips are normal; 20+ a day is not. |
| `thresholds.sensor_failure` | 5 | |

Notifications appear in Home Assistant's bell icon (top right) and in the
mobile app if installed. Each notification uses `notification_id =
"esphome_logger_<device-name>"`, so subsequent alerts for the same device
overwrite the previous one rather than stacking.

To pause notifications without losing log capture, either set
`notifications.enabled: false` globally or `notify: false` on individual
devices.

### Daily summary

At a configurable time each day (default 08:00 local), the add-on posts a
single consolidated `persistent_notification` summarizing the last
`window_hours` of activity for every device with notifications enabled. The
notification uses `notification_id = "esphome_logger_daily_summary"` so each
day's summary replaces the previous one.

| Field             | Default | Notes |
| ----------------- | ------- | ----- |
| `enabled`         | `true`  | Master switch for the daily summary. |
| `hour`            | `8`     | Local hour, 0–23. |
| `minute`          | `0`     | Local minute, 0–59. |
| `post_when_clean` | `true`  | If false, the summary is only posted when at least one device has a non-zero category. With `true` you get a daily heartbeat even on quiet days. |

Verdict icons in the title:
- ✅ all devices clean over the window
- ⚠️ at least one device has minor activity (warnings, modest Wi-Fi blips)
- 🚨 at least one device crossed a critical threshold (errors, reboots,
  brownouts, watchdog/crashes, sensor failures)

The summary uses the same sliding window the real-time notifier uses, so the
counts you see at 08:00 reflect the prior 24 hours of stream activity. If the
add-on was recently restarted the window may be partial — the message
explicitly says so when that's the case.

### Rotation semantics (read this once)

- `hourly`: the current log file is `<name>.log`; at the top of each hour the
  current file is renamed to `<name>.log.YYYY-MM-DD_HH`. Files past
  `retention` are deleted by Python's logging machinery.
- `daily`: rotates at local midnight. Archived as `<name>.log.YYYY-MM-DD`.
- `weekly`: rotates Monday 00:00.
- `size`: rotates when `<name>.log` exceeds `max_size_mb`. Archives are
  `<name>.log.1`, `<name>.log.2`, ..., up to `retention` files.

## Reviewing logs later

```
ls -lh /share/esphome-logs/crowpanel/
tail -f /share/esphome-logs/crowpanel/crowpanel.log
grep -i 'error\|warn' /share/esphome-logs/crowpanel/*

# Across all devices:
grep -ri 'wifi disconnect' /share/esphome-logs/

# Time-bounded:
awk '$1 >= "2026-05-11T08:00" && $1 <= "2026-05-11T09:00"' \
    /share/esphome-logs/crowpanel/crowpanel.log
```

## Troubleshooting

- **mDNS shows no devices**: make sure `host_network: true` is in
  `config.yaml` (it is by default in this build). On HA-OS this should expose
  the host LAN to the container. If your network spans VLANs, mDNS may not
  cross them — fall back to typing IP addresses into `devices:` directly.
- **`Authentication failed`** on a device: `noise_psk` doesn't match
  `api.encryption.key` in that device's YAML.
- **Build fails on Pi for `noiseprotocol`**: the Dockerfile already installs
  `build-base / libffi-dev / openssl-dev` to handle cffi compilation. If it
  still fails, check the Supervisor log for the actual pip error.
- **Add-on starts but no files appear**: check the add-on Log tab — there
  should be a `[device] writing to /share/...` line per device. If not, the
  `devices:` list is empty or all entries failed validation.

## Upgrading from v1.0 (single-device, bash version)

The slug changed from `crowpanel_logger` to `esphome_logger`. To upgrade:
remove the old add-on, drop in this folder, reload the store, install the new
one. Existing files under `/share/esphome-logs/crowpanel-*.log` are not
touched — the v2 layout puts logs under
`/share/esphome-logs/<name>/<name>.log`, so they coexist.

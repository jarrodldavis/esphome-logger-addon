# ESPHome Logger

Captures live logs from your ESPHome devices, writes them to `/share/esphome-logs/<device>/<device>.log`, and pings you in Home Assistant when something looks off.

I built this because the ESPHome dashboard's log viewer is great for debugging live, but it doesn't keep anything. When a sensor flakes out at 3am or a device starts rebooting itself, you want history.

## What it does

Reads HA's own config entries to learn what ESPHome devices exist on your network, where they live, and what their PSKs are. No mDNS browsing, no copying keys out of `secrets.yaml`. Streams logs over each device's native API. Rotates daily by default and keeps 30 days; hourly, weekly, and size-based rotation are options too.

Watches for trouble in real time: errors, warnings, reboots, brownouts, watchdog crashes, Wi-Fi disconnects, sensor failures. When the count crosses a threshold inside a rolling 24-hour window, you get a persistent notification in HA. Debounced to at most one per device per hour by default, so a stuck sensor can't spam you.

Posts a consolidated daily summary at 8am (configurable) with a verdict icon per device, so you know everything's healthy without having to look. Set `post_when_clean: false` if you only want to hear about bad days.

## Install

In Home Assistant: Settings → Add-ons → Add-on Store → ⋮ → Repositories → paste `https://github.com/dcgrove/esphome-logger-addon` → Add. ESPHome Logger appears in the store; install it, set boot=auto and watchdog=on, start.

For a direct copy without going through the repository URL, drop this folder into `/addons/esphome_logger/` on the HA host. After any `map:` or `host_network:` change you'll need `ha supervisor restart` to get the supervisor to re-read the manifest.

## First run

Start the add-on with the defaults and check its log. You'll see something like:

```
discovery: 12 ESPHome device(s) registered in HA:
  - crowpanel5         address=192.168.1.197   psk=set       (CrowPanel 5)
  - upsy-desky         address=192.168.1.42    psk=set       (Upsy Desky)
  - kiln-controller    address=192.168.1.99    psk=MISSING   (Kiln Controller)
  ...
```

Pick which ones to capture and add them under `devices:`, or just set `auto_add_devices: true` to enroll everything HA knows about.

## Configuration

```yaml
discover_on_start: true
auto_add_devices: false        # set true to capture every HA-known device
log_subdir: esphome-logs

defaults:
  rotation: daily
  retention: 30
  max_size_mb: 10
  log_level: DEBUG

devices:
  - name: crowpanel
    address: 192.168.1.197
    noise_psk: "your-base64-psk"
    rotation: daily
    retention: 30

  - name: noisy-sensor
    address: 192.168.1.50
    noise_psk: "..."
    rotation: size
    max_size_mb: 5
    retention: 10              # 10 backup files of 5MB each
    notify: false              # don't page me until I fix this

notifications:
  enabled: true
  debounce_minutes: 60
  window_hours: 24
  thresholds:
    error: 1                   # any single error fires
    warning: 10
    reboot: 2
    brownout: 1
    wifi_disconnect: 20
    watchdog: 1
    sensor_failure: 5

daily_summary:
  enabled: true
  hour: 8
  minute: 0
  post_when_clean: true
```

## Per-device options

`name` and `address` are required. Everything else falls back to `defaults:` if you don't set it.

- `noise_psk` — base64 key from `api: encryption: key:` in the device's YAML. Required if the device has encryption enabled (it almost certainly does on anything built in the last couple of years).
- `password` — only for legacy API password auth. Skip if you use noise.
- `rotation` — `hourly`, `daily`, `weekly`, or `size`.
- `retention` — for time-based rotation, how many past periods to keep (30 daily = 30 days). For size-based, how many backup files.
- `max_size_mb` — only used when `rotation: size`. 10 MB default.
- `log_level` — `NONE` / `ERROR` / `WARN` / `INFO` / `DEBUG` / `VERBOSE` / `VERY_VERBOSE`. Don't use VERY_VERBOSE on anything you care about; it can choke the device.
- `notify` — set false to keep capturing the log but stop paging on this device. Useful for the one chatty sensor you haven't gotten around to fixing.

## Notifications

Each log line gets sorted into a category: error (`[E]`), warning (`[W]`), reboot, brownout, watchdog, Wi-Fi disconnect, or sensor failure. The add-on keeps a rolling window per device. When any category crosses its threshold, it posts a `persistent_notification` — that's the bell icon in the HA UI and the mobile app notification if you have it.

Each device gets at most one notification per `debounce_minutes`. The notification ID is per-device (`esphome_logger_<name>`), so a new alert for the same device overwrites the previous one rather than stacking ten of them in your bell.

The daily summary uses its own ID (`esphome_logger_daily_summary`) and gets one combined notification per day with all devices' counts and a ✅ / ⚠️ / 🚨 verdict.

HA Repair cards aren't an option from an add-on — they need a custom integration to register with HA's issue registry. Persistent notifications are the closest equivalent and work fine.

## Output layout

```
/share/esphome-logs/
    crowpanel/
        crowpanel.log              ← current
        crowpanel.log.2026-05-16   ← yesterday
        crowpanel.log.2026-05-15
    upsy-desky/
        upsy-desky.log
        ...
```

Every line is prefixed with an ISO-8601 timestamp. ANSI color codes are stripped on the way in.

Browse `/share` with the File editor, Studio Code Server, Samba, or Advanced SSH add-on. From a terminal: `tail -f /share/esphome-logs/crowpanel/crowpanel.log`.

## Rotation

- `hourly` — rotates at the top of each hour. Archives named `<name>.log.YYYY-MM-DD_HH`.
- `daily` — rotates at midnight local time. Archives named `<name>.log.YYYY-MM-DD`.
- `weekly` — rotates Monday 00:00 local.
- `size` — rotates when the file exceeds `max_size_mb`. Archives numbered `<name>.log.1`, `.log.2`, etc.

Anything past `retention` gets pruned automatically.

## Troubleshooting

**Discovery shows no devices.** Either nothing's connected to HA via the ESPHome integration yet, or the `homeassistant_config:ro` mount didn't take effect. The add-on log will say `no readable core.config_entries` in the latter case. Fix: `ha supervisor restart`.

**Connected but the log file stays empty.** The device's `logger:` block has `level: NONE` or is missing entirely. INFO is fine and quiet; DEBUG is fine and chatty.

**`Authentication failed`.** The `noise_psk` doesn't match the key in the device's YAML. The first-run discovery message tells you which devices HA has a PSK for; if it says `psk=MISSING`, the integration in HA was set up without encryption (rare but possible).

**Notifications return 401.** The supervisor's manifest cache hasn't picked up `homeassistant_api: true`. `ha supervisor restart` followed by `ha addons update local_esphome_logger` usually fixes it.

**Updated the add-on but new options or mounts don't show up.** Same root cause — the supervisor caches the manifest at install time. `ha supervisor restart` forces a re-read of every local add-on's `config.yaml`.

## License

MIT. See LICENSE.

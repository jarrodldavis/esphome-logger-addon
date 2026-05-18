# ESPHome Logger — Home Assistant add-on repository

This repository contains a single Home Assistant OS add-on:

| Add-on | Description |
| ------ | ----------- |
| [esphome_logger](./esphome_logger/) | Discovers ESPHome devices on the LAN, persists logs from a selected subset to `/share/esphome-logs/<device>/<device>.log` with per-device rotation/retention, posts HA `persistent_notification`s on threshold-breach anomalies, and emits a consolidated daily summary at a configured time. |

## Installation

Open Home Assistant → **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, paste this repo's URL, click **Add**. The ESPHome Logger add-on will then appear at the bottom of the store.

(If the repository is private, your Home Assistant has to be able to read it — either make the repo public, or supply HA with a credentials helper. For a single private HA instance, the simplest workaround is to keep the repository public-but-unlisted: don't share the URL, but leave the repo public on GitHub so the Supervisor can clone it. Use a private repo if you want stronger access control and are comfortable configuring HA's git auth.)

## Add-on docs

See [`esphome_logger/README.md`](./esphome_logger/README.md) for full configuration reference, rotation semantics, notification rules, and troubleshooting.

## Versioning

Tagged releases follow the version in `esphome_logger/config.yaml` (currently **2.2.0**). Increment the version field whenever you change anything that affects behaviour so the Supervisor picks up the update.

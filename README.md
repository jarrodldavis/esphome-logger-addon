# Home Assistant add-on repository

One add-on lives here: **ESPHome Logger**. It captures live logs from your ESPHome devices, writes them under `/share`, and pokes Home Assistant when something looks off.

## Install

In Home Assistant:

1. Settings → Add-ons → Add-on Store
2. Three-dot menu (top right) → Repositories
3. Paste `https://github.com/dcgrove/esphome-logger-addon` and click Add

ESPHome Logger then shows up at the bottom of the store. Install it, flip on "Start on boot" and "Watchdog", start, and check the Configuration tab.

The add-on needs to be public on GitHub for the supervisor to clone it. If you fork this and make your fork private, the URL install won't work and you'll need to copy `esphome_logger/` into `/addons/` on your HA host instead. After any change to `config.yaml`'s `map:` or `host_network:` fields, run `ha supervisor restart` so the supervisor re-reads the manifest.

## What's here

```
esphome_logger/     the add-on (config.yaml, Dockerfile, main.py, …)
repository.yaml     HA store metadata
LICENSE             MIT
```

Full docs and configuration reference are in [`esphome_logger/README.md`](./esphome_logger/README.md).

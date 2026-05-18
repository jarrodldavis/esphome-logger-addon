#!/usr/bin/env python3
"""ESPHome Logger - Home Assistant local add-on.

Discovers ESPHome devices on the LAN via mDNS and persists logs from a
user-selected subset to /share/<log_subdir>/<device>/<device>.log, with
per-device rotation (hourly/daily/weekly/size) and retention.

Watches the live log stream for anomalies (errors, warnings, reboots, Wi-Fi
disconnects, brownouts, watchdog resets, sensor failures) and posts an HA
persistent_notification when configured thresholds are exceeded within a
rolling window. Debounced so a single device cannot spam notifications.

Configuration is read from /data/options.json (populated by the Supervisor
from the add-on's Configuration tab).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import logging.handlers
import os
import pathlib
import re
import signal
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter, deque
from typing import Any, Deque, Dict, List, Optional, Tuple

# ----- third-party ----------------------------------------------------------
# Imported lazily so that --self-check can run without them being installed.
try:
    from aioesphomeapi import (
        APIClient,
        APIConnectionError,
        LogLevel,
        ReconnectLogic,
    )
except ImportError as exc:  # pragma: no cover - only hit if image build broke
    APIClient = None  # type: ignore
    _aioesphomeapi_err = exc
else:
    _aioesphomeapi_err = None

try:
    from zeroconf import IPVersion, ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
except ImportError as exc:  # pragma: no cover
    AsyncZeroconf = None  # type: ignore
    _zeroconf_err = exc
else:
    _zeroconf_err = None

# ----- constants ------------------------------------------------------------

CONFIG_PATH = os.environ.get("OPTIONS_JSON", "/data/options.json")
SHARE_ROOT = pathlib.Path(os.environ.get("SHARE_ROOT", "/share"))

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

ROTATION_HANDLER_ARGS: Dict[str, Tuple[str, int, str]] = {
    # rotation name -> (when, interval, suffix_for_archived_file)
    "hourly": ("H",  1, "%Y-%m-%d_%H"),
    "daily":  ("midnight", 1, "%Y-%m-%d"),
    "weekly": ("W0", 1, "%Y-%m-%d"),  # rotate Monday 00:00
}

DEFAULT_DEFAULTS: Dict[str, Any] = {
    "rotation": "daily",
    "retention": 30,
    "max_size_mb": 10,
    "log_level": "DEBUG",
    "notify": True,
}

DEFAULT_NOTIFICATIONS: Dict[str, Any] = {
    "enabled": True,
    "debounce_minutes": 60,
    "window_hours": 24,
    "thresholds": {
        "error": 1,
        "warning": 10,
        "reboot": 2,
        "brownout": 1,
        "wifi_disconnect": 20,
        "watchdog": 1,
        "sensor_failure": 5,
    },
}

DEFAULT_DAILY_SUMMARY: Dict[str, Any] = {
    "enabled": True,
    "hour": 8,
    "minute": 0,
    # Whether to post even when every device is clean.
    "post_when_clean": True,
}

# Categorization patterns. Order matters - first match wins.
# Tested against the post-ANSI-stripped log line text (no leading ISO ts).
CATEGORY_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("error",           re.compile(r"\[E\]")),
    ("warning",         re.compile(r"\[W\]")),
    ("reboot",          re.compile(r"ESPHome version|Running through setup\(\)|"
                                   r"rst:0x|esp_image:|ESP-ROM:")),
    ("brownout",        re.compile(r"[Bb]rownout")),
    ("watchdog",        re.compile(r"Task watchdog|wdt:|Guru Meditation|"
                                   r"Exception\(|abort\(\)")),
    ("wifi_disconnect", re.compile(r"WiFi.*[Dd]isconnect|Lost connection.*WiFi|"
                                   r"Disconnected from WiFi")),
    ("sensor_failure",  re.compile(r"Failed to read|Sensor .* invalid|"
                                   r"checksum.*invalid|CRC check failed",
                                   re.IGNORECASE)),
]

CATEGORY_ORDER = [c for c, _ in CATEGORY_PATTERNS]

SUPERVISOR_API = "http://supervisor/core/api"

# ----- logging --------------------------------------------------------------

# Top-level stdout logger for the add-on's own messages (visible in the add-on
# Log tab). Per-device file loggers are built separately.
_ROOT = logging.getLogger("esphome_logger")
_ROOT.setLevel(logging.INFO)
_stdout = logging.StreamHandler(sys.stdout)
_stdout.setFormatter(logging.Formatter("[%(asctime)s] %(message)s",
                                       datefmt="%Y-%m-%dT%H:%M:%S"))
_ROOT.addHandler(_stdout)


def info(msg: str) -> None:
    _ROOT.info(msg)


def warn(msg: str) -> None:
    _ROOT.warning(msg)


def error(msg: str) -> None:
    _ROOT.error(msg)


# ----- option loading -------------------------------------------------------

def load_options() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r") as f:
        opts = json.load(f)
    return opts


def merge_device(dev: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Fill missing optional fields in a device dict from `defaults`."""
    merged = dict(dev)
    for key, val in defaults.items():
        if key not in merged or merged.get(key) in (None, ""):
            merged[key] = val
    merged["retention"] = max(1, int(merged.get("retention", 30)))
    merged["max_size_mb"] = max(1, int(merged.get("max_size_mb", 10)))
    merged["rotation"] = merged.get("rotation", "daily")
    if merged["rotation"] not in ("hourly", "daily", "weekly", "size"):
        merged["rotation"] = "daily"
    merged["log_level"] = str(merged.get("log_level", "DEBUG")).upper()
    merged["notify"] = bool(merged.get("notify", True))
    return merged


def normalize_notifications(opts: Dict[str, Any]) -> Dict[str, Any]:
    user = opts.get("notifications") or {}
    n: Dict[str, Any] = {
        **DEFAULT_NOTIFICATIONS,
        **{k: v for k, v in user.items() if k != "thresholds"},
    }
    user_thresholds = (user.get("thresholds") or {})
    n["thresholds"] = {
        **DEFAULT_NOTIFICATIONS["thresholds"],
        **{k: int(v) for k, v in user_thresholds.items() if v is not None},
    }
    n["enabled"] = bool(n.get("enabled", True))
    n["debounce_minutes"] = max(1, int(n.get("debounce_minutes", 60)))
    n["window_hours"] = max(1, int(n.get("window_hours", 24)))
    return n


def normalize_daily_summary(opts: Dict[str, Any]) -> Dict[str, Any]:
    user = opts.get("daily_summary") or {}
    d: Dict[str, Any] = {**DEFAULT_DAILY_SUMMARY, **user}
    d["enabled"] = bool(d.get("enabled", True))
    d["hour"] = max(0, min(23, int(d.get("hour", 8))))
    d["minute"] = max(0, min(59, int(d.get("minute", 0))))
    d["post_when_clean"] = bool(d.get("post_when_clean", True))
    return d


# ----- per-device file logger factory --------------------------------------

def build_file_logger(name: str,
                      log_dir: pathlib.Path,
                      rotation: str,
                      retention: int,
                      max_size_mb: int) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"

    file_logger = logging.getLogger(f"esphome_logger.device.{name}")
    file_logger.setLevel(logging.DEBUG)
    file_logger.propagate = False
    for h in list(file_logger.handlers):
        file_logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    if rotation == "size":
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=retention,
            encoding="utf-8",
        )
    else:
        when, interval, suffix = ROTATION_HANDLER_ARGS[rotation]
        h = logging.handlers.TimedRotatingFileHandler(
            log_path,
            when=when,
            interval=interval,
            backupCount=retention,
            encoding="utf-8",
            utc=False,
        )
        h.suffix = suffix
        handler = h

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(message)s",
                          datefmt="%Y-%m-%dT%H:%M:%S")
    )
    file_logger.addHandler(handler)
    return file_logger


# ----- HA persistent notification client ------------------------------------

class HANotifier:
    """Posts persistent_notification.create via the Supervisor's HA proxy.

    Uses urllib in a worker thread; calls are infrequent (debounced), so no
    need for a full async HTTP client dependency.
    """

    def __init__(self) -> None:
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not self.token:
            warn("SUPERVISOR_TOKEN not set - persistent notifications will "
                 "be disabled. Set homeassistant_api: true in config.yaml.")

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def create(self, title: str, message: str, notification_id: str) -> bool:
        if not self.available:
            return False
        url = f"{SUPERVISOR_API}/services/persistent_notification/create"
        payload = json.dumps({
            "title": title,
            "message": message,
            "notification_id": notification_id,
        }).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        def _post() -> Tuple[int, str]:
            req = urllib.request.Request(url, data=payload, method="POST",
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace") if e.fp else str(e)
            except Exception as e:
                return -1, repr(e)

        status, body = await asyncio.to_thread(_post)
        if status == 200 or status == 201:
            return True
        error(f"persistent_notification.create failed: status={status} body={body[:200]}")
        return False


# ----- mDNS discovery -------------------------------------------------------

async def discover_esphome(timeout: float) -> List[Tuple[str, str, str]]:
    if AsyncZeroconf is None:
        warn(f"zeroconf not available ({_zeroconf_err!r}); skipping discovery")
        return []

    info(f"discovery: browsing _esphomelib._tcp.local. for {timeout}s ...")
    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    found: Dict[str, Tuple[str, str]] = {}

    def on_state_change(zeroconf, service_type, name, state_change):
        if state_change != ServiceStateChange.Added:
            return
        try:
            inf = zeroconf.get_service_info(service_type, name, timeout=2000)
        except Exception:
            return
        if not inf:
            return
        addrs = inf.parsed_addresses() if hasattr(inf, "parsed_addresses") else []
        ipv4 = next((a for a in addrs if ":" not in a), "?")
        host = (inf.server or name).rstrip(".")
        found[name] = (host, ipv4)

    browser = AsyncServiceBrowser(
        aiozc.zeroconf,
        "_esphomelib._tcp.local.",
        handlers=[on_state_change],
    )
    try:
        await asyncio.sleep(timeout)
    finally:
        await browser.async_cancel()
        await aiozc.async_close()

    results: List[Tuple[str, str, str]] = []
    for full_name, (host, ipv4) in sorted(found.items()):
        short = full_name.split(".")[0]
        results.append((short, host, ipv4))

    if not results:
        info("discovery: no ESPHome devices found via mDNS")
    else:
        info(f"discovery: {len(results)} device(s) found:")
        for short, host, ipv4 in results:
            info(f"  - {short}   address={ipv4}   hostname={host}")
        info("discovery: paste any of the above into the 'devices' list in "
             "Configuration to capture its logs.")
    return results


# ----- anomaly classifier ---------------------------------------------------

def categorize(line: str) -> Optional[str]:
    for cat, pat in CATEGORY_PATTERNS:
        if pat.search(line):
            return cat
    return None


# ----- per-device runner ----------------------------------------------------

class DeviceRunner:
    def __init__(self,
                 cfg: Dict[str, Any],
                 log_subdir: str,
                 notifications_cfg: Dict[str, Any],
                 notifier: HANotifier):
        self.name: str = cfg["name"]
        self.address: str = cfg["address"]
        self.noise_psk: Optional[str] = cfg.get("noise_psk") or None
        self.password: str = cfg.get("password") or ""
        self.rotation: str = cfg["rotation"]
        self.retention: int = cfg["retention"]
        self.max_size_mb: int = cfg["max_size_mb"]
        self.log_level_name: str = cfg["log_level"]
        self.notify_enabled: bool = bool(cfg.get("notify", True))
        self.log_subdir = log_subdir
        self.notifications_cfg = notifications_cfg
        self.notifier = notifier

        self._stopping = False
        self._reconnect: Optional[ReconnectLogic] = None
        self._file_logger: Optional[logging.Logger] = None
        self._client: Optional[APIClient] = None

        # Sliding-window event log: deque of (epoch_seconds, category)
        self._events: Deque[Tuple[float, str]] = deque()
        self._last_notify_at: float = 0.0
        # Recent sample lines per category for inclusion in notifications.
        self._samples: Dict[str, Deque[str]] = {
            c: deque(maxlen=3) for c in CATEGORY_ORDER
        }

    @property
    def log_level(self) -> "LogLevel":
        attr = f"LOG_LEVEL_{self.log_level_name}"
        return getattr(LogLevel, attr, LogLevel.LOG_LEVEL_DEBUG)

    # --- event window helpers -----------------------------------------------

    def _window_seconds(self) -> int:
        return int(self.notifications_cfg["window_hours"]) * 3600

    def _debounce_seconds(self) -> int:
        return int(self.notifications_cfg["debounce_minutes"]) * 60

    def _trim_window(self, now: float) -> None:
        cutoff = now - self._window_seconds()
        d = self._events
        while d and d[0][0] < cutoff:
            d.popleft()

    def _evaluate_thresholds(self) -> List[Tuple[str, int, int]]:
        """Return [(category, count, threshold), ...] for breached categories."""
        thresholds: Dict[str, int] = self.notifications_cfg["thresholds"]
        counts = Counter(cat for _, cat in self._events)
        breached: List[Tuple[str, int, int]] = []
        for cat in CATEGORY_ORDER:
            t = thresholds.get(cat)
            if not t:
                continue
            c = counts.get(cat, 0)
            if c >= t:
                breached.append((cat, c, t))
        return breached

    def _build_message(self, breached: List[Tuple[str, int, int]]) -> str:
        window_h = int(self.notifications_cfg["window_hours"])
        lines = [
            f"Device **{self.name}** at `{self.address}` crossed log "
            f"thresholds over the last {window_h}h:",
            "",
        ]
        for cat, count, threshold in breached:
            lines.append(f"- **{cat}**: {count} (threshold {threshold})")
            samples = list(self._samples.get(cat, []))
            if samples:
                lines.append("  ```")
                for s in samples[-2:]:
                    lines.append(f"  {s[:160]}")
                lines.append("  ```")
        lines.append("")
        lines.append(
            f"Full log: `/share/{self.log_subdir}/{self.name}/{self.name}.log`"
        )
        return "\n".join(lines)

    async def _maybe_notify(self, now: float) -> None:
        if not (self.notify_enabled
                and self.notifications_cfg.get("enabled", True)
                and self.notifier.available):
            return
        if now - self._last_notify_at < self._debounce_seconds():
            return
        breached = self._evaluate_thresholds()
        if not breached:
            return
        title = f"ESPHome Logger: {self.name} - {len(breached)} alert(s)"
        message = self._build_message(breached)
        notification_id = f"esphome_logger_{self.name}"
        ok = await self.notifier.create(title, message, notification_id)
        if ok:
            self._last_notify_at = now
            info(f"[{self.name}] notification posted: "
                 f"{', '.join(f'{c}={n}' for c,n,_ in breached)}")

    # --- log handler --------------------------------------------------------

    def _on_log_message(self, msg) -> None:
        try:
            payload = msg.message
            if isinstance(payload, bytes):
                text = payload.decode("utf-8", errors="replace")
            else:
                text = str(payload)
            text = ANSI_RE.sub("", text).rstrip()
            if not text:
                return
            assert self._file_logger is not None
            self._file_logger.info(text)

            cat = categorize(text)
            if cat is not None:
                now = time.time()
                self._events.append((now, cat))
                self._samples[cat].append(text)
                self._trim_window(now)
                asyncio.create_task(self._maybe_notify(now))
        except Exception:
            error(f"[{self.name}] error handling log message:\n"
                  f"{traceback.format_exc()}")

    async def _on_connect(self) -> None:
        info(f"[{self.name}] connected ({self.address})")
        assert self._client is not None
        try:
            self._client.subscribe_logs(
                self._on_log_message,
                log_level=self.log_level,
            )
        except APIConnectionError as exc:
            warn(f"[{self.name}] subscribe_logs failed: {exc}; disconnecting "
                 f"so reconnect logic retries")
            await self._client.disconnect()

    async def _on_disconnect(self, expected_disconnect: bool) -> None:
        kind = "clean" if expected_disconnect else "unexpected"
        info(f"[{self.name}] disconnected ({kind})")

    async def run(self) -> None:
        log_dir = SHARE_ROOT / self.log_subdir / self.name
        self._file_logger = build_file_logger(
            self.name, log_dir,
            self.rotation, self.retention, self.max_size_mb,
        )
        info(f"[{self.name}] writing to {log_dir}/{self.name}.log "
             f"(rotation={self.rotation}, retention={self.retention}, "
             f"level={self.log_level_name}, notify={self.notify_enabled})")

        self._client = APIClient(
            address=self.address,
            port=6053,
            password=self.password,
            noise_psk=self.noise_psk,
        )
        self._reconnect = ReconnectLogic(
            client=self._client,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            zeroconf_instance=None,
            name=self.name,
        )

        try:
            await self._reconnect.start()
            while not self._stopping:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        except Exception:
            error(f"[{self.name}] runner crashed:\n{traceback.format_exc()}")
        finally:
            self._stopping = True
            if self._reconnect is not None:
                try:
                    await self._reconnect.stop()
                except Exception:
                    pass
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
            info(f"[{self.name}] runner stopped")


# ----- daily summary scheduler ---------------------------------------------

def _seconds_until(target_hour: int, target_minute: int, now: dt.datetime) -> float:
    target = now.replace(hour=target_hour, minute=target_minute,
                         second=0, microsecond=0)
    if target <= now:
        target = target + dt.timedelta(days=1)
    return (target - now).total_seconds()


def _verdict_for_device(runner: "DeviceRunner") -> Tuple[str, Dict[str, int]]:
    """Return (verdict_word, counts) for a device based on its current
    sliding window and the global thresholds."""
    counts: Dict[str, int] = dict(Counter(cat for _, cat in runner._events))
    thresholds: Dict[str, int] = runner.notifications_cfg["thresholds"]
    critical_cats = {"error", "reboot", "brownout", "watchdog", "sensor_failure"}

    breached = [c for c, t in thresholds.items() if counts.get(c, 0) >= t]
    if any(c in critical_cats for c in breached):
        verdict = "investigate"
    elif breached:
        verdict = "minor"
    else:
        verdict = "clean"
    return verdict, counts


def _build_daily_summary(runners: List["DeviceRunner"],
                         window_hours: int,
                         started_at: float,
                         now: float) -> Tuple[str, str, str]:
    """Return (title, message_markdown, overall_verdict_word)."""
    age_hours = (now - started_at) / 3600.0
    short_window = age_hours < window_hours * 0.9  # less than ~90% of window

    icon = {"clean": "✅", "minor": "⚠️", "investigate": "🚨"}
    per_device: List[Tuple[str, str, Dict[str, int]]] = []
    overall = "clean"
    for r in runners:
        if not r.notify_enabled:
            continue
        v, counts = _verdict_for_device(r)
        per_device.append((r.name, v, counts))
        if v == "investigate":
            overall = "investigate"
        elif v == "minor" and overall != "investigate":
            overall = "minor"

    today = dt.date.today().isoformat()
    title = f"ESPHome Logger daily summary {today} {icon[overall]}"

    lines: List[str] = []
    lines.append(f"Window: last {window_hours}h"
                 + ("  (partial — add-on only running for "
                    f"{age_hours:.1f}h)" if short_window else ""))
    lines.append("")
    if not per_device:
        lines.append("No devices with notifications enabled.")
        return title, "\n".join(lines), overall

    for name, v, counts in per_device:
        lines.append(f"**{name}** {icon[v]}")
        if not counts:
            lines.append("- no notable events")
        else:
            for cat in CATEGORY_ORDER:
                c = counts.get(cat, 0)
                if c:
                    lines.append(f"- {cat}: {c}")
        lines.append("")
    return title, "\n".join(lines).rstrip(), overall


class DailySummaryScheduler:
    def __init__(self,
                 cfg: Dict[str, Any],
                 notifications_cfg: Dict[str, Any],
                 runners: List["DeviceRunner"],
                 notifier: HANotifier):
        self.cfg = cfg
        self.notifications_cfg = notifications_cfg
        self.runners = runners
        self.notifier = notifier
        self.started_at = time.time()
        self._stopping = False

    async def run(self) -> None:
        if not self.cfg.get("enabled", True):
            info("daily_summary: disabled")
            return
        if not self.notifier.available:
            warn("daily_summary: HA API token not available - cannot post; "
                 "scheduler will idle.")
            return
        hour = self.cfg["hour"]
        minute = self.cfg["minute"]
        info(f"daily_summary: will post at {hour:02d}:{minute:02d} local "
             f"every day")
        try:
            while not self._stopping:
                wait = _seconds_until(hour, minute, dt.datetime.now())
                info(f"daily_summary: next post in {wait/3600:.2f}h")
                # Wake every minute to be cancellable / clock-change safe
                slept = 0.0
                while slept < wait and not self._stopping:
                    chunk = min(60.0, wait - slept)
                    await asyncio.sleep(chunk)
                    slept += chunk
                if self._stopping:
                    break
                await self._post_summary()
        except asyncio.CancelledError:
            pass
        finally:
            info("daily_summary: scheduler stopped")

    async def _post_summary(self) -> None:
        try:
            window_h = int(self.notifications_cfg.get("window_hours", 24))
            title, message, verdict = _build_daily_summary(
                self.runners, window_h, self.started_at, time.time(),
            )
            if verdict == "clean" and not self.cfg.get("post_when_clean", True):
                info("daily_summary: all clean and post_when_clean is false; "
                     "skipping post")
                return
            notification_id = "esphome_logger_daily_summary"
            ok = await self.notifier.create(title, message, notification_id)
            if ok:
                info(f"daily_summary: posted ({verdict})")
            else:
                warn("daily_summary: post failed (see error above)")
        except Exception:
            error(f"daily_summary: builder/post crashed:\n"
                  f"{traceback.format_exc()}")


# ----- main -----------------------------------------------------------------

async def main_async() -> int:
    if _aioesphomeapi_err is not None:
        error(f"aioesphomeapi not importable: {_aioesphomeapi_err!r}")
        return 2

    try:
        opts = load_options()
    except Exception:
        error(f"failed to load {CONFIG_PATH}:\n{traceback.format_exc()}")
        return 2

    info("ESPHome Logger starting")
    redact_keys = {"devices", "notifications"}
    info("options: " + json.dumps(
        {k: ("***" if "psk" in k or "password" in k else v)
         for k, v in opts.items() if k not in redact_keys}
    ))

    log_subdir = str(opts.get("log_subdir") or "esphome-logs")
    defaults = {**DEFAULT_DEFAULTS, **(opts.get("defaults") or {})}
    notifications_cfg = normalize_notifications(opts)
    daily_summary_cfg = normalize_daily_summary(opts)
    info(f"notifications: enabled={notifications_cfg['enabled']}, "
         f"window={notifications_cfg['window_hours']}h, "
         f"debounce={notifications_cfg['debounce_minutes']}min, "
         f"thresholds={notifications_cfg['thresholds']}")
    info(f"daily_summary: enabled={daily_summary_cfg['enabled']}, "
         f"at={daily_summary_cfg['hour']:02d}:"
         f"{daily_summary_cfg['minute']:02d} local, "
         f"post_when_clean={daily_summary_cfg['post_when_clean']}")
    notifier = HANotifier()

    if opts.get("discover_on_start", True):
        try:
            await discover_esphome(float(opts.get("discovery_timeout") or 6))
        except Exception:
            warn(f"discovery failed:\n{traceback.format_exc()}")

    devices_cfg: List[Dict[str, Any]] = opts.get("devices") or []
    if not devices_cfg:
        info("no devices configured under 'devices:' - nothing to log. "
             "Add entries based on the discovery results above and restart.")
        while True:
            await asyncio.sleep(3600)

    seen: Dict[str, Dict[str, Any]] = {}
    for raw in devices_cfg:
        if not raw.get("name") or not raw.get("address"):
            warn(f"skipping device with missing name or address: {raw!r}")
            continue
        merged = merge_device(raw, defaults)
        if merged["name"] in seen:
            warn(f"duplicate device name '{merged['name']}'; keeping first")
            continue
        seen[merged["name"]] = merged

    if not seen:
        error("no valid devices after parsing 'devices:'")
        return 2

    runners = [
        DeviceRunner(cfg, log_subdir, notifications_cfg, notifier)
        for cfg in seen.values()
    ]
    info(f"starting {len(runners)} device runner(s): "
         f"{', '.join(r.name for r in runners)}")

    stop_event = asyncio.Event()

    def request_stop():
        if not stop_event.is_set():
            info("shutdown signal received")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(r.run(), name=f"runner-{r.name}")
        for r in runners
    ]
    summary_scheduler = DailySummaryScheduler(
        daily_summary_cfg, notifications_cfg, runners, notifier,
    )
    tasks.append(asyncio.create_task(summary_scheduler.run(),
                                     name="daily-summary"))

    await stop_event.wait()
    for r in runners:
        r._stopping = True
    summary_scheduler._stopping = True
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    info("goodbye")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

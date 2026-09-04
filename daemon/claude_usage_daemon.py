#!/usr/bin/env python3
"""Claude Usage Tracker Daemon (BLE) — macOS port of claude-usage-daemon.sh.

Polls Claude API rate-limit headers and writes a JSON payload to the
ESP32 "Clawdmeter" peripheral over a custom GATT service. Uses
bleak (CoreBluetooth backend on macOS).
"""

import asyncio
import calendar
import datetime
import getpass
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from bleak import BleakClient
from bleak.exc import BleakError

# ---------------------------------------------------------------------------
# Settings come from, in order of precedence:
#   1. a real environment variable (CLAWDMETER_*) — for a launchd/systemd unit
#   2. a `.env` file in the repo root — the usual place to edit them
#   3. ~/.config/claude-usage-monitor/config — written by the installer
#   4. the defaults below
# Nothing here is secret: the OAuth token is read from the Keychain (macOS) or
# ~/.claude/.credentials.json at runtime and never stored in this repo.
# ---------------------------------------------------------------------------
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
ENV_PREFIX = "CLAWDMETER_"

_env_cache: dict[str, str] | None = None
_env_mtime: float | None = None


def _load_env_file() -> dict[str, str]:
    """Parse the repo-root .env, re-reading it whenever it changes on disk.

    Deliberately quiet: the file is optional, so an unreadable one falls back
    to the other sources rather than failing the daemon.
    """
    global _env_cache, _env_mtime
    try:
        mtime = ENV_FILE.stat().st_mtime
    except OSError:
        _env_cache, _env_mtime = {}, None
        return _env_cache
    if _env_cache is not None and _env_mtime == mtime:
        return _env_cache

    values: dict[str, str] = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.split(" #", 1)[0].strip()          # trailing comment
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]                          # quoted value
            values[key.strip().upper()] = val
    except OSError:
        values = {}
    _env_cache, _env_mtime = values, mtime
    return values


def env_setting(name: str) -> str | None:
    """A setting from the process environment, else the repo's .env, else None."""
    key = ENV_PREFIX + name.upper()
    val = os.environ.get(key)
    if val is None:
        val = _load_env_file().get(key)
    val = val.strip() if val else ""
    return val or None


def env_number(name: str, default: float) -> float:
    """Numeric setting, falling back to `default` when unset or unparseable."""
    raw = env_setting(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


DEVICE_NAME = env_setting("device_name") or "Clawdmeter"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = env_number("poll_interval", 60)
TICK = env_number("tick", 5)
CONNECT_TIMEOUT = 20.0

# macOS: token lives in Keychain (service "Claude Code-credentials").
# Linux: token lives in ~/.claude/.credentials.json.
KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_CONFIG_DIR = Path.home() / ".claude"
SAVED_ADDR_FILE = Path.home() / ".config" / "claude-usage-monitor" / "ble-address"
CONFIG_FILE = Path.home() / ".config" / "claude-usage-monitor" / "config"

API_URL = "https://api.anthropic.com/v1/messages"
# Internal, beta-gated: the only source for per-model ("scoped") limits, which
# the /v1/messages rate-limit headers omit entirely. Treated as best-effort.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_BETA = "oauth-2025-04-20"
# This endpoint rate-limits hard (429), and a per-model *weekly* window barely
# moves, so it is cached rather than polled every cycle. On a refusal the last
# known value is reused: a limit that exists must not blink out of the display.
SCOPED_TTL = env_number("scoped_ttl", 600.0)      # between successful refreshes
SCOPED_RETRY = env_number("scoped_retry", 120.0)  # before retrying a refusal
MAX_PLANS = 3   # firmware holds this many pages; keeps the payload inside 512 B
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


class TokenExpired(Exception):
    """Raised by poll_api on a 401/403 — the access token is dead. The daemon never
    refreshes (pure free-ride: Claude Code owns refreshing), so the caller just
    signals "No data" to the device until the CLI re-seeds the token."""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        tok = data.get("accessToken")
        if isinstance(tok, str) and tok.strip():
            return tok
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict):
                tok = v.get("accessToken")
                if isinstance(tok, str) and tok.strip():
                    return tok
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _decode_keychain_blob(raw: str) -> str:
    """Transparently decode a hex-dumped Keychain secret back to text.

    ``security … -w`` prints the password as a continuous hex string whenever
    the stored bytes aren't cleanly printable (e.g. an embedded newline). A
    normal credentials blob is JSON, which is never valid hex (it contains
    '{', '"', …), so all-hex detection is unambiguous and safe.
    """
    s = raw.strip()
    if s and len(s) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", s):
        try:
            return bytes.fromhex(s).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return raw
    return raw


def _keychain_blob(service: str) -> str | None:
    """Raw credentials blob from a macOS Keychain service, decoded, or None.

    ``security … -w`` may hex-dump the stored secret (see _decode_keychain_blob),
    so decode before the caller parses it.
    """
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        # rc 44 = no such item: expected when probing a dir that has no entry.
        if e.returncode != 44:
            log(f"Keychain read failed (rc={e.returncode}): {e.stderr.strip()}")
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Keychain access error: {e}")
        return None
    return _decode_keychain_blob(out.stdout)


def _read_token_keychain(service: str = KEYCHAIN_SERVICE) -> str | None:
    """Read the OAuth access token from a macOS Keychain service, or None."""
    blob = _keychain_blob(service)
    return _extract_access_token(blob) if blob else None


def _blob_expiry(blob: str) -> int:
    """``expiresAt`` (ms) from a credentials blob; 0 when absent/unparseable."""
    try:
        obj = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return 0
    if isinstance(obj, dict):
        inner = obj.get("claudeAiOauth")
        src = inner if isinstance(inner, dict) else obj
        exp = src.get("expiresAt")
        if isinstance(exp, (int, float)):
            return int(exp)
    return 0


def _keychain_services_for(config_dir: Path) -> list[str]:
    """Keychain services that may hold this config dir's token.

    Claude Code keys each CLAUDE_CONFIG_DIR by sha256(abs path)[:8]. The default
    dir may additionally have the older unsuffixed entry, and on some installs
    that is the one the running CLI keeps refreshing — so both are candidates.
    """
    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
    services = [f"{KEYCHAIN_SERVICE}-{digest}"]
    if config_dir == DEFAULT_CONFIG_DIR:
        services.append(KEYCHAIN_SERVICE)
    return services


def _read_token_keychain_for(config_dir: Path) -> str | None:
    """Freshest Keychain token for one config dir, or None.

    Stale entries linger after a CLI upgrade or a re-login, so pick by latest
    ``expiresAt`` rather than by a fixed service order — whichever entry the
    running CLI refreshes is the live one.
    """
    best_token: str | None = None
    best_exp = -1
    for service in _keychain_services_for(config_dir):
        blob = _keychain_blob(service)
        if not blob:
            continue
        token = _extract_access_token(blob)
        if not token:
            continue
        exp = _blob_expiry(blob)
        if exp > best_exp:
            best_token, best_exp = token, exp
    return best_token


def read_config_dirs() -> list[Path]:
    """Claude config dirs to poll, from the `config_dirs` option (comma list).

    Defaults to [~/.claude] so existing single-plan setups are unchanged. ~ is
    expanded. Mirrors the Linux bash daemon's read_config_dirs.
    """
    raw = env_setting("config_dirs") or ""
    try:
        if not raw and CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "config_dirs":
                    raw = val.strip()
    except OSError:
        pass
    if not raw:
        return [DEFAULT_CONFIG_DIR]
    dirs = [Path(p.strip()).expanduser() for p in raw.split(",") if p.strip()]
    return dirs or [DEFAULT_CONFIG_DIR]


def read_token_for(config_dir: Path) -> str | None:
    """Read the OAuth token for one config dir.

    Linux: each dir keeps its own ``<dir>/.credentials.json``. macOS: tokens live
    in the Keychain with no file, under a per-dir service name, so every
    configured dir resolves — not just the default one.
    """
    cred = config_dir / ".credentials.json"
    try:
        if cred.exists():
            return _extract_access_token(cred.read_text())
    except OSError as e:
        log(f"Error reading credentials in {config_dir}: {e}")
    if sys.platform == "darwin":
        return _read_token_keychain_for(config_dir)
    return None


PLAN_LABELS = {
    "team": "Team",
    "pro": "Pro",
    "max": "Max",
    "enterprise": "Enterprise",
    "free": "Free",
}


def _credentials_blob(config_dir: Path) -> str | None:
    """The raw credentials blob for a config dir (file, else Keychain)."""
    cred = config_dir / ".credentials.json"
    try:
        if cred.exists():
            return cred.read_text()
    except OSError:
        pass
    if sys.platform == "darwin":
        for service in _keychain_services_for(config_dir):
            blob = _keychain_blob(service)
            if blob:
                return blob
    return None


def read_plan_label(config_dir: Path) -> str:
    """Display name for a config dir's plan, e.g. "Team" / "Pro".

    Read from ``subscriptionType`` in the stored credentials, which is present
    even when the access token has expired. Falls back to the dir's own suffix
    (``~/.claude-personal`` -> "Personal") so a page is never unlabelled.
    """
    blob = _credentials_blob(config_dir)
    if blob:
        try:
            obj = json.loads(blob)
            src = obj.get("claudeAiOauth") if isinstance(obj, dict) else None
            src = src if isinstance(src, dict) else obj
            sub = str(src.get("subscriptionType") or "").lower()
            if sub in PLAN_LABELS:
                return PLAN_LABELS[sub]
            if sub:
                return sub.title()[:11]
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    name = config_dir.name
    suffix = name[len(".claude-"):] if name.startswith(".claude-") else ""
    return suffix.title()[:11] if suffix else "Claude"


def load_cached_address() -> str | None:
    if not SAVED_ADDR_FILE.exists():
        return None
    addr = SAVED_ADDR_FILE.read_text().strip()
    # Accept both Linux MAC (AA:BB:CC:DD:EE:FF) and macOS CoreBluetooth UUID
    # (E621E1F8-C36C-495A-93FC-0C247A3E6E5F).
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", addr) or re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", addr
    ):
        return addr
    log("Cached address malformed, discarding")
    SAVED_ADDR_FILE.unlink(missing_ok=True)
    return None


# --- macOS: recover a device the OS already holds as an HID keyboard --------
#
# The firmware advertises as a BLE HID keyboard so its buttons type into the
# Mac. macOS auto-connects to that HID, and CoreBluetooth then EXCLUDES the
# peripheral from BleakScanner.discover() results (already-connected devices
# never appear in scans). bleak's connect-by-address path also scans
# internally, so a cached address can't help either. The documented escape
# hatch is retrieveConnectedPeripheralsWithServices_, which returns
# peripherals the system is already connected to. We wrap the result in a
# BLEDevice carrying the live (peripheral, manager) details so BleakClient
# connects to it directly without scanning. CoreBluetooth shares the single
# physical link, so this rides the existing HID connection — the keyboard
# keeps working.
_cb_manager = None  # reused CentralManagerDelegate (CoreBluetooth)


async def _get_cb_manager():
    """Lazily create and ready a shared CoreBluetooth central manager."""
    global _cb_manager
    if _cb_manager is None:
        from bleak.backends.corebluetooth.CentralManagerDelegate import (
            CentralManagerDelegate,
        )

        mgr = CentralManagerDelegate()
        await mgr.wait_until_ready()  # raises if Bluetooth is unauthorized/off
        _cb_manager = mgr
    return _cb_manager


async def retrieve_connected_macos(skip_addr: str | None = None):
    """Return a BLEDevice for a system-connected 'Clawdmeter', or None.

    Two-step lookup, strongest signal first:

    1. Peripherals connected under our CUSTOM service UUID. Membership in
       that service is unambiguous (no other device exposes it), so we accept
       by service alone — the peripheral's name can be None on macOS.
    2. Fall back to the generic HID service 0x1812, but ONLY trust a
       peripheral whose name matches DEVICE_NAME. 0x1812 also matches
       unrelated keyboards/mice, so picking blindly here could grab the
       wrong device.

    ``skip_addr`` skips a peripheral whose UUID just failed to connect, so a
    stale CoreBluetooth handle can't trap us into never trying a fresh scan.
    """
    from CoreBluetooth import CBUUID
    from bleak.backends.device import BLEDevice

    try:
        manager = await _get_cb_manager()
    except Exception as e:  # BleakBluetoothNotAvailableError etc.
        log(f"CoreBluetooth unavailable: {e}")
        return None

    cm = manager.central_manager

    def _wrap(p):
        addr = p.identifier().UUIDString()
        log(f"Found system-connected peripheral: {p.name()!r} [{addr}]")
        return BLEDevice(addr, p.name(), (p, manager))

    def _ok(p) -> bool:
        return not (skip_addr and p.identifier().UUIDString() == skip_addr)

    # 1. Custom service — accept by service membership alone.
    custom = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_(SERVICE_UUID)]
    )
    for p in custom or []:
        if _ok(p):
            return _wrap(p)

    # 2. Generic HID service — require an exact name match.
    hid = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_("1812")]
    )
    for p in hid or []:
        if _ok(p) and p.name() == DEVICE_NAME:
            return _wrap(p)

    return None


async def discover_target(skip_addr: str | None = None):
    """Return a connectable target, or None.

    The daemon only ever targets the device this system already holds — it
    never scans for a nearby device by name, so it can't grab a stranger's or
    the wrong nearby unit. On macOS that's the system-connected peripheral (the
    firmware advertises as an HID keyboard, so once paired the OS auto-connects
    and holds it — HID-grabbed devices are invisible to scans anyway). On other
    platforms it's a previously-pinned address in the cache file. If the device
    isn't held/pinned, we log and wait rather than scanning. ``skip_addr`` skips
    a peripheral whose handle just failed to connect.
    """
    if sys.platform == "darwin":
        dev = await retrieve_connected_macos(skip_addr=skip_addr)
        if dev is None:
            log("Device not held by OS; waiting (not scanning by name)")
        return dev

    address = load_cached_address()
    if not address:
        log("No pinned address cached; waiting (not scanning by name)")
    return address


def read_chime_setting() -> str:
    """Read the `chime` option from the config file. One of: off|on.

    Defaults to "off" (the device stays silent) so existing setups are
    unaffected until the user opts in.
    """
    env = env_setting("chime")
    if env:
        return "on" if env.strip().lower() in ("on", "1", "true", "yes") else "off"
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "chime":
                    val = val.strip().lower()
                    if val in ("off", "on"):
                        return val
    except OSError:
        pass
    return "off"


def read_clock_setting() -> str:
    """Read the `clock` option from the config file. One of: off|auto|12|24.

    Defaults to "off" (no clock; the device keeps showing "Usage") so existing
    setups are unaffected until the user opts in.
    """
    env = env_setting("clock")
    if env and env.strip().lower() in ("off", "auto", "12", "24"):
        return env.strip().lower()
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "clock":
                    val = val.strip().lower()
                    if val in ("off", "auto", "12", "24"):
                        return val
    except OSError:
        pass
    return "off"


def add_chime_field(payload: dict) -> None:
    """Add "c":1 to the payload when the config opts in, so the firmware may
    sound the session-reset chime. Omitted entirely when chime is off."""
    if read_chime_setting() == "on":
        payload["c"] = 1


def detect_hour_format() -> int:
    """Best-effort 12h/24h detection for the host. Returns 12 or 24 (default 24)."""
    # macOS: the explicit System Settings toggle lives in NSGlobalDomain.
    for key, result in (("AppleICUForce24HourTime", 24), ("AppleICUForce12HourTime", 12)):
        try:
            out = subprocess.run(["defaults", "read", "-g", key],
                                 capture_output=True, text=True, timeout=3)
            if out.stdout.strip() == "1":
                return result
        except (OSError, subprocess.SubprocessError):
            pass
    # Fallback to the C locale's time format (may be C/24h under launchd).
    try:
        import locale
        locale.setlocale(locale.LC_TIME, "")
        fmt = locale.nl_langinfo(locale.T_FMT)
        if "%p" in fmt or "%r" in fmt or "%I" in fmt:
            return 12
    except (ImportError, locale.Error, AttributeError):
        pass
    return 24


def add_clock_fields(payload: dict) -> None:
    """Add wall-clock fields to the payload when the config opts in.

    "t"  = local wall-clock epoch (UTC epoch shifted by the tz offset) so the
           device can show the time without an RTC.
    "tf" = 12 or 24, the hour format the device should render.
    """
    clock = read_clock_setting()
    if clock == "off":
        return
    tf = 24 if clock == "24" else 12 if clock == "12" else detect_hour_format()
    payload["t"] = int(time.time()) + time.localtime().tm_gmtoff
    payload["tf"] = tf


async def poll_api(token: str) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as e:
        log(f"API call failed: {e}")
        return None
    if resp.status_code in (401, 403):
        log(f"API HTTP {resp.status_code} (token expired/invalid)")
        raise TokenExpired()
    if resp.status_code >= 400:
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    # Pro/Max accounts expose 5h/7d windows; Enterprise/overage use a single
    # spending-limit model reported via overage-utilization.
    if resp.headers.get("anthropic-ratelimit-unified-5h-utilization"):
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
            "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
            "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
            "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
            "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
            "acct": "pro",
            "ok": True,
        }
        scoped = await fetch_scoped_limit(token)
        if scoped:
            payload.update(scoped)
    else:
        reset_ts = hdr("anthropic-ratelimit-unified-overage-reset")
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-overage-utilization")),
            "sr": reset_minutes(reset_ts),
            "w": 0,
            "wr": 0,
            "st": hdr("anthropic-ratelimit-unified-status", "unknown"),
            "acct": "ent",
            **_billing_period_info(now, reset_ts),
            "ok": True,
        }
    add_chime_field(payload)   # adds "c":1 iff the config opts in
    add_clock_fields(payload)   # adds "t" + "tf" iff the config opts in
    return payload


def _iso_reset_minutes(ts: str | None) -> int | None:
    """Minutes until an ISO-8601 timestamp, or None when unparseable."""
    if not ts:
        return None
    try:
        when = datetime.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    mins = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60.0
    return int(round(mins)) if mins > 0 else 0


_SCOPED_CACHE: dict[str, dict] = {}   # token -> {"value": ..., "next_try": ts}


async def fetch_scoped_limit(token: str) -> dict | None:
    """The account's binding per-model limit, e.g. Fable's weekly window.

    The /v1/messages rate-limit headers carry only the 5h and 7d windows, so a
    scoped weekly limit — which on some plans is the limit actually binding you
    — is invisible there. This endpoint reports every limit, per-model included.

    Cached for SCOPED_TTL, and on any failure the previous value is served
    again: the endpoint answers 429 often enough that fetching per cycle made
    the row appear and disappear between polls. A 200 that reports no scoped
    limit *does* clear it — that is a real absence, not a refusal.

    Returns {"x": pct, "xr": reset_mins, "xn": model name} or None.
    """
    now = time.time()
    cached = _SCOPED_CACHE.get(token)
    if cached and now < cached["next_try"]:
        return cached["value"]

    def keep(reason: str) -> dict | None:
        """Serve the last known value and schedule a retry."""
        prev = cached["value"] if cached else None
        _SCOPED_CACHE[token] = {"value": prev, "next_try": now + SCOPED_RETRY}
        log(f"Scoped-limit fetch {reason}; "
            + ("keeping last known value" if prev else "no value yet"))
        return prev

    headers = {"authorization": f"Bearer {token}", "anthropic-beta": USAGE_BETA}
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(USAGE_URL, headers=headers)
    except httpx.HTTPError as e:
        return keep(f"failed ({e})")
    if resp.status_code != 200:
        return keep(f"HTTP {resp.status_code}")
    try:
        limits = resp.json().get("limits") or []
    except (ValueError, AttributeError, TypeError) as e:
        return keep(f"unparseable ({e})")

    best: tuple[int, str, str | None] | None = None
    for lim in limits:
        if not isinstance(lim, dict):
            continue
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        if not model:
            continue
        try:
            pct = int(round(float(lim.get("percent") or 0)))
        except (TypeError, ValueError):
            continue
        if best is None or pct > best[0]:   # most-binding scoped limit wins
            best = (pct, str(model), lim.get("resets_at"))

    out: dict | None = None
    if best is not None:
        pct, model, resets_at = best
        out = {"x": max(0, min(100, pct)), "xn": model[:9]}
        mins = _iso_reset_minutes(resets_at)
        if mins is not None:
            out["xr"] = mins
    _SCOPED_CACHE[token] = {"value": out, "next_try": now + SCOPED_TTL}
    return out


def _billing_period_info(now: float, reset_ts: str) -> dict:
    """Fraction of billing period elapsed (tp, 0-100) and period length in days (pd).

    Billing periods are assumed calendar-monthly: period_end is the reset
    timestamp, period_start is the same day/time one calendar month earlier.

    The rate-limit headers expose only the reset timestamp, not the period
    length, so the monthly window is an assumption — but a documented one:
    Enterprise spend-limit `period` "the only value today is monthly"
    (Claude Enterprise Admin API reference). The doc notes period is an open
    string that may gain other values later; revisit this if so.
    """
    try:
        period_end = float(reset_ts)
    except ValueError:
        return {"tp": 0, "pd": 30}
    if period_end <= 0:
        # reset_ts defaults to "0" when the overage-reset header is absent.
        # fromtimestamp(0) is 1970; stepping a month back lands in 1969, and
        # datetime.timestamp() raises OSError for pre-1970 dates on Windows.
        # Benign on macOS/Linux, but guard here too to keep the daemons parallel.
        return {"tp": 0, "pd": 30}
    dt_end = datetime.datetime.fromtimestamp(period_end)
    prev_month = dt_end.month - 1 or 12
    prev_year = dt_end.year if dt_end.month > 1 else dt_end.year - 1
    prev_day = min(dt_end.day, calendar.monthrange(prev_year, prev_month)[1])
    dt_start = dt_end.replace(year=prev_year, month=prev_month, day=prev_day)
    period_start = dt_start.timestamp()
    period_len = period_end - period_start
    if period_len <= 0:
        return {"tp": 0, "pd": 30}
    pct_val = (now - period_start) / period_len * 100
    total_days = int(round(period_len / 86400))
    rd = f"{dt_end.strftime('%b')} {dt_end.day}"
    return {
        "tp": max(0, min(100, int(round(pct_val)))),
        "pd": total_days,
        "rd": rd,
    }


class PlanSelector:
    """Decide which config dir's plan is "active" across polls.

    "Active" = the plan whose session % rose most recently (recent API activity).
    A rise stamps a monotonic poll counter, so the choice is sticky and a window
    reset (a drop to 0) isn't mistaken for use. Before any rise is seen (startup)
    the highest current session % wins. Mirrors the Linux bash daemon.
    """

    def __init__(self) -> None:
        self.prev_s: dict[Path, int] = {}
        self.last_active: dict[Path, int] = {}
        self.seq = 0

    def choose(self, sessions: dict[Path, int]) -> Path:
        """Update state from this cycle's {dir: session_pct} and return the active dir."""
        self.seq += 1
        for d, s in sessions.items():
            if d in self.prev_s and s > self.prev_s[d]:
                self.last_active[d] = self.seq
            self.prev_s[d] = s
        # Most recent activity wins; ties (and the startup case) break by highest %.
        return max(sessions, key=lambda d: (self.last_active.get(d, 0), sessions[d]))


# Module-level so the active-plan state survives reconnects.
_SELECTOR = PlanSelector()


async def poll_all() -> dict[Path, dict]:
    """Poll every configured config dir once, in config order.

    Returns ``{dir: {"state": ..., "payload": ...}}`` where state is:
      "ok"       — the token authenticated (payload may still be None if the
                   call itself failed this cycle; the dir counts as live)
      "expired"  — a 401: only Claude Code can re-seed that token
      "notoken"  — nothing stored at all (logged out)

    Callers need the expired/notoken split: an expired plan still has a label
    and deserves a page saying so, a logged-out one has nothing to show.
    """
    results: dict[Path, dict] = {}
    for d in read_config_dirs()[:MAX_PLANS]:
        token = read_token_for(d)
        if not token:
            log(f"No token in {d}; skipping")
            results[d] = {"state": "notoken", "payload": None}
            continue
        try:
            payload = await poll_api(token)
        except TokenExpired:
            log(f"Token in {d} expired/invalid; skipping")
            results[d] = {"state": "expired", "payload": None}
            continue
        results[d] = {"state": "ok", "payload": payload}
    return results


async def poll_active(selector: PlanSelector = _SELECTOR) -> tuple[dict | None, bool]:
    """Poll every configured config dir; return ``(active_payload, all_dead)``.

    ``active_payload`` — the active plan's payload dict, or None when no dir
    yields a usable payload this cycle. A single configured dir (the default)
    collapses to exactly the old single-poll path.

    ``all_dead`` — True when *every* configured dir lacked a usable token this
    cycle (file/Keychain empty, or a 401/expired token), so the caller can
    signal "No data". False when at least one token authenticated — including a
    transient non-auth poll failure worth retrying silently rather than idling.

    Pure free-ride: a 401 (TokenExpired) means that dir's token has expired and
    only Claude Code (its owner) can re-seed it — we never refresh it ourselves.
    """
    results = await poll_all()
    payloads = {d: r["payload"] for d, r in results.items() if r["payload"] is not None}
    any_live = any(r["state"] == "ok" for r in results.values())
    if not payloads:
        return None, not any_live
    sessions = {d: int(p.get("s", 0) or 0) for d, p in payloads.items()}
    active = selector.choose(sessions)
    if len(results) > 1:
        log(f"Active plan: {active} (s={sessions[active]})")
    return payloads[active], False


# Keys that live once at the top level of a multi-plan payload, not per plan.
_TOP_LEVEL_KEYS = ("ok", "c", "t", "tf")


async def build_multi_payload(
    selector: PlanSelector = _SELECTOR,
) -> tuple[dict | None, bool]:
    """The multi-plan payload the firmware renders as swipeable pages.

    Shape: ``{"p": [{plan}, ...], "ap": <active index>, "ok": true}``. Each plan
    carries a label ("n") plus either its numbers or ``"e": 1`` when its token
    has expired — an expired plan keeps its page and says so, rather than
    vanishing and renumbering the pages under your finger.

    Returns the same ``(payload, all_dead)`` contract as :func:`poll_active`, so
    a transient failure still yields ``(None, False)`` and the device holds its
    last reading instead of flickering.
    """
    results = await poll_all()
    if any(r["state"] == "ok" and r["payload"] is None for r in results.values()):
        return None, False   # transient: keep what the device already shows

    plans: list[dict] = []
    active_dirs: dict[Path, int] = {}
    sessions: dict[Path, int] = {}
    for d, r in results.items():
        if r["state"] == "notoken":
            continue
        entry: dict = {"n": read_plan_label(d)}
        if r["payload"] is None:
            entry["e"] = 1
        else:
            entry.update({k: v for k, v in r["payload"].items()
                          if k not in _TOP_LEVEL_KEYS})
            sessions[d] = int(r["payload"].get("s", 0) or 0)
        active_dirs[d] = len(plans)
        plans.append(entry)

    if not plans:
        return None, True
    payload: dict = {"p": plans, "ok": True}
    if sessions:
        active = selector.choose(sessions)
        payload["ap"] = active_dirs[active]
        if len(plans) > 1:
            log(f"Active plan: {active} (s={sessions[active]})")
    add_chime_field(payload)
    add_clock_fields(payload)
    return payload, False


async def poll_active_payload(selector: PlanSelector = _SELECTOR) -> dict | None:
    """The active plan's payload, or None when no dir yields one this cycle.

    Thin wrapper over :func:`poll_active` for callers that don't need the
    all-dead flag.
    """
    payload, _dead = await poll_active(selector)
    return payload


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        # start_notify awaits CoreBluetooth's CCCD-write confirmation, which
        # never arrives if the peripheral doesn't ACK the subscribe (a
        # half-open link after the OS auto-connects the HID). Unbounded, that
        # await wedges the whole daemon between "Connected" and the first poll
        # — the device then shows nothing until a manual restart. Bound it: the
        # subscription is only an optional device-initiated refresh nudge (we
        # poll every POLL_INTERVAL regardless), so on timeout we proceed.
        try:
            await asyncio.wait_for(
                self.client.start_notify(REQ_CHAR_UUID, self._on_refresh),
                timeout=10,
            )
        except (BleakError, ValueError) as e:
            log(f"Refresh subscription unavailable: {e}")
        except asyncio.TimeoutError:
            log("Refresh subscription timed out; polling without it")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except BleakError as e:
            log(f"Write failed: {e}")
            return False


def _is_encryption_error(exc: BaseException) -> bool:
    """True if a connect error is a macOS bonding/encryption mismatch.

    macOS reports a stale bond as CBErrorDomain Code=15 ("Failed to encrypt
    the connection..."). Match on the message text so we don't depend on how
    bleak wraps the underlying CoreBluetooth error.
    """
    s = str(exc).lower()
    return "code=15" in s or "encrypt" in s


# blueutil talks to Bluetooth via IOBluetooth, which on recent macOS needs its
# OWN Bluetooth TCC grant (separate from the daemon's CoreBluetooth grant).
# Without it, blueutil *hangs* instead of erroring — so every call is bounded
# by a timeout and a hang is reported as a permission problem, not a crash.
BLUEUTIL_TIMEOUT = 8


def _blueutil(*args: str) -> str | None:
    """Run `blueutil <args>`, returning stdout, or None on failure/timeout.

    A timeout almost always means blueutil lacks Bluetooth permission (it
    blocks rather than failing), so we surface that cause explicitly.
    """
    try:
        return subprocess.run(
            ["blueutil", *args],
            capture_output=True, text=True,
            timeout=BLUEUTIL_TIMEOUT, check=True,
        ).stdout
    except subprocess.TimeoutExpired:
        log(f"blueutil {' '.join(args)} timed out — it likely lacks Bluetooth "
            "permission. Grant it under System Settings > Privacy & Security > "
            "Bluetooth (run `blueutil --paired` once from Terminal to prompt).")
        return None
    except (subprocess.SubprocessError, OSError) as e:
        log(f"blueutil {' '.join(args)} failed: {e}")
        return None


def unpair_macos() -> bool:
    """Forget a stale macOS bond for DEVICE_NAME so the device can re-pair.

    A Code=15 "failed to encrypt" connect error means macOS holds bonding
    keys that no longer match the ESP32's (e.g. after a firmware reflash or
    the on-device bond-clear gesture). The firmware pairs "just works" (no
    MITM), so once the stale bond is gone the next connect re-bonds silently
    with no GUI prompt.

    CoreBluetooth exposes no unpair API, so we shell out to `blueutil`. The
    daemon only knows the peripheral's CoreBluetooth UUID, not the BD_ADDR
    that blueutil needs, so we map by name via `blueutil --paired`. Returns
    True if a bond was removed. Mirrors the Linux daemon's `bluetoothctl
    remove` self-heal.
    """
    if not shutil.which("blueutil"):
        log("Stale bond detected but `blueutil` is not installed; cannot "
            "auto-recover. Run `brew install blueutil`, or forget "
            f"'{DEVICE_NAME}' in System Settings > Bluetooth and reconnect.")
        return False

    out = _blueutil("--paired")
    if out is None:
        return False

    # Each line looks like:
    #   address: 28-84-85-55-5c-3d, ... name: "Clawdmeter", ...
    addr = None
    for line in out.splitlines():
        if f'name: "{DEVICE_NAME}"' in line:
            m = re.search(r"address:\s*([0-9a-fA-F:-]+)", line)
            if m:
                addr = m.group(1)
                break
    if not addr:
        log(f"No paired '{DEVICE_NAME}' found to unpair (already forgotten?)")
        return False

    if _blueutil("--unpair", addr) is None:
        return False
    log(f"Unpaired stale bond for '{DEVICE_NAME}' [{addr}]; re-pairing on "
        "next connect")
    return True


async def connect_and_run(target, stop_event: asyncio.Event) -> bool:
    """Connect to a target and poll until disconnected or stopped.

    ``target`` is either an address string (Linux) or a BLEDevice carrying
    live CoreBluetooth details (macOS). Returns True if the connection was
    used successfully (so the caller keeps the cached address), False if the
    connection failed and the cache should be invalidated.
    """
    display = target if isinstance(target, str) else target.address
    log(f"Connecting to {display}...")
    client = BleakClient(target)
    try:
        # Bound the connect the same way #84 bounded the refresh subscribe.
        # On macOS the OS auto-connects the firmware's HID link, so
        # CoreBluetooth can hand us a half-open peripheral whose GATT connect
        # handshake never completes. BleakClient's own timeout governs
        # discovery, not connectPeripheral, so an unbounded await here wedges
        # the single-threaded daemon forever at "Connecting..." (observed ~13h,
        # device stuck on stale data). wait_for raises TimeoutError, which the
        # handler below already treats as a connection failure -> drop the
        # cached address and rescan.
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
    except (BleakError, asyncio.TimeoutError) as e:
        log(f"Connection failed: {e}")
        if sys.platform == "darwin" and _is_encryption_error(e):
            log("Encryption failed — likely a stale macOS bond; self-healing")
            unpair_macos()
        return False

    if not client.is_connected:
        log("Connection failed (no error but not connected)")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_poll = 0.0
    used_successfully = False
    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            if session.refresh_requested.is_set() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                # Pure free-ride: read whatever access token(s) Claude Code
                # currently holds across the configured config dirs and NEVER
                # refresh them ourselves. Claude Code (the token's owner) does all
                # refreshing; refreshing here would race its rotation and feed the
                # OAuth endpoint's rate limit (429). When no dir has a usable token
                # we signal "No data" so the device idles instead of holding stale
                # numbers until the CLI re-seeds it.
                payload, dead = await build_multi_payload()
                if payload is not None:
                    if await session.write_payload(payload):
                        last_poll = time.time()
                        used_successfully = True
                elif dead:
                    # No live token in any config dir (missing, or a 401/expired
                    # token) -> show "No data" now instead of stale numbers. Guard
                    # last_poll on the write result (like the data path) so a
                    # failed beat retries next tick instead of throttling what may
                    # be a healthy link for a full POLL_INTERVAL.
                    log("No usable token; signalling no-data to device — run "
                        "`claude login` or use the CLI to let Claude Code renew it")
                    if await session.write_payload({"ok": False}):
                        last_poll = time.time()
                else:
                    # Transient poll failure (a live token that didn't answer this
                    # cycle) -> stay silent and retry next tick.
                    log("No usable config dir this cycle")

            try:
                await asyncio.wait_for(session.refresh_requested.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await client.disconnect()
        except BleakError:
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)

    log("=== Claude Usage Tracker Daemon (BLE, macOS) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    backoff = 1
    skip_addr: str | None = None  # macOS: a peripheral to skip for one cycle
    while not stop_event.is_set():
        # Apply any pending skip exactly once, then clear it so the next
        # cycle re-tries retrieveConnected (the device may have recovered).
        target = await discover_target(skip_addr=skip_addr)
        skip_addr = None
        if not target:
            log(f"Device not found, retrying in {backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
            continue

        addr = target if isinstance(target, str) else target.address
        ok = await connect_and_run(target, stop_event)
        if not ok:
            if sys.platform == "darwin":
                # No string cache to drop; instead skip this stale handle on
                # the next retrieveConnected so the scan fallback is reachable.
                skip_addr = addr
            else:
                log("Invalidating cached address")
                SAVED_ADDR_FILE.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)

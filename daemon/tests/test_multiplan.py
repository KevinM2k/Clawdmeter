#!/usr/bin/env python3
"""Unit tests for the multi-plan payload and the scoped (per-model) limit.

Covers read_plan_label, _iso_reset_minutes, fetch_scoped_limit, poll_all and
build_multi_payload — the pieces behind the firmware's swipeable plan pages.

Run: python -m pytest daemon/tests/test_multiplan.py -x -q
"""
import asyncio
import datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import daemon.claude_usage_daemon as mod
from daemon.claude_usage_daemon import PlanSelector


@pytest.fixture(autouse=True)
def _clear_scoped_cache():
    """The scoped limit is cached per token; tests must not inherit each other's."""
    mod._SCOPED_CACHE.clear()
    yield
    mod._SCOPED_CACHE.clear()


def _run(coro):
    return asyncio.run(coro)


def _creds(dir_path: Path, sub: str, token: str = "TOK") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": token, "subscriptionType": sub}}))
    return dir_path


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, *_a, **_kw):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch_usage_get(monkeypatch, response):
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **_kw: _FakeClient(response))


# ---------------------------------------------------------------------------
# read_plan_label — the page's name, readable even with an expired token
# ---------------------------------------------------------------------------

def test_plan_label_from_subscription_type(tmp_path):
    assert mod.read_plan_label(_creds(tmp_path / "work", "team")) == "Team"
    assert mod.read_plan_label(_creds(tmp_path / "home", "pro")) == "Pro"


def test_plan_label_titlecases_unknown_subscription(tmp_path):
    assert mod.read_plan_label(_creds(tmp_path / "x", "startup")) == "Startup"


def test_plan_label_falls_back_to_dir_suffix(tmp_path):
    """No credentials at all -> name it after the dir, never leave it blank."""
    d = tmp_path / ".claude-personal"
    d.mkdir()
    monkey = mod.sys.platform
    assert monkey is not None
    with patch.object(mod, "_credentials_blob", return_value=None):
        assert mod.read_plan_label(d) == "Personal"
        assert mod.read_plan_label(tmp_path / ".claude") == "Claude"


def test_plan_label_survives_garbage_credentials(tmp_path):
    d = tmp_path / ".claude-side"
    d.mkdir()
    (d / ".credentials.json").write_text("not json at all")
    assert mod.read_plan_label(d) == "Side"


# ---------------------------------------------------------------------------
# _iso_reset_minutes
# ---------------------------------------------------------------------------

def test_iso_reset_minutes_future_and_past():
    now = datetime.datetime.now(datetime.timezone.utc)
    soon = (now + datetime.timedelta(minutes=90)).isoformat()
    gone = (now - datetime.timedelta(minutes=5)).isoformat()
    assert mod._iso_reset_minutes(soon) == 90
    assert mod._iso_reset_minutes(gone) == 0   # never negative


def test_iso_reset_minutes_rejects_garbage():
    assert mod._iso_reset_minutes(None) is None
    assert mod._iso_reset_minutes("") is None
    assert mod._iso_reset_minutes("tomorrow-ish") is None


# ---------------------------------------------------------------------------
# fetch_scoped_limit — the per-model limit the rate-limit headers omit
# ---------------------------------------------------------------------------

_TOK = "tok-abc"


def test_scoped_limit_picks_model_scoped_entry(monkeypatch):
    now = datetime.datetime.now(datetime.timezone.utc)
    _patch_usage_get(monkeypatch, _FakeResponse(200, {"limits": [
        {"kind": "session", "percent": 1, "scope": None},
        {"kind": "weekly_all", "percent": 34, "scope": None},
        {"kind": "weekly_scoped", "percent": 37,
         "resets_at": (now + datetime.timedelta(minutes=120)).isoformat(),
         "scope": {"model": {"display_name": "Fable"}}},
    ]}))
    assert _run(mod.fetch_scoped_limit(_TOK)) == {"x": 37, "xn": "Fable", "xr": 120}


def test_scoped_limit_takes_the_most_binding_of_several(monkeypatch):
    _patch_usage_get(monkeypatch, _FakeResponse(200, {"limits": [
        {"percent": 12, "scope": {"model": {"display_name": "Sonnet"}}},
        {"percent": 55, "scope": {"model": {"display_name": "Fable"}}},
    ]}))
    out = _run(mod.fetch_scoped_limit(_TOK))
    assert out["x"] == 55 and out["xn"] == "Fable"


def test_scoped_limit_none_when_no_model_scope(monkeypatch):
    """A Pro plan reports no per-model limit — no scoped bar should appear."""
    _patch_usage_get(monkeypatch, _FakeResponse(200, {"limits": [
        {"kind": "session", "percent": 5, "scope": None},
    ]}))
    assert _run(mod.fetch_scoped_limit(_TOK)) is None


def test_scoped_limit_none_on_error_status(monkeypatch):
    _patch_usage_get(monkeypatch, _FakeResponse(404, {}))
    assert _run(mod.fetch_scoped_limit(_TOK)) is None


def test_scoped_limit_survives_transport_failure(monkeypatch):
    """The endpoint is internal and beta-gated: failure must never be fatal."""
    _patch_usage_get(monkeypatch, mod.httpx.ConnectError("boom"))
    assert _run(mod.fetch_scoped_limit(_TOK)) is None


def test_scoped_limit_survives_bad_json(monkeypatch):
    _patch_usage_get(monkeypatch, _FakeResponse(200, ValueError("not json")))
    assert _run(mod.fetch_scoped_limit(_TOK)) is None


def test_scoped_limit_omits_reset_when_unparseable(monkeypatch):
    _patch_usage_get(monkeypatch, _FakeResponse(200, {"limits": [
        {"percent": 20, "resets_at": None,
         "scope": {"model": {"display_name": "Fable"}}},
    ]}))
    assert _run(mod.fetch_scoped_limit(_TOK)) == {"x": 20, "xn": "Fable"}


def test_scoped_limit_is_cached_between_calls(monkeypatch):
    """The endpoint 429s under per-cycle polling, so a hit must not refetch."""
    calls = {"n": 0}

    class CountingClient(_FakeClient):
        async def get(self, *a, **kw):
            calls["n"] += 1
            return await super().get(*a, **kw)

    resp = _FakeResponse(200, {"limits": [
        {"percent": 37, "scope": {"model": {"display_name": "Fable"}}},
    ]})
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **_kw: CountingClient(resp))
    first = _run(mod.fetch_scoped_limit(_TOK))
    second = _run(mod.fetch_scoped_limit(_TOK))
    assert first == second == {"x": 37, "xn": "Fable"}
    assert calls["n"] == 1


def test_scoped_limit_survives_a_429_by_reusing_last_value(monkeypatch):
    """A refusal must not blink an existing row out of the display."""
    ok = _FakeResponse(200, {"limits": [
        {"percent": 37, "scope": {"model": {"display_name": "Fable"}}},
    ]})
    _patch_usage_get(monkeypatch, ok)
    assert _run(mod.fetch_scoped_limit(_TOK))["x"] == 37

    mod._SCOPED_CACHE[_TOK]["next_try"] = 0    # force a refresh attempt
    _patch_usage_get(monkeypatch, _FakeResponse(429, {"error": "rate_limit_error"}))
    assert _run(mod.fetch_scoped_limit(_TOK)) == {"x": 37, "xn": "Fable"}


def test_scoped_limit_cleared_when_200_reports_none(monkeypatch):
    """A real absence differs from a refusal and must clear the cached value."""
    _patch_usage_get(monkeypatch, _FakeResponse(200, {"limits": [
        {"percent": 37, "scope": {"model": {"display_name": "Fable"}}},
    ]}))
    assert _run(mod.fetch_scoped_limit(_TOK))["x"] == 37

    mod._SCOPED_CACHE[_TOK]["next_try"] = 0
    _patch_usage_get(monkeypatch, _FakeResponse(200, {"limits": [
        {"kind": "session", "percent": 5, "scope": None},
    ]}))
    assert _run(mod.fetch_scoped_limit(_TOK)) is None


def test_scoped_limit_retries_sooner_after_a_refusal(monkeypatch):
    _patch_usage_get(monkeypatch, _FakeResponse(429, {}))
    _run(mod.fetch_scoped_limit(_TOK))
    wait = mod._SCOPED_CACHE[_TOK]["next_try"] - mod.time.time()
    assert 0 < wait <= mod.SCOPED_RETRY < mod.SCOPED_TTL


# ---------------------------------------------------------------------------
# poll_all — the expired / logged-out split the pages depend on
# ---------------------------------------------------------------------------

A, B, C = Path("/a"), Path("/b"), Path("/c")


def _wire(monkeypatch, dirs, tokens):
    monkeypatch.setattr(mod, "read_config_dirs", lambda: dirs)
    monkeypatch.setattr(mod, "read_token_for", lambda d: tokens.get(d))


def test_poll_all_reports_expired_and_notoken_separately(monkeypatch):
    _wire(monkeypatch, [A, B, C], {A: "tA", B: "tB"})   # C logged out

    async def fake_poll(token):
        if token == "tB":
            raise mod.TokenExpired()
        return {"s": 10, "ok": True}

    with patch.object(mod, "poll_api", new=AsyncMock(side_effect=fake_poll)):
        res = _run(mod.poll_all())
    assert res[A]["state"] == "ok" and res[A]["payload"]["s"] == 10
    assert res[B]["state"] == "expired" and res[B]["payload"] is None
    assert res[C]["state"] == "notoken"


def test_poll_all_caps_at_max_plans(monkeypatch):
    dirs = [Path(f"/d{i}") for i in range(mod.MAX_PLANS + 2)]
    _wire(monkeypatch, dirs, {d: "t" for d in dirs})
    with patch.object(mod, "poll_api", new=AsyncMock(return_value={"s": 1})):
        res = _run(mod.poll_all())
    assert len(res) == mod.MAX_PLANS   # payload must stay inside the BLE buffer


# ---------------------------------------------------------------------------
# build_multi_payload — what the firmware actually pages through
# ---------------------------------------------------------------------------

def _no_labels(monkeypatch, names):
    monkeypatch.setattr(mod, "read_plan_label", lambda d: names[d])


def test_multi_payload_emits_a_page_per_plan(monkeypatch):
    _wire(monkeypatch, [A, B], {A: "tA", B: "tB"})
    _no_labels(monkeypatch, {A: "Team", B: "Pro"})

    async def fake_poll(token):
        return ({"s": 2, "w": 33, "x": 37, "xn": "Fable", "ok": True}
                if token == "tA" else {"s": 1, "w": 96, "ok": True})

    with patch.object(mod, "poll_api", new=AsyncMock(side_effect=fake_poll)):
        payload, dead = _run(mod.build_multi_payload(PlanSelector()))
    assert dead is False
    assert [p["n"] for p in payload["p"]] == ["Team", "Pro"]
    assert payload["p"][0]["x"] == 37 and payload["p"][0]["xn"] == "Fable"
    assert "x" not in payload["p"][1]        # Pro has no per-model limit
    assert payload["ok"] is True


def test_multi_payload_strips_top_level_keys_from_plans(monkeypatch):
    """ok/c/t/tf belong once at the top, not repeated on every page."""
    _wire(monkeypatch, [A], {A: "tA"})
    _no_labels(monkeypatch, {A: "Team"})
    with patch.object(mod, "poll_api", new=AsyncMock(
            return_value={"s": 2, "ok": True, "c": 1, "t": 123, "tf": 24})):
        payload, _ = _run(mod.build_multi_payload(PlanSelector()))
    assert payload["p"][0] == {"n": "Team", "s": 2}


def test_multi_payload_keeps_expired_plan_as_a_page(monkeypatch):
    """An expired plan must not vanish — pages would renumber under your finger."""
    _wire(monkeypatch, [A, B], {A: "tA", B: "tB"})
    _no_labels(monkeypatch, {A: "Team", B: "Pro"})

    async def fake_poll(token):
        if token == "tB":
            raise mod.TokenExpired()
        return {"s": 2, "ok": True}

    with patch.object(mod, "poll_api", new=AsyncMock(side_effect=fake_poll)):
        payload, dead = _run(mod.build_multi_payload(PlanSelector()))
    assert dead is False
    assert payload["p"][1] == {"n": "Pro", "e": 1}
    assert payload["ap"] == 0          # active index skips the expired plan


def test_multi_payload_omits_logged_out_dirs(monkeypatch):
    _wire(monkeypatch, [A, B], {A: "tA"})
    _no_labels(monkeypatch, {A: "Team", B: "Pro"})
    with patch.object(mod, "poll_api", new=AsyncMock(return_value={"s": 2, "ok": True})):
        payload, _ = _run(mod.build_multi_payload(PlanSelector()))
    assert [p["n"] for p in payload["p"]] == ["Team"]


def test_multi_payload_active_index_tracks_the_used_plan(monkeypatch):
    _wire(monkeypatch, [A, B], {A: "tA", B: "tB"})
    _no_labels(monkeypatch, {A: "Team", B: "Pro"})

    async def fake_poll(token):
        return {"s": 5, "ok": True} if token == "tA" else {"s": 40, "ok": True}

    sel = PlanSelector()
    with patch.object(mod, "poll_api", new=AsyncMock(side_effect=fake_poll)):
        payload, _ = _run(mod.build_multi_payload(sel))
    assert payload["ap"] == 1          # startup -> highest utilisation wins


def test_multi_payload_silent_on_transient_failure(monkeypatch):
    """A live token that didn't answer -> hold the last reading, don't flicker."""
    _wire(monkeypatch, [A, B], {A: "tA", B: "tB"})
    _no_labels(monkeypatch, {A: "Team", B: "Pro"})

    async def fake_poll(token):
        return None if token == "tB" else {"s": 2, "ok": True}

    with patch.object(mod, "poll_api", new=AsyncMock(side_effect=fake_poll)):
        payload, dead = _run(mod.build_multi_payload(PlanSelector()))
    assert payload is None and dead is False


def test_multi_payload_dead_when_nothing_has_a_token(monkeypatch):
    _wire(monkeypatch, [A, B], {})
    with patch.object(mod, "poll_api", new=AsyncMock(return_value=None)):
        payload, dead = _run(mod.build_multi_payload(PlanSelector()))
    assert payload is None and dead is True


def test_multi_payload_expired_only_is_not_dead(monkeypatch):
    """All plans expired still renders pages saying so, rather than "No data"."""
    _wire(monkeypatch, [A], {A: "tA"})
    _no_labels(monkeypatch, {A: "Team"})
    with patch.object(mod, "poll_api", new=AsyncMock(side_effect=mod.TokenExpired())):
        payload, dead = _run(mod.build_multi_payload(PlanSelector()))
    assert dead is False and payload["p"] == [{"n": "Team", "e": 1}]
    assert "ap" not in payload         # no plan has numbers to be active


def test_multi_payload_fits_the_firmware_buffer(monkeypatch):
    """Three fully-populated plans must still fit the 512-byte RX buffer."""
    dirs = [Path(f"/d{i}") for i in range(mod.MAX_PLANS)]
    _wire(monkeypatch, dirs, {d: "t" for d in dirs})
    monkeypatch.setattr(mod, "read_plan_label", lambda d: "Enterprise")
    full = {"s": 100, "sr": 9999, "w": 100, "wr": 99999, "st": "allowed",
            "acct": "ent", "tp": 100, "pd": 31, "rd": "Sep 30",
            "x": 100, "xn": "Fable", "xr": 99999, "ok": True}
    with patch.object(mod, "poll_api", new=AsyncMock(return_value=full)):
        payload, _ = _run(mod.build_multi_payload(PlanSelector()))
    blob = json.dumps(payload, separators=(",", ":"))
    assert len(blob) < 512, f"payload is {len(blob)} bytes"

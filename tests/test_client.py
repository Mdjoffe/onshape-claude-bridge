"""Tests for call accounting and the polling backoff.

Both exist because the Free plan meters API calls against a small annual
allowance, so these assert cost, not just correctness.
"""

import pytest

from onshape_bridge.client import OnshapeClient


class FakeResponse:
    ok = True
    content = b"{}"
    status_code = 200

    def json(self):
        return {"ok": True}


def make_client(monkeypatch):
    client = OnshapeClient("access", "secret")
    monkeypatch.setattr(client._session, "request", lambda *a, **k: FakeResponse())
    return client


def test_call_count_starts_at_zero():
    assert OnshapeClient("access", "secret").call_count == 0


def test_every_request_is_counted(monkeypatch):
    client = make_client(monkeypatch)

    client.get_json("/one")
    client.get_json("/two")
    client.post_json("/three", {})

    assert client.call_count == 3


def test_a_failed_request_still_counts(monkeypatch):
    """A 404 is metered the same as a 200, so it must not be counted free."""
    client = OnshapeClient("access", "secret")

    class Failing(FakeResponse):
        ok = False
        status_code = 404
        text = "Not found."

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Failing())

    with pytest.raises(Exception):
        client.get_json("/missing")

    assert client.call_count == 1


def poll_delays(monkeypatch, active_polls: int) -> list[float]:
    """Run wait_for_translation against N ACTIVE responses, capturing the sleeps."""
    client = OnshapeClient("access", "secret")
    states = ["ACTIVE"] * active_polls + ["DONE"]
    calls = {"n": 0}

    def fake_translation(translation_id):
        state = states[calls["n"]]
        calls["n"] += 1
        return {"requestState": state, "id": translation_id}

    delays: list[float] = []
    monkeypatch.setattr(client, "translation", fake_translation)
    monkeypatch.setattr("onshape_bridge.client.time.sleep", delays.append)

    client.wait_for_translation("t1")
    return delays


def test_poll_delay_doubles(monkeypatch):
    assert poll_delays(monkeypatch, active_polls=4) == [2.0, 4.0, 8.0, 16.0]


def test_poll_delay_is_capped(monkeypatch):
    delays = poll_delays(monkeypatch, active_polls=8)

    assert delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]
    assert max(delays) == 30.0


def test_backoff_costs_far_fewer_calls_than_flat_polling(monkeypatch):
    """A five-minute export should cost a dozen-ish calls, not 150."""
    delays = poll_delays(monkeypatch, active_polls=20)

    elapsed = 0.0
    polls_within_five_minutes = 0
    for delay in delays:
        if elapsed >= 300:
            break
        polls_within_five_minutes += 1
        elapsed += delay

    assert polls_within_five_minutes <= 15
    assert 300 / 2.0 > 100  # flat 2s polling would have cost over 100

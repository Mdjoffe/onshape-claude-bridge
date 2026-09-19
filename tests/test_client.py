"""Tests for call accounting and the polling backoff.

Both exist because the Free plan meters API calls against a small annual
allowance, so these assert cost, not just correctness.
"""

import pytest

from onshape_bridge.client import OnshapeClient, OnshapeError


class FakeResponse:
    ok = True
    content = b"{}"
    status_code = 200
    headers: dict = {}

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


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
def test_a_failed_request_is_free(monkeypatch, status):
    """Onshape meters 2xx and 3xx only -- counting failures overstates spend."""
    client = OnshapeClient("access", "secret")

    class Failing(FakeResponse):
        ok = False
        status_code = status
        text = "Nope."

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Failing())

    with pytest.raises(OnshapeError):
        client.get_json("/missing")

    assert client.call_count == 0


def test_a_redirect_counts(monkeypatch):
    """A 307 is a 3xx, and Onshape meters those."""
    client = OnshapeClient("access", "secret")

    class Redirected(FakeResponse):
        status_code = 307

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Redirected())

    client.get_json("/moved")

    assert client.call_count == 1


def test_no_content_parses_as_none(monkeypatch):
    """204 has no body; calling .json() on it raises instead of returning {}."""
    client = OnshapeClient("access", "secret")

    class Empty(FakeResponse):
        status_code = 204
        content = b""

        def json(self):
            raise ValueError("no body to parse")

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Empty())

    assert client.get_json("/empty") is None
    assert client.call_count == 1


def test_a_429_is_retried_after_the_wait_it_names(monkeypatch):
    client = OnshapeClient("access", "secret")
    slept: list[float] = []
    monkeypatch.setattr("onshape_bridge.client.time.sleep", slept.append)

    class Limited(FakeResponse):
        ok = False
        status_code = 429
        headers = {"Retry-After": "5"}
        text = "Too many requests."

    responses = [Limited(), FakeResponse()]
    monkeypatch.setattr(client._session, "request", lambda *a, **k: responses.pop(0))

    client.get_json("/busy")

    assert slept == [5], "should wait exactly as long as Onshape asked"
    assert client.call_count == 1, "the 429 itself is free; only the retry counts"


def test_a_long_retry_after_is_not_waited_out(monkeypatch):
    """450s is Onshape's own documented example. Fail rather than park a job."""
    client = OnshapeClient("access", "secret", max_retry_after=60)
    slept: list[float] = []
    monkeypatch.setattr("onshape_bridge.client.time.sleep", slept.append)

    class Limited(FakeResponse):
        ok = False
        status_code = 429
        headers = {"Retry-After": "450"}
        text = "Too many requests."

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Limited())

    with pytest.raises(OnshapeError) as caught:
        client.get_json("/busy")

    assert slept == []
    assert caught.value.retry_after == 450


def test_response_headers_are_recorded(monkeypatch):
    """X-Api-Version is how an unversioned base URL reveals what it resolved to."""
    client = OnshapeClient("access", "secret")

    class Versioned(FakeResponse):
        headers = {"X-Api-Version": "v10", "X-Rate-Limit-Remaining": "3000"}

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Versioned())

    client.get_json("/anything")

    assert client.api_version == "v10"
    assert client.rate_limit_remaining == 3000


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

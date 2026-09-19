"""Tests for call accounting and the polling backoff.

Both exist because the Free plan meters API calls against a small annual
allowance, so these assert cost, not just correctness.
"""

import pytest

from onshape_bridge.client import ElementRef, OnshapeClient, OnshapeError


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


class Recorder:
    """Captures each hop so tests can assert on URLs and credentials sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def test_a_synchronous_export_follows_its_redirect_and_counts_both_hops(monkeypatch):
    """Onshape's sync exports 307 to the file. Both hops are metered."""
    client = OnshapeClient("access", "secret")

    class Moved(FakeResponse):
        status_code = 307
        headers = {"Location": "https://cad.onshape.com/api/download/abc"}

    class Payload(FakeResponse):
        content = b"solid CRANK\nendsolid\n"

    recorder = Recorder([Moved(), Payload()])
    monkeypatch.setattr(client._session, "request", recorder)

    data = client.export_part_studio_stl(ElementRef("D", "W", "E"))

    assert data == b"solid CRANK\nendsolid\n"
    assert client.call_count == 2, "the 307 and the fetch are both metered"
    assert recorder.calls[1]["url"] == "https://cad.onshape.com/api/download/abc"


def test_redirects_are_not_followed_by_requests_itself(monkeypatch):
    """Letting requests follow would hide a metered hop from the counter."""
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.get_json("/anything")

    assert recorder.calls[0]["allow_redirects"] is False


def test_credentials_are_withheld_from_an_off_host_redirect(monkeypatch):
    """Storage redirects carry their own auth; our API keys must not follow."""
    client = OnshapeClient("access", "secret")

    class Moved(FakeResponse):
        status_code = 307
        headers = {"Location": "https://files.example-cdn.com/signed/abc"}

    recorder = Recorder([Moved(), FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.request("GET", "/partstudios/d/D/w/W/e/E/stl")

    assert "auth" in recorder.calls[1], "must send explicit no-op auth, not None"
    assert recorder.calls[1]["auth"] is not None
    assert "auth" not in recorder.calls[0], "the first hop uses session credentials"


def test_same_host_redirect_keeps_credentials(monkeypatch):
    client = OnshapeClient("access", "secret")

    class Moved(FakeResponse):
        status_code = 307
        headers = {"Location": "/api/elsewhere"}

    recorder = Recorder([Moved(), FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.request("GET", "/somewhere")

    assert "auth" not in recorder.calls[1], "session credentials still apply"
    assert recorder.calls[1]["url"] == "https://cad.onshape.com/api/elsewhere"


def test_a_redirect_loop_is_bounded(monkeypatch):
    client = OnshapeClient("access", "secret")

    class Loop(FakeResponse):
        status_code = 307
        headers = {"Location": "https://cad.onshape.com/api/round-we-go"}

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Loop())

    client.request("GET", "/start")

    assert client.call_count == 1 + client.max_redirects


def test_translator_formats_is_one_call(monkeypatch):
    client = OnshapeClient("access", "secret")

    class Formats(FakeResponse):
        def json(self):
            return [{"name": "STEP", "validDestinationFormat": True}]

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Formats())

    assert client.translator_formats()[0]["name"] == "STEP"
    assert client.call_count == 1


def test_evaluating_featurescript_is_one_call(monkeypatch):
    """The lambda decides what comes back, so batched metrics cost one call."""
    client = OnshapeClient("access", "secret")

    class Measured(FakeResponse):
        def json(self):
            return {"result": {"message": {"value": 42}}}

    recorder = Recorder([Measured()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.evaluate_featurescript(
        ElementRef("D", "W", "E"), "function(context is Context) { return 42; }"
    )

    assert client.call_count == 1
    sent = recorder.calls[0]
    assert sent["url"].endswith("/partstudios/d/D/w/W/e/E/featurescript")
    assert sent["params"] == {"rollbackBarIndex": -1}
    assert "libraryVersion" not in sent["json"], "omitted unless asked for"


def test_a_pinned_library_version_is_sent(monkeypatch):
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.evaluate_featurescript(ElementRef("D", "W", "E"), "f", library_version=2144)

    assert recorder.calls[0]["json"]["libraryVersion"] == 2144


def test_the_tight_bounding_box_script_is_a_lambda(monkeypatch):
    """Onshape's own bounding-box endpoint is approximate; this one measures."""
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.tight_bounding_box(ElementRef("D", "W", "E"))

    script = recorder.calls[0]["json"]["script"]
    assert script.startswith("function(context is Context")
    assert '"tight": true' in script

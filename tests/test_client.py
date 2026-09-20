"""Tests for call accounting and the polling backoff.

Both exist because the Free plan meters API calls against a small annual
allowance, so these assert cost, not just correctness.
"""

import pytest
import requests

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


FEATURE_LISTING = {
    "features": [
        {"featureId": "f1", "name": "Sketch 1", "featureType": "newSketch"},
        {"featureId": "f2", "name": "Bracket 1", "featureType": "bracket"},
    ],
    "featureStates": {
        "f1": {"featureStatus": "OK", "inactive": False},
        "f2": {"featureStatus": "ERROR", "inactive": False},
    },
    "libraryVersion": 2232,
}


def test_feature_health_names_the_broken_feature(monkeypatch):
    """This is how a custom feature that failed to regenerate shows itself."""
    client = OnshapeClient("access", "secret")

    class Listing(FakeResponse):
        def json(self):
            return FEATURE_LISTING

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Listing())

    health = client.feature_health(ElementRef("D", "W", "E"))

    assert [f["status"] for f in health] == ["OK", "ERROR"]
    assert [f["name"] for f in health if f["status"] != "OK"] == ["Bracket 1"]
    assert client.call_count == 1, "one call answers it"


def test_feature_health_survives_a_missing_state(monkeypatch):
    client = OnshapeClient("access", "secret")

    class Listing(FakeResponse):
        def json(self):
            return {"features": [{"featureId": "f9", "name": "Orphan"}]}

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Listing())

    assert client.feature_health(ElementRef("D", "W", "E"))[0]["status"] == "UNKNOWN"


def test_sketch_geometry_is_left_out_by_default(monkeypatch):
    """Sketch entities dominate the payload and are not what we came for."""
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.part_studio_features(ElementRef("D", "W", "E"))

    assert recorder.calls[0]["params"]["noSketchGeometry"] == "true"
    assert recorder.calls[0]["params"]["rollbackBarIndex"] == -1


def test_mass_properties_are_one_call_for_the_whole_part_studio(monkeypatch):
    """Part Studio level, not per part -- one call covers every body."""
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.part_studio_mass_properties(ElementRef("D", "W", "E"))

    assert recorder.calls[0]["url"].endswith("/partstudios/d/D/w/W/e/E/massproperties")
    assert client.call_count == 1


ELEMENT_METADATA = {
    "jsonType": "metadata-element",
    "elementId": "E",
    "properties": [
        {"name": "Name", "value": "NEW_PART", "propertyId": "p-name",
         "editable": True, "valueType": "STRING"},
        {"name": "Description", "value": "", "propertyId": "p-desc",
         "editable": True, "valueType": "STRING"},
        {"name": "Tab Id", "value": "E", "propertyId": "p-tab",
         "editable": False, "computedProperty": True},
    ],
}


def test_property_ids_skips_what_cannot_be_written(monkeypatch):
    """Offering a read-only property would only invite a rejected call."""
    mapping = OnshapeClient.property_ids(ELEMENT_METADATA)

    assert mapping == {"Name": "p-name", "Description": "p-desc"}
    assert OnshapeClient.property_ids(ELEMENT_METADATA, editable_only=False)["Tab Id"] == "p-tab"


def test_property_ids_tolerates_an_empty_payload():
    assert OnshapeClient.property_ids({}) == {}


def test_renaming_a_tab_posts_property_ids(monkeypatch):
    """A tab name is metadata; the write is keyed by id, not by name."""
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.update_element_metadata(ElementRef("D", "W", "E"), {"p-name": "PISTON"})

    sent = recorder.calls[0]
    assert sent["method"] == "POST"
    assert sent["url"].endswith("/metadata/d/D/w/W/e/E")
    assert sent["json"] == {"properties": [{"propertyId": "p-name", "value": "PISTON"}]}
    assert client.call_count == 1


def test_part_metadata_writes_carry_the_part_id_twice(monkeypatch):
    """The docs put partId in the path and in the body; both are required."""
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.update_part_metadata(ElementRef("D", "W", "E"), "JHD", {"p-desc": "Drill bit"})

    sent = recorder.calls[0]
    assert sent["url"].endswith("/metadata/d/D/w/W/e/E/p/JHD")
    assert sent["json"]["partId"] == "JHD"
    assert sent["json"]["jsonType"] == "metadata-part"


def test_a_cached_property_map_makes_a_write_one_call(monkeypatch):
    """Read once, cache the ids, and later writes stop paying for discovery."""
    client = OnshapeClient("access", "secret")

    class Metadata(FakeResponse):
        def json(self):
            return ELEMENT_METADATA

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Metadata())
    cached = client.property_ids(client.element_metadata(ElementRef("D", "W", "E")))
    assert client.call_count == 1

    client.update_element_metadata(ElementRef("D", "W", "E"), {cached["Name"]: "BRACKET"})
    assert client.call_count == 2, "the write itself is one call"


CONFIGURATION = {
    "btType": "BTConfigurationResponse-2019",
    "configurationParameters": [
        {
            "parameterId": "List_sCW2T7xBCmN6an",
            "parameterName": "Drill_Bit_Length",
            "defaultValue": "Default",
            "options": [
                {"optionName": "250 mm", "option": "Default"},
                {"optionName": "500 mm", "option": "_500_mm"},
            ],
        }
    ],
    "libraryVersion": 2641,
}


def test_configuration_options_expose_the_value_the_api_wants():
    """optionName is for people; only `option` is accepted as a value."""
    options = OnshapeClient.configuration_options(CONFIGURATION)

    assert options == {"Drill_Bit_Length": {"250 mm": "Default", "500 mm": "_500_mm"}}
    assert options["Drill_Bit_Length"]["500 mm"] == "_500_mm"


def test_configuration_options_tolerates_an_unconfigured_element():
    assert OnshapeClient.configuration_options({"configurationParameters": []}) == {}


def test_encoding_a_configuration_omits_the_workspace(monkeypatch):
    """Onshape's own path for this endpoint carries no workspace segment."""
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.encode_configuration(ElementRef("D", "W", "E"), {"List_x": "_500_mm"})

    sent = recorder.calls[0]
    assert sent["url"].endswith("/elements/d/D/e/E/configurationencodings")
    assert "/w/W/" not in sent["url"]
    assert sent["json"] == {
        "parameters": [{"parameterId": "List_x", "parameterValue": "_500_mm"}]
    }


def test_a_configured_export_sends_the_encoded_id_once(monkeypatch):
    """The queryParam form would double-encode into a different configuration."""
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.export_part_studio_stl(
        ElementRef("D", "W", "E"), configuration="List_x=_500_mm"
    )

    params = recorder.calls[0]["params"]
    assert params["configuration"] == "List_x=_500_mm"
    assert not params["configuration"].startswith("configuration=")


def test_an_unconfigured_export_sends_no_configuration(monkeypatch):
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.export_part_studio_stl(ElementRef("D", "W", "E"))

    assert "configuration" not in recorder.calls[0]["params"]


def paged(*pages):
    """Responses for a paginated search: each page but the last names a next."""
    made = []
    for index, items in enumerate(pages):
        following = f"https://cad.onshape.com/api/documents?offset={index + 1}"

        class Page(FakeResponse):
            payload = {"items": items, "next": following if index + 1 < len(pages) else None}

            def json(self):
                return self.payload

        made.append(Page())
    return made


def test_listing_documents_follows_next_until_it_runs_out(monkeypatch):
    client = OnshapeClient("access", "secret")
    recorder = Recorder(paged([{"id": "a"}, {"id": "b"}], [{"id": "c"}]))
    monkeypatch.setattr(client._session, "request", recorder)

    found = client.list_documents()

    assert [d["id"] for d in found] == ["a", "b", "c"]
    assert client.call_count == 2, "one call per page"
    assert recorder.calls[1]["url"].startswith("https://cad.onshape.com/api/documents?offset=1")


def test_listing_documents_stops_at_max_pages(monkeypatch):
    """A search matching more than expected must not empty the year page by page."""
    client = OnshapeClient("access", "secret")

    class Endless(FakeResponse):
        def json(self):
            return {"items": [{"id": "x"}], "next": "https://cad.onshape.com/api/documents?p=1"}

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Endless())

    found = client.list_documents(max_pages=3)

    assert len(found) == 3
    assert client.call_count == 3


def test_owner_type_is_only_sent_when_given(monkeypatch):
    """Onshape defaults it to 1 (Company); a user search must say 0 itself."""
    client = OnshapeClient("access", "secret")
    recorder = Recorder([next(iter(paged([])))])
    monkeypatch.setattr(client._session, "request", recorder)

    client.list_documents(owner="u1")

    assert "ownerType" not in recorder.calls[0]["params"]
    assert recorder.calls[0]["params"]["owner"] == "u1"
    assert recorder.calls[0]["params"]["filter"] == 0


def test_a_company_search_sends_both(monkeypatch):
    client = OnshapeClient("access", "secret")
    recorder = Recorder([next(iter(paged([])))])
    monkeypatch.setattr(client._session, "request", recorder)

    client.list_documents(q="Onshape API Guide", document_filter=7, owner="c1", owner_type=1)

    params = recorder.calls[0]["params"]
    assert (params["filter"], params["owner"], params["ownerType"]) == (7, "c1", 1)
    assert params["q"] == "Onshape API Guide"


def test_creating_a_version_repeats_the_document_id(monkeypatch):
    """Onshape wants it in the path and in the body."""
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.create_version("D", "W", "commit 9ee2cd6")

    sent = recorder.calls[0]
    assert sent["url"].endswith("/documents/d/D/versions")
    assert sent["json"] == {"documentId": "D", "workspaceId": "W", "name": "commit 9ee2cd6"}


# -- the API version is pinned, not inherited -----------------------------


def test_the_default_base_url_names_a_version():
    """An unversioned URL resolves to whatever Onshape calls oldest."""
    from onshape_bridge.client import DEFAULT_API_VERSION, DEFAULT_BASE_URL

    assert DEFAULT_BASE_URL.endswith(f"/api/{DEFAULT_API_VERSION}")
    assert DEFAULT_API_VERSION.startswith("v")


def test_the_pinned_version_rejects_rather_than_repairs():
    """From v10, configuration endpoints 400 instead of silently repairing.

    A rejection is free; a silent repair is a wrong result that costs a call
    and does not announce itself.
    """
    from onshape_bridge.client import DEFAULT_API_VERSION

    assert int(DEFAULT_API_VERSION.lstrip("v")) >= 10


def test_requests_go_to_the_pinned_version(monkeypatch):
    client = OnshapeClient("access", "secret")
    recorder = Recorder([FakeResponse()])
    monkeypatch.setattr(client._session, "request", recorder)

    client.get_json("/users/sessioninfo")

    assert recorder.calls[0]["url"] == "https://cad.onshape.com/api/v10/users/sessioninfo"


def test_an_explicit_base_url_still_wins():
    """Moving to another version must not need a code change."""
    client = OnshapeClient("access", "secret", base_url="https://cad.onshape.com/api/v16")

    assert client.base_url == "https://cad.onshape.com/api/v16"


# -- creating and destroying ----------------------------------------------
#
# These are the first methods here that destroy anything, so the tests pin the
# verb and the path rather than just the happy path: a DELETE sent to the wrong
# URL is not a failed call, it is a deleted something-else.


class RecordingClient(OnshapeClient):
    """Captures the request instead of sending it."""

    def __init__(self, status=200, body=b'{"ok": true}'):
        super().__init__("key", "secret")
        self.sent: list[tuple[str, str]] = []
        self._status = status
        self._body = body

    def request(self, method, path, **kwargs):
        self.sent.append((method, path))
        self.last_json = kwargs.get("json")
        response = requests.Response()
        response.status_code = self._status
        response._content = self._body
        response.headers["Content-Type"] = "application/json"
        return response


REF = ElementRef("DOC", "WS", "EL")


def test_delete_element_uses_delete_on_the_element_path():
    client = RecordingClient()
    client.delete_element(REF)
    assert client.sent == [("DELETE", "/elements/d/DOC/w/WS/e/EL")]


def test_delete_feature_targets_one_feature_id():
    client = RecordingClient()
    client.delete_feature(REF, "FID")
    assert client.sent == [("DELETE", "/partstudios/d/DOC/w/WS/e/EL/features/featureid/FID")]


def test_add_feature_posts_the_body_unchanged():
    client = RecordingClient()
    body = {"btType": "BTFeatureDefinitionCall-1406", "feature": {}}
    client.add_feature(REF, body)
    assert client.sent == [("POST", "/partstudios/d/DOC/w/WS/e/EL/features")]
    assert client.last_json == body


def test_create_feature_studio_posts_to_the_workspace_not_an_element():
    client = RecordingClient()
    client.create_feature_studio("DOC", "WS", "sphere")
    assert client.sent == [("POST", "/featurestudios/d/DOC/w/WS")]
    assert client.last_json == {"name": "sphere"}


def test_create_part_studio_posts_to_the_workspace():
    client = RecordingClient()
    client.create_part_studio("DOC", "WS", "parts")
    assert client.sent == [("POST", "/partstudios/d/DOC/w/WS")]


def test_a_delete_answering_204_reads_as_no_body():
    client = RecordingClient(status=204, body=b"")
    assert client.delete_element(REF) is None


def test_a_delete_answering_unparseable_content_does_not_raise():
    """Some deletes answer with a bare string. The status already said it worked."""
    client = RecordingClient(status=200, body=b"deleted")
    assert client.delete_element(REF) is None


# -- keeping a tree the same size across re-runs ---------------------------


FEATURE = {
    "btType": "BTFeatureDefinitionCall-1406",
    "feature": {"btType": "BTMFeature-134", "featureType": "extrude", "name": "Extrude 1"},
}


def test_update_feature_targets_the_feature_id():
    client = RecordingClient()
    client.update_feature(REF, "FID", FEATURE)
    assert client.sent == [("POST", "/partstudios/d/DOC/w/WS/e/EL/features/featureid/FID")]


def test_update_feature_refuses_a_body_missing_featureType():
    """Onshape blanks omitted fields rather than leaving them; that is unrecoverable."""
    client = RecordingClient()
    body = {"feature": {"btType": "BTMFeature-134", "name": "Extrude 1"}}
    with pytest.raises(ValueError, match="featureType"):
        client.update_feature(REF, "FID", body)
    assert client.sent == []


def test_update_feature_refuses_a_body_missing_name():
    client = RecordingClient()
    body = {"feature": {"btType": "BTMFeature-134", "featureType": "extrude"}}
    with pytest.raises(ValueError, match="name"):
        client.update_feature(REF, "FID", body)
    assert client.sent == []


def test_update_feature_reads_through_a_message_wrapper():
    client = RecordingClient()
    wrapped = {"feature": {"message": {"featureType": "extrude", "name": "Extrude 1"}}}
    client.update_feature(REF, "FID", wrapped)
    assert len(client.sent) == 1


def test_the_rollback_bar_moves_without_deleting():
    client = RecordingClient()
    client.move_rollback_bar(REF, 3)
    assert client.sent == [("POST", "/partstudios/d/DOC/w/WS/e/EL/features/rollback")]
    assert client.last_json == {"rollbackIndex": 3}


def test_a_version_ref_cannot_be_written_to():
    client = RecordingClient()
    for call in (
        lambda: client.update_feature(REF.at_version("V"), "F", FEATURE),
        lambda: client.delete_feature(REF.at_version("V"), "F"),
        lambda: client.add_feature(REF.at_version("V"), FEATURE),
        lambda: client.delete_element(REF.at_version("V")),
        lambda: client.move_rollback_bar(REF.at_version("V"), 0),
    ):
        with pytest.raises(ValueError, match="immutable"):
            call()
    assert client.sent == []

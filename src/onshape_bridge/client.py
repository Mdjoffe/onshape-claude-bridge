"""Thin client over the Onshape REST API.

Authentication uses an API key pair (access key + secret key) as HTTP basic
auth, which is what Onshape's own sample code does for server-to-server
scripts. Keys are created at https://cad.onshape.com/appstore/dev-portal
under "API keys"; the secret is shown exactly once.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

DEFAULT_BASE_URL = "https://cad.onshape.com/api"


class OnshapeError(RuntimeError):
    """An Onshape API call failed.

    A failed call is free: Onshape meters only 2xx and 3xx responses, so an
    error here costs nothing but the round trip.
    """

    def __init__(self, status: int, method: str, path: str, body: str, retry_after: int | None = None):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        # Seconds until this endpoint's rate-limit window resets, on a 429.
        self.retry_after = retry_after
        detail = f"{method} {path} -> HTTP {status}: {body[:500]}"
        if retry_after is not None:
            detail += f" (retry after {retry_after}s)"
        super().__init__(detail)


@dataclass(frozen=True)
class ElementRef:
    """Points at one element (tab) inside an Onshape document workspace."""

    document_id: str
    workspace_id: str
    element_id: str

    @property
    def path_suffix(self) -> str:
        return f"d/{self.document_id}/w/{self.workspace_id}/e/{self.element_id}"


class OnshapeClient:
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 60,
        max_retry_after: int = 60,
    ):
        if not access_key or not secret_key:
            raise ValueError("access_key and secret_key are both required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # A 429 names its own wait in Retry-After. Waiting it out is right up to
        # a point; past this many seconds, fail and let the caller decide rather
        # than park a CI job for the rest of the window.
        self.max_retry_after = max_retry_after
        # Onshape's synchronous exports redirect once. More than a couple of
        # hops means something is wrong, and every hop costs a call.
        self.max_redirects = 5
        # The Free plan meters calls against a small annual allowance, so every
        # metered request is counted and reported rather than left to guesswork.
        self.call_count = 0
        # Read back off the responses, so a run can report what it was talking
        # to. X-Api-Version settles which API version an unversioned base URL
        # actually resolved to, and costs nothing to observe.
        self.api_version: str | None = None
        self.rate_limit_remaining: int | None = None
        self._session = requests.Session()
        self._session.auth = (access_key, secret_key)
        self._session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )

    @classmethod
    def from_env(cls, base_url: str | None = None) -> "OnshapeClient":
        """Build a client from ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY."""
        access = os.environ.get("ONSHAPE_ACCESS_KEY", "")
        secret = os.environ.get("ONSHAPE_SECRET_KEY", "")
        if not access or not secret:
            raise ValueError(
                "Set ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY. Create a key pair at "
                "https://cad.onshape.com/appstore/dev-portal under 'API keys'."
            )
        return cls(access, secret, base_url or os.environ.get("ONSHAPE_BASE_URL", DEFAULT_BASE_URL))

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _retry_after(response: requests.Response) -> int | None:
        """Seconds Onshape asked us to wait, if it said so in a parseable way."""
        raw = response.headers.get("Retry-After")
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    def _absolute(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        # Redirects are followed by hand in `request`, not here: each hop is
        # separately metered, and credentials must not travel to a new host.
        kwargs.setdefault("allow_redirects", False)
        response = self._session.request(method, url, timeout=self.timeout, **kwargs)

        # Onshape meters 2xx and 3xx only; 4xx and 5xx are explicitly free. An
        # earlier version of this client counted failures too, on the reasoning
        # that a 404 proves the server did a lookup. Onshape's published limits
        # say otherwise, and counting them overstates the spend.
        #
        # A 3xx is metered like any other answer, which is why `request` follows
        # redirects one deliberate hop at a time: each lands here and is counted.
        if 200 <= response.status_code < 400:
            self.call_count += 1

        version = response.headers.get("X-Api-Version")
        if version:
            self.api_version = version
        remaining = response.headers.get("X-Rate-Limit-Remaining")
        if remaining is not None:
            try:
                self.rate_limit_remaining = int(remaining)
            except ValueError:
                pass
        return response

    def _send_once(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """One hop, with the single 429 retry Onshape's Retry-After asks for."""
        response = self._send(method, url, **kwargs)

        # A 429 is a rate limit, not the annual allowance, and it costs nothing.
        # Retrying once after the wait Onshape names is usually all it takes.
        if response.status_code == 429:
            delay = self._retry_after(response)
            if delay is not None and 0 <= delay <= self.max_retry_after:
                time.sleep(delay)
                response = self._send(method, url, **kwargs)
        return response

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = self._absolute(path)
        response = self._send_once(method, url, **kwargs)

        # Onshape's synchronous exports answer with a 307 pointing at wherever
        # the file actually lives, and the docs say applications must follow it
        # themselves. We do it here rather than leaving it to requests for two
        # reasons: each hop is separately metered, so following silently would
        # under-report the spend; and the target may be storage on another host,
        # which carries its own authorization in the URL and must not be handed
        # our API keys.
        hops = 0
        while 300 <= response.status_code < 400 and hops < self.max_redirects:
            location = response.headers.get("Location")
            if not location:
                break
            target = urljoin(url, location)
            off_host = urlsplit(target).netloc != urlsplit(self.base_url).netloc
            follow = dict(kwargs)
            if off_host:
                # A callable auth that changes nothing: requests treats None as
                # "fall back to the session's credentials", so this is the only
                # way to genuinely send none.
                follow["auth"] = lambda request: request
            response = self._send_once(method, target, **follow)
            url = target
            hops += 1

        if not response.ok:
            raise OnshapeError(
                response.status_code,
                method,
                path,
                response.text,
                self._retry_after(response),
            )
        return response

    def get_json(self, path: str, **kwargs: Any) -> Any:
        response = self.request("GET", path, **kwargs)
        # 204 carries no body at all, and parsing one raises rather than
        # returning nothing. Callers get None and can tell "empty" from "{}".
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def post_json(self, path: str, payload: Any, **kwargs: Any) -> Any:
        response = self.request("POST", path, json=payload, **kwargs)
        return response.json() if response.content else None

    # -- account ----------------------------------------------------------

    def session_info(self) -> dict:
        """Identify the authenticated user. The cheapest proof a key works."""
        return self.get_json("/users/sessioninfo")

    # -- documents and elements -------------------------------------------

    def document(self, document_id: str) -> dict:
        return self.get_json(f"/documents/{document_id}")

    def elements(self, document_id: str, workspace_id: str) -> list[dict]:
        return self.get_json(f"/documents/d/{document_id}/w/{workspace_id}/elements")

    # -- feature studios (git is the source of truth) ---------------------

    def get_feature_studio(self, ref: ElementRef) -> dict:
        return self.get_json(f"/featurestudios/{ref.path_suffix}")

    def get_feature_studio_contents(self, ref: ElementRef) -> str:
        return self.get_feature_studio(ref).get("contents", "")

    def update_feature_studio_contents(self, ref: ElementRef, contents: str) -> dict:
        """Overwrite a Feature Studio's source. This is the git -> Onshape push."""
        return self.post_json(f"/featurestudios/{ref.path_suffix}", {"contents": contents})

    # -- configurations ---------------------------------------------------

    def get_configuration(self, ref: ElementRef) -> dict:
        return self.get_json(f"/elements/{ref.path_suffix}/configuration")

    # -- exports (Onshape -> git) -----------------------------------------

    def start_translation(
        self,
        ref: ElementRef,
        format_name: str,
        element_kind: str = "partstudios",
        extra: dict | None = None,
    ) -> dict:
        """Kick off an export. Returns a translation job record with an id."""
        payload: dict[str, Any] = {
            "formatName": format_name,
            "storeInDocument": False,
        }
        if extra:
            payload.update(extra)
        return self.post_json(f"/{element_kind}/{ref.path_suffix}/translations", payload)

    # A lambda that measures the real geometry. Onshape's own
    # getPartStudioBoundingBoxes endpoint is documented as approximate -- "meant
    # for graphics and visualization" -- so a tight box has to be evaluated.
    TIGHT_BOUNDING_BOX = (
        "function(context is Context, definition is map) {"
        ' return evBox3d(context, { "topology":'
        ' qConstructionFilter(qEverything(), ConstructionObject.NO),'
        ' "tight": true }); }'
    )

    def evaluate_featurescript(
        self,
        ref: ElementRef,
        script: str,
        library_version: int | None = None,
        rollback_bar_index: int = -1,
    ) -> Any:
        """Run a FeatureScript lambda against a Part Studio and get the result.

        One metered call, and the lambda decides what comes back -- so several
        measurements that would each be their own REST call can be answered
        together, by one script that returns a map of them.

        Only lambda expressions evaluate here. A Feature Studio's source, with
        its `FeatureScript` version header, its imports and its exported
        feature definitions, is not a lambda: this endpoint cannot be used to
        check that such a file compiles.
        """
        payload: dict[str, Any] = {"script": script}
        if library_version is not None:
            payload["libraryVersion"] = library_version
        return self.post_json(
            f"/partstudios/{ref.path_suffix}/featurescript",
            payload,
            params={"rollbackBarIndex": rollback_bar_index},
        )

    def tight_bounding_box(self, ref: ElementRef, library_version: int | None = None) -> Any:
        """Measure a Part Studio's real extents, excluding construction geometry."""
        return self.evaluate_featurescript(ref, self.TIGHT_BOUNDING_BOX, library_version)

    def translator_formats(self) -> list[dict]:
        """Every format Onshape can translate, and in which direction.

        Each entry carries `name` (what `formatName` must be set to, casing and
        all), `validSourceFormat`, `validDestinationFormat` and
        `couldBeAssembly`. One call, and it turns a guessed `formatName` --
        which would fail a translation after the expensive part -- into a
        checkable one.
        """
        return self.get_json("/translations/translationformats")

    def export_part_studio_stl(
        self,
        ref: ElementRef,
        mode: str = "binary",
        units: str = "millimeter",
        grouping: bool = True,
        scale: float = 1.0,
    ) -> bytes:
        """Export a Part Studio to STL synchronously, in two metered calls.

        Onshape offers synchronous exports for STL, Parasolid and glTF that
        answer with a 307 to wherever the file lives. That is two calls -- the
        redirect and the fetch -- against the dozen or so a translation job
        costs in POST plus polling plus download. The trade is no control over
        tessellation beyond these arguments.
        """
        response = self.request(
            "GET",
            f"/partstudios/{ref.path_suffix}/stl",
            params={
                "mode": mode,
                "units": units,
                "grouping": str(grouping).lower(),
                "scale": scale,
            },
            headers={"Accept": "*/*"},
        )
        return response.content

    def translation(self, translation_id: str) -> dict:
        return self.get_json(f"/translations/{translation_id}")

    def wait_for_translation(
        self,
        translation_id: str,
        poll_seconds: float = 2.0,
        max_poll_seconds: float = 30.0,
        timeout_seconds: float = 600.0,
    ) -> dict:
        """Poll until the export job leaves ACTIVE, or raise on timeout.

        The delay doubles up to `max_poll_seconds`, because each poll is a
        metered API call. At a flat 2s a five-minute export costs about 150
        calls; backing off brings that to roughly a dozen, at the price of
        noticing completion up to `max_poll_seconds` late.
        """
        deadline = time.monotonic() + timeout_seconds
        delay = poll_seconds
        while True:
            job = self.translation(translation_id)
            state = job.get("requestState")
            if state != "ACTIVE":
                if state == "FAILED":
                    raise OnshapeError(
                        200,
                        "GET",
                        f"/translations/{translation_id}",
                        job.get("failureReason", "translation failed"),
                    )
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"translation {translation_id} still ACTIVE after {timeout_seconds}s"
                )
            time.sleep(delay)
            delay = min(delay * 2, max_poll_seconds)

    def download_external_data(self, document_id: str, foreign_id: str) -> bytes:
        """Fetch the bytes a finished translation stored in the document."""
        response = self.request(
            "GET",
            f"/documents/d/{document_id}/externaldata/{foreign_id}",
            headers={"Accept": "*/*"},
            stream=True,
        )
        return response.content

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

# Onshape versions its API in the path. An unversioned URL does not mean
# "current" -- it resolves to whatever Onshape considers oldest, which was
# measured as v1 on 2026-09-19 despite the docs saying v0. That is a moving
# target nobody chose, so pin one.
#
# v10 rather than the newest: from v10 onward, configuration endpoints reject
# bad visibility conditions with a 400 instead of silently repairing them.
# Failing is the cheaper outcome -- Onshape does not meter 4xx, so a rejection
# costs nothing while a silent repair costs a wrong result you may not notice.
DEFAULT_API_VERSION = "v10"
DEFAULT_BASE_URL = f"https://cad.onshape.com/api/{DEFAULT_API_VERSION}"


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
    """Points at one element (tab) inside an Onshape document.

    Onshape addresses an element three ways, and the path carries which:
    `w/` a workspace, `v/` a version, `m/` a microversion. Only the workspace
    is mutable, so only it can be written to -- and only it can go stale.
    Anything read at `v/` or `m/` is fixed forever, which is what makes a
    version a usable rollback source and a permanently valid cache key.

    `wvm` defaults to the workspace, because that is what every caller before
    this wanted and what every write still needs.
    """

    document_id: str
    workspace_id: str
    element_id: str
    #: "w" workspace, "v" version, "m" microversion.
    wvm: str = "w"

    @property
    def path_suffix(self) -> str:
        return f"d/{self.document_id}/{self.wvm}/{self.workspace_id}/e/{self.element_id}"

    @property
    def writable(self) -> bool:
        """Versions and microversions are immutable; only a workspace is not."""
        return self.wvm == "w"

    def at_version(self, version_id: str) -> "ElementRef":
        """The same element as it was at a version. Read-only, never stale."""
        return ElementRef(self.document_id, version_id, self.element_id, wvm="v")

    def at_microversion(self, microversion_id: str) -> "ElementRef":
        """The same element at one exact change. Read-only, never stale."""
        return ElementRef(self.document_id, microversion_id, self.element_id, wvm="m")


def _require_writable(ref: ElementRef, what: str) -> None:
    if not ref.writable:
        raise ValueError(
            f"cannot {what} at {ref.wvm}/{ref.workspace_id}: versions and "
            "microversions are immutable. Write to the workspace instead."
        )


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
        # to. With a pinned base URL this should echo the version asked for --
        # if it ever does not, that version has been retired and the difference
        # is worth seeing rather than discovering through changed behaviour.
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

    def delete_json(self, path: str, **kwargs: Any) -> Any:
        """DELETE, returning whatever came back.

        Deletes commonly answer 200 with a body, 204 with nothing, or 200 with
        an empty body, so all three have to read the same to the caller.
        """
        response = self.request("DELETE", path, **kwargs)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            # Some deletes answer with a bare string or no content type worth
            # trusting. The status already said it worked.
            return None

    # -- account ----------------------------------------------------------

    def session_info(self) -> dict:
        """Identify the authenticated user. The cheapest proof a key works."""
        return self.get_json("/users/sessioninfo")

    # -- documents and elements -------------------------------------------

    def document(self, document_id: str) -> dict:
        return self.get_json(f"/documents/{document_id}")

    def create_document(self, name: str, **attributes: Any) -> dict:
        """Create a document. The response carries the new id."""
        return self.post_json("/documents", {"name": name, **attributes})

    def update_document(self, document_id: str, **attributes: Any) -> dict:
        """Change a document's own attributes, such as `name` or `description`."""
        return self.post_json(f"/documents/{document_id}", attributes)

    def list_documents(
        self,
        q: str | None = None,
        document_filter: int = 0,
        owner: str | None = None,
        owner_type: int | None = None,
        sort_column: str = "createdAt",
        sort_order: str = "desc",
        limit: int = 20,
        max_pages: int = 10,
    ) -> list[dict]:
        """Search documents, following `next` until the results run out.

        `document_filter` selects the set, mirroring the Onshape UI's own search
        options: 0 my documents, 1 created, 2 shared, 3 trash, 4 public,
        5 recent, 6 by owner, 7 by company, 9 by team. Filters cannot be
        combined server-side -- ask for one and narrow the rest in Python.

        `owner_type` is only sent when given. Onshape's default is 1, meaning
        Company, so a search by *user* id must pass 0 explicitly or quietly
        match nothing.

        `max_pages` bounds the spend. Each page is a metered call, and a search
        that matches more than expected should stop rather than empty the
        year's allowance a page at a time.
        """
        params: dict[str, Any] = {
            "filter": document_filter,
            "sortColumn": sort_column,
            "sortOrder": sort_order,
            "offset": 0,
            "limit": limit,
        }
        if q:
            params["q"] = q
        if owner:
            params["owner"] = owner
        if owner_type is not None:
            params["ownerType"] = owner_type

        found: list[dict] = []
        target: str = "/documents"
        query: dict[str, Any] | None = params
        # max_pages bounds calls, not pages kept: every page fetched is used,
        # so stopping early never means having paid for a discarded one.
        for _ in range(max_pages):
            page = self.get_json(target, params=query)
            found.extend(page.get("items") or [])
            following = page.get("next")
            if not following:
                break
            # `next` is a complete URL and already carries the query.
            target, query = following, None
        return found

    def document_versions(self, document_id: str) -> list[dict]:
        return self.get_json(f"/documents/d/{document_id}/versions")

    def create_version(self, document_id: str, workspace_id: str, name: str) -> dict:
        """Freeze a workspace as a named version.

        The document id goes in the path and again in the body; Onshape wants
        both. Naming the version after the commit that produced it is what ties
        Onshape's history to git's, for the geometry git cannot hold.
        """
        return self.post_json(
            f"/documents/d/{document_id}/versions",
            {"documentId": document_id, "workspaceId": workspace_id, "name": name},
        )

    def elements(self, document_id: str, workspace_id: str) -> list[dict]:
        return self.get_json(f"/documents/d/{document_id}/w/{workspace_id}/elements")

    # -- feature studios (git is the source of truth) ---------------------

    def get_feature_studio(self, ref: ElementRef) -> dict:
        return self.get_json(f"/featurestudios/{ref.path_suffix}")

    def get_feature_studio_contents(self, ref: ElementRef) -> str:
        return self.get_feature_studio(ref).get("contents", "")

    def update_feature_studio_contents(self, ref: ElementRef, contents: str) -> dict:
        """Overwrite a Feature Studio's source. This is the git -> Onshape push.

        Refuses a version or microversion ref before spending anything. Onshape
        would reject it too, but its error is about the route rather than about
        the mistake, and this one costs no call to produce.
        """
        _require_writable(ref, "write a Feature Studio")
        return self.post_json(f"/featurestudios/{ref.path_suffix}", {"contents": contents})

    # -- configurations ---------------------------------------------------

    def get_configuration(self, ref: ElementRef) -> dict:
        return self.get_json(f"/elements/{ref.path_suffix}/configuration")

    def update_configuration(self, ref: ElementRef, configuration: dict) -> dict:
        """Rewrite a Part Studio's or Assembly's configuration definition."""
        return self.post_json(f"/elements/{ref.path_suffix}/configuration", configuration)

    @staticmethod
    def configuration_options(configuration: dict) -> dict[str, dict[str, str]]:
        """Parameter name -> {readable option name: the value the API accepts}.

        Exists for one trap: each option carries both `optionName` ("500 mm")
        and `option` ("_500_mm"), and only `option` is accepted as a
        parameterValue. Reading the pretty one and sending it fails quietly.
        """
        mapping: dict[str, dict[str, str]] = {}
        for parameter in configuration.get("configurationParameters") or []:
            name = parameter.get("parameterName")
            if not name:
                continue
            mapping[name] = {
                option["optionName"]: option["option"]
                for option in parameter.get("options") or []
                if option.get("optionName") and option.get("option")
            }
        return mapping

    def encode_configuration(self, ref: ElementRef, parameters: dict[str, str]) -> dict:
        """Turn {parameterId: option} into the encoded forms other calls want.

        Returns `encodedId` -- for request bodies, such as a translation -- and
        `queryParam`, which is that same string with `configuration=` already
        glued on the front.

        Note the path carries document and element but no workspace. That is
        Onshape's own shape, not an omission: an encoding is not workspace
        specific.
        """
        return self.post_json(
            f"/elements/d/{ref.document_id}/e/{ref.element_id}/configurationencodings",
            {
                "parameters": [
                    {"parameterId": k, "parameterValue": v} for k, v in parameters.items()
                ]
            },
        )

    def decode_configuration(self, ref: ElementRef, encoding_id: str) -> dict:
        return self.get_json(f"/elements/{ref.path_suffix}/configurationencodings/{encoding_id}")

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

    # -- metadata ---------------------------------------------------------
    #
    # The second thing in Onshape that is text, after FeatureScript: names,
    # part numbers, descriptions and custom properties. Text diffs, merges and
    # costs nothing to store, so unlike geometry it can live in git.
    #
    # Every write needs a propertyId, and the only documented way to learn one
    # is to read the metadata and match on `name`. That makes a naive write two
    # calls. The ids look stable across documents, so reading once and caching
    # the map in the repo turns later writes back into one call each.

    @staticmethod
    def property_ids(metadata: dict, editable_only: bool = True) -> dict[str, str]:
        """Map property name -> propertyId, for caching rather than re-reading.

        Computed and read-only properties are dropped by default: they cannot
        be written, so offering them would only invite a rejected call.
        """
        return {
            prop["name"]: prop["propertyId"]
            for prop in metadata.get("properties") or []
            if prop.get("name") and prop.get("propertyId")
            and (not editable_only or prop.get("editable"))
        }

    def element_metadata(self, ref: ElementRef) -> dict:
        """Metadata for one tab, including its name."""
        return self.get_json(f"/metadata/{ref.path_suffix}")

    def update_element_metadata(self, ref: ElementRef, properties: dict[str, str]) -> dict:
        """Write element properties, keyed by propertyId rather than name.

        Keyed by id because that is what the API takes, and because resolving a
        name costs a read. Use `property_ids` once to build the mapping.
        """
        return self.post_json(
            f"/metadata/{ref.path_suffix}",
            {"properties": [{"propertyId": k, "value": v} for k, v in properties.items()]},
        )

    def part_metadata(self, ref: ElementRef, part_id: str) -> dict:
        return self.get_json(f"/metadata/{ref.path_suffix}/p/{part_id}")

    def update_part_metadata(
        self, ref: ElementRef, part_id: str, properties: dict[str, str]
    ) -> dict:
        return self.post_json(
            f"/metadata/{ref.path_suffix}/p/{part_id}",
            {
                "jsonType": "metadata-part",
                "partId": part_id,
                "properties": [
                    {"propertyId": k, "value": v} for k, v in properties.items()
                ],
            },
        )

    # -- part studios -----------------------------------------------------

    # -- creating and destroying ------------------------------------------
    #
    # Everything here changes state, so every one of these is metered and none
    # of it is undoable from this side. Onshape's own version history is the
    # undo; nothing in this client replaces it.
    #
    # Two of these paths are inferred rather than documented, and each says so.
    # Probing an inferred path is free -- a wrong path answers 404 or 405, and
    # Onshape does not meter 4xx -- so the cheap way to settle one is to call
    # it, not to reason about it.

    def create_part_studio(self, document_id: str, workspace_id: str, name: str) -> dict:
        """Add a Part Studio tab. Documented on the Part Studios page."""
        return self.post_json(f"/partstudios/d/{document_id}/w/{workspace_id}", {"name": name})

    def create_feature_studio(self, document_id: str, workspace_id: str, name: str) -> dict:
        """Add a Feature Studio tab.

        **[inference]** By analogy with `create_part_studio`, which *is*
        documented. The Feature Studio equivalent is not written down anywhere
        this project has read, so treat a 404 here as the answer rather than as
        a surprise -- and a free one.
        """
        return self.post_json(f"/featurestudios/d/{document_id}/w/{workspace_id}", {"name": name})

    def delete_element(self, ref: ElementRef) -> Any:
        """Delete one tab.

        **[inference]** No guide this project has read documents deleting an
        element; only deleting a whole *document* is written down. This is the
        natural REST reading of the element path. A wrong guess costs nothing.

        There is no undo. The tab and everything in it leave the workspace, and
        only an Onshape version made beforehand brings them back.
        """
        _require_writable(ref, "delete an element")
        return self.delete_json(f"/elements/{ref.path_suffix}")

    def add_feature(self, ref: ElementRef, feature: dict) -> dict:
        """Append one feature to a Part Studio's tree.

        `feature` is a `BTMFeature-134` body; the call wraps whatever it is
        given. The response carries the new feature's `featureId`, which is the
        only way to learn it.
        """
        _require_writable(ref, "add a feature")
        return self.post_json(f"/partstudios/{ref.path_suffix}/features", feature)

    def delete_feature(self, ref: ElementRef, feature_id: str) -> Any:
        """Remove one feature from a Part Studio's tree.

        Documented on the Part Studios page as `DELETE` on the same path that
        updates a feature.

        Order matters and this does not manage it: deleting a feature that a
        later one consumes leaves the later one in error. Walk the tree
        backwards.
        """
        _require_writable(ref, "delete a feature")
        return self.delete_json(f"/partstudios/{ref.path_suffix}/features/featureid/{feature_id}")

    def part_studio_features(
        self,
        ref: ElementRef,
        rollback_bar_index: int = -1,
        include_geometry_ids: bool = True,
        no_sketch_geometry: bool = True,
    ) -> dict:
        """The feature list, with each feature's regeneration status.

        `noSketchGeometry` defaults to true here where Onshape's example sends
        false: sketch entities and constraints dominate the response, and the
        thing worth reading is usually `featureStates`.
        """
        return self.get_json(
            f"/partstudios/{ref.path_suffix}/features",
            params={
                "rollbackBarIndex": rollback_bar_index,
                "includeGeometryIds": str(include_geometry_ids).lower(),
                "noSketchGeometry": str(no_sketch_geometry).lower(),
            },
        )

    def feature_health(self, ref: ElementRef, **kwargs: Any) -> list[dict]:
        """Every feature paired with the status Onshape last regenerated it to.

        One call, and the cheapest honest answer to "did this FeatureScript
        work?": a custom feature that failed to compile or regenerate appears
        here with a `status` other than OK. Unlike evaluating a lambda, this
        reports on the real feature as the Part Studio built it.
        """
        listing = self.part_studio_features(ref, **kwargs)
        states = listing.get("featureStates") or {}
        health = []
        for feature in listing.get("features") or []:
            feature_id = feature.get("featureId")
            state = states.get(feature_id) or {}
            health.append(
                {
                    "featureId": feature_id,
                    "name": feature.get("name"),
                    "featureType": feature.get("featureType"),
                    "status": state.get("featureStatus", "UNKNOWN"),
                    "inactive": state.get("inactive", False),
                }
            )
        return health

    def part_studio_mass_properties(self, ref: ElementRef) -> dict:
        """Mass, volume, centroid and inertia for the whole Part Studio.

        One call for every body together, under `bodies["-all-"]`, rather than
        one per part. Each scalar comes back as [value, lower, upper] -- the
        bounds are Onshape's tolerance on the figure, not three separate
        answers. `hasMass` is false until a material is assigned.
        """
        return self.get_json(f"/partstudios/{ref.path_suffix}/massproperties")

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
        configuration: str | None = None,
    ) -> bytes:
        """Export a Part Studio to STL synchronously, in two metered calls.

        Onshape offers synchronous exports for STL, Parasolid and glTF that
        answer with a 307 to wherever the file lives. That is two calls -- the
        redirect and the fetch -- against the dozen or so a translation job
        costs in POST plus polling plus download. The trade is no control over
        tessellation beyond these arguments.

        `configuration` takes the **encodedId** from `encode_configuration`, not
        its `queryParam`. The two differ by a leading `configuration=`, and
        passing the query-param form here produces a doubly-encoded parameter
        that Onshape reads as a different configuration entirely.
        """
        params: dict[str, Any] = {
            "mode": mode,
            "units": units,
            "grouping": str(grouping).lower(),
            "scale": scale,
        }
        if configuration:
            params["configuration"] = configuration
        response = self.request(
            "GET",
            f"/partstudios/{ref.path_suffix}/stl",
            params=params,
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

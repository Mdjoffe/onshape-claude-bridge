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

import requests

DEFAULT_BASE_URL = "https://cad.onshape.com/api"


class OnshapeError(RuntimeError):
    """An Onshape API call failed."""

    def __init__(self, status: int, method: str, path: str, body: str):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")


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
    ):
        if not access_key or not secret_key:
            raise ValueError("access_key and secret_key are both required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
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

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        response = self._session.request(method, url, timeout=self.timeout, **kwargs)
        if not response.ok:
            raise OnshapeError(response.status_code, method, path, response.text)
        return response

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).json()

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

    def translation(self, translation_id: str) -> dict:
        return self.get_json(f"/translations/{translation_id}")

    def wait_for_translation(
        self, translation_id: str, poll_seconds: float = 2.0, timeout_seconds: float = 600.0
    ) -> dict:
        """Poll until the export job leaves ACTIVE, or raise on timeout."""
        deadline = time.monotonic() + timeout_seconds
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
            time.sleep(poll_seconds)

    def download_external_data(self, document_id: str, foreign_id: str) -> bytes:
        """Fetch the bytes a finished translation stored in the document."""
        response = self.request(
            "GET",
            f"/documents/d/{document_id}/externaldata/{foreign_id}",
            headers={"Accept": "*/*"},
            stream=True,
        )
        return response.content

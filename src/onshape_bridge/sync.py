"""The two sync directions.

    push  git -> Onshape   FeatureScript source into Feature Studios
    pull  Onshape -> git   translated exports (STEP/STL/...) into the repo

Geometry drawn by hand in a Part Studio moves in neither direction; Onshape's
own versions and branches are the history for that.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import OnshapeClient
from .config import ProjectConfig, is_placeholder


@dataclass
class SyncResult:
    project: str
    action: str
    target: str
    status: str  # "updated" | "unchanged" | "skipped" | "unconfigured" | "would-update"
    detail: str = ""
    #: What Onshape sent back, for a call that sent something. Kept off the
    #: printed line -- it is for inspection, not for reading in a log -- but
    #: kept, because a write response is the only thing the caller cannot get
    #: again for free.
    response: dict | None = None

    def __str__(self) -> str:
        line = f"[{self.status:>13}] {self.project} {self.action} {self.target}"
        extra = ", ".join(filter(None, (self.detail, response_shape(self.response))))
        return f"{line} ({extra})" if extra else line


def response_shape(response: dict | None) -> str:
    """The keys a write came back with, minus the echo of what we sent.

    A Feature Studio write is the one place a compile complaint could surface,
    and nothing this project has read says what such a response is called.
    Rather than guess a field name and silently find nothing, print the names
    that actually arrived: one run then settles it. `contents` is dropped
    because it is our own source handed back.
    """
    if not response:
        return ""
    names = sorted(key for key in response if key != "contents")
    return "returned: " + ", ".join(names) if names else ""


def _unconfigured(project: ProjectConfig, action: str) -> list[SyncResult]:
    """One result explaining the skip, or an empty list if the project is ready."""
    pending = project.unconfigured
    if not pending:
        return []
    return [
        SyncResult(
            project.name,
            action,
            "-",
            "unconfigured",
            "still placeholders: " + ", ".join(pending),
        )
    ]


def push_feature_studios(
    client: OnshapeClient,
    project: ProjectConfig,
    dry_run: bool = False,
    assume_changed: bool = False,
) -> list[SyncResult]:
    """Upload each FeatureScript file whose repo copy differs from Onshape's.

    `assume_changed` skips the comparison read and uploads unconditionally,
    halving the call cost per file. Worth it when git already established the
    file changed -- a CI run filtered on this project's paths, or a manual sync
    you triggered because you edited something. The cost of being wrong is one
    needless Onshape microversion.
    """
    if skipped := _unconfigured(project, "push"):
        return skipped

    results: list[SyncResult] = []
    for sync in project.feature_studios:
        source = project.source_path(sync)
        target = str(sync.source)
        if is_placeholder(sync.element_id):
            results.append(
                SyncResult(project.name, "push", target, "unconfigured", "placeholder element id")
            )
            continue
        if not source.is_file():
            results.append(
                SyncResult(project.name, "push", target, "skipped", "no such file in repo")
            )
            continue

        local = source.read_text()

        if not assume_changed:
            remote = client.get_feature_studio_contents(project.ref(sync.element_id))
            if local == remote:
                results.append(SyncResult(project.name, "push", target, "unchanged"))
                continue

        if dry_run:
            results.append(
                SyncResult(
                    project.name,
                    "push",
                    target,
                    "would-update",
                    "not compared" if assume_changed else "",
                )
            )
            continue

        response = client.update_feature_studio_contents(project.ref(sync.element_id), local)
        results.append(
            SyncResult(
                project.name,
                "push",
                target,
                "updated",
                "not compared" if assume_changed else "",
                response=response if isinstance(response, dict) else None,
            )
        )
    return results


def pull_exports(
    client: OnshapeClient, project: ProjectConfig, dry_run: bool = False
) -> list[SyncResult]:
    """Run each configured translation and write the bytes into the repo."""
    if skipped := _unconfigured(project, "pull"):
        return skipped

    results: list[SyncResult] = []
    for sync in project.exports:
        target = str(sync.output)
        if is_placeholder(sync.element_id):
            results.append(
                SyncResult(project.name, "pull", target, "unconfigured", "placeholder element id")
            )
            continue
        if dry_run:
            results.append(
                SyncResult(project.name, "pull", target, "would-update", sync.format_name)
            )
            continue

        ref = project.ref(sync.element_id)
        job = client.start_translation(
            ref, sync.format_name, element_kind=sync.element_kind, extra=sync.options
        )
        finished = client.wait_for_translation(job["id"])

        foreign_ids = finished.get("resultExternalDataIds") or []
        if not foreign_ids:
            results.append(
                SyncResult(project.name, "pull", target, "skipped", "translation returned no data")
            )
            continue

        data = client.download_external_data(project.document_id, foreign_ids[0])
        destination = project.output_path(sync)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.is_file() and destination.read_bytes() == data:
            results.append(SyncResult(project.name, "pull", target, "unchanged"))
            continue

        destination.write_bytes(data)
        results.append(
            SyncResult(project.name, "pull", target, "updated", f"{len(data)} bytes")
        )
    return results

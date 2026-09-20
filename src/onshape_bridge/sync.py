"""The two sync directions.

    push  git -> Onshape   FeatureScript source into Feature Studios
    pull  Onshape -> git   translated exports (STEP/STL/...) into the repo

Geometry drawn by hand in a Part Studio moves in neither direction; Onshape's
own versions and branches are the history for that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import ElementRef, OnshapeClient
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


def restore_feature_studio(
    client: OnshapeClient,
    ref: ElementRef,
    version_id: str,
    dry_run: bool = False,
    compare: bool = False,
) -> SyncResult:
    """Put a Feature Studio back to how it was at a version.

    **2 calls**, or 3 with `compare`. This is rollback using nothing we have
    not already proven: a version is immutable, so reading the element at
    `v/{vid}` gives exactly the source that was there, and writing it to the
    workspace is the same call `push` already makes. No restore endpoint, no
    branch, nothing left to verify.

    `compare` spends a third call reading the workspace first, and buys only
    the avoidance of a needless microversion when the rollback is a no-op.
    That is the same trade as `--assume-changed` and it defaults the same way:
    off, because a rollback is something you asked for deliberately and the
    usual case is that it will change something.

    It does not roll back *geometry*. Hand-drawn work and feature trees are not
    text and do not come back this way -- for those Onshape's own restore is
    the only route, and this project has not established what that costs.
    """
    source = client.get_feature_studio_contents(ref.at_version(version_id))
    if dry_run:
        return SyncResult(
            "", "restore", ref.element_id, "would-update", f"{len(source)} bytes from {version_id}"
        )
    if compare and source == client.get_feature_studio_contents(ref):
        return SyncResult("", "restore", ref.element_id, "unchanged", f"already at {version_id}")
    response = client.update_feature_studio_contents(ref, source)
    return SyncResult(
        "",
        "restore",
        ref.element_id,
        "updated",
        f"from {version_id}",
        response=response if isinstance(response, dict) else None,
    )


def tag_version(
    client: OnshapeClient,
    project: ProjectConfig,
    name: str,
    results: list[SyncResult] | None = None,
) -> SyncResult | None:
    """Name an Onshape version after the commit that produced it. 1 call.

    This is the only thing that ties the two histories together. git holds the
    FeatureScript; Onshape holds the geometry, the versions and everything
    drawn by hand. Neither can reconstruct the other, so without a shared name
    "which FeatureScript made this part?" has no answer at all.

    Returns None when nothing was written. A version of a workspace nothing
    changed is a call spent recording that nothing happened, and versions are
    immutable, so the document accumulates them forever.
    """
    if results is not None and not any(r.status == "updated" for r in results):
        return None
    response = client.create_version(project.document_id, project.workspace_id, name)
    return SyncResult(
        project.name,
        "version",
        name,
        "created",
        (response or {}).get("id", ""),
        response=response if isinstance(response, dict) else None,
    )


#: Formats Onshape will export synchronously, answering with a redirect to the
#: finished file instead of running a translation job. Two calls against about
#: a dozen. STL is the only one the config schema can currently ask for; the
#: others are here because the endpoint takes them and the next format added
#: should not have to rediscover that.
SYNCHRONOUS_FORMATS = {"STL"}


def _export_route(sync: Any) -> str:
    """Which export path this one should take, and why it is not automatic.

    The synchronous export is six times cheaper but gives up control over
    tessellation: no chord tolerance, no angle tolerance, no per-face control.
    So it is the default for the formats that support it, and any export that
    sets a tessellation option is sent the expensive way instead -- asking for
    a tolerance and silently not getting it is worse than paying for it.
    """
    if sync.format_name.upper() not in SYNCHRONOUS_FORMATS:
        return "translation"
    if sync.element_kind != "partstudios":
        return "translation"
    tessellation = {"angleTolerance", "chordTolerance", "maximumChordLength", "resolution"}
    if tessellation & set(sync.options or {}):
        return "translation"
    return "synchronous"


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
        route = _export_route(sync)
        if route == "synchronous":
            data = client.export_part_studio_stl(
                ref,
                mode=str(sync.options.get("mode", "binary")),
                units=str(sync.options.get("units", "millimeter")),
            )
            cost = "2 calls, synchronous"
        else:
            job = client.start_translation(
                ref, sync.format_name, element_kind=sync.element_kind, extra=sync.options
            )
            finished = client.wait_for_translation(job["id"])

            foreign_ids = finished.get("resultExternalDataIds") or []
            if not foreign_ids:
                results.append(
                    SyncResult(
                        project.name, "pull", target, "skipped", "translation returned no data"
                    )
                )
                continue

            data = client.download_external_data(project.document_id, foreign_ids[0])
            cost = "translation job"

        destination = project.output_path(sync)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.is_file() and destination.read_bytes() == data:
            results.append(SyncResult(project.name, "pull", target, "unchanged"))
            continue

        destination.write_bytes(data)
        results.append(
            SyncResult(project.name, "pull", target, "updated", f"{len(data)} bytes, {cost}")
        )
    return results

"""Parsing and validation for a project's `onshape.yml`.

One file per project directory. It is the mapping from repo paths to Onshape
document/workspace/element ids, and it is what lets a single CI workflow serve
every project in the repo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .client import ElementRef

VALID_ELEMENT_KINDS = {"partstudios", "assemblies", "drawings", "blobelements"}

# Scaffolded configs ship with ids like REPLACE_WITH_DOCUMENT_ID. Sending one to
# Onshape earns a 404 that reads like a real failure, so they are detected and
# skipped instead -- which lets you configure one project at a time without the
# rest of the repo breaking the run.
PLACEHOLDER_PREFIX = "REPLACE_WITH"


def is_placeholder(value: str) -> bool:
    """True if this id is still the scaffold's placeholder rather than a real id."""
    return value.strip().upper().startswith(PLACEHOLDER_PREFIX)


class ConfigError(ValueError):
    """An onshape.yml is missing a field or has an unusable value."""


@dataclass(frozen=True)
class FeatureStudioSync:
    """One FeatureScript file pushed from git into an Onshape Feature Studio."""

    source: Path
    element_id: str


@dataclass(frozen=True)
class ExportSync:
    """One artifact translated out of Onshape and written into the repo."""

    element_id: str
    format_name: str
    output: Path
    element_kind: str = "partstudios"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    document_id: str
    workspace_id: str
    root: Path
    feature_studios: tuple[FeatureStudioSync, ...] = ()
    exports: tuple[ExportSync, ...] = ()

    def ref(self, element_id: str) -> ElementRef:
        return ElementRef(self.document_id, self.workspace_id, element_id)

    @property
    def unconfigured(self) -> tuple[str, ...]:
        """Which document-level ids are still placeholders. Empty means ready."""
        pending = []
        if is_placeholder(self.document_id):
            pending.append("document.id")
        if is_placeholder(self.workspace_id):
            pending.append("document.workspace")
        return tuple(pending)

    def source_path(self, sync: FeatureStudioSync) -> Path:
        return self.root / sync.source

    def output_path(self, sync: ExportSync) -> Path:
        return self.root / sync.output


def _require(mapping: dict, key: str, where: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"{where}: missing required key '{key}'")
    return mapping[key]


def load_project(path: Path) -> ProjectConfig:
    """Load one onshape.yml. `path` may be the file or its project directory."""
    path = Path(path)
    config_path = path / "onshape.yml" if path.is_dir() else path
    if not config_path.is_file():
        raise ConfigError(f"no onshape.yml at {config_path}")

    raw = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: expected a mapping at the top level")

    where = str(config_path)
    document = _require(raw, "document", where)
    if not isinstance(document, dict):
        raise ConfigError(f"{where}: 'document' must be a mapping")

    studios = []
    for i, entry in enumerate(raw.get("feature_studios") or []):
        item_where = f"{where}: feature_studios[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{item_where}: expected a mapping")
        studios.append(
            FeatureStudioSync(
                source=Path(_require(entry, "source", item_where)),
                element_id=str(_require(entry, "element", item_where)),
            )
        )

    exports = []
    for i, entry in enumerate(raw.get("exports") or []):
        item_where = f"{where}: exports[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{item_where}: expected a mapping")
        kind = str(entry.get("kind", "partstudios"))
        if kind not in VALID_ELEMENT_KINDS:
            raise ConfigError(
                f"{item_where}: kind '{kind}' is not one of {sorted(VALID_ELEMENT_KINDS)}"
            )
        options = entry.get("options") or {}
        if not isinstance(options, dict):
            raise ConfigError(f"{item_where}: 'options' must be a mapping")
        exports.append(
            ExportSync(
                element_id=str(_require(entry, "element", item_where)),
                format_name=str(_require(entry, "format", item_where)),
                output=Path(_require(entry, "output", item_where)),
                element_kind=kind,
                options=options,
            )
        )

    return ProjectConfig(
        name=str(raw.get("project") or config_path.parent.name),
        document_id=str(_require(document, "id", where)),
        workspace_id=str(_require(document, "workspace", where)),
        root=config_path.parent,
        feature_studios=tuple(studios),
        exports=tuple(exports),
    )


def discover_projects(root: Path) -> list[ProjectConfig]:
    """Find every project under `root`, sorted by path for stable CI output."""
    return [load_project(p) for p in sorted(Path(root).rglob("onshape.yml"))]

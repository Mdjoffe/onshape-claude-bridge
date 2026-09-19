from pathlib import Path

import pytest

from onshape_bridge.config import ConfigError, discover_projects, load_project

VALID = """
project: dock
document:
  id: DOC123
  workspace: WS456
feature_studios:
  - source: featurescript/dock.fs
    element: FS789
exports:
  - element: PS012
    format: STEP
    output: exports/dock.step
  - element: PS012
    kind: assemblies
    format: STL
    output: exports/dock.stl
    options:
      mode: binary
"""


def write(tmp_path: Path, body: str, name: str = "proj") -> Path:
    project = tmp_path / name
    project.mkdir(parents=True)
    (project / "onshape.yml").write_text(body)
    return project


def test_loads_full_config(tmp_path):
    config = load_project(write(tmp_path, VALID))

    assert config.name == "dock"
    assert config.document_id == "DOC123"
    assert config.ref("E1").path_suffix == "d/DOC123/w/WS456/e/E1"
    assert [s.element_id for s in config.feature_studios] == ["FS789"]
    assert [e.format_name for e in config.exports] == ["STEP", "STL"]
    assert config.exports[1].element_kind == "assemblies"
    assert config.exports[1].options == {"mode": "binary"}


def test_paths_resolve_against_project_root(tmp_path):
    project = write(tmp_path, VALID)
    config = load_project(project)

    assert config.source_path(config.feature_studios[0]) == project / "featurescript/dock.fs"
    assert config.output_path(config.exports[0]) == project / "exports/dock.step"


def test_name_defaults_to_directory(tmp_path):
    config = load_project(write(tmp_path, "document:\n  id: D\n  workspace: W\n", name="widget"))
    assert config.name == "widget"


def test_accepts_file_path_as_well_as_directory(tmp_path):
    project = write(tmp_path, VALID)
    assert load_project(project / "onshape.yml").document_id == "DOC123"


@pytest.mark.parametrize(
    "body, expected",
    [
        ("feature_studios: []\n", "missing required key 'document'"),
        ("document:\n  id: D\n", "missing required key 'workspace'"),
        (
            "document:\n  id: D\n  workspace: W\nexports:\n"
            "  - element: E\n    format: STEP\n    output: o\n    kind: bogus\n",
            "not one of",
        ),
        (
            "document:\n  id: D\n  workspace: W\nfeature_studios:\n  - source: a.fs\n",
            "missing required key 'element'",
        ),
    ],
)
def test_rejects_bad_config(tmp_path, body, expected):
    with pytest.raises(ConfigError, match=expected):
        load_project(write(tmp_path, body))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="no onshape.yml"):
        load_project(tmp_path / "nope")


def test_discover_walks_a_tree_in_stable_order(tmp_path):
    write(tmp_path / "projects", VALID, name="beta")
    write(tmp_path / "projects", VALID.replace("project: dock", "project: alpha"), name="alpha")

    assert [p.name for p in discover_projects(tmp_path)] == ["alpha", "dock"]

from pathlib import Path

from onshape_bridge.config import load_project
from onshape_bridge.sync import pull_exports, push_feature_studios

CONFIG = """
project: dock
document:
  id: DOC123
  workspace: WS456
feature_studios:
  - source: featurescript/dock.fs
    element: FS789
"""


class FakeClient:
    """Records calls instead of reaching the network."""

    def __init__(self, remote_contents=""):
        self.remote_contents = remote_contents
        self.updates: list[tuple[str, str]] = []

    def get_feature_studio_contents(self, ref):
        return self.remote_contents

    def update_feature_studio_contents(self, ref, contents):
        self.updates.append((ref.element_id, contents))
        return {}


def make_project(tmp_path: Path, source: str | None = "FeatureScript 1234;\n"):
    project = tmp_path / "dock"
    (project / "featurescript").mkdir(parents=True)
    (project / "onshape.yml").write_text(CONFIG)
    if source is not None:
        (project / "featurescript" / "dock.fs").write_text(source)
    return load_project(project)


def test_push_uploads_when_contents_differ(tmp_path):
    project = make_project(tmp_path)
    client = FakeClient(remote_contents="stale")

    results = push_feature_studios(client, project)

    assert [r.status for r in results] == ["updated"]
    assert client.updates == [("FS789", "FeatureScript 1234;\n")]


def test_push_is_a_noop_when_already_in_sync(tmp_path):
    project = make_project(tmp_path)
    client = FakeClient(remote_contents="FeatureScript 1234;\n")

    results = push_feature_studios(client, project)

    assert [r.status for r in results] == ["unchanged"]
    assert client.updates == []


def test_dry_run_never_writes(tmp_path):
    project = make_project(tmp_path)
    client = FakeClient(remote_contents="stale")

    results = push_feature_studios(client, project, dry_run=True)

    assert [r.status for r in results] == ["would-update"]
    assert client.updates == []


def test_push_skips_a_source_missing_from_the_repo(tmp_path):
    project = make_project(tmp_path, source=None)
    client = FakeClient()

    results = push_feature_studios(client, project)

    assert [r.status for r in results] == ["skipped"]
    assert client.updates == []


def test_pull_with_nothing_configured(tmp_path):
    assert pull_exports(FakeClient(), make_project(tmp_path)) == []

from pathlib import Path

from onshape_bridge.client import ElementRef
from onshape_bridge.config import load_project
from onshape_bridge.sync import (
    SyncResult,
    pull_exports,
    push_feature_studios,
    response_shape,
    restore_feature_studio,
    tag_version,
)

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
        self.update_response: dict = {}

    def get_feature_studio_contents(self, ref):
        return self.remote_contents

    def update_feature_studio_contents(self, ref, contents):
        self.updates.append((ref.element_id, contents))
        return self.update_response


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


PLACEHOLDER_CONFIG = """
project: unset
document:
  id: REPLACE_WITH_DOCUMENT_ID
  workspace: REPLACE_WITH_WORKSPACE_ID
feature_studios:
  - source: featurescript/dock.fs
    element: REPLACE_WITH_FEATURE_STUDIO_ELEMENT_ID
"""

PLACEHOLDER_ELEMENT_ONLY = """
project: half-set
document:
  id: DOC123
  workspace: WS456
feature_studios:
  - source: featurescript/dock.fs
    element: REPLACE_WITH_FEATURE_STUDIO_ELEMENT_ID
"""


class ExplodingClient(FakeClient):
    """Any API call is a failure: unconfigured projects must not reach the network."""

    def get_feature_studio_contents(self, ref):
        raise AssertionError(f"should not have called Onshape for {ref}")


def write_project(tmp_path: Path, config: str, name: str):
    project = tmp_path / name
    (project / "featurescript").mkdir(parents=True)
    (project / "onshape.yml").write_text(config)
    (project / "featurescript" / "dock.fs").write_text("FeatureScript 1234;\n")
    return load_project(project)


def test_placeholder_document_skips_without_touching_onshape(tmp_path):
    project = write_project(tmp_path, PLACEHOLDER_CONFIG, "unset")

    results = push_feature_studios(ExplodingClient(), project)

    assert [r.status for r in results] == ["unconfigured"]
    assert "document.id" in results[0].detail
    assert "document.workspace" in results[0].detail


def test_placeholder_element_skips_only_that_entry(tmp_path):
    project = write_project(tmp_path, PLACEHOLDER_ELEMENT_ONLY, "half-set")

    results = push_feature_studios(ExplodingClient(), project)

    assert [r.status for r in results] == ["unconfigured"]
    assert results[0].detail == "placeholder element id"


def test_pull_also_skips_an_unconfigured_project(tmp_path):
    project = write_project(tmp_path, PLACEHOLDER_CONFIG, "unset")

    results = pull_exports(ExplodingClient(), project)

    assert [r.status for r in results] == ["unconfigured"]


class CountingClient(FakeClient):
    """Counts reads so tests can assert the call cost of a push."""

    def __init__(self, remote_contents=""):
        super().__init__(remote_contents)
        self.reads = 0

    def get_feature_studio_contents(self, ref):
        self.reads += 1
        return super().get_feature_studio_contents(ref)


def test_assume_changed_skips_the_comparison_read(tmp_path):
    project = make_project(tmp_path)
    client = CountingClient(remote_contents="stale")

    results = push_feature_studios(client, project, assume_changed=True)

    assert client.reads == 0, "the comparison read is the call we are saving"
    assert [r.status for r in results] == ["updated"]
    assert client.updates == [("FS789", "FeatureScript 1234;\n")]


def test_default_still_compares_before_writing(tmp_path):
    project = make_project(tmp_path)
    client = CountingClient(remote_contents="stale")

    push_feature_studios(client, project)

    assert client.reads == 1


def test_assume_changed_uploads_even_when_identical(tmp_path):
    """The documented cost of the shortcut: a needless microversion."""
    project = make_project(tmp_path)
    client = CountingClient(remote_contents="FeatureScript 1234;\n")

    results = push_feature_studios(client, project, assume_changed=True)

    assert [r.status for r in results] == ["updated"]
    assert len(client.updates) == 1


def test_assume_changed_still_skips_unconfigured_projects(tmp_path):
    """The shortcut must not bypass the placeholder guard and spend a call."""
    project = write_project(tmp_path, PLACEHOLDER_CONFIG, "unset")

    results = push_feature_studios(ExplodingClient(), project, assume_changed=True)

    assert [r.status for r in results] == ["unconfigured"]


def test_assume_changed_dry_run_writes_nothing(tmp_path):
    project = make_project(tmp_path)
    client = CountingClient(remote_contents="stale")

    results = push_feature_studios(client, project, dry_run=True, assume_changed=True)

    assert [r.status for r in results] == ["would-update"]
    assert client.updates == []
    assert client.reads == 0


def test_push_keeps_what_onshape_returned(tmp_path):
    """A write response is the one thing a caller cannot fetch again for free."""
    project = make_project(tmp_path)
    client = FakeClient(remote_contents="stale")
    client.update_response = {"contents": "...", "notices": [{"message": "undefined variable"}]}

    results = push_feature_studios(client, project)

    assert results[0].response == client.update_response


def test_response_shape_names_the_keys_that_arrived():
    """We do not know what a compile complaint is called, so print what came."""
    assert response_shape({"contents": "x", "notices": [], "libraryVersion": 2}) == (
        "returned: libraryVersion, notices"
    )


def test_response_shape_drops_our_own_source_echoed_back():
    assert response_shape({"contents": "FeatureScript 1234;"}) == ""


def test_response_shape_says_nothing_when_there_was_no_write():
    assert response_shape(None) == ""
    assert response_shape({}) == ""


def test_a_result_line_shows_the_returned_keys(tmp_path):
    project = make_project(tmp_path)
    client = FakeClient(remote_contents="stale")
    client.update_response = {"notices": []}

    line = str(push_feature_studios(client, project)[0])

    assert "returned: notices" in line


def test_a_result_line_is_unchanged_when_nothing_came_back(tmp_path):
    project = make_project(tmp_path)
    client = FakeClient(remote_contents="stale")

    assert str(push_feature_studios(client, project)[0]).endswith("featurescript/dock.fs")


# -- export routing --------------------------------------------------------


class ExportClient(FakeClient):
    """Records which export path a pull took."""

    def __init__(self):
        super().__init__()
        self.route: list[str] = []
        self.versions: list[tuple[str, str, str]] = []

    def export_part_studio_stl(self, ref, **kwargs):
        self.route.append("synchronous")
        self.stl_kwargs = kwargs
        return b"solid"

    def start_translation(self, ref, fmt, **kwargs):
        self.route.append("translation")
        return {"id": "JOB"}

    def wait_for_translation(self, job_id):
        return {"resultExternalDataIds": ["FID"]}

    def download_external_data(self, document_id, foreign_id):
        return b"solid"

    def create_version(self, document_id, workspace_id, name):
        self.versions.append((document_id, workspace_id, name))
        return {"id": "V1"}


EXPORTS = """
project: dock
document:
  id: DOC123
  workspace: WS456
exports:
  - element: EL789
    kind: partstudios
    format: {fmt}
    output: exports/out.{ext}
{options}
"""


def export_project(tmp_path, fmt="STL", ext="stl", options=""):
    project = tmp_path / "dock"
    project.mkdir(parents=True)
    (project / "onshape.yml").write_text(
        EXPORTS.format(fmt=fmt, ext=ext, options=options)
    )
    return load_project(project)


def test_stl_takes_the_two_call_synchronous_path(tmp_path):
    client = ExportClient()
    results = pull_exports(client, export_project(tmp_path))
    assert client.route == ["synchronous"]
    assert "synchronous" in results[0].detail


def test_step_still_takes_the_translation_job(tmp_path):
    """Only some formats have a synchronous endpoint; STEP is not one."""
    client = ExportClient()
    pull_exports(client, export_project(tmp_path, fmt="STEP", ext="step"))
    assert client.route == ["translation"]


def test_a_tessellation_option_buys_back_the_expensive_path(tmp_path):
    """Asking for a tolerance and silently not getting it is worse than paying."""
    client = ExportClient()
    options = "    options:\n      chordTolerance: 0.01\n"
    pull_exports(client, export_project(tmp_path, options=options))
    assert client.route == ["translation"]


def test_plain_stl_options_do_not_force_the_expensive_path(tmp_path):
    client = ExportClient()
    options = "    options:\n      mode: binary\n      units: millimeter\n"
    pull_exports(client, export_project(tmp_path, options=options))
    assert client.route == ["synchronous"]
    assert client.stl_kwargs == {"mode": "binary", "units": "millimeter"}


# -- version tagging -------------------------------------------------------


def test_a_version_is_created_when_something_was_pushed(tmp_path):
    client = ExportClient()
    pushed = [SyncResult("dock", "push", "a.fs", "updated")]
    result = tag_version(client, export_project(tmp_path), "commit abc123", pushed)
    assert client.versions == [("DOC123", "WS456", "commit abc123")]
    assert result.status == "created" and result.detail == "V1"


def test_no_version_when_nothing_changed(tmp_path):
    """Versions are immutable and accumulate forever; an empty one is waste."""
    client = ExportClient()
    unchanged = [SyncResult("dock", "push", "a.fs", "unchanged")]
    assert tag_version(client, export_project(tmp_path), "commit abc", unchanged) is None
    assert client.versions == []


def test_a_version_can_be_forced_without_consulting_results(tmp_path):
    client = ExportClient()
    assert tag_version(client, export_project(tmp_path), "release 1", None) is not None


# -- rollback from a version -----------------------------------------------


class VersionClient(FakeClient):
    """Serves different source for the workspace and for a version."""

    def __init__(self, at_version="old source", in_workspace="new source"):
        super().__init__()
        self.at_version = at_version
        self.in_workspace = in_workspace
        self.reads: list[str] = []

    def get_feature_studio_contents(self, ref):
        self.reads.append(ref.path_suffix)
        return self.at_version if ref.wvm == "v" else self.in_workspace


def test_restore_reads_the_version_and_writes_the_workspace():
    client = VersionClient()
    ref = ElementRef("DOC", "WS", "EL")

    result = restore_feature_studio(client, ref, "V1")

    assert client.reads == ["d/DOC/v/V1/e/EL"]
    assert client.updates == [("EL", "old source")]
    assert result.status == "updated"


def test_restore_costs_two_reads_only_when_asked_to_compare():
    client = VersionClient()
    restore_feature_studio(client, ElementRef("DOC", "WS", "EL"), "V1", compare=True)
    assert client.reads == ["d/DOC/v/V1/e/EL", "d/DOC/w/WS/e/EL"]


def test_restore_is_a_noop_when_comparing_and_already_there():
    client = VersionClient(at_version="same", in_workspace="same")
    result = restore_feature_studio(client, ElementRef("DOC", "WS", "EL"), "V1", compare=True)
    assert result.status == "unchanged"
    assert client.updates == []


def test_restore_writes_blind_without_compare_even_if_identical():
    """Two calls, and a needless microversion, is the stated trade."""
    client = VersionClient(at_version="same", in_workspace="same")
    restore_feature_studio(client, ElementRef("DOC", "WS", "EL"), "V1")
    assert client.updates == [("EL", "same")]


def test_dry_run_reads_the_version_but_writes_nothing():
    client = VersionClient()
    result = restore_feature_studio(client, ElementRef("DOC", "WS", "EL"), "V1", dry_run=True)
    assert result.status == "would-update"
    assert client.updates == []


# -- immutable refs --------------------------------------------------------


def test_a_version_ref_addresses_v_not_w():
    assert ElementRef("D", "W", "E").at_version("V").path_suffix == "d/D/v/V/e/E"


def test_a_microversion_ref_addresses_m():
    assert ElementRef("D", "W", "E").at_microversion("M").path_suffix == "d/D/m/M/e/E"


def test_only_a_workspace_is_writable():
    ref = ElementRef("D", "W", "E")
    assert ref.writable
    assert not ref.at_version("V").writable
    assert not ref.at_microversion("M").writable

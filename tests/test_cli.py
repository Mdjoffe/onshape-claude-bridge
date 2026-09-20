import json

from onshape_bridge.cli import _capture, build_parser
from onshape_bridge.sync import SyncResult


def test_capture_writes_a_file_per_response(tmp_path):
    results = [
        SyncResult("dock", "push", "featurescript/dock.fs", "updated", response={"notices": []}),
        SyncResult("dock", "push", "featurescript/other.fs", "unchanged"),
    ]

    _capture(results, tmp_path)

    written = sorted(path.name for path in tmp_path.iterdir())
    assert written == ["dock-featurescript-dock-fs.json"]
    assert json.loads((tmp_path / written[0]).read_text()) == {"notices": []}


def test_capture_creates_the_directory_it_was_given(tmp_path):
    target = tmp_path / "captures" / "push"
    _capture([SyncResult("dock", "push", "a.fs", "updated", response={"ok": 1})], target)
    assert (target / "dock-a-fs.json").is_file()


def test_capture_is_a_noop_when_nothing_was_written(tmp_path):
    _capture([SyncResult("dock", "push", "a.fs", "unchanged")], tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_capture_defaults_to_off():
    for command in ("push", "pull"):
        assert build_parser().parse_args([command, "."]).capture is None


def test_capture_is_a_path():
    args = build_parser().parse_args(["push", ".", "--capture", "captures/push"])
    assert args.capture.name == "push"

"""Archive root normalization regressions."""

from pathlib import Path

import pytest

from backend.app.services.archive import flatten_single_root


@pytest.mark.parametrize("child_is_directory", [True, False])
def test_flatten_same_named_child_preserves_contents(tmp_path, child_is_directory):
    wrapper = tmp_path / "医疗任务"
    wrapper.mkdir()
    child = wrapper / wrapper.name
    if child_is_directory:
        child.mkdir()
        payload = child / "task.toml"
    else:
        payload = child
    payload.write_bytes(b"payload")
    (wrapper / "second.json").write_bytes(b"second")
    (wrapper / ".DS_Store").write_bytes(b"inner metadata")
    (tmp_path / ".DS_Store").write_bytes(b"outer metadata")
    (tmp_path / "__MACOSX").mkdir()

    flatten_single_root(tmp_path)

    expected = tmp_path / wrapper.name / "task.toml" if child_is_directory else tmp_path / wrapper.name
    assert expected.read_bytes() == b"payload"
    assert (tmp_path / "second.json").read_bytes() == b"second"
    assert (tmp_path / ".DS_Store").read_bytes() == b"outer metadata"
    assert not list(tmp_path.glob(".prt-flatten-*"))


def test_flatten_move_failure_restores_original_tree(tmp_path, monkeypatch):
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir()
    for name in ("one.json", "two.json"):
        (wrapper / name).write_text(name)
    original_rename = Path.rename
    moved = 0

    def fail_second_child(self, target):
        nonlocal moved
        if self.parent.name == "contents":
            moved += 1
            if moved == 2:
                raise OSError("simulated move failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_second_child)
    with pytest.raises(OSError, match="simulated move failure"):
        flatten_single_root(tmp_path)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["wrapper"]
    assert sorted(path.read_text() for path in wrapper.iterdir()) == ["one.json", "two.json"]

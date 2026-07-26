from __future__ import annotations

from scripts import check_public_tree


def test_deleted_index_path_is_not_a_privacy_failure(
    tmp_path, monkeypatch, capsys
) -> None:
    missing = tmp_path / "deleted-from-working-tree.md"
    monkeypatch.setattr(check_public_tree, "ROOT", tmp_path)
    monkeypatch.setattr(check_public_tree, "public_files", lambda: [missing])

    assert check_public_tree.main() == 0
    assert capsys.readouterr().out == "Public-tree privacy check passed: 0 files inspected.\n"

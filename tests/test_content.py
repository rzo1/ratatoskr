"""Wiki, LFS, issue/MR export (checkout) and their import on the target (push)."""

import json

import pytest
from conftest import SECRET, refs

from ratatoskr import cli, gitops
from ratatoskr.source import SourceError


@pytest.fixture
def exported(tmp_path, source_repo, source_wiki, write_csv, fake_source, run_cli):
    """inst/sub/repo1 cloned (code + wiki) and its issues/MRs exported."""
    csv_path = write_csv(
        [
            {
                "id": 7,
                "path_with_namespace": "inst/sub/repo1",
                "ssh_url_to_repo": source_repo,
                "web_url": f"{fake_source.base}/inst/sub/repo1",
                "default_branch": "main",
            }
        ]
    )
    mirrors = tmp_path / "mirrors"
    args = ("checkout", "--from-csv", csv_path, "--source-url", fake_source.base)
    assert run_cli(*args, "--protocol", "ssh", "--dest", mirrors) == 0
    return csv_path, mirrors


def push_args(csv_path, mirrors, *extra):
    return (
        "push",
        "--target-url",
        "gitlab.example.org",
        "--group",
        "dept/team",
        "--from-csv",
        csv_path,
        "--mirror-dir",
        mirrors,
        "--protocol",
        "ssh",
        "--flatten",
        "-y",
        *extra,
    )


# ---------------------------------------------------------------- checkout / export


def test_checkout_clones_wiki_and_exports(exported, fake_source):
    _, mirrors = exported
    assert refs(mirrors / "inst/sub/repo1.wiki.git") == ["refs/heads/main"]

    issues = mirrors / "inst/sub/repo1-issues"
    assert sorted(p.name for p in issues.glob("*.md")) == ["0001-login-fails.md", "0003-release.md"]
    md = (issues / "0001-login-fails.md").read_text()
    assert f"![shot](attachments/{SECRET}/shot.png)" in md
    assert "Same for me" in md and "## Activity (1)" in md
    assert (issues / f"attachments/{SECRET}/shot.png").read_bytes() == b"PNG-DATA"
    raw = json.loads((issues / "0001-login-fails.json").read_text())
    assert f"/uploads/{SECRET}/shot.png" in raw["item"]["description"]  # original kept
    assert [n["id"] for n in raw["notes"]] == [101, 102]

    mr = (mirrors / "inst/sub/repo1-merge-requests/0001-fix-login.md").read_text()
    assert "`fix` → `main`" in mr and "LGTM @bob" in mr
    meta = json.loads((mirrors / "inst/sub/repo1-meta.json").read_text())
    assert meta["labels"][0]["name"] == "bug"


def test_export_is_incremental(exported, fake_source, run_cli):
    csv_path, mirrors = exported
    fake_source.calls.clear()
    args = ("checkout", "--from-csv", csv_path, "--source-url", fake_source.base)
    assert run_cli(*args, "--protocol", "ssh", "--dest", mirrors) == 0
    assert not [c for c in fake_source.calls if c.endswith("/notes")]  # nothing changed


def test_checkout_without_merge_requests(
    tmp_path, source_repo, write_csv, fake_source, run_cli, capsys
):
    csv_path = write_csv([{"id": 7, "path_with_namespace": "a/b", "ssh_url_to_repo": source_repo}])
    args = ("checkout", "--from-csv", csv_path, "--source-url", fake_source.base, "--no-mrs")
    assert run_cli(*args, "--protocol", "ssh", "--dest", tmp_path / "m") == 0
    assert not any("merge_requests" in c for c in fake_source.calls)
    assert not (tmp_path / "m/a/b-merge-requests").exists()
    assert "2 issues" in capsys.readouterr().out


def test_export_tolerates_disabled_features_and_missing_attachments(
    tmp_path, source_repo, write_csv, fake_source, run_cli, capsys
):
    fake_source.lists["/projects/7/merge_requests"] = SourceError(403, "disabled")
    fake_source.files.clear()
    csv_path = write_csv([{"id": 7, "path_with_namespace": "a/b", "ssh_url_to_repo": source_repo}])
    args = ("checkout", "--from-csv", csv_path, "--source-url", fake_source.base)
    assert run_cli(*args, "--protocol", "ssh", "--dest", tmp_path / "m") == 0
    out = capsys.readouterr().out
    assert "0 merge-requests" in out and "1 attachments MISSING" in out
    md = (tmp_path / "m/a/b-issues/0001-login-fails.md").read_text()
    assert f"/uploads/{SECRET}/shot.png" in md  # link left as it was


def test_export_errors_fail_the_repo(
    tmp_path, source_repo, write_csv, fake_source, run_cli, capsys
):
    fake_source.lists["/projects/7/issues"] = SourceError(500, "boom")
    csv_path = write_csv([{"id": 7, "path_with_namespace": "a/b", "ssh_url_to_repo": source_repo}])
    args = ("checkout", "--from-csv", csv_path, "--source-url", fake_source.base)
    assert run_cli(*args, "--protocol", "ssh", "--dest", tmp_path / "m") == 1
    assert "FAILED   a/b" in capsys.readouterr().out


# ---------------------------------------------------------------- push / import


def test_push_imports_everything(exported, gitlab, run_cli, capsys):
    csv_path, mirrors = exported
    assert run_cli(*push_args(csv_path, mirrors)) == 0
    project = gitlab.projects["dept/team/repo1"]

    assert refs(project["_wiki"]) == ["refs/heads/main"]
    labels = {lb["name"]: lb["color"] for lb in project["_labels"]}
    assert labels == {"bug": "#ff0000", "release": "#6699cc"}
    assert [(m["title"], m["state"]) for m in project["_milestones"]] == [("v1.0", "closed")]

    issues = project["_issues"]
    assert sorted(issues) == [1, 3]  # numbers kept, no placeholder needed as owner
    first, third = issues[1], issues[3]
    assert first["state"] == "opened" and third["state"] == "closed"
    assert first["created_at"] == "2024-01-02T10:00:00.000Z"
    assert third["milestone_id"] == project["_milestones"][0]["id"]
    assert (
        "> Migrated from https://source.example.org/inst/sub/repo1/-/issues/1"
        in first["description"]
    )
    assert "cc `@alice`" in first["description"]  # mentions neutralized
    assert "Activity on the source (1)" in first["description"]
    upload_secret, (name, content) = next(iter(project["_uploads"].items()))
    assert (name, content) == ("shot.png", b"PNG-DATA")
    assert f"/-/project/{project['id']}/uploads/{upload_secret}/shot.png" in first["description"]

    [note] = first["_notes"]  # the system note is not a comment
    assert note["body"].startswith("> Dave (`@dave`) commented on 2024-01-02 11:00")
    assert "bob@example.org" in note["body"]  # e-mail addresses are not mentions
    assert note["created_at"] == "2024-01-02T11:00:00.000Z"

    wikis = project["_wikis"]
    assert set(wikis) == {"merge-requests/0001-fix-login", "merge-requests"}
    assert "LGTM `@bob`" in wikis["merge-requests/0001-fix-login"]
    assert "[Fix login](merge-requests/0001-fix-login)" in wikis["merge-requests"]
    out = capsys.readouterr().out
    assert "pushed wiki" in out
    assert "2 labels, 1 milestones, 2 issues (1 comments), 1 merge request pages created" in out


def test_push_import_is_idempotent(exported, gitlab, run_cli):
    csv_path, mirrors = exported
    assert run_cli(*push_args(csv_path, mirrors)) == 0
    before = len(gitlab.writes())
    assert run_cli(*push_args(csv_path, mirrors, "--update-existing")) == 0
    new_writes = gitlab.writes()[before:]
    assert [(m, p) for m, p, _ in new_writes if m == "POST"] == []
    assert [p for m, p, _ in new_writes if m == "PUT"] == [
        f"/projects/{gitlab.projects['dept/team/repo1']['id']}/wikis/merge-requests"
    ]  # only the MR index page is refreshed


def test_push_without_owner_rights_fills_gaps(exported, gitlab, run_cli):
    gitlab.access_level = 40
    csv_path, mirrors = exported
    assert run_cli(*push_args(csv_path, mirrors)) == 0
    issues = gitlab.projects["dept/team/repo1"]["_issues"]
    assert sorted(issues) == [1, 2, 3]
    assert issues[2]["title"] == "Placeholder for missing issue #2"
    assert issues[2]["state"] == "closed"
    assert issues[3]["title"] == "Release"
    assert "created_at" not in issues[1]["_notes"][0]


def test_push_options_for_mentions_and_merge_requests(exported, gitlab, run_cli):
    csv_path, mirrors = exported
    assert run_cli(*push_args(csv_path, mirrors, "--keep-mentions", "--no-mrs")) == 0
    project = gitlab.projects["dept/team/repo1"]
    assert "cc @alice" in project["_issues"][1]["description"]
    assert project["_wikis"] == {}


def test_push_dry_run_reports_import(exported, gitlab, run_cli, capsys):
    csv_path, mirrors = exported
    assert run_cli(*push_args(csv_path, mirrors, "--dry-run")) == 0
    assert gitlab.writes() == []
    out = capsys.readouterr().out
    assert "would push wiki" in out
    assert "would import 1 labels, 1 milestones, 2 issues, 1 merge requests as wiki pages" in out


def test_push_archives_after_import(exported, gitlab, run_cli, write_csv, source_repo):
    _, mirrors = exported
    csv_path = write_csv(
        [{"id": 7, "path_with_namespace": "inst/sub/repo1", "archived": "True"}], name="a.csv"
    )
    assert run_cli(*push_args(csv_path, mirrors, "--archive")) == 0
    project = gitlab.projects["dept/team/repo1"]
    assert project["archived"] is True
    assert sorted(project["_issues"]) == [1, 3]


# ---------------------------------------------------------------- LFS


def test_lfs_objects_are_pushed_before_the_code(exported, gitlab, run_cli, monkeypatch, capsys):
    csv_path, mirrors = exported
    calls = []
    monkeypatch.setattr(cli, "lfs_push", lambda url, repo, env: calls.append("lfs") or 3)
    real_push_refs = cli.push_refs

    def push_refs(*args):
        calls.append("git")
        return real_push_refs(*args)

    monkeypatch.setattr(cli, "push_refs", push_refs)
    assert run_cli(*push_args(csv_path, mirrors)) == 0
    assert calls == ["lfs", "git"]
    assert "pushed 3 LFS objects" in capsys.readouterr().out


def test_lfs_push_needs_git_lfs_when_objects_exist(tmp_path, monkeypatch):
    repo = tmp_path / "repo.git"
    assert gitops.lfs_push("url", repo, {}) == 0  # no objects: nothing to do
    (repo / "lfs/objects/ab/cd").mkdir(parents=True)
    (repo / "lfs/objects/ab/cd/abcd1234").write_bytes(b"x")
    assert gitops.lfs_object_count(repo) == 1
    monkeypatch.setattr(gitops, "has_git_lfs", lambda: False)
    with pytest.raises(RuntimeError, match="git-lfs is not installed"):
        gitops.lfs_push("url", repo, {})
    assert gitops.lfs_fetch(repo, {}) == (None, 0)

import pytest
from conftest import git, refs

from ratatoskr import gitops

GIT_ONLY = ("--no-issues", "--no-mrs")

ALL_REFS = ["refs/heads/dev", "refs/heads/main", "refs/tags/v1", "refs/tags/v2"]


@pytest.fixture
def mirrored(tmp_path, source_repo, write_csv, run_cli):
    """Two repos in different source subgroups, already cloned via `checkout`."""
    csv_path = write_csv(
        [
            {
                "path_with_namespace": "inst/sub/repo1",
                "name": "Repo One",
                "ssh_url_to_repo": source_repo,
                "default_branch": "dev",
            },
            {
                "path_with_namespace": "inst/other/deep/repo2",
                "ssh_url_to_repo": source_repo,
                "default_branch": "main",
                "archived": "True",
            },
        ]
    )
    mirrors = tmp_path / "mirrors"
    assert (
        run_cli(
            "checkout", "--from-csv", csv_path, "--protocol", "ssh", "--dest", mirrors, *GIT_ONLY
        )
        == 0
    )
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
        *extra,
    )


def test_push_asks_layout_creates_subgroups_and_pushes(mirrored, gitlab, run_cli, answers):
    csv_path, mirrors = mirrored
    answers("1", "y")  # strip "inst", confirm plan

    assert run_cli(*push_args(csv_path, mirrors)) == 0

    assert set(gitlab.groups) == {
        "dept/team",
        "dept/team/sub",
        "dept/team/other",
        "dept/team/other/deep",
    }
    repo1 = gitlab.projects["dept/team/sub/repo1"]
    repo2 = gitlab.projects["dept/team/other/deep/repo2"]
    assert refs(repo1["_repo"]) == ALL_REFS
    assert refs(repo2["_repo"]) == ALL_REFS
    assert repo1["name"] == "Repo One"
    assert repo1["visibility"] == "private"
    assert repo1["default_branch"] == "dev"
    assert repo2["archived"] is False  # only with --archive


def test_push_rerun_skips_unless_update_existing(mirrored, gitlab, run_cli, source_repo, capsys):
    csv_path, mirrors = mirrored
    assert run_cli(*push_args(csv_path, mirrors, "--flatten", "-y")) == 0
    capsys.readouterr()

    assert run_cli(*push_args(csv_path, mirrors, "--flatten", "-y")) == 0
    assert "0 ok, 2 skipped" in capsys.readouterr().out

    git("branch", "feature", cwd=source_repo)
    git("-C", str(mirrors / "inst/sub/repo1.git"), "remote", "update")
    assert run_cli(*push_args(csv_path, mirrors, "--flatten", "-y", "--update-existing")) == 0
    assert "refs/heads/feature" in refs(gitlab.projects["dept/team/repo1"]["_repo"])


def test_push_dry_run_writes_nothing(mirrored, gitlab, run_cli, capsys):
    csv_path, mirrors = mirrored
    assert run_cli(*push_args(csv_path, mirrors, "--strip", "1", "--dry-run")) == 0
    assert gitlab.writes() == []
    out = capsys.readouterr().out
    assert "would create subgroup dept/team/other/deep" in out
    assert "would create project" in out


def test_push_existing_subgroup_is_reused_and_archive(mirrored, gitlab, run_cli):
    csv_path, mirrors = mirrored
    gitlab.groups["dept/team/sub"] = {"id": 7, "full_path": "dept/team/sub"}
    assert run_cli(*push_args(csv_path, mirrors, "--strip", "1", "-y", "--archive")) == 0
    created = [data["path"] for method, path, data in gitlab.writes() if path == "/groups"]
    assert created == ["other", "deep"]
    assert gitlab.projects["dept/team/other/deep/repo2"]["archived"] is True


def test_push_target_path_from_csv(tmp_path, source_repo, write_csv, gitlab, run_cli, answers):
    csv_path = write_csv(
        [
            {
                "path_with_namespace": "inst/repo",
                "ssh_url_to_repo": source_repo,
                "target_path": "/renamed/new-repo/",
            }
        ]
    )
    mirrors = tmp_path / "mirrors"
    run_cli("checkout", "--from-csv", csv_path, "--protocol", "ssh", "--dest", mirrors, *GIT_ONLY)
    answers("y")  # no layout question: every row has a target_path
    assert run_cli(*push_args(csv_path, mirrors)) == 0
    assert refs(gitlab.projects["dept/team/renamed/new-repo"]["_repo"]) == ALL_REFS


def test_push_from_working_copy(tmp_path, source_repo, write_csv, gitlab, run_cli):
    csv_path = write_csv([{"path_with_namespace": "inst/repo", "ssh_url_to_repo": source_repo}])
    work = tmp_path / "work"
    run_cli(
        "checkout",
        "--from-csv",
        csv_path,
        "--protocol",
        "ssh",
        "--dest",
        work,
        "--working-copy",
        *GIT_ONLY,
    )
    assert run_cli(*push_args(csv_path, work, "--flatten", "-y")) == 0
    # all remote branches arrive, but not the origin/HEAD symref
    assert refs(gitlab.projects["dept/team/repo"]["_repo"]) == ALL_REFS


def test_push_missing_clone_fails_but_continues(
    mirrored, tmp_path, source_repo, write_csv, gitlab, run_cli, capsys
):
    _, mirrors = mirrored
    csv_path = write_csv(
        [
            {"path_with_namespace": "inst/not-cloned", "ssh_url_to_repo": source_repo},
            {"path_with_namespace": "inst/sub/repo1", "ssh_url_to_repo": source_repo},
        ],
        name="other.csv",
    )
    assert run_cli(*push_args(csv_path, mirrors, "--flatten", "-y")) == 1
    out = capsys.readouterr().out
    assert "run 'ratatoskr checkout' first" in out
    assert "1 ok, 0 skipped, 1 failed" in out
    assert "dept/team/repo1" in gitlab.projects


def test_push_empty_source_creates_project_only(tmp_path, write_csv, gitlab, run_cli):
    csv_path = write_csv([{"path_with_namespace": "inst/empty", "empty_repo": "True"}])
    assert run_cli(*push_args(csv_path, tmp_path / "none", "--flatten", "-y")) == 0
    assert refs(gitlab.projects["dept/team/empty"]["_repo"]) == []


def test_push_requires_api_scope(mirrored, gitlab, run_cli):
    csv_path, mirrors = mirrored
    gitlab.scopes = ["read_api"]
    with pytest.raises(SystemExit, match="'api' scope"):
        run_cli(*push_args(csv_path, mirrors, "--flatten", "-y"))


def test_push_unknown_group(mirrored, gitlab, run_cli):
    csv_path, mirrors = mirrored
    args = list(push_args(csv_path, mirrors, "--flatten", "-y"))
    args[args.index("dept/team")] = "nope"
    with pytest.raises(SystemExit, match="'nope' not found"):
        run_cli(*args)


def test_push_group_by_numeric_id(mirrored, gitlab, run_cli):
    csv_path, mirrors = mirrored
    args = list(push_args(csv_path, mirrors, "--flatten", "-y"))
    args[args.index("dept/team")] = "1"
    assert run_cli(*args) == 0
    assert {"dept/team/repo1", "dept/team/repo2"} <= set(gitlab.projects)


def test_push_reports_branches_tags_and_verifies(mirrored, gitlab, run_cli, capsys):
    csv_path, mirrors = mirrored
    assert run_cli(*push_args(csv_path, mirrors, "--flatten", "-y")) == 0
    out = capsys.readouterr().out
    assert out.count("pushed 2 branches, 2 tags") == 2
    assert out.count("verified: all branches and tags match") == 2
    target = gitlab.projects["dept/team/repo1"]["_repo"]
    # the annotated tag arrives as tag object, not just as the commit it points to
    assert git("cat-file", "-t", "refs/tags/v2", cwd=target).strip() == "tag"


def test_push_prune_removes_deleted_refs_only_on_request(mirrored, gitlab, run_cli, source_repo):
    csv_path, mirrors = mirrored
    assert run_cli(*push_args(csv_path, mirrors, "--flatten", "-y")) == 0
    git("branch", "-D", "dev", cwd=source_repo)
    git("tag", "-d", "v1", cwd=source_repo)
    git("-C", str(mirrors / "inst/sub/repo1.git"), "remote", "update", "--prune")
    target = gitlab.projects["dept/team/repo1"]["_repo"]

    sync = push_args(csv_path, mirrors, "--flatten", "-y", "--update-existing")
    assert run_cli(*sync) == 0
    assert refs(target) == ALL_REFS  # without --prune nothing is deleted

    assert run_cli(*sync, "--prune") == 0
    assert refs(target) == ["refs/heads/main", "refs/tags/v2"]


def test_push_fails_when_verification_finds_differences(
    mirrored, gitlab, run_cli, monkeypatch, capsys
):
    csv_path, mirrors = mirrored
    monkeypatch.setattr(gitops, "remote_refs", lambda url, repo, env: {})
    assert run_cli(*push_args(csv_path, mirrors, "--flatten", "-y")) == 1
    assert "verification failed, 4 refs missing/different" in capsys.readouterr().out

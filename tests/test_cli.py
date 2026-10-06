from types import SimpleNamespace

import pytest

from ratatoskr import cli, source


def test_with_scheme():
    assert source.with_scheme("gitlab.example.org/") == "https://gitlab.example.org"
    assert source.with_scheme("http://localhost:8080") == "http://localhost:8080"


def test_next_link():
    header = (
        '<https://gl/api/v4/projects?id_after=42&page=2>; rel="next", '
        '<https://gl/api/v4/projects?page=1>; rel="first"'
    )
    assert source.next_link(header) == "https://gl/api/v4/projects?id_after=42&page=2"
    assert source.next_link('<https://gl/api/v4/projects?page=1>; rel="first"') is None
    assert source.next_link("") is None


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        ({"project_access": {"access_level": 30}, "group_access": None}, "developer"),
        ({"project_access": {"access_level": 20}, "group_access": {"access_level": 50}}, "owner"),
        ({"project_access": None, "group_access": None}, "none (visible only)"),
        (None, "none (visible only)"),
    ],
)
def test_my_access(permissions, expected):
    assert source.my_access({"permissions": permissions}) == expected


def test_write_outputs_and_read_selection(tmp_path):
    projects = [
        {
            "path_with_namespace": "grp/a",
            "ssh_url_to_repo": "git@gl:grp/a.git",
            "permissions": {"project_access": {"access_level": 40}},
        },
        {"path_with_namespace": "grp/b", "ssh_url_to_repo": "git@gl:grp/b.git"},
    ]
    source.write_outputs(projects, tmp_path)

    assert (tmp_path / "repos.txt").read_text() == "git@gl:grp/a.git\ngit@gl:grp/b.git\n"
    csv_path = tmp_path / "repos.csv"
    rows = source.read_csv_selection(csv_path)
    assert [r["path_with_namespace"] for r in rows] == ["grp/a", "grp/b"]
    assert rows[0]["my_access"] == "maintainer"

    csv_path.write_text(csv_path.read_text().replace("yes,,maintainer", "no,,maintainer"))
    assert [r["path_with_namespace"] for r in source.read_csv_selection(csv_path)] == ["grp/b"]


@pytest.mark.parametrize(
    ("strip", "flatten", "target_path", "expected"),
    [
        (0, False, "", "inst/dept/repo"),
        (1, False, "", "dept/repo"),
        (2, False, "", "repo"),
        (9, False, "", "repo"),  # never strips the repo name itself
        (0, True, "", "repo"),
        (1, False, "/custom/name/", "custom/name"),  # explicit target_path wins
    ],
)
def test_target_rel_path(strip, flatten, target_path, expected):
    args = SimpleNamespace(strip=strip, flatten=flatten)
    row = {"path_with_namespace": "inst/dept/repo", "target_path": target_path}
    assert cli.target_rel_path(row, args) == expected


def test_ask_layout_retries_until_valid(answers, capsys):
    repos = [{"path_with_namespace": "inst/a/r1"}, {"path_with_namespace": "inst/b/c/r2"}]
    answers("7", "x", "1")
    assert cli.ask_layout(repos, "dept/team") == (1, False)
    out = capsys.readouterr().out
    assert "[1] strip 1 level: dept/team/a/<repo>, dept/team/b/c/<repo>" in out
    assert "[3]" not in out  # deepest offered level is the shallowest source namespace


def test_ask_layout_flatten_and_default(answers):
    repos = [{"path_with_namespace": "inst/a/r1"}]
    answers("f")
    assert cli.ask_layout(repos, "t") == (0, True)
    answers("")
    assert cli.ask_layout(repos, "t") == (0, False)


def test_ask_layout_skipped_when_all_have_target_path(answers):
    answers()  # no prompt expected
    assert cli.ask_layout([{"path_with_namespace": "a/b", "target_path": "x"}], "t") == (0, False)


def test_confirm_plan_rejects_collisions():
    args = SimpleNamespace(strip=0, flatten=True, dry_run=False, yes=True)
    repos = [{"path_with_namespace": "a/repo"}, {"path_with_namespace": "b/repo"}]
    with pytest.raises(SystemExit, match="dept/team/repo"):
        cli.confirm_plan(repos, "dept/team", args)


def test_confirm_plan_abort(answers):
    args = SimpleNamespace(strip=0, flatten=False, dry_run=False, yes=False)
    answers("n")
    with pytest.raises(SystemExit, match="Aborted"):
        cli.confirm_plan([{"path_with_namespace": "a/repo"}], "t", args)


def test_source_url_required(run_cli, monkeypatch):
    monkeypatch.delenv("RATATOSKR_SOURCE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_cli("list")
    assert exc.value.code == 2


def test_step_aliases(run_cli, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "checkout", lambda args: calls.append(("checkout", args.mode)) or 0)
    monkeypatch.setattr(cli, "push", lambda args: calls.append(("push", args.mode)) or 0)
    run_cli("download", "--from-csv", "x.csv", "--no-issues", "--no-mrs")
    run_cli("migrate", "--target-url", "t.example.org", "--group", "g")
    assert calls == [("checkout", "checkout"), ("push", "push")]


DOWNLOAD = ("download", "--from-csv", "x.csv", "--no-issues", "--no-mrs")


@pytest.fixture
def no_git_lfs(monkeypatch):
    monkeypatch.setattr(cli, "has_git_lfs", lambda: False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    calls = []
    monkeypatch.setattr(cli, "checkout", lambda args: calls.append(args.lfs) or 0)
    return calls


def test_missing_git_stops_before_anything(run_cli, monkeypatch):
    monkeypatch.setattr(cli, "has_git", lambda: False)
    monkeypatch.setattr(cli, "checkout", lambda args: pytest.fail("must not start"))
    with pytest.raises(SystemExit, match="git is not installed"):
        run_cli(*DOWNLOAD)


def test_missing_git_lfs_continue_without(run_cli, answers, no_git_lfs, capsys):
    answers("x", "c")
    assert run_cli(*DOWNLOAD) == 0
    assert no_git_lfs == [False]  # LFS switched off for this run
    assert "git-lfs is not installed" in capsys.readouterr().out


def test_missing_git_lfs_abort_is_default(run_cli, answers, no_git_lfs):
    answers("")
    with pytest.raises(SystemExit, match="Aborted"):
        run_cli(*DOWNLOAD)
    assert no_git_lfs == []


def test_missing_git_lfs_without_terminal_aborts(run_cli, no_git_lfs, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit, match="--no-lfs"):
        run_cli(*DOWNLOAD)


def test_no_lfs_needs_no_git_lfs(run_cli, no_git_lfs):
    assert run_cli(*DOWNLOAD, "--no-lfs") == 0
    assert no_git_lfs == [False]

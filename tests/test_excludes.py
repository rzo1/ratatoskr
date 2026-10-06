import csv

import pytest

from ratatoskr import source
from ratatoskr.excludes import Excludes, normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("git@gitlab.cc-asp.fraunhofer.de:fhg/ai/office-helm.git", "fhg/ai/office-helm"),
        ("https://gitlab.example.org/much/fhg-mattermost.git", "much/fhg-mattermost"),
        ("ssh://git@gitlab.example.org/a/b.git", "a/b"),
        ("  fhg-intern/  ", "fhg-intern"),
        ("fhg/**/test*", "fhg/**/test*"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    ("pattern", "path", "excluded"),
    [
        # a group excludes everything below it, but not groups with the same prefix
        ("fhg", "fhg/ai/office-helm", True),
        ("fhg", "fhg-intern/dive", False),
        ("fhg/", "fhg/dev-ops-2022-demo", True),
        # * stays within one level, ** crosses levels
        ("fhgdemo/*/test", "fhgdemo/dev/test", True),
        ("fhgdemo/*/test", "fhgdemo/dev/temp/test", False),
        ("fhgdemo/*/temp/test", "fhgdemo/dev/temp/test", True),
        ("fhgdemo/**/test", "fhgdemo/dev/temp/test", True),
        ("*/automation/test*", "network/automation/testprojekt2", True),
        ("*/automation/test*", "network/fns/templates", False),
        ("**/infrastructure-as-code", "fhgdemo/dev/dummylab01/baz/infrastructure-as-code", True),
        ("network/fns/automatisierungsdemo", "network/fns/automatisierungsdemo/ansible", True),
        ("network/fns/automatisierungsdemo", "network/fns/autodoc-demo", False),
        # paths are case-insensitive in GitLab
        ("fhg-intern/test", "fhg-intern/Test", True),
        # a pasted clone URL excludes exactly that project
        ("git@gitlab.cc-asp.fraunhofer.de:zowalla/demo-app.git", "zowalla/demo-app", True),
        ("git@gitlab.cc-asp.fraunhofer.de:zowalla/demo-app.git", "zowalla/demo-app-2", False),
    ],
)
def test_matches(pattern, path, excluded):
    assert Excludes([pattern]).matches(path) is excluded


def test_from_args_reads_files_with_comments(tmp_path):
    file = tmp_path / "exclude.txt"
    file.write_text(
        "# internal stuff\n"
        "fhg-intern\n"
        "\n"
        "git@gitlab.cc-asp.fraunhofer.de:huginn/huginn.git   # not ours\n"
    )
    args = type("Args", (), {"exclude": ["fhgdemo"], "exclude_file": [str(file)]})()
    excludes = Excludes.from_args(args)
    assert excludes.patterns == ["fhgdemo", "fhg-intern", "huginn/huginn"]
    assert not Excludes.from_args(type("Args", (), {})())


def test_list_marks_excluded_repos_in_csv(tmp_path, capsys):
    projects = [
        {"path_with_namespace": p, "ssh_url_to_repo": f"git@gl:{p}.git"}
        for p in ("fhg/ai/x", "riscv/airisc", "fhg-intern/dive")
    ]
    source.write_outputs(projects, tmp_path, Excludes(["fhg"]))
    with open(tmp_path / "repos.csv") as f:
        rows = {r["path_with_namespace"]: r["migrate"] for r in csv.DictReader(f)}
    assert rows == {"fhg/ai/x": "no", "riscv/airisc": "yes", "fhg-intern/dive": "yes"}
    assert "git@gl:fhg/ai/x.git" not in (tmp_path / "repos.txt").read_text()
    assert "(1 excluded: migrate=no)" in capsys.readouterr().out


def test_checkout_and_push_skip_excluded(tmp_path, source_repo, write_csv, gitlab, run_cli, capsys):
    csv_path = write_csv(
        [
            {"path_with_namespace": "keep/repo", "ssh_url_to_repo": source_repo},
            {"path_with_namespace": "fhgdemo/dev/repo", "ssh_url_to_repo": source_repo},
        ]
    )
    excludes = tmp_path / "exclude.txt"
    excludes.write_text("fhgdemo\n")
    mirrors = tmp_path / "mirrors"
    checkout = ("checkout", "--from-csv", csv_path, "--protocol", "ssh", "--dest", mirrors)
    assert run_cli(*checkout, "--no-issues", "--no-mrs", "--exclude-file", excludes) == 0
    assert (mirrors / "keep/repo.git").exists()
    assert not (mirrors / "fhgdemo").exists()

    push = ("push", "--target-url", "gl.example.org", "--group", "dept/team")
    push += ("--from-csv", csv_path, "--mirror-dir", mirrors, "--protocol", "ssh")
    assert run_cli(*push, "--flatten", "-y", "--exclude", "fhgdemo/**") == 0
    assert set(gitlab.projects) == {"dept/team/repo"}
    assert capsys.readouterr().out.count("Skipping 1 repos matching the exclude patterns") == 2

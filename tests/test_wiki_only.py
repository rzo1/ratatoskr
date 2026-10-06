"""Projects whose repository feature is disabled and that only have a wiki."""

from conftest import git, refs

from ratatoskr.gitops import git_error

GIT_ONLY = ("--no-issues", "--no-mrs")


def wiki_only_project(tmp_path):
    """No git repository at <tmp>/docs, but a wiki at <tmp>/docs.wiki.git."""
    wiki = tmp_path / "docs.wiki.git"
    git("init", "-q", "-b", "main", str(wiki))
    (wiki / "home.md").write_text("# Docs\n")
    git("add", "home.md", cwd=wiki)
    git("commit", "-q", "-m", "home", cwd=wiki)
    return tmp_path / "docs"


def test_git_error_picks_the_real_error():
    stderr = (
        "ERROR: The project you were looking for could not be found or you don't have "
        "permission to view it.\n"
        "fatal: Konnte nicht vom Remote-Repository lesen.\n\n"
        "Bitte stellen Sie sicher, dass die korrekten Zugriffsberechtigungen bestehen\n"
        "und das Repository existiert.\n"
    )
    assert git_error(stderr) == (
        "ERROR: The project you were looking for could not be found or you don't have "
        "permission to view it. / fatal: Konnte nicht vom Remote-Repository lesen."
    )
    assert git_error("just one line\n") == "just one line"
    assert git_error("") == "(no output)"


def test_wiki_only_project_is_downloaded_and_migrated(tmp_path, write_csv, gitlab, run_cli, capsys):
    url = wiki_only_project(tmp_path)
    csv_path = write_csv(
        [{"path_with_namespace": "kodis/cloud/documentation", "ssh_url_to_repo": url}]
    )
    mirrors = tmp_path / "mirrors"
    checkout = ("checkout", "--from-csv", csv_path, "--protocol", "ssh", "--dest", mirrors)
    assert run_cli(*checkout, *GIT_ONLY) == 0
    out = capsys.readouterr().out
    assert "wiki     kodis/cloud/documentation (the project has no repository, only a wiki)" in out
    assert not (mirrors / "kodis/cloud/documentation.git").exists()
    assert refs(mirrors / "kodis/cloud/documentation.wiki.git") == ["refs/heads/main"]

    push = ("push", "--target-url", "gl.example.org", "--group", "dept/team", "--from-csv")
    push += (csv_path, "--mirror-dir", mirrors, "--protocol", "ssh", "--flatten", "-y")
    assert run_cli(*push, *GIT_ONLY) == 0
    project = gitlab.projects["dept/team/documentation"]
    assert refs(project["_wiki"]) == ["refs/heads/main"]
    assert refs(project["_repo"]) == []
    assert "no repository on the source (wiki only)" in capsys.readouterr().out


def test_disabled_repository_is_not_even_cloned(tmp_path, write_csv, run_cli, capsys):
    url = wiki_only_project(tmp_path)
    csv_path = write_csv(
        [
            {
                "path_with_namespace": "kodis/docs",
                "ssh_url_to_repo": url,
                "repository_access_level": "disabled",
            }
        ]
    )
    mirrors = tmp_path / "mirrors"
    checkout = ("checkout", "--from-csv", csv_path, "--protocol", "ssh", "--dest", mirrors)
    assert run_cli(*checkout, *GIT_ONLY) == 0
    assert "only a wiki" in capsys.readouterr().out
    assert run_cli(*checkout, *GIT_ONLY, "--no-wiki") == 0
    assert "repository disabled on the source, no wiki" in capsys.readouterr().out


def test_missing_repository_without_wiki_still_fails(tmp_path, write_csv, run_cli, capsys):
    csv_path = write_csv([{"path_with_namespace": "g/gone", "ssh_url_to_repo": tmp_path / "gone"}])
    checkout = ("checkout", "--from-csv", csv_path, "--protocol", "ssh", "--dest", tmp_path / "m")
    assert run_cli(*checkout, *GIT_ONLY) == 1
    out = capsys.readouterr().out
    assert "FAILED   g/gone" in out and "fatal:" in out

"""LFS detection: repos using LFS whose files cannot be downloaded are skipped with a warning."""

import hashlib
import shutil
import time

import pytest
from conftest import git

from ratatoskr import gitops

needs_git_lfs = pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs missing")
GIT_ONLY = ("--no-issues", "--no-mrs")
CONTENT = b"abc"
OID = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture
def lfs_source(tmp_path):
    """A repo with one LFS pointer whose object exists nowhere (like a broken LFS storage)."""
    repo = tmp_path / "lfs-source"
    git("init", "-q", "-b", "main", str(repo))
    (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    (repo / "big.bin").write_text(
        f"version https://git-lfs.github.com/spec/v1\noid sha256:{OID}\nsize 3\n"
    )
    git("-c", "filter.lfs.clean=cat", "add", ".", cwd=repo)
    git("commit", "-q", "-m", "lfs file", cwd=repo)
    return repo


def test_run_watched_kills_a_stalled_download(tmp_path):
    started = time.monotonic()
    with pytest.raises(gitops.LfsError, match="no download progress for 1s"):
        gitops.run_watched(
            ["-c", "alias.stall=!sleep 30", "stall"], tmp_path, None, tmp_path, 1, poll=0.2
        )
    assert time.monotonic() - started < 10


def test_run_watched_reports_git_errors(tmp_path):
    with pytest.raises(gitops.LfsError, match="fatal:"):
        gitops.run_watched(["-C", str(tmp_path / "nope"), "status"], tmp_path, None, tmp_path, 5)


def test_problem_marker_roundtrip(tmp_path):
    repo = tmp_path / "r.git"
    repo.mkdir()
    assert gitops.lfs_problem(repo) is None
    gitops.set_lfs_problem(repo, "205 LFS files could not be downloaded")
    assert gitops.lfs_problem(repo) == "205 LFS files could not be downloaded"
    gitops.set_lfs_problem(repo, None)
    assert gitops.lfs_problem(repo) is None


@needs_git_lfs
def test_repos_without_lfs_are_not_fetched(source_repo, tmp_path, monkeypatch):
    mirror = tmp_path / "m.git"
    git("clone", "-q", "--mirror", str(source_repo), str(mirror))
    monkeypatch.setattr(gitops, "run_watched", lambda *a, **kw: pytest.fail("must not fetch"))
    assert gitops.lfs_fetch(mirror, None) == (0, 0)


@needs_git_lfs
def test_broken_lfs_project_is_skipped_with_warning(
    tmp_path, lfs_source, source_repo, write_csv, gitlab, run_cli, capsys
):
    csv_path = write_csv(
        [
            {"path_with_namespace": "k10/syslit", "ssh_url_to_repo": lfs_source},
            {"path_with_namespace": "k10/fine", "ssh_url_to_repo": source_repo},
        ]
    )
    mirrors = tmp_path / "mirrors"
    checkout = ("checkout", "--from-csv", csv_path, "--protocol", "ssh", "--dest", mirrors)
    assert run_cli(*checkout, *GIT_ONLY) == 1  # exit code signals the problem
    out = capsys.readouterr().out
    assert "cloned   k10/syslit (LFS PROBLEM - see the warning at the end)" in out
    assert "WARNING: 1 project(s) with LFS problems" in out
    assert "1 LFS files could not be downloaded" in out
    assert (mirrors / "k10/syslit.git").exists()  # the code is kept

    push = ("push", "--target-url", "gl.example.org", "--group", "dept/team", "--from-csv")
    push += (csv_path, "--mirror-dir", mirrors, "--protocol", "ssh", "--flatten", "-y")
    assert run_cli(*push, *GIT_ONLY) == 1
    out = capsys.readouterr().out
    assert set(gitlab.projects) == {"dept/team/fine"}  # syslit is not even created
    assert "SKIPPED - LFS problem" in out
    assert "1 skipped because of LFS problems" in out
    assert "These projects were NOT migrated" in out

    # once the object is available again, the next download clears the problem
    obj = lfs_source / ".git/lfs/objects" / OID[:2] / OID[2:4] / OID
    obj.parent.mkdir(parents=True)
    obj.write_bytes(CONTENT)
    assert run_cli(*checkout, *GIT_ONLY) == 0
    assert gitops.lfs_problem(mirrors / "k10/syslit.git") is None


@needs_git_lfs
def test_skip_lfs_projects_does_not_even_try(
    tmp_path, lfs_source, write_csv, run_cli, monkeypatch, capsys
):
    monkeypatch.setattr(gitops, "run_watched", lambda *a, **kw: pytest.fail("must not fetch"))
    csv_path = write_csv([{"path_with_namespace": "k10/syslit", "ssh_url_to_repo": lfs_source}])
    checkout = ("checkout", "--from-csv", csv_path, "--protocol", "ssh", "--dest", tmp_path / "m")
    assert run_cli(*checkout, *GIT_ONLY, "--skip-lfs-projects") == 1
    assert "1 LFS files not downloaded (--skip-lfs-projects)" in capsys.readouterr().out

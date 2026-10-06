import pytest

from ratatoskr import gitops


@pytest.mark.parametrize(
    ("line", "short"),
    [
        (
            "Writing objects:  45% (2470/5487), 180.00 MiB | 5.20 MiB/s",
            "writing 45%, 180.00 MiB at 5.20 MiB/s",
        ),
        (
            "Receiving objects:  12% (66/549), 1.01 MiB | 1.99 MiB/s",
            "receiving 12%, 1.01 MiB at 1.99 MiB/s",
        ),
        ("Compressing objects: 100% (3/3), done.", "compressing 100%"),
        ("remote: Counting objects:  50% (1/2)", "counting 50%"),
        (
            "Uploading LFS objects:  50% (1/2), 2.0 MB | 1.0 MB/s",
            "uploading 50%, 2.0 MB at 1.0 MB/s",
        ),
        ("To https://gitlab.example.org/g/p.git", None),
        (" * [new branch]      main -> main", None),
    ],
)
def test_short_progress(line, short):
    assert gitops.short_progress(line) == short


def test_run_streaming_reports_progress_and_keeps_stderr(tmp_path):
    script = (
        "printf 'Writing objects:  10%% (1/10)\\r"
        "Writing objects: 100%% (10/10), 1.00 MiB | 2.00 MiB/s\\n' >&2; "
        "printf 'fatal: nope\\n' >&2; echo out; exit 3"
    )
    seen = []
    res = gitops.run_streaming(["sh", "-c", script], tmp_path, None, seen.append)
    assert seen == ["writing 10%", "writing 100%, 1.00 MiB at 2.00 MiB/s"]
    assert res.returncode == 3
    assert res.stdout == "out\n"
    assert gitops.git_error(res.stderr) == "fatal: nope"


def test_push_reports_git_progress(tmp_path, source_repo):
    target = tmp_path / "t.git"
    gitops.run_git(["init", "-q", "--bare", str(target)], tmp_path, None)
    seen = []
    gitops.push_refs(str(target), source_repo, False, None, seen.append)
    assert any(s.startswith("writing") for s in seen)


def test_clone_reports_git_progress(tmp_path, source_repo):
    seen = []
    action, res = gitops.clone_one(str(source_repo), tmp_path / "m.git", False, None, seen.append)
    assert (action, res.returncode) == ("cloned", 0)
    action, res = gitops.clone_one(str(source_repo), tmp_path / "m.git", False, None, seen.append)
    assert (action, res.returncode) == ("updated", 0)


def test_git_runs_in_english(monkeypatch):
    env = gitops.english({"LC_ALL": "de_DE.UTF-8", "PATH": "/usr/bin"})
    assert env["LC_MESSAGES"] == "C" and env["LANGUAGE"] == "C"
    assert "LC_ALL" not in env and env["LANG"] == "de_DE.UTF-8"
    monkeypatch.setenv("LANGUAGE", "de")
    assert gitops.english(None)["LANGUAGE"] == "C"

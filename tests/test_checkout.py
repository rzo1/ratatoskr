import os
import subprocess

from conftest import git, refs

from ratatoskr import cli

GIT_ONLY = ("--no-issues", "--no-mrs")


def test_checkout_mirrors_and_updates(tmp_path, source_repo, write_csv, run_cli, capsys):
    csv_path = write_csv(
        [
            {"path_with_namespace": "grp/sub/repo", "ssh_url_to_repo": source_repo},
            {"path_with_namespace": "grp/broken", "ssh_url_to_repo": tmp_path / "missing"},
            {"path_with_namespace": "grp/skipped", "ssh_url_to_repo": source_repo, "migrate": "no"},
        ]
    )
    dest = tmp_path / "mirrors"
    args = ("checkout", "--from-csv", csv_path, "--protocol", "ssh", "--dest", dest, *GIT_ONLY)

    assert run_cli(*args) == 1  # the broken repo fails, the others continue
    mirror = dest / "grp/sub/repo.git"
    assert refs(mirror) == ["refs/heads/dev", "refs/heads/main", "refs/tags/v1", "refs/tags/v2"]
    assert not (dest / "grp/skipped.git").exists()
    out = capsys.readouterr().out
    assert "cloned   grp/sub/repo" in out
    assert "FAILED   grp/broken" in out

    git("branch", "feature", cwd=source_repo)
    git("branch", "-D", "dev", cwd=source_repo)
    run_cli(*args)
    assert "updated  grp/sub/repo" in capsys.readouterr().out
    assert refs(mirror) == ["refs/heads/feature", "refs/heads/main", "refs/tags/v1", "refs/tags/v2"]


def test_checkout_working_copy(tmp_path, source_repo, write_csv, run_cli):
    csv_path = write_csv([{"path_with_namespace": "grp/repo", "ssh_url_to_repo": source_repo}])
    dest = tmp_path / "work"
    args = ("checkout", "--from-csv", csv_path, "--protocol", "ssh", "--dest", dest, *GIT_ONLY)
    assert run_cli(*args, "--working-copy") == 0
    assert (dest / "grp/repo/.git").is_dir()


def test_checkout_asks_for_protocol(
    tmp_path, source_repo, write_csv, run_cli, answers, monkeypatch
):
    csv_path = write_csv([{"path_with_namespace": "grp/repo", "http_url_to_repo": source_repo}])
    answers("ftp", "h", "")  # invalid answer, https, default username
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "glpat-secret")
    tokens = []
    real_askpass_env = cli.askpass_env

    def spy(user, token):
        tokens.append((user, token))
        return real_askpass_env(user, token)

    monkeypatch.setattr(cli, "askpass_env", spy)
    assert run_cli("checkout", "--from-csv", csv_path, "--dest", tmp_path / "m", *GIT_ONLY) == 0
    assert tokens == [(None, "glpat-secret")]
    remote = git("config", "remote.origin.url", cwd=tmp_path / "m/grp/repo.git").strip()
    assert "glpat-secret" not in remote


def test_askpass_script_answers_prompts():
    env, script = cli.askpass_env("alice", "tok$en'")
    try:

        def ask(prompt):
            res = subprocess.run([script, prompt], env=env, capture_output=True, text=True)
            return res.stdout.strip()

        assert ask("Username for 'https://gl':") == "alice"
        assert ask("Password for 'https://alice@gl':") == "tok$en'"
    finally:
        os.unlink(script)


def test_clone_status_line():
    stages = {"grp/big": "LFS", "grp/other": "cloning"}
    line = cli.clone_status(3, 10, stages, cli.time.monotonic() - 75)
    assert line == "  3/10 done, 1:15 elapsed - grp/big (LFS), grp/other (cloning)"


def test_checkout_one_tracks_and_clears_its_stage(tmp_path, source_repo, monkeypatch):
    seen = []
    real_clone_one = cli.clone_one

    def clone_one(url, dest, working_copy, env, on_progress=None):
        seen.append(dict(stages))
        return real_clone_one(url, dest, working_copy, env, on_progress)

    monkeypatch.setattr(cli, "clone_one", clone_one)
    stages = {}
    args = type("A", (), {"dest": tmp_path, "working_copy": False, "lfs": False, "wiki": False})
    ok, _ = cli.checkout_one(
        {"path_with_namespace": "g/r", "ssh_url_to_repo": str(source_repo)},
        "ssh_url_to_repo",
        args,
        {},
        stages,
    )
    assert ok and seen == [{"g/r": "cloning"}] and stages == {}

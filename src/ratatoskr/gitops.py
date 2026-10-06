"""Git operations: clone/update local copies, push them, verify the result."""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# answers git's username/password prompts from env vars, so tokens never end up in remote URLs
ASKPASS = """#!/bin/sh
case "$1" in
  Username*) printf '%s\\n' "$RATATOSKR_GIT_USER" ;;
  *) printf '%s\\n' "$RATATOSKR_GIT_TOKEN" ;;
esac
"""

TAG_REFSPEC = "+refs/tags/*:refs/tags/*"
MIRROR_REFSPECS = ["+refs/heads/*:refs/heads/*", TAG_REFSPEC]
ORIGIN = "refs/remotes/origin/"


def askpass_env(user, token):
    fd, askpass = tempfile.mkstemp(prefix="ratatoskr-askpass-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(ASKPASS)
    os.chmod(askpass, stat.S_IRWXU)
    env = {
        **os.environ,
        "GIT_ASKPASS": askpass,
        "GIT_TERMINAL_PROMPT": "0",
        "RATATOSKR_GIT_USER": user or "",
        "RATATOSKR_GIT_TOKEN": token,
    }
    return env, askpass


def ssh_env():
    return {
        **os.environ,
        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes"),
    }


def git_error(stderr):
    """The meaningful part of git's error output (not the generic hints after it)."""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    errors = [
        line
        for line in lines
        if line.lower().startswith(("error:", "fatal:", "remote: error", "remote: fatal"))
    ]
    return " / ".join(errors[:2]) if errors else (lines[-1] if lines else "(no output)")


def run_git(args, cwd, env):
    res = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {git_error(res.stderr)}")
    return res.stdout


def wiki_url(repo_url):
    return repo_url.removesuffix(".git") + ".wiki.git"


# ---------------------------------------------------------------- clone


def clone_one(url, dest: Path, working_copy, env):
    if dest.exists():
        cmd = (
            ["git", "-C", str(dest), "fetch", "--all", "--prune"]
            if working_copy
            else ["git", "-C", str(dest), "remote", "update", "--prune"]
        )
        action = "updated"
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", *([] if working_copy else ["--mirror"]), url, str(dest)]
        action = "cloned"
    res = subprocess.run(
        cmd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )  # never hang on an interactive prompt
    return action, res


def clone_wiki(repo_url, dest: Path, env):
    """Mirror the project's wiki; returns a short status ("cloned", "updated" or "none")."""
    action, res = clone_one(wiki_url(repo_url), dest, False, env)
    if res.returncode != 0:
        if action == "cloned":
            shutil.rmtree(dest, ignore_errors=True)
        return "none"
    if not local_refs(dest, False, env):
        shutil.rmtree(dest, ignore_errors=True)  # wiki enabled but without any page
        return "none"
    return action


def local_repo(mirror_dir, path_with_namespace):
    """(path, is_working_copy) of the local clone, or (None, None)."""
    bare = Path(mirror_dir) / f"{path_with_namespace}.git"
    if bare.exists():
        return bare, False
    work = Path(mirror_dir) / path_with_namespace
    if (work / ".git").exists():
        return work, True
    return None, None


# ---------------------------------------------------------------- push


def local_refs(repo, working_copy, env=None):
    """{target ref: sha} of all branches and tags that should end up on the target.

    Working copies keep branches as origin/* refs; symrefs such as origin/HEAD are skipped.
    """
    branch_ns = ORIGIN if working_copy else "refs/heads/"
    out = run_git(
        ["for-each-ref", "--format=%(objectname) %(refname) %(symref)", branch_ns, "refs/tags/"],
        repo,
        env,
    )
    refs = {}
    for line in out.splitlines():
        sha, ref, *symref = line.split()
        if not symref:
            refs[ref.replace(ORIGIN, "refs/heads/", 1) if ref.startswith(ORIGIN) else ref] = sha
    return refs


def remote_refs(url, repo, env):
    out = run_git(["ls-remote", "--heads", "--tags", url], repo, env)
    pairs = (line.split("\t") for line in out.splitlines())
    return {ref: sha for sha, ref in pairs if not ref.endswith("^{}")}


def push_refs(url, repo, working_copy, env):
    """Push all branches and tags; returns the {ref: sha} that were pushed."""
    refs = local_refs(repo, working_copy, env)
    refspecs = MIRROR_REFSPECS
    if working_copy:
        refspecs = [
            f"+{ORIGIN}{ref.removeprefix('refs/heads/')}:{ref}"
            for ref in refs
            if ref.startswith("refs/heads/")
        ] + [TAG_REFSPEC]
    run_git(["push", url, *refspecs], repo, env)
    return refs


def prune_refs(url, repo, refs, env):
    """Delete branches/tags on the target that no longer exist locally."""
    stale = sorted(set(remote_refs(url, repo, env)) - set(refs))
    if stale:
        run_git(["push", url, *(f":{ref}" for ref in stale)], repo, env)
    return stale


def verify_refs(url, repo, refs, env):
    remote = remote_refs(url, repo, env)
    differing = sorted(ref for ref, sha in refs.items() if remote.get(ref) != sha)
    if differing:
        raise RuntimeError(
            f"verification failed, {len(differing)} refs missing/different on target: "
            + ", ".join(differing[:5])
        )


def push_wiki(url, wiki_dir, env):
    """Push the wiki without force, so pages added on the target are never overwritten."""
    refs = local_refs(wiki_dir, False, env)
    try:
        run_git(["push", url, "refs/heads/*:refs/heads/*"], wiki_dir, env)
    except RuntimeError as e:
        raise RuntimeError(f"wiki: {e} (target wiki has diverged from the source?)") from e
    verify_refs(url, wiki_dir, refs, env)
    return len(refs)


# ---------------------------------------------------------------- LFS


def has_git_lfs():
    return shutil.which("git-lfs") is not None


def has_git():
    return shutil.which("git") is not None


def install_hint(package):
    if sys.platform == "darwin":
        return f"brew install {package}"
    if sys.platform == "win32":
        return f"winget install {'Git.Git' if package == 'git' else 'GitHub.GitLFS'}"
    return f"sudo apt install {package}   (Fedora: sudo dnf install {package})"


LFS_PROBLEM_FILE = "ratatoskr-lfs-problem"


class LfsError(RuntimeError):
    pass


def git_dir(repo):
    repo = Path(repo)
    return repo / ".git" if (repo / ".git").is_dir() else repo


def lfs_object_count(repo):
    objects = git_dir(repo) / "lfs" / "objects"
    return sum(1 for f in objects.rglob("*") if f.is_file()) if objects.is_dir() else 0


def lfs_pointer_count(repo, env=None):
    """Number of LFS files referenced in any branch or tag (0 = the repo does not use LFS)."""
    return len(run_git(["lfs", "ls-files", "--all", "--name-only"], repo, env).splitlines())


def lfs_problem(repo):
    """The recorded LFS problem of a local clone, or None."""
    marker = git_dir(repo) / LFS_PROBLEM_FILE
    return marker.read_text().strip() if marker.exists() else None


def set_lfs_problem(repo, problem):
    marker = git_dir(repo) / LFS_PROBLEM_FILE
    if problem:
        marker.write_text(problem + "\n")
    else:
        marker.unlink(missing_ok=True)


def _dir_size(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


def run_watched(args, cwd, env, watch_dir, stall_timeout, poll=2.0):
    """Run git, but kill it when ``watch_dir`` has not grown for ``stall_timeout`` seconds."""
    with tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(
            ["git", *args],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            text=True,
        )
        size, last_change = _dir_size(watch_dir), time.monotonic()
        while True:
            try:
                proc.wait(timeout=poll)
                break
            except subprocess.TimeoutExpired:
                current = _dir_size(watch_dir)
                if current != size:
                    size, last_change = current, time.monotonic()
                elif time.monotonic() - last_change > stall_timeout:
                    proc.kill()
                    proc.wait()
                    raise LfsError(f"no download progress for {stall_timeout}s") from None
        if proc.returncode != 0:
            err.seek(0)
            raise LfsError(git_error(err.read()))


def lfs_fetch(repo, env, stall_timeout=120, skip_lfs_projects=False):
    """Fetch all LFS objects of all refs from origin.

    Returns (pointers, objects): the number of LFS files the repo references and of the
    objects now available locally; (None, 0) if git-lfs is not installed. Problems (stalled
    or failed download, or skipped on request) are recorded with ``set_lfs_problem``.
    """
    if not has_git_lfs():
        return None, 0
    pointers = lfs_pointer_count(repo, env)
    if not pointers:
        set_lfs_problem(repo, None)
        return 0, 0
    if skip_lfs_projects:
        set_lfs_problem(repo, f"{pointers} LFS files not downloaded (--skip-lfs-projects)")
        return pointers, lfs_object_count(repo)
    try:
        run_watched(
            ["lfs", "fetch", "--all", "origin"], repo, env, git_dir(repo) / "lfs", stall_timeout
        )
    except LfsError as e:
        set_lfs_problem(repo, f"{pointers} LFS files could not be downloaded: {e}")
        return pointers, lfs_object_count(repo)
    set_lfs_problem(repo, None)
    return pointers, lfs_object_count(repo)


def lfs_push(url, repo, env):
    """Upload all local LFS objects to ``url``; returns their number (0 = nothing to do)."""
    count = lfs_object_count(repo)
    if not count:
        return 0
    if not has_git_lfs():
        raise RuntimeError(f"{count} LFS objects to push, but git-lfs is not installed")
    run_git(["lfs", "push", "--all", url], repo, env)
    return count

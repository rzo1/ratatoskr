"""Git operations: clone/update local copies, push them, verify the result."""

import os
import shutil
import stat
import subprocess
import tempfile
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


def run_git(args, cwd, env):
    res = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if res.returncode != 0:
        last_line = (res.stderr.strip().splitlines() or ["(no output)"])[-1]
        raise RuntimeError(f"git {args[0]} failed: {last_line}")
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


def lfs_object_count(repo):
    repo = Path(repo)
    for objects in (repo / "lfs" / "objects", repo / ".git" / "lfs" / "objects"):
        if objects.is_dir():
            return sum(1 for f in objects.rglob("*") if f.is_file())
    return 0


def lfs_fetch(repo, env):
    """Fetch all LFS objects of all refs from origin; returns the number of local objects."""
    if not has_git_lfs():
        return None
    run_git(["lfs", "fetch", "--all", "origin"], repo, env)
    return lfs_object_count(repo)


def lfs_push(url, repo, env):
    """Upload all local LFS objects to ``url``; returns their number (0 = nothing to do)."""
    count = lfs_object_count(repo)
    if not count:
        return 0
    if not has_git_lfs():
        raise RuntimeError(f"{count} LFS objects to push, but git-lfs is not installed")
    run_git(["lfs", "push", "--all", url], repo, env)
    return count

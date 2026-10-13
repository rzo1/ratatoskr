"""--protocol api: rebuilding repositories from the REST API, checked against real git repos."""

import random
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import git, refs

from ratatoskr import apiclone, cli
from ratatoskr.apiclone import ApiCloner, inexact_summary, snapshot_wiki
from ratatoskr.source import SourceError

GIT_ONLY = ("--no-issues", "--no-mrs")


def git_env(when, tz="+0000", name="Test", email="test@example.org"):
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_DATE": f"{when} {tz}",
        "GIT_COMMITTER_DATE": f"{when} {tz}",
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
    }


def commit(repo, message, when, tz="+0000", **who):
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message],
        cwd=repo,
        env=git_env(when, tz, **who),
        check=True,
    )
    return git("rev-parse", "HEAD", cwd=repo).strip()


def raw(repo, *args, data=None):
    return subprocess.run(
        ["git", *args], cwd=repo, input=data, capture_output=True, check=True
    ).stdout


# ---------------------------------------------------------------- fake API backed by git


class Resp:
    def __init__(self, status, body=b""):
        self.status = status
        self.ok = status < 400
        self._body = body

    def body(self):
        return self._body


class GitApi:
    min_interval = 0.2

    """Answers the REST API calls of ApiCloner like GitLab would, from a real git repo.

    ``order``: "date" (GitLab's default, like git log), or "shuffle" to check that any
    order works.
    ``short_messages``: commit lists cut long messages (as GitLab may do), the single
    commit endpoint returns them in full.
    """

    base = "https://source.example.org"
    api = f"{base}/api/v4"
    user = {"username": "me"}

    def __init__(self, repo: Path, order="date", short_messages=False):
        self.repo = repo
        self.order = order
        self.short_messages = short_messages
        self.requests = []
        self.wiki_pages = []
        self.tag_zone = UTC  # GitLab shows tag dates in UTC

    def _git(self, *args):
        return raw(self.repo, *args)

    def _commit_json(self, sha, full=True):
        raw_obj = self._git("cat-file", "commit", sha).decode()
        head, _, message = raw_obj.partition("\n\n")
        info = {"parent_ids": []}
        for line in head.split("\n"):
            key, _, value = line.partition(" ")
            if key == "parent":
                info["parent_ids"].append(value)
            elif key in ("author", "committer"):
                person, _, stamp = value.rpartition("> ")
                name, _, email = person.partition(" <")
                info[f"{key}_name"], info[f"{key}_email"] = name, email
                info[f"{'authored' if key == 'author' else 'committed'}_date"] = iso(stamp)
        if not full and self.short_messages and len(message) > 40:
            message = message[:40] + "..."
        return {"id": sha, "title": message.split("\n")[0], "message": message, **info}

    def _tags(self):
        out = self._git(
            "for-each-ref", "--format=%(refname:short) %(objectname) %(objecttype)", "refs/tags"
        ).decode()
        tags = []
        for line in out.splitlines():
            name, target, kind = line.split()
            peeled = self._git("rev-parse", f"{target}^{{}}").decode().strip()
            peeled_type = self._git("cat-file", "-t", peeled).decode().strip()
            tag = {"name": name, "target": target, "message": "", "created_at": None}
            tag["commit"] = self._commit_json(peeled) if peeled_type == "commit" else None
            if kind == "tag":
                head, _, message = self._git("cat-file", "tag", target).decode().partition("\n\n")
                tagger = next(x for x in head.split("\n") if x.startswith("tagger "))
                stamp = iso(tagger.rpartition("> ")[2])
                tag["message"] = message.rstrip("\n")  # GitLab strips the message
                created = datetime.fromisoformat(stamp).astimezone(self.tag_zone)
                tag["created_at"] = created.isoformat()
            tags.append(tag)
        return tags

    def _tree(self, ref, path=None):
        spec = f"{ref}:{path}" if path else f"{ref}^{{tree}}"
        entries = []
        for item in self._git("ls-tree", "-z", spec).split(b"\0"):
            if not item:
                continue
            meta, name = item.split(b"\t", 1)
            mode, kind, sha = meta.decode().split()
            entries.append(
                {
                    "id": sha,
                    "name": name.decode(errors="replace"),
                    "type": kind,
                    "mode": mode.zfill(6),
                }
            )
        return entries

    def _route(self, path, params):
        prefix = "/projects/7/repository"
        assert path.startswith(prefix), path
        path = path.removeprefix(prefix)
        params = params or {}
        if path == "/branches":
            out = self._git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
            return [
                {"name": n, "commit": self._commit_json(s)}
                for n, s in (line.split() for line in out.decode().splitlines())
            ]
        if path == "/tags":
            return self._tags()
        if path == "/commits":
            assert "order" not in params  # topo order is too expensive for the source
            shas = self._git("rev-list", params["ref_name"]).decode().split()
            if self.order == "shuffle":
                random.Random(4).shuffle(shas)
            return [self._commit_json(s, full=False) for s in shas]
        if path.startswith("/commits/"):
            return self._commit_json(path.rsplit("/", 1)[1])
        if path == "/tree":
            return self._tree(params["ref"], params.get("path"))
        raise AssertionError(f"unexpected API call {path}")

    def paginate(self, path, params=None, on_page=None):
        self.requests.append(path)
        if path == "/projects/7/wikis":
            yield from self.wiki_pages
            return
        yield from self._route(path, params)

    def get(self, path, params=None):
        self.requests.append(path)
        return self._route(path, params)

    def _request(self, url, params=None):
        self.requests.append(url)
        sha = url.split("/blobs/")[1].removesuffix("/raw")
        return Resp(200, self._git("cat-file", "blob", sha))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def iso(stamp):
    """'1714557600 +0200' -> ISO date with that offset (as GitLab returns it)."""
    seconds, tz = stamp.split()
    sign = -1 if tz[0] == "-" else 1
    offset = sign * (int(tz[1:3]) * 60 + int(tz[3:5]))
    return datetime.fromtimestamp(int(seconds), timezone(timedelta(minutes=offset))).isoformat()


def ref_map(repo):
    out = git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo)
    return dict(line.split() for line in out.splitlines())


# ---------------------------------------------------------------- source repos


@pytest.fixture
def rich_repo(tmp_path):
    """Merges, nested dirs, odd names, modes, symlink, submodule, time zones, tags."""
    repo = tmp_path / "rich"
    git("init", "-q", "-b", "main", str(repo))
    files = {
        "README.md": "hello\n",
        "a.b": "dot\n",
        "a-b/x.txt": "dash dir\n",
        "a/inner.txt": "dir named like a file prefix\n",
        "src/deep/er/mod.py": "print('x')\n",
        "Ümlaut ü.txt": "unicode\n",
        "with space/and\ttab.txt": "tab in name\n",
        "empty": "",
    }
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    (repo / "run.sh").write_text("#!/bin/sh\n")
    (repo / "run.sh").chmod(0o755)
    (repo / "link").symlink_to("README.md")
    git("add", "-A", cwd=repo)
    sub = "1234567890abcdef1234567890abcdef12345678"
    git("update-index", "--add", "--cacheinfo", f"160000,{sub},vendor/lib", cwd=repo)
    commit(repo, "initial\n\nwith a body\n", 1700000000, "+0530")
    git("tag", "-a", "v1", "-m", "first release", cwd=repo)  # tagger = committer

    git("checkout", "-q", "-b", "feature/x", cwd=repo)
    (repo / "src/deep/er/mod.py").write_text("print('feature')\n")
    git("add", "-A", cwd=repo)
    commit(repo, "feature work", 1700001000, "-0800", name="Ana", email="ana@example.org")

    git("checkout", "-q", "main", cwd=repo)
    (repo / "README.md").write_text("hello again\n")
    git("add", "-A", cwd=repo)
    commit(repo, "main work", 1700002000, "+0000")
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "-m", "Merge feature/x", "feature/x"],
        cwd=repo,
        env=git_env(1700003000, "+0100"),
        check=True,
    )
    git("tag", "light", cwd=repo)

    # a commit only reachable via a tag
    git("checkout", "-q", "--detach", cwd=repo)
    detached = commit(repo, "only tagged", 1700004000)
    git("tag", "detached-tag", detached, cwd=repo)
    git("checkout", "-q", "main", cwd=repo)
    return repo


def clone(src, dest, **kw):
    cloner = ApiCloner(src, 7, dest, **kw)
    return cloner, cloner.run("main")


# ---------------------------------------------------------------- tests


def test_rebuilds_a_repo_byte_exact(tmp_path, rich_repo):
    src = GitApi(rich_repo)
    dest = tmp_path / "out.git"
    cloner, action = clone(src, dest)

    assert action == "cloned"
    assert ref_map(dest) == ref_map(rich_repo)  # every branch and tag has the original SHA
    assert inexact_summary(dest) is None
    assert git("symbolic-ref", "HEAD", cwd=dest).strip() == "refs/heads/main"
    git("fsck", "--strict", cwd=dest)
    assert cloner.stats["commits"] == 5


def test_any_listing_order_works(tmp_path, rich_repo):
    dest = tmp_path / "out.git"
    clone(GitApi(rich_repo, order="shuffle"), dest)
    assert ref_map(dest) == ref_map(rich_repo)


def test_shortened_messages_are_fetched_in_full(tmp_path, rich_repo):
    git("commit", "-q", "--allow-empty", "-m", "x" * 100, cwd=rich_repo)
    src = GitApi(rich_repo, short_messages=True)
    dest = tmp_path / "out.git"
    clone(src, dest)
    assert ref_map(dest) == ref_map(rich_repo)
    assert any(r.startswith("/projects/7/repository/commits/") for r in src.requests)


def test_update_fetches_only_what_is_new_and_prunes(tmp_path, rich_repo):
    dest = tmp_path / "out.git"
    clone(GitApi(rich_repo), dest)

    (rich_repo / "new.txt").write_text("new\n")
    git("add", "new.txt", cwd=rich_repo)
    commit(rich_repo, "add new", 1700005000)
    git("branch", "-D", "feature/x", cwd=rich_repo)
    git("tag", "-d", "light", cwd=rich_repo)

    src = GitApi(rich_repo)
    cloner, action = clone(src, dest)
    assert action == "updated"
    assert ref_map(dest) == ref_map(rich_repo)
    assert cloner.stats == {"commits": 1, "trees": 1, "blobs": 1, "requests": 5}
    blobs = [r for r in src.requests if "/blobs/" in r]
    assert len(blobs) == 1


def test_interrupted_run_continues(tmp_path, rich_repo, monkeypatch):
    dest = tmp_path / "out.git"
    src = GitApi(rich_repo)
    real = ApiCloner.write_commit
    calls = []

    def flaky(self, c):
        calls.append(c["id"])
        if len(calls) == 3:
            raise SourceError(0, "connection lost")
        return real(self, c)

    monkeypatch.setattr(ApiCloner, "write_commit", flaky)
    with pytest.raises(SourceError):
        clone(src, dest)
    assert refs(dest) == []  # refs are only written at the end

    monkeypatch.setattr(ApiCloner, "write_commit", real)
    cloner, _ = clone(GitApi(rich_repo), dest)
    assert ref_map(dest) == ref_map(rich_repo)
    assert cloner.stats["commits"] == 3  # the first two were kept


def signed_commit(repo, parent, message="signed change"):
    tree = git("rev-parse", f"{parent}^{{tree}}", cwd=repo).strip()
    body = (
        f"tree {tree}\nparent {parent}\n"
        "author Sig <sig@example.org> 1700006000 +0200\n"
        "committer Sig <sig@example.org> 1700006000 +0200\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n"
        " -----END PGP SIGNATURE-----\n"
        f"\n{message}\n"
    )
    out = raw(repo, "hash-object", "-t", "commit", "-w", "--stdin", data=body.encode())
    return out.decode().strip()


def test_signed_commit_gets_new_sha_and_so_do_its_descendants(tmp_path, rich_repo):
    main = git("rev-parse", "main", cwd=rich_repo).strip()
    signed = signed_commit(rich_repo, main)
    git("update-ref", "refs/heads/main", signed, cwd=rich_repo)
    (rich_repo / "after.txt").write_text("after\n")
    git("add", "after.txt", cwd=rich_repo)
    child = commit(rich_repo, "after the signed one", 1700007000)

    dest = tmp_path / "out.git"
    clone(GitApi(rich_repo), dest)

    local, orig = ref_map(dest), ref_map(rich_repo)
    assert local["refs/heads/main"] != child
    assert {r: s for r, s in local.items() if r != "refs/heads/main"} == {
        r: s for r, s in orig.items() if r != "refs/heads/main"
    }
    # same content, same history shape, only the signature is gone
    tree = "main^{tree}"
    assert git("rev-parse", tree, cwd=dest) == git("rev-parse", tree, cwd=rich_repo)
    assert git("rev-parse", "main~1^{tree}", cwd=dest) == git(
        "rev-parse", "main~1^{tree}", cwd=rich_repo
    )
    assert git("rev-parse", "main~2", cwd=dest) == git("rev-parse", "main~2", cwd=rich_repo)
    assert "gpgsig" not in git("cat-file", "commit", "main~1", cwd=dest)
    assert inexact_summary(dest) == (
        "1 commit, 1 later commit could not be rebuilt exactly and got new SHAs"
    )
    git("fsck", cwd=dest)

    # the remap is kept: a second run neither refetches nor duplicates anything
    cloner, _ = clone(GitApi(rich_repo), dest)
    assert cloner.stats["commits"] == 0
    assert ref_map(dest)["refs/heads/main"] == local["refs/heads/main"]
    assert inexact_summary(dest).startswith("1 commit, 1 later commit")


def test_branch_replaced_by_a_nested_one(tmp_path, rich_repo):
    git("branch", "foo", cwd=rich_repo)
    dest = tmp_path / "out.git"
    clone(GitApi(rich_repo), dest)
    git("branch", "-D", "foo", cwd=rich_repo)
    git("branch", "foo/bar", cwd=rich_repo)
    clone(GitApi(rich_repo), dest)
    assert ref_map(dest) == ref_map(rich_repo)


def test_inexact_commit_keeps_its_full_message(tmp_path, rich_repo):
    main = git("rev-parse", "main", cwd=rich_repo).strip()
    long_message = "signed " + "y" * 200
    git(
        "update-ref", "refs/heads/main", signed_commit(rich_repo, main, long_message), cwd=rich_repo
    )
    dest = tmp_path / "out.git"
    clone(GitApi(rich_repo, short_messages=True), dest)
    assert git("log", "-1", "--format=%B", "main", cwd=dest).strip() == long_message


def test_unreadable_directory_names_the_problem(tmp_path, rich_repo):
    src = GitApi(rich_repo)
    real = src._route

    def no_subdirs(path, params):
        if path.endswith("/tree") and params.get("path"):
            raise SourceError(404, "404 Tree Not Found")
        return real(path, params)

    src._route = no_subdirs
    with pytest.raises(RuntimeError, match="not valid UTF-8.*--protocol ssh/https"):
        clone(src, tmp_path / "out.git")


def test_switch_from_api_to_git_later(tmp_path, rich_repo):
    main = git("rev-parse", "main", cwd=rich_repo).strip()
    git("update-ref", "refs/heads/main", signed_commit(rich_repo, main), cwd=rich_repo)
    dest = tmp_path / "out.git"
    clone(GitApi(rich_repo), dest)
    assert inexact_summary(dest)

    action, res = cli.clone_one(str(rich_repo), dest, False, None)
    assert (action, res.returncode) == ("updated", 0), res.stderr
    assert ref_map(dest) == ref_map(rich_repo)  # now with the real (signed) commit
    assert inexact_summary(dest) is None


def test_tag_by_someone_else_gets_a_new_sha(tmp_path, rich_repo):
    subprocess.run(
        ["git", "tag", "-a", "v9", "-m", "by the release manager", "main"],
        cwd=rich_repo,
        env=git_env(1700009000, "+0200", name="Rel", email="rel@example.org"),
        check=True,
    )
    dest = tmp_path / "out.git"
    clone(GitApi(rich_repo), dest)
    local, orig = ref_map(dest), ref_map(rich_repo)
    assert local["refs/tags/v9"] != orig["refs/tags/v9"]
    assert local["refs/tags/v1"] == orig["refs/tags/v1"]
    assert git("rev-parse", "v9^{}", cwd=dest) == git("rev-parse", "v9^{}", cwd=rich_repo)
    assert git("cat-file", "-t", "v9", cwd=dest).strip() == "tag"
    assert inexact_summary(dest) == "1 tag could not be rebuilt exactly and got new SHAs"


def test_tag_on_a_tree_is_skipped(tmp_path, rich_repo):
    tree = git("rev-parse", "main^{tree}", cwd=rich_repo).strip()
    git("tag", "tree-tag", tree, cwd=rich_repo)
    cloner, _ = clone(GitApi(rich_repo), tmp_path / "out.git")
    assert cloner.skipped_tags == ["tree-tag"]


def test_wrong_blob_content_is_an_error(tmp_path, rich_repo):
    src = GitApi(rich_repo)
    src._request = lambda url, params=None: Resp(200, b"not what you asked for")
    with pytest.raises(RuntimeError, match="different content"):
        clone(src, tmp_path / "out.git")


def test_missing_history_is_an_error(tmp_path, rich_repo):
    src = GitApi(rich_repo)
    real = src._route

    def gap(path, params):
        result = real(path, params)
        return result[:1] if path.endswith("/commits") else result

    src._route = gap
    with pytest.raises(RuntimeError, match="missing from the API's history"):
        clone(src, tmp_path / "out.git")


def test_empty_repo(tmp_path):
    repo = tmp_path / "empty"
    git("init", "-q", "-b", "main", str(repo))
    cloner, action = clone(GitApi(repo), tmp_path / "out.git")
    assert action == "cloned" and refs(tmp_path / "out.git") == []


def test_object_store_writes_what_git_reads(tmp_path):
    git("init", "-q", "--bare", str(tmp_path / "r.git"))
    store = apiclone.ObjectStore(tmp_path / "r.git")
    sha = store.write("blob", b"hello\n")
    assert sha == "ce013625030ba8dba906f756967f9e9ca394464a"  # git hash-object of "hello\n"
    assert git("cat-file", "blob", sha, cwd=tmp_path / "r.git") == "hello\n"
    assert apiclone.ObjectStore(tmp_path / "r.git").local(sha) == sha


def test_git_time_and_variants():
    assert apiclone.git_time("2023-11-14T22:13:20.000+05:30") == "1699980200 +0530"
    assert apiclone.git_time("2023-11-14T16:43:20Z") == "1699980200 +0000"
    assert apiclone.git_time("2023-11-14T08:43:20-08:00") == "1699980200 -0800"
    assert apiclone.time_variants("1 +0000") == ["1 +0000", "1 -0000"]


def test_hammer_warning_is_loud():
    text = apiclone.hammer_warning(3, 5.0)
    assert "3 project(s)" in text and "|##########|" in text
    assert "at most 5 requests/s" in text
    assert "WITHOUT a rate limit" in apiclone.hammer_warning(1)
    assert all(len(line) <= 79 for line in text.splitlines())


# ---------------------------------------------------------------- wiki


def test_wiki_snapshot(tmp_path):
    src = GitApi(tmp_path)
    src.wiki_pages = [
        {"slug": "home", "format": "markdown", "content": "# Home\n"},
        {"slug": "guides/setup", "format": "asciidoc", "content": "= Setup\n"},
        {
            "slug": "odd",
            "format": "markdown",
            "content": "body\n",
            "front_matter": {"title": "A: title / with slash"},
            "encoding": "UTF-8",
        },
    ]
    wiki = tmp_path / "w.wiki.git"
    assert snapshot_wiki(src, 7, wiki) == 3
    files = git("ls-tree", "-r", "--name-only", "main", cwd=wiki).split()
    assert files == ["guides/setup.asciidoc", "home.md", "odd.md"]
    assert (
        git("show", "main:odd.md", cwd=wiki) == '---\ntitle: "A: title / with slash"\n---\nbody\n'
    )
    first = git("rev-parse", "main", cwd=wiki)

    assert snapshot_wiki(src, 7, wiki) == 3  # unchanged: no new commit
    assert git("rev-parse", "main", cwd=wiki) == first

    src.wiki_pages[0]["content"] = "# Home v2\n"
    snapshot_wiki(src, 7, wiki)
    assert git("rev-parse", "main~1", cwd=wiki) == first
    git("fsck", "--strict", cwd=wiki)


def test_wiki_disabled(tmp_path):
    class NoWiki(GitApi):
        def paginate(self, path, params=None, on_page=None):
            raise SourceError(404, "404 Not Found")

    assert snapshot_wiki(NoWiki(tmp_path), 7, tmp_path / "w.wiki.git") == 0
    assert not (tmp_path / "w.wiki.git").exists()


# ---------------------------------------------------------------- CLI


@pytest.fixture
def api_source(rich_repo, monkeypatch):
    src = GitApi(rich_repo)
    src.wiki_pages = [{"slug": "home", "format": "markdown", "content": "# Home\n"}]
    monkeypatch.setattr(cli, "SourceSession", lambda *a, **kw: src)
    return src


def test_checkout_via_api_and_push(
    tmp_path, rich_repo, api_source, write_csv, gitlab, run_cli, capsys
):
    csv_path = write_csv([{"id": 7, "path_with_namespace": "grp/rich", "default_branch": "main"}])
    mirrors = tmp_path / "mirrors"
    checkout = ("checkout", "--from-csv", csv_path, "--transport", "api", "--dest", mirrors)
    assert run_cli(*checkout, "--source-url", "src.example.org", *GIT_ONLY) == 0
    out = capsys.readouterr().out
    assert "|##########|=." in out  # the hammer
    assert "cloned   grp/rich (" in out and "wiki snapshot: 1 pages" in out
    assert ref_map(mirrors / "grp/rich.git") == ref_map(rich_repo)

    push = ("push", "--target-url", "gl.example.org", "--group", "dept/team", "--from-csv")
    push += (csv_path, "--mirror-dir", mirrors, "--protocol", "ssh", "--flatten", "-y")
    assert run_cli(*push, *GIT_ONLY) == 0
    project = gitlab.projects["dept/team/rich"]
    assert ref_map(project["_repo"]) == ref_map(rich_repo)
    assert refs(project["_wiki"]) == ["refs/heads/main"]


def test_checkout_via_api_warns_about_new_shas(
    tmp_path, rich_repo, api_source, write_csv, run_cli, capsys
):
    main = git("rev-parse", "main", cwd=rich_repo).strip()
    signed = signed_commit(rich_repo, main)
    git("update-ref", "refs/heads/main", signed, cwd=rich_repo)
    csv_path = write_csv([{"id": 7, "path_with_namespace": "grp/rich", "default_branch": "main"}])
    args = ("checkout", "--from-csv", csv_path, "--protocol", "api", "--dest", tmp_path / "m")
    assert run_cli(*args, "--source-url", "s.example.org", "--no-wiki", *GIT_ONLY) == 0
    out = capsys.readouterr().out
    assert "NEW SHAs - see the warning at the end" in out
    assert "WARNING: 1 project(s) with rewritten history" in out
    assert "grp/rich\n!!        1 commit could not be rebuilt exactly" in out


def test_checkout_via_api_marks_lfs_projects(
    tmp_path, rich_repo, api_source, write_csv, run_cli, capsys, monkeypatch
):
    monkeypatch.setattr(cli, "lfs_pointer_count", lambda repo: 2)
    csv_path = write_csv([{"id": 7, "path_with_namespace": "grp/rich", "default_branch": "main"}])
    args = ("checkout", "--from-csv", csv_path, "--protocol", "api", "--dest", tmp_path / "m")
    assert run_cli(*args, "--source-url", "s.example.org", "--no-wiki", *GIT_ONLY) == 1
    out = capsys.readouterr().out
    assert "2 LFS files not downloaded: LFS needs git access" in out


def test_api_is_offered_and_needs_a_source(tmp_path, write_csv, run_cli, answers):
    csv_path = write_csv([{"id": 7, "path_with_namespace": "grp/rich"}])
    answers("a")
    with pytest.raises(SystemExit, match="--source-url"):
        run_cli("checkout", "--from-csv", csv_path, "--dest", tmp_path / "m", *GIT_ONLY)


def test_api_cannot_make_working_copies(tmp_path, write_csv, run_cli):
    csv_path = write_csv([{"id": 7, "path_with_namespace": "grp/rich"}])
    with pytest.raises(SystemExit, match="--working-copy"):
        run_cli(
            "checkout",
            "--from-csv",
            csv_path,
            "--protocol",
            "api",
            "--working-copy",
            "--source-url",
            "s.example.org",
        )


def test_api_failure_is_reported_and_others_continue(
    tmp_path, rich_repo, api_source, write_csv, run_cli, capsys
):
    csv_path = write_csv(
        [
            {"id": 8, "path_with_namespace": "grp/broken"},
            {"id": 7, "path_with_namespace": "grp/rich", "default_branch": "main"},
        ]
    )
    args = ("checkout", "--from-csv", csv_path, "--protocol", "api", "--dest", tmp_path / "m")
    assert run_cli(*args, "--source-url", "s.example.org", "--no-wiki", *GIT_ONLY) == 1
    out = capsys.readouterr().out
    assert "FAILED   grp/broken" in out
    assert "cloned   grp/rich" in out


def test_wiki_failure_does_not_block_the_repository(
    tmp_path, rich_repo, api_source, write_csv, run_cli, capsys
):
    def broken_wiki(path, params=None, on_page=None):
        if path.endswith("/wikis"):
            raise SourceError(500, "GET /wikis -> 500")
        yield from GitApi.paginate(api_source, path, params)

    api_source.paginate = broken_wiki
    csv_path = write_csv([{"id": 7, "path_with_namespace": "grp/rich", "default_branch": "main"}])
    args = ("checkout", "--from-csv", csv_path, "--protocol", "api", "--dest", tmp_path / "m")
    assert run_cli(*args, "--source-url", "s.example.org", *GIT_ONLY) == 1
    assert "FAILED   grp/rich (repository cloned, wiki failed)" in capsys.readouterr().out
    assert ref_map(tmp_path / "m/grp/rich.git") == ref_map(rich_repo)

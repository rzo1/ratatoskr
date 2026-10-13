"""Clone without git access: rebuild a repository from the source's REST API.

For sources where neither SSH nor HTTPS git work with the browser session, every git
object is rebuilt from the REST API and written into a local bare repository:

- commits from /repository/commits (author, committer, dates with offset, message, parents)
- trees from /repository/tree (names, modes and object ids of one directory)
- blobs from /repository/blobs/<sha>/raw (the exact bytes)
- branches and tags from /repository/branches and /repository/tags

Objects are content-addressed, so every rebuilt object is checked against the SHA the
source reports. Blobs must match exactly. A commit or tag that cannot be rebuilt exactly
(e.g. a signed commit: the API does not return the signature) is written with what the
API returns; it gets a new SHA, and so do all commits built on top of it. These are kept
in a remap file and reported, the content is still complete.

Objects already present (from an earlier run, or an earlier git clone) are never fetched
again, so an interrupted run continues where it stopped. Wikis cannot be read via git
either: they are rebuilt as one snapshot commit of the current pages (no history, no
attachments).
"""

import hashlib
import json
import subprocess
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from ratatoskr.gitops import API_REMAP_FILE, english, git_dir, run_git
from ratatoskr.source import SourceError

REMAP_FILE = API_REMAP_FILE
WIKI_EXTENSIONS = {"markdown": "md", "rdoc": "rdoc", "asciidoc": "asciidoc", "org": "org"}
WIKI_IDENTITY = "Ratatoskr <ratatoskr@localhost>"
# every UTC offset in use (quarter hours from -12:00 to +14:00), to find a tag's time zone
ALL_ZONES = [timedelta(minutes=m) for m in range(-12 * 60, 14 * 60 + 1, 15)]

HAMMER = r"""
 __________         _    ___  ___     _  _    _    __  __  __  __  ___  ___
|##########|=.     /_\  | _ \|_ _|   | || |  /_\  |  \/  ||  \/  || __|| _ \
|##########|=|    / _ \ |  _/ | |    | __ | / _ \ | |\/| || |\/| || _| |   /
'----------'='   /_/ \_\|_|  |___|   |_||_|/_/ \_\|_|  |_||_|  |_||___||_|_\
     ||
     ||          Every commit, directory and file is one request to the
     ||          source GitLab - thousands of requests per repo, for hours.
     ||          Only use it if SSH and HTTPS are really impossible.
""".strip("\n")


def hammer_warning(count, max_rate=None):
    bar = "#" * 79
    pace = f"at most {max_rate:g} requests/s" if max_rate else "WITHOUT a rate limit"
    return (
        f"\n{bar}\n{HAMMER}\n"
        f"   {count} project(s) will be rebuilt request by request, one after another,\n"
        f"   {pace} (--max-rate).\n"
        "   Interrupted runs continue where they stopped (Ctrl+C is safe).\n"
        f"{bar}\n"
    )


def object_hash(kind, body: bytes):
    return hashlib.sha1(f"{kind} {len(body)}\0".encode() + body).hexdigest()


def stamp(seconds, offset: timedelta):
    """git's 'seconds +hhmm'."""
    minutes = int(offset.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"{seconds} {sign}{abs(minutes) // 60:02d}{abs(minutes) % 60:02d}"


def git_time(iso):
    """'2024-05-01T12:00:00.000+02:00' -> '1714557600 +0200'."""
    dt = datetime.fromisoformat(iso)
    return stamp(int(dt.timestamp()), dt.utcoffset() or timedelta(0))


def time_variants(stamp):
    """A UTC stamp may also have been written as '-0000' (which ISO dates cannot show)."""
    return [stamp, stamp[:-5] + "-0000"] if stamp.endswith("+0000") else [stamp]


def message_variants(message):
    variants = [message, message + "\n", message.rstrip("\n") + "\n", message.rstrip("\n")]
    return list(dict.fromkeys(variants))


def tree_sort_key(entry):
    """git sorts tree entries by name, comparing directories as 'name/'."""
    name, mode, _ = entry
    return name + (b"/" if mode == "40000" else b"")


def tree_body(entries):
    """Raw tree object from [(name bytes, mode, hex sha)]."""
    return b"".join(
        mode.encode() + b" " + name + b"\0" + bytes.fromhex(sha)
        for name, mode, sha in sorted(entries, key=tree_sort_key)
    )


class ObjectStore:
    """Writes loose objects into a bare repo and knows which objects it already has.

    ``remap`` maps source SHAs that could not be rebuilt exactly to their local SHA.
    """

    def __init__(self, repo: Path, env=None):
        self.repo = Path(repo)
        self.env = env
        self.objects = git_dir(repo) / "objects"
        out = run_git(["cat-file", "--batch-all-objects", "--batch-check=%(objectname)"], repo, env)
        self.have = set(out.split())
        self.remap_path = git_dir(repo) / REMAP_FILE
        self.remap, self.inexact = {}, {}
        if self.remap_path.exists():
            for line in self.remap_path.read_text().splitlines():
                orig, local, kind = line.split()
                self.remap[orig] = local
                self.inexact[orig] = kind

    def local(self, sha):
        """Local SHA of a source object, or None if it is not here yet."""
        if sha in self.remap:
            return self.remap[sha]
        return sha if sha in self.have else None

    def write(self, kind, body: bytes):
        sha = object_hash(kind, body)
        if sha not in self.have:
            path = self.objects / sha[:2] / sha[2:]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"tmp-{path.name}")
            tmp.write_bytes(zlib.compress(f"{kind} {len(body)}\0".encode() + body))
            tmp.replace(path)  # atomic: an interrupted run never leaves a broken object
            self.have.add(sha)
        return sha

    def add_remap(self, orig, local, kind):
        """Record that source object ``orig`` exists here as ``local`` (``kind`` for reports)."""
        self.remap[orig] = local
        self.inexact[orig] = kind
        with open(self.remap_path, "a") as f:
            f.write(f"{orig} {local} {kind}\n")

    def counts(self):
        """{kind: number} of the source objects that got a different SHA."""
        result = {}
        for kind in self.inexact.values():
            result[kind] = result.get(kind, 0) + 1
        return result


class ApiCloner:
    """Rebuild one project's branches and tags from the API into ``dest`` (a bare repo)."""

    def __init__(self, source, project_id, dest: Path, env=None, on_progress=None):
        self.source = source
        self.pid = quote(str(project_id), safe="")
        self.dest = Path(dest)
        self.env = env
        self.on_progress = on_progress or (lambda text: None)
        self.stats = {"commits": 0, "trees": 0, "blobs": 0, "requests": 0}

    def _repo(self, path):
        return f"/projects/{self.pid}/repository{path}"

    def _list(self, path, params=None):
        self.stats["requests"] += 1
        return self.source.paginate(self._repo(path), params)

    def _progress(self, todo=None):
        s = self.stats
        done = f"{s['commits']}/{todo}" if todo else str(s["commits"])
        self.on_progress(
            f"API: {done} commits, {s['trees']} dirs, {s['blobs']} files, {s['requests']} requests"
        )

    # ------------------------------------------------------------ objects

    def fetch_blob(self, sha):
        if self.store.local(sha):
            return
        self.stats["requests"] += 1
        resp = self.source._request(f"{self.source.api}{self._repo(f'/blobs/{sha}/raw')}")
        if not resp.ok:
            raise SourceError(resp.status, f"file {sha}: HTTP {resp.status}")
        if self.store.write("blob", resp.body()) != sha:
            raise RuntimeError(f"file {sha}: the API returned different content")
        self.stats["blobs"] += 1

    def build_tree(self, commit, path=""):
        """Local SHA of the tree at ``path`` in source commit ``commit``."""
        params = {"ref": commit, "per_page": 100, **({"path": path} if path else {})}
        entries = []
        for e in self._list("/tree", params):
            mode = e["mode"].lstrip("0") if e["type"] == "tree" else e["mode"]
            sha = e["id"]
            if e["type"] == "tree" and not (sha := self.store.local(e["id"])):
                sha = self.build_subtree(commit, f"{path}/{e['name']}" if path else e["name"])
                if sha != e["id"]:  # a file name the API returned changed (not UTF-8)
                    self.store.add_remap(e["id"], sha, "tree")
            elif e["type"] == "blob":
                self.fetch_blob(sha)
            entries.append((e["name"].encode(), mode, sha))  # "commit": submodule, kept as is
        self.stats["trees"] += 1
        self._progress()
        return self.store.write("tree", tree_body(entries))

    def build_subtree(self, commit, path):
        try:
            return self.build_tree(commit, path)
        except SourceError as e:
            if e.status != 404:
                raise
            raise RuntimeError(
                f"directory {path!r} of commit {commit} cannot be read via the API (its name "
                "is probably not valid UTF-8) - use --protocol ssh/https for this project"
            ) from e

    # ------------------------------------------------------------ commits

    def missing_commits(self, tip):
        """{sha: commit} of all ancestors of ``tip`` (incl.) that are not here yet."""
        needed = set() if self.store.local(tip) else {tip}
        seen, new = {}, {}

        def take(sha):
            stack = [sha]
            while stack:
                s = stack.pop()
                if s in new or self.store.local(s):
                    continue
                if s not in seen:
                    needed.add(s)
                    continue
                new[s] = seen.pop(s)
                needed.discard(s)
                stack.extend(new[s]["parent_ids"])

        if not needed:
            return new
        # any order works; order=topo would make the source walk the whole history per page
        params = {"ref_name": tip, "per_page": 100}
        for c in self._list("/commits", params):
            seen[c["id"]] = c
            if c["id"] in needed:
                take(c["id"])
            if not needed:
                return new
        raise RuntimeError(
            f"{len(needed)} commits (e.g. {min(needed)}) are missing from the API's history "
            f"of {tip}"
        )

    @staticmethod
    def parents_first(commits):
        order, done = [], set()
        for start in commits:
            stack = [(start, False)]
            while stack:
                sha, expanded = stack.pop()
                if sha in done or sha not in commits:
                    continue
                if expanded:
                    done.add(sha)
                    order.append(sha)
                    continue
                stack.append((sha, True))
                stack.extend((p, False) for p in commits[sha]["parent_ids"])
        return order

    def commit_candidates(self, c, tree, parents):
        head = [f"tree {tree}", *(f"parent {p}" for p in parents)]
        author = f"{c['author_name']} <{c['author_email']}>"
        committer = f"{c['committer_name']} <{c['committer_email']}>"
        for a_time in time_variants(git_time(c["authored_date"])):
            for c_time in time_variants(git_time(c["committed_date"])):
                lines = [*head, f"author {author} {a_time}", f"committer {committer} {c_time}"]
                for message in message_variants(c["message"]):
                    yield ("\n".join(lines) + "\n\n" + message).encode()

    def write_commit(self, c):
        tree = self.build_tree(c["id"])
        parents = [self.store.local(p) for p in c["parent_ids"]]
        if None in parents:
            raise RuntimeError(f"commit {c['id']}: parent missing locally")
        exact = parents == c["parent_ids"]
        if exact:
            for attempt in (c, None):
                if attempt is None:  # list entries may shorten long messages
                    self.stats["requests"] += 1
                    c = attempt = self.source.get(self._repo(f"/commits/{c['id']}"))
                for body in self.commit_candidates(attempt, tree, parents):
                    if object_hash("commit", body) == c["id"]:
                        self.store.write("commit", body)
                        return
        body = next(self.commit_candidates(c, tree, parents))
        kind = "commit" if exact else "descendant"
        self.store.add_remap(c["id"], self.store.write("commit", body), kind)

    def fetch_history(self, tip):
        new = self.missing_commits(tip)
        for i, sha in enumerate(self.parents_first(new), 1):
            self.write_commit(new[sha])
            self.stats["commits"] += 1
            self._progress(f"{i}/{len(new)} new")

    # ------------------------------------------------------------ tags

    def tag_candidates(self, tag, target):
        commit = tag["commit"]
        people = dict.fromkeys(
            [
                f"{commit['committer_name']} <{commit['committer_email']}>",
                f"{commit['author_name']} <{commit['author_email']}>",
            ]
        )
        # the API shows the tag date in its own zone and the tagger's is not known, so the
        # commit's zones are tried first (the first candidate is the fallback), then all
        created = datetime.fromisoformat(tag.get("created_at") or commit["committed_date"])
        seconds = int(created.timestamp())
        zones = [
            datetime.fromisoformat(commit["committed_date"]).utcoffset(),
            datetime.fromisoformat(commit["authored_date"]).utcoffset(),
            created.utcoffset(),
            *ALL_ZONES,
        ]
        stamps = [stamp(seconds, z or timedelta(0)) for z in dict.fromkeys(zones)]
        stamps = [v for s in stamps for v in time_variants(s)]
        for person in people:
            for when in stamps:
                head = f"object {target}\ntype commit\ntag {tag['name']}\ntagger {person} {when}"
                for message in message_variants(tag.get("message") or ""):
                    yield f"{head}\n\n{message}".encode()

    def tag_ref(self, tag):
        """Local SHA for refs/tags/<name>, or None for tags not pointing to a commit."""
        commit = tag.get("commit")
        if not commit:
            return None
        target = self.store.local(commit["id"])
        if tag["target"] == commit["id"]:
            return target  # lightweight tag
        if local := self.store.local(tag["target"]):
            return local
        if target == commit["id"]:
            for body in self.tag_candidates(tag, target):
                if object_hash("tag", body) == tag["target"]:
                    return self.store.write("tag", body)
        body = next(self.tag_candidates(tag, target))
        local = self.store.write("tag", body)
        self.store.add_remap(tag["target"], local, "tag")
        return local

    # ------------------------------------------------------------ refs

    def update_refs(self, refs, default_branch):
        current = run_git(
            ["for-each-ref", "--format=%(refname)", "refs/heads/", "refs/tags/"],
            self.dest,
            self.env,
        ).split()
        # deletes first and on their own: git cannot create foo/bar while foo still exists,
        # not even when the same transaction deletes foo
        deletes = [f"delete {ref}\n" for ref in current if ref not in refs]
        updates = [f"update {ref} {sha}\n" for ref, sha in refs.items()]
        for commands in (deletes, updates):
            if not commands:
                continue
            res = subprocess.run(
                ["git", "update-ref", "--stdin"],
                cwd=self.dest,
                env=english(self.env),
                input="".join(commands),
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise RuntimeError(f"git update-ref failed: {res.stderr.strip()}")
        if default_branch and f"refs/heads/{default_branch}" in refs:
            run_git(["symbolic-ref", "HEAD", f"refs/heads/{default_branch}"], self.dest, self.env)

    def run(self, default_branch=None):
        """Rebuild all branches and tags; returns the action ("cloned" or "updated")."""
        action = "updated" if self.dest.exists() else "cloned"
        if not self.dest.exists():
            self.dest.parent.mkdir(parents=True, exist_ok=True)
            run_git(["init", "-q", "--bare", str(self.dest)], None, self.env)
        self.store = ObjectStore(self.dest, self.env)
        branches = list(self._list("/branches", {"per_page": 100}))
        tags = list(self._list("/tags", {"per_page": 100}))
        tips = [b["commit"]["id"] for b in branches]
        tips += [t["commit"]["id"] for t in tags if t.get("commit")]
        for tip in dict.fromkeys(tips):
            self.fetch_history(tip)

        refs = {f"refs/heads/{b['name']}": self.store.local(b["commit"]["id"]) for b in branches}
        self.skipped_tags = []
        for tag in tags:
            if sha := self.tag_ref(tag):
                refs[f"refs/tags/{tag['name']}"] = sha
            else:
                self.skipped_tags.append(tag["name"])
        self.update_refs(refs, default_branch)
        if self.stats["commits"]:
            run_git(["repack", "-a", "-d", "-q"], self.dest, self.env)
        run_git(
            ["fsck", "--connectivity-only", "--no-dangling", "--no-progress"], self.dest, self.env
        )
        return action


def inexact_summary(repo):
    """'2 commits, 5 later commits, 1 tag got new SHAs' for a repo built via the API, or None."""
    path = git_dir(repo) / REMAP_FILE
    if not path.exists():
        return None
    counts = {}
    for line in path.read_text().splitlines():
        kind = line.split()[2]
        counts[kind] = counts.get(kind, 0) + 1
    labels = {"commit": "commit", "descendant": "later commit", "tag": "tag"}
    parts = [
        f"{counts[k]} {labels[k]}{'s' if counts[k] != 1 else ''}" for k in labels if k in counts
    ]
    return ", ".join(parts) + " could not be rebuilt exactly and got new SHAs"


# ---------------------------------------------------------------- wiki


def wiki_tree(store, pages):
    """Write the pages as files (nested by their slug); returns the root tree SHA."""
    root = {}
    for page in pages:
        content = page.get("content") or ""
        if page.get("front_matter"):  # the API returns it apart from the content
            yaml = "".join(f"{k}: {json.dumps(v)}\n" for k, v in page["front_matter"].items())
            content = f"---\n{yaml}---\n{content}"
        data = content.encode()
        ext = WIKI_EXTENSIONS.get(page.get("format"), "md")
        *dirs, name = page["slug"].split("/")
        node = root
        for d in dirs:
            node = node.setdefault(d, {})
        node[f"{name}.{ext}"] = data

    def write(node):
        entries = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append((name.encode(), "40000", write(value)))
            else:
                entries.append((name.encode(), "100644", store.write("blob", value)))
        return store.write("tree", tree_body(entries))

    return write(root)


def snapshot_wiki(source, project_id, dest: Path, env=None, now=None):
    """Save the wiki's current pages as a commit in ``dest``; returns the number of pages.

    A new commit is only made if the pages changed, on top of the previous snapshot, so the
    wiki can be pushed again without force.
    """
    pid = quote(str(project_id), safe="")
    try:
        pages = list(source.paginate(f"/projects/{pid}/wikis", {"with_content": 1}))
    except SourceError as e:
        if e.status in (403, 404):  # wiki disabled
            return 0
        raise
    if not pages:
        return 0
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        run_git(["init", "-q", "--bare", "--initial-branch=main", str(dest)], None, env)
    store = ObjectStore(dest, env)
    tree = wiki_tree(store, pages)
    branch = run_git(["symbolic-ref", "HEAD"], dest, env).strip()
    parent = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", branch],
        cwd=dest,
        env=english(env),
        capture_output=True,
        text=True,
    ).stdout.strip()
    if parent and run_git(["rev-parse", f"{parent}^{{tree}}"], dest, env).strip() == tree:
        return len(pages)
    stamp = git_time((now or datetime.now().astimezone()).isoformat())
    lines = [f"tree {tree}", *([f"parent {parent}"] if parent else [])]
    lines += [f"author {WIKI_IDENTITY} {stamp}", f"committer {WIKI_IDENTITY} {stamp}"]
    message = f"Wiki snapshot of {len(pages)} pages, read via the API of {source.base}\n"
    sha = store.write("commit", ("\n".join(lines) + "\n\n" + message).encode())
    run_git(["update-ref", branch, sha], dest, env)
    return len(pages)

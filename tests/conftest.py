import csv
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest
import requests

from ratatoskr import cli, source

CSV_HEADER = ["migrate", "target_path", "my_access", *source.FIELDS]


def git(*args, cwd=None):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def refs(repo):
    return sorted(git("for-each-ref", "--format=%(refname)", cwd=repo).split())


@pytest.fixture
def source_repo(tmp_path):
    """A source repo with branches main + dev, a lightweight tag v1 and an annotated tag v2."""
    repo = tmp_path / "source"
    git("init", "-q", "-b", "main", str(repo))
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    git("tag", "v1", cwd=repo)
    git("branch", "dev", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "second", cwd=repo)
    git("tag", "-a", "v2", "-m", "release 2", cwd=repo)
    return repo


@pytest.fixture
def source_wiki(source_repo):
    """The wiki repo GitLab would serve next to the source repo (<repo>.wiki.git)."""
    wiki = source_repo.parent / f"{source_repo.name}.wiki.git"
    git("init", "-q", "-b", "main", str(wiki))
    (wiki / "home.md").write_text("# Home\n")
    git("add", "home.md", cwd=wiki)
    git("commit", "-q", "-m", "wiki home", cwd=wiki)
    return wiki


@pytest.fixture
def write_csv(tmp_path):
    def _write(rows, name="repos.csv"):
        path = tmp_path / name
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_HEADER)
            w.writeheader()
            for row in rows:
                w.writerow({"migrate": "yes", **row})
        return path

    return _write


@pytest.fixture
def run_cli(monkeypatch):
    def _run(*argv):
        monkeypatch.setattr(sys, "argv", ["ratatoskr", *map(str, argv)])
        return cli.main()

    return _run


@pytest.fixture
def answers(monkeypatch):
    """Feed answers to input() prompts; fails if a prompt is not expected."""

    def _answers(*values):
        queue = list(values)

        def fake_input(prompt=""):
            assert queue, f"unexpected prompt: {prompt!r}"
            return queue.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)
        return queue

    return _answers


# ---------------------------------------------------------------- fake source


class FakeSource:
    """Stands in for SourceSession: API lists by path, downloadable files by URL."""

    base = "https://source.example.org"
    api = f"{base}/api/v4"

    def __init__(self):
        self.lists = {}
        self.files = {}
        self.calls = []
        self.user = {"username": "me"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def paginate(self, path, params=None, on_page=None):
        self.calls.append(path)
        value = self.lists.get(path, [])
        if isinstance(value, Exception):
            raise value
        if on_page:
            on_page(1, len(value))
        yield from value

    def download(self, url):
        return self.files.get(url)


SECRET = "0123456789abcdef0123456789abcdef"


def user(name):
    return {"username": name, "name": name.title()}


@pytest.fixture
def fake_source(monkeypatch):
    """Source project 7 (inst/sub/repo1) with labels, milestones, issues #1 #3 and MR !1."""
    src = FakeSource()
    web = f"{src.base}/inst/sub/repo1"
    milestone = {"id": 70, "title": "v1.0", "state": "closed", "due_date": "2024-12-31"}
    src.lists = {
        "/projects/7/labels": [{"name": "bug", "color": "#ff0000", "description": "Broken"}],
        "/projects/7/milestones": [milestone],
        "/projects/7/issues": [
            {
                "iid": 1,
                "title": "Login fails",
                "state": "opened",
                "description": f"See ![shot](/uploads/{SECRET}/shot.png), cc @alice",
                "author": user("bob"),
                "assignees": [user("alice")],
                "labels": ["bug"],
                "created_at": "2024-01-02T10:00:00.000Z",
                "updated_at": "2024-01-03T10:00:00.000Z",
                "web_url": f"{web}/-/issues/1",
            },
            {
                "iid": 3,
                "title": "Release",
                "state": "closed",
                "description": "Ship it",
                "author": user("carol"),
                "labels": ["release"],
                "milestone": milestone,
                "created_at": "2024-02-01T10:00:00.000Z",
                "updated_at": "2024-02-02T10:00:00.000Z",
                "closed_at": "2024-02-02T10:00:00.000Z",
                "closed_by": user("carol"),
                "web_url": f"{web}/-/issues/3",
            },
        ],
        "/projects/7/issues/1/notes": [
            {
                "id": 101,
                "body": "Same for me, mail me at bob@example.org",
                "author": user("dave"),
                "created_at": "2024-01-02T11:00:00.000Z",
            },
            {
                "id": 102,
                "body": "changed the description",
                "system": True,
                "author": user("bob"),
                "created_at": "2024-01-02T12:00:00.000Z",
            },
        ],
        "/projects/7/merge_requests": [
            {
                "iid": 1,
                "title": "Fix login",
                "state": "merged",
                "description": "Fixes #1",
                "author": user("alice"),
                "source_branch": "fix",
                "target_branch": "main",
                "merged_at": "2024-01-05T10:00:00.000Z",
                "merged_by": user("bob"),
                "created_at": "2024-01-04T10:00:00.000Z",
                "updated_at": "2024-01-05T10:00:00.000Z",
                "web_url": f"{web}/-/merge_requests/1",
            }
        ],
        "/projects/7/merge_requests/1/notes": [
            {
                "id": 201,
                "body": "LGTM @bob",
                "author": user("carol"),
                "created_at": "2024-01-05T09:00:00.000Z",
            }
        ],
    }
    src.files = {f"{src.api}/projects/7/uploads/{SECRET}/shot.png": b"PNG-DATA"}
    monkeypatch.setattr(cli, "SourceSession", lambda *a, **kw: src)
    return src


# ---------------------------------------------------------------- fake target


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self._payload = payload
        self.text = str(payload)
        self.headers = {}

    def json(self):
        return self._payload


class FakeGitLab:
    """In-memory GitLab API; created projects are real bare repos under target/."""

    def __init__(self, root: Path, scopes=("api",)):
        self.root = root
        self.scopes = list(scopes)
        self.access_level = 50  # owner of the created projects
        self.groups = {"dept/team": {"id": 1, "full_path": "dept/team"}}
        self.projects = {}
        self.calls = []
        self._next_id = 100

    def _id(self):
        self._next_id += 1
        return self._next_id

    def _group(self, ref):
        return next(
            (g for g in self.groups.values() if ref in (g["full_path"], str(g["id"]))), None
        )

    def _project_by_id(self, pid):
        return next(p for p in self.projects.values() if str(p["id"]) == str(pid))

    def _project_view(self, project):
        empty = not git("for-each-ref", cwd=project["_repo"]).strip()
        view = {k: v for k, v in project.items() if not k.startswith("_")}
        return view | {
            "empty_repo": empty,
            "permissions": {"project_access": {"access_level": self.access_level}},
        }

    def request(self, method, url, data=None, files=None, **_):
        path = unquote(url.split("/api/v4", 1)[1])
        self.calls.append((method, path, data))
        if m := re.fullmatch(r"/projects/(\d+)(?:/(.*))?", path):
            return self._project_route(method, self._project_by_id(m[1]), m[2] or "", data, files)
        match method, path.split("/", 2)[1:]:
            case "GET", ["user"]:
                return FakeResponse(200, {"username": "tester", "is_admin": False})
            case "GET", ["personal_access_tokens", "self"]:
                return FakeResponse(200, {"scopes": self.scopes})
            case "GET", ["groups", ref]:
                group = self._group(ref)
                return FakeResponse(200, group) if group else FakeResponse(404)
            case "POST", ["groups"]:
                parent = self._group(str(data["parent_id"]))
                full = f"{parent['full_path']}/{data['path']}"
                self.groups[full] = {"id": self._id(), "full_path": full}
                return FakeResponse(201, self.groups[full])
            case "GET", ["projects", full] if full in self.projects:
                return FakeResponse(200, self._project_view(self.projects[full]))
            case "GET", ["projects", _]:
                return FakeResponse(404)
            case "POST", ["projects"]:
                return FakeResponse(201, self._create_project(data))
        raise AssertionError(f"unexpected API call: {method} {path}")

    def _create_project(self, data):
        ns = self._group(str(data["namespace_id"]))
        full = f"{ns['full_path']}/{data['path']}"
        repo = self.root / f"{full}.git"
        git("init", "-q", "--bare", str(repo))
        git("init", "-q", "--bare", str(self.root / f"{full}.wiki.git"))
        self.projects[full] = {
            "id": self._id(),
            "path_with_namespace": full,
            "name": data["name"],
            "visibility": data["visibility"],
            "default_branch": None,
            "archived": False,
            "ssh_url_to_repo": str(repo),
            "http_url_to_repo": str(repo),
            "_repo": repo,
            "_wiki": self.root / f"{full}.wiki.git",
            "_labels": [],
            "_milestones": [],
            "_issues": {},
            "_uploads": {},
            "_wikis": {},
        }
        return self._project_view(self.projects[full])

    def _project_route(self, method, project, rest, data, files):
        if project["archived"] and method != "GET":
            return FakeResponse(403, "project is archived")
        issues = project["_issues"]
        match method, rest.split("/"):
            case "GET", [""]:
                return FakeResponse(200, self._project_view(project))
            case "PUT", [""]:
                project.update(data)
                return FakeResponse(200, self._project_view(project))
            case "POST", ["archive"]:
                project["archived"] = True
                return FakeResponse(201, self._project_view(project))
            case "GET", ["labels"]:
                return FakeResponse(200, project["_labels"])
            case "POST", ["labels"]:
                assert data["name"] not in {lb["name"] for lb in project["_labels"]}
                project["_labels"].append(data)
                return FakeResponse(201, data)
            case "GET", ["milestones"]:
                return FakeResponse(200, project["_milestones"])
            case "POST", ["milestones"]:
                milestone = {**data, "id": self._id(), "state": "active"}
                project["_milestones"].append(milestone)
                return FakeResponse(201, milestone)
            case "PUT", ["milestones", mid]:
                milestone = next(m for m in project["_milestones"] if str(m["id"]) == mid)
                milestone["state"] = "closed" if data.get("state_event") == "close" else "active"
                return FakeResponse(200, milestone)
            case "GET", ["issues"]:
                return FakeResponse(200, sorted(issues.values(), key=lambda i: -i["iid"]))
            case "POST", ["issues"]:
                iid = int(data["iid"]) if "iid" in data and self.access_level >= 50 else None
                iid = iid or max(issues, default=0) + 1
                assert iid not in issues
                issues[iid] = {**data, "iid": iid, "state": "opened", "_notes": []}
                return FakeResponse(201, issues[iid])
            case "PUT", ["issues", iid]:
                if data.get("state_event") == "close":
                    issues[int(iid)]["state"] = "closed"
                return FakeResponse(200, issues[int(iid)])
            case "POST", ["issues", iid, "notes"]:
                issues[int(iid)]["_notes"].append(data)
                return FakeResponse(201, data)
            case "POST", ["uploads"]:
                name, content = files["file"]
                secret = f"{len(project['_uploads']):032x}"
                project["_uploads"][secret] = (name, content)
                return FakeResponse(
                    201,
                    {
                        "url": f"/uploads/{secret}/{name}",
                        "full_path": f"/-/project/{project['id']}/uploads/{secret}/{name}",
                    },
                )
            case "POST", ["wikis"]:
                assert data["title"] not in project["_wikis"]
                project["_wikis"][data["title"]] = data["content"]
                return FakeResponse(201, data)
            case "PUT", ["wikis", *slug]:
                project["_wikis"]["/".join(slug)] = data["content"]
                return FakeResponse(200, data)
        raise AssertionError(f"unexpected project API call: {method} {rest}")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def gitlab(tmp_path, monkeypatch):
    fake = FakeGitLab(tmp_path / "target")
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda _session, method, url, **kw: fake.request(method, url, **kw),
    )
    monkeypatch.setenv("RATATOSKR_TARGET_TOKEN", "glpat-test")
    return fake

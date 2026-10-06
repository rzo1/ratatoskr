"""Read side: the SSO-protected source GitLab, accessed through a logged-in browser session.

A real (headed) Chromium window is opened and you log in via SSO yourself. The login is
detected by polling /api/v4/user with the browser's session cookie, and every later API
call reuses that session, so no personal access token is needed on the source. If the
session expires during a long run, you are asked to log in again in the same window.

The source may be slow: every request gets a generous timeout, and timeouts, connection
errors, 429 and 5xx answers are retried with exponential backoff (honouring Retry-After).
"""

import csv
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError

FIELDS = [
    "id",
    "path_with_namespace",
    "name",
    "ssh_url_to_repo",
    "http_url_to_repo",
    "web_url",
    "default_branch",
    "visibility",
    "archived",
    "empty_repo",
    "last_activity_at",
    "description",
]

ACCESS_LEVELS = {
    10: "guest",
    15: "planner",
    20: "reporter",
    30: "developer",
    40: "maintainer",
    50: "owner",
}

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_BACKOFF = 120


class SourceError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def with_scheme(url):
    url = url.rstrip("/")
    return url if urlparse(url).scheme else f"https://{url}"


def next_link(link_header):
    for part in link_header.split(","):
        url, _, rel = part.partition(";")
        if 'rel="next"' in rel:
            return url.strip().strip("<>")
    return None


def max_access_level(project):
    perms = project.get("permissions") or {}
    return max(
        (perms.get(k) or {}).get("access_level", 0) for k in ("project_access", "group_access")
    )


def my_access(project):
    return ACCESS_LEVELS.get(max_access_level(project), "none (visible only)")


class SourceSession:
    """Browser context with an SSO session; API calls and downloads reuse its cookies."""

    def __init__(
        self, base_url, profile_dir=".browser-profile", login_timeout=600, timeout=120, retries=5
    ):
        self.base = with_scheme(base_url)
        self.api = f"{self.base}/api/v4"
        self.profile_dir = profile_dir
        self.login_timeout = login_timeout
        self.timeout_ms = timeout * 1000
        self.retries = retries
        self.user = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(self.profile_dir, headless=False)
        self.ctx.set_default_timeout(self.timeout_ms)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.login()
        return self

    def __exit__(self, *exc):
        self.ctx.close()
        self._pw.stop()

    def login(self):
        try:
            # only wait until the server starts answering: a slow page must not abort the login
            self.page.goto(f"{self.base}/users/sign_in", wait_until="commit")
        except PlaywrightError as e:
            print(f"The sign-in page is slow to load ({e.message.splitlines()[0]}).")
        print("Log in via SSO in the browser window. Waiting for a valid session...")
        deadline = time.time() + self.login_timeout
        while time.time() < deadline:
            try:
                resp = self._get(f"{self.api}/user")
            except PlaywrightError:
                resp = None  # slow or unreachable right now: keep waiting
            if resp is not None and resp.ok:
                self.user = resp.json()
                print(f"Logged in as {self.user['username']} ({self.user.get('name')})")
                return self.user
            time.sleep(2)
        sys.exit(f"Not logged in after {self.login_timeout}s - giving up.")

    def _get(self, url, params=None):
        return self.ctx.request.get(
            url, params=params, fail_on_status_code=False, timeout=self.timeout_ms
        )

    def _request(self, url, params=None):
        """GET with retries for slow/overloaded servers and re-login on an expired session."""
        relogged = False
        attempt = 0
        while True:
            try:
                resp = self._get(url, params)
            except PlaywrightError as e:
                resp, problem = None, e.message.splitlines()[0]
            else:
                if resp.status == 401 and not relogged:
                    print("\nThe source session has expired - please log in again in the browser.")
                    self.login()
                    relogged = True
                    continue
                if resp.status not in RETRY_STATUSES:
                    return resp
                problem = f"HTTP {resp.status}"
            if attempt >= self.retries:
                if resp is not None:
                    return resp
                raise SourceError(0, f"GET {url} failed after {attempt + 1} attempts: {problem}")
            delay = min(2**attempt * 5, MAX_BACKOFF)
            retry_after = resp.headers.get("retry-after", "") if resp is not None else ""
            if retry_after.isdigit():
                delay = min(int(retry_after), MAX_BACKOFF)
            print(f"  source is slow or unavailable ({problem}), retrying in {delay}s ...")
            time.sleep(delay)
            attempt += 1

    def get(self, path, params=None):
        resp = self._request(f"{self.api}{path}", params)
        if not resp.ok:
            raise SourceError(resp.status, f"GET {path} -> {resp.status}: {resp.text()[:300]}")
        return resp.json()

    def paginate(self, path, params=None):
        """Yield all items of a list endpoint, following the Link header."""
        url, query = f"{self.api}{path}", {"per_page": 100, **(params or {})}
        while url:
            resp = self._request(url, query)
            if not resp.ok:
                raise SourceError(resp.status, f"GET {path} -> {resp.status}: {resp.text()[:300]}")
            yield from resp.json()
            url, query = next_link(resp.headers.get("link", "")), None

    def download(self, url):
        """Bytes of a file (e.g. an upload), or None if it is not available."""
        resp = self._request(url)
        if not resp.ok or resp.headers.get("content-type", "").startswith("text/html"):
            return None
        return resp.body()


def list_projects(source, args):
    """All projects matching the --scope/--visibility/--include-archived options."""
    params = {"simple": "false", "pagination": "keyset", "order_by": "id", "sort": "asc"}
    if args.scope == "membership":
        params["membership"] = "true"
    elif args.scope == "owned":
        params["owned"] = "true"
    if not args.include_archived:
        params["archived"] = "false"

    projects = []
    for project in source.paginate("/projects", params):
        projects.append(project)
        if len(projects) % 100 == 0:
            print(f"  {len(projects)} projects ...")
    projects.sort(key=lambda pr: pr["path_with_namespace"].lower())

    total = len(projects)
    projects = [pr for pr in projects if pr.get("visibility") in args.visibility]
    if len(projects) < total:
        wanted = "/".join(args.visibility)
        print(f"Skipped {total - len(projects)} projects not matching visibility {wanted}")
    return projects


def write_outputs(projects, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "repos.json").write_text(json.dumps(projects, indent=2, ensure_ascii=False))
    with open(out / "repos.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["migrate", "target_path", "my_access", *FIELDS])
        for pr in projects:
            w.writerow(["yes", "", my_access(pr), *(pr.get(k, "") for k in FIELDS)])
    (out / "repos.txt").write_text("".join(pr["ssh_url_to_repo"] + "\n" for pr in projects))
    print(f"\n{len(projects)} projects written to {out}/repos.{{json,csv,txt}}")


def read_csv_selection(path):
    with open(path, newline="") as f:
        return [
            r
            for r in csv.DictReader(f)
            if r["migrate"].strip().lower() in ("yes", "y", "1", "true")
        ]

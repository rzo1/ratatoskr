"""Write side: the target GitLab, accessed via REST API with a personal access token.

The target may rate-limit (e.g. wiki page creation): 429 answers and 400 "rate limited"
answers are retried after the time the server asks for (Retry-After / RateLimit-Reset) or
with exponential backoff. ``write_delay`` paces all writes to stay below the limits.
"""

import sys
import time
from urllib.parse import quote

from ratatoskr.source import max_access_level


def enc(path):
    return quote(str(path), safe="")


MAX_BACKOFF = 120


def is_rate_limited(resp):
    return resp.status_code == 429 or (
        resp.status_code == 400 and "rate limit" in resp.text.lower()
    )


def retry_delay(resp, attempt):
    """Seconds to wait before the next attempt, as the server asks or exponential."""
    headers = {k.lower(): v for k, v in resp.headers.items()}
    if headers.get("retry-after", "").isdigit():
        return min(int(headers["retry-after"]) + 1, MAX_BACKOFF)
    if headers.get("ratelimit-reset", "").isdigit():
        return min(max(int(headers["ratelimit-reset"]) - int(time.time()), 1) + 1, MAX_BACKOFF)
    return min(10 * 2**attempt, MAX_BACKOFF)


class TargetApi:
    def __init__(self, base, token, dry_run=False, retries=8, write_delay=0.0):
        import requests

        self.api = f"{base}/api/v4"
        self.dry_run = dry_run
        self.retries = retries
        self.write_delay = write_delay
        self.session = requests.Session()
        self.session.headers["PRIVATE-TOKEN"] = token
        self.groups = {}  # full_path -> group id (None = would be created in a dry run)
        self.user = None

    def call(self, method, path, **kwargs):
        attempt = 0
        while True:
            if method != "GET" and self.write_delay:
                time.sleep(self.write_delay)
            resp = self.session.request(method, f"{self.api}{path}", timeout=120, **kwargs)
            if not is_rate_limited(resp) or attempt >= self.retries:
                break
            delay = retry_delay(resp, attempt)
            print(f"   target rate limit on {method} {path}, waiting {delay}s ...", flush=True)
            time.sleep(delay)
            attempt += 1
        if not resp.ok and resp.status_code != 404:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp

    def get(self, path):
        resp = self.call("GET", path)
        return resp.json() if resp.ok else None

    def paginate(self, path, params=None):
        page = 1
        while page:
            resp = self.call("GET", path, params={**(params or {}), "per_page": 100, "page": page})
            if not resp.ok:
                return
            yield from resp.json()
            page = int(resp.headers.get("x-next-page") or 0)

    def write(self, method, path, data=None, files=None):
        resp = self.call(method, path, data=data, files=files)
        if not resp.ok:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def upload(self, project_id, filename, content):
        """Upload a file to a project; returns the absolute-path URL to reference it."""
        result = self.write(
            "POST", f"/projects/{project_id}/uploads", files={"file": (filename, content)}
        )
        return result.get("full_path") or result["url"]

    def check_token(self):
        self.user = self.get("/user")
        if not self.user:
            sys.exit("Target GitLab rejected the token.")
        scopes = (self.get("/personal_access_tokens/self") or {}).get("scopes", [])
        if "api" not in scopes:
            sys.exit(f"Token needs the 'api' scope (has: {', '.join(scopes) or 'unknown'}).")
        return self.user

    def can_keep_ids_and_dates(self, project_id):
        """Issue numbers and dates can only be set by admins and owners."""
        if (self.user or {}).get("is_admin"):
            return True
        return max_access_level(self.get(f"/projects/{project_id}") or {}) >= 50

    def root_group(self, ref):
        group = self.get(f"/groups/{enc(ref)}")
        if not group:
            sys.exit(f"Target group '{ref}' not found (or no access).")
        self.groups[group["full_path"]] = group["id"]
        return group

    def ensure_group(self, full_path, visibility):
        if full_path in self.groups:
            return self.groups[full_path]
        parent_path, _, name = full_path.rpartition("/")
        parent_id = self.ensure_group(parent_path, visibility)
        group = None if parent_id is None else self.get(f"/groups/{enc(full_path)}")
        if group:
            gid = group["id"]
        elif self.dry_run:
            print(f"   would create subgroup {full_path}")
            gid = None
        else:
            gid = self.write(
                "POST",
                "/groups",
                {"name": name, "path": name, "parent_id": parent_id, "visibility": visibility},
            )["id"]
            print(f"   created subgroup {full_path}")
        self.groups[full_path] = gid
        return gid

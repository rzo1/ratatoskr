"""Write side: the target GitLab, accessed via REST API with a personal access token."""

import sys
from urllib.parse import quote

from ratatoskr.source import max_access_level


def enc(path):
    return quote(str(path), safe="")


class TargetApi:
    def __init__(self, base, token, dry_run=False):
        import requests

        self.api = f"{base}/api/v4"
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers["PRIVATE-TOKEN"] = token
        self.groups = {}  # full_path -> group id (None = would be created in a dry run)
        self.user = None

    def call(self, method, path, **kwargs):
        resp = self.session.request(method, f"{self.api}{path}", timeout=120, **kwargs)
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

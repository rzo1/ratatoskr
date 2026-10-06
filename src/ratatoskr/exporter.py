"""Export issues, merge requests, labels and milestones of a source project.

Layout per project, next to the git mirror (same format as Freki)::

    <ns>/<project>-issues/0042-fix-login.md            human readable
    <ns>/<project>-issues/0042-fix-login.json          raw API data (item + notes)
    <ns>/<project>-issues/attachments/<secret>/<file>  downloaded uploads
    <ns>/<project>-issues/.index.json                  iid -> updated_at (skip unchanged)
    <ns>/<project>-merge-requests/...                  same for merge requests
    <ns>/<project>-meta.json                           labels and milestones

In the Markdown files attachment links are rewritten to the local copies; the JSON keeps
the original text, which ``importer`` needs to map uploads to the target.
"""

import json
import re
from pathlib import Path
from urllib.parse import unquote

from ratatoskr.source import SourceError

KINDS = {"issues": "issues", "merge-requests": "merge_requests"}  # folder suffix -> API path

# ``/uploads/<32 hex>/<filename>`` in any of the forms GitLab renders them:
#   /uploads/<secret>/<file>
#   /<group>/<project>/uploads/<secret>/<file>
#   /-/project/<id>/uploads/<secret>/<file>
#   https://host/<any of the above>
UPLOAD_RE = re.compile(
    r"(?:https?://[^\s()<>\"']+?)?(?:/[^\s()<>\"']*?)?/uploads/([0-9a-f]{32})/([^\s()<>\"']+)"
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
INDEX_FILE = ".index.json"


def slugify(text, max_len=50):
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def safe_filename(name):
    name = unquote(name).replace("/", "_").replace("\\", "_").strip()
    return name or "file"


def attachment_path(secret, filename):
    return f"attachments/{secret}/{safe_filename(filename)}"


def display(user):
    """``Jane Doe (@jdoe)`` for a user dict, ``-`` when absent."""
    if not user:
        return "-"
    name, username = user.get("name"), user.get("username")
    if name and username:
        return f"{name} (@{username})"
    return f"@{username}" if username else str(name or "-")


def date(value):
    return value.replace("T", " ")[:16] if value else "-"


def item_stem(data):
    return f"{int(data['iid']):04d}-{slugify(str(data.get('title') or ''))}"


def render_markdown(kind, data, notes, rewrite=lambda text: text or ""):
    """Readable Markdown of one issue or merge request with all its comments."""
    sigil = "#" if kind == "issues" else "!"
    rows = [
        ("Type", "Issue" if kind == "issues" else "Merge request"),
        ("State", str(data.get("state", "-"))),
        ("Author", display(data.get("author"))),
        ("Assignees", ", ".join(display(a) for a in data.get("assignees") or []) or "-"),
    ]
    if kind == "merge-requests":
        rows += [
            ("Reviewers", ", ".join(display(r) for r in data.get("reviewers") or []) or "-"),
            ("Branches", f"`{data.get('source_branch')}` → `{data.get('target_branch')}`"),
        ]
        if data.get("merged_at"):
            merger = data.get("merged_by") or data.get("merge_user")
            rows.append(("Merged", f"{date(data['merged_at'])} by {display(merger)}"))
        if data.get("merge_commit_sha"):
            rows.append(("Merge commit", f"`{data['merge_commit_sha']}`"))
    rows.append(("Labels", ", ".join(data.get("labels") or []) or "-"))
    rows.append(("Milestone", (data.get("milestone") or {}).get("title", "-")))
    rows.append(("Created", date(data.get("created_at"))))
    if data.get("closed_at"):
        rows.append(("Closed", f"{date(data['closed_at'])} by {display(data.get('closed_by'))}"))
    if data.get("due_date"):
        rows.append(("Due", str(data["due_date"])))
    rows.append(("Source", str(data.get("web_url", "-"))))

    lines = [f"# {sigil}{data['iid']} {data.get('title', '')}", "", "| | |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    lines += ["", "## Description", "", rewrite(data.get("description")) or "_(empty)_", ""]

    comments = [n for n in notes if not n.get("system")]
    lines += [f"## Comments ({len(comments)})", ""]
    for note in comments:
        lines += [note_header(note), "", rewrite(note.get("body")), ""]
    activity = activity_lines(notes)
    if activity:
        lines += [f"## Activity ({len(activity)})", "", *activity, ""]
    return "\n".join(lines)


def note_header(note):
    header = f"### {display(note.get('author'))} — {date(note.get('created_at'))}"
    pos = note.get("position") or {}
    if pos.get("new_path"):
        header += f" (on `{pos['new_path']}"
        header += f":{pos['new_line']}`)" if pos.get("new_line") else "`)"
    return header


def activity_lines(notes):
    return [
        f"- {date(n.get('created_at'))} @{(n.get('author') or {}).get('username', '?')}: "
        + (n.get("body") or "").strip().replace("\n", " ")
        for n in notes
        if n.get("system")
    ]


class ProjectExporter:
    def __init__(self, source, row, out_root):
        self.source = source
        self.row = row
        self.pid = row["id"]
        self.base = Path(out_root) / row["path_with_namespace"]
        self.attachments = 0
        self.missing_attachments = 0

    def export(self, issues=True, mrs=True):
        """Export everything; returns a one-line summary."""
        parts = [self.export_meta()]
        for kind, enabled in (("issues", issues), ("merge-requests", mrs)):
            if enabled:
                total, written = self.export_kind(kind)
                parts.append(f"{total} {kind} ({written} updated)" if total else f"0 {kind}")
        if self.attachments or self.missing_attachments:
            parts.append(f"{self.attachments} attachments")
        if self.missing_attachments:
            parts.append(f"{self.missing_attachments} attachments MISSING")
        return ", ".join(parts)

    def _list(self, path, params=None):
        try:
            return list(self.source.paginate(path, params))
        except SourceError as e:
            if e.status in (403, 404):  # feature disabled for this project
                return []
            raise

    def export_meta(self):
        labels = self._list(f"/projects/{self.pid}/labels", {"include_ancestor_groups": "true"})
        milestones = self._list(f"/projects/{self.pid}/milestones", {"include_ancestors": "true"})
        meta = self.base.parent / f"{self.base.name}-meta.json"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(
            json.dumps({"labels": labels, "milestones": milestones}, indent=1, sort_keys=True)
        )
        return f"{len(labels)} labels, {len(milestones)} milestones"

    def export_kind(self, kind):
        api_kind = KINDS[kind]
        folder = self.base.parent / f"{self.base.name}-{kind}"
        params = {"scope": "all", "state": "all", "order_by": "created_at", "sort": "asc"}
        items = self._list(f"/projects/{self.pid}/{api_kind}", params)
        index = load_index(folder)
        written = 0
        for data in items:
            iid = str(data["iid"])
            entry = index.get(iid)
            stem = item_stem(data)
            if (
                entry
                and entry.get("updated_at") == data.get("updated_at")
                and entry.get("stem") == stem
                and (folder / f"{stem}.json").exists()
            ):
                continue
            notes = self._list(
                f"/projects/{self.pid}/{api_kind}/{iid}/notes",
                {"sort": "asc", "order_by": "created_at"},
            )
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob(f"{int(iid):04d}-*"):  # title changed: drop old files
                if old.stem != stem:
                    old.unlink()
            markdown = render_markdown(kind, data, notes, lambda t: self.localize(t, folder))
            (folder / f"{stem}.md").write_text(markdown)
            (folder / f"{stem}.json").write_text(
                json.dumps({"item": data, "notes": notes}, indent=1, sort_keys=True)
            )
            index[iid] = {"updated_at": data.get("updated_at"), "stem": stem}
            written += 1
        if items:
            save_index(folder, index)
        return len(items), written

    def localize(self, text, folder):
        """Download all uploads referenced in ``text`` and point the links to the copies."""

        def replace(match):
            secret, filename = match.groups()
            rel = attachment_path(secret, filename)
            local = folder / rel
            if not local.exists():
                content = self.download(secret, filename)
                if content is None:
                    self.missing_attachments += 1
                    return match.group(0)
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(content)
                self.attachments += 1
            return rel

        return UPLOAD_RE.sub(replace, text or "")

    def download(self, secret, filename):
        candidates = [f"{self.source.api}/projects/{self.pid}/uploads/{secret}/{filename}"]
        if self.row.get("web_url"):
            candidates.append(f"{self.row['web_url']}/uploads/{secret}/{filename}")
        for url in candidates:
            content = self.source.download(url)
            if content is not None:
                return content
        return None


def load_index(folder):
    try:
        return json.loads((folder / INDEX_FILE).read_text())
    except OSError, ValueError:
        return {}


def save_index(folder, index):
    (folder / INDEX_FILE).write_text(json.dumps(index, indent=1, sort_keys=True))

"""Recreate exported labels, milestones, issues and merge requests on the target project.

- Labels and milestones are created first (matched by name/title, existing ones are reused).
- Issues keep their number and creation date if the token's user is an admin or owner of the
  target project; otherwise numbering gaps are filled with closed placeholder issues, so
  ``#42`` still points to the right issue. The original author and date are noted at the top
  of every issue and comment, because everything is created by the token's user.
- Merge requests cannot be recreated faithfully (their branches are often merged or gone),
  so each one becomes a wiki page under ``merge-requests/`` plus an index page.
- Attachments are uploaded to the target project and the links rewritten.
- ``@mentions`` are turned into code (`` `@name` ``) so nobody is notified about old issues.

Progress is stored in ``<project>-import-state.json`` next to the export, so an interrupted
run continues where it stopped and a re-run never creates duplicates.
"""

import json
import re
from pathlib import Path

from ratatoskr.exporter import (
    UPLOAD_RE,
    activity_lines,
    attachment_path,
    date,
    display,
    item_stem,
    render_markdown,
    safe_filename,
)

MENTION_RE = re.compile(r"(?<![\w`/.@])@([A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?)")
DEFAULT_LABEL_COLOR = "#6699cc"
STATE_KEYS = ("labels", "milestones", "issues", "notes", "uploads", "closed", "mr_pages")


def neutralize_mentions(text):
    return MENTION_RE.sub(r"`@\1`", text or "")


def load_items(folder):
    items = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        data = json.loads(path.read_text())
        if "item" in data:
            items.append(data)
    return sorted(items, key=lambda d: int(d["item"]["iid"]))


class ProjectImporter:
    def __init__(self, api, project, export_base: Path, keep_mentions=False, dry_run=False):
        """``export_base`` is ``<mirror dir>/<source path_with_namespace>``."""
        self.api = api
        self.project = project
        self.pid = project["id"] if project else None
        self.export_base = Path(export_base)
        self.keep_mentions = keep_mentions
        self.dry_run = dry_run
        self.state_path = self.sibling("import-state.json")
        try:
            self.all_state = json.loads(self.state_path.read_text())
        except OSError, ValueError:
            self.all_state = {}
        self.state = self.all_state.setdefault(f"{api.api}#{self.pid}", {})
        for key in STATE_KEYS:
            self.state.setdefault(key, {})

    def sibling(self, suffix):
        return self.export_base.parent / f"{self.export_base.name}-{suffix}"

    def has_export(self):
        return any(self.sibling(s).exists() for s in ("meta.json", "issues", "merge-requests"))

    def save(self):
        self.state_path.write_text(json.dumps(self.all_state, indent=1, sort_keys=True))

    # ---------------------------------------------------------------- text

    def rewrite(self, text, folder):
        """Upload referenced attachments to the target and neutralize mentions."""

        def replace(match):
            secret, filename = match.groups()
            key = f"{secret}/{filename}"
            if key not in self.state["uploads"]:
                local = folder / attachment_path(secret, filename)
                if not local.exists():
                    return match.group(0)
                url = self.api.upload(self.pid, safe_filename(filename), local.read_bytes())
                self.state["uploads"][key] = url
                self.save()
            return self.state["uploads"][key]

        text = UPLOAD_RE.sub(replace, text or "")
        return text if self.keep_mentions else neutralize_mentions(text)

    # ---------------------------------------------------------------- run

    def run(self, issues=True, mrs=True):
        meta = {}
        if self.sibling("meta.json").exists():
            meta = json.loads(self.sibling("meta.json").read_text())
        issue_items = load_items(self.sibling("issues")) if issues else []
        mr_items = load_items(self.sibling("merge-requests")) if mrs else []
        if self.dry_run:
            return (
                f"would import {len(meta.get('labels', []))} labels, "
                f"{len(meta.get('milestones', []))} milestones, {len(issue_items)} issues, "
                f"{len(mr_items)} merge requests as wiki pages"
            )
        labels = self.import_labels(meta.get("labels", []), issue_items)
        milestones = self.import_milestones(meta.get("milestones", []), issue_items)
        created, comments = self.import_issues(issue_items)
        pages = self.import_merge_requests(mr_items)
        return (
            f"{labels} labels, {milestones} milestones, {created} issues ({comments} comments), "
            f"{pages} merge request pages created"
        )

    def import_labels(self, labels, issue_items):
        wanted = {label["name"]: label for label in labels}
        for entry in issue_items:
            for name in entry["item"].get("labels") or []:
                wanted.setdefault(name, {"name": name})
        existing = {
            label["name"]
            for label in self.api.paginate(
                f"/projects/{self.pid}/labels", {"include_ancestor_groups": "true"}
            )
        }
        created = 0
        for name, label in wanted.items():
            if name in existing or name in self.state["labels"]:
                continue
            self.api.write(
                "POST",
                f"/projects/{self.pid}/labels",
                {
                    "name": name,
                    "color": label.get("color") or DEFAULT_LABEL_COLOR,
                    "description": label.get("description") or "",
                },
            )
            self.state["labels"][name] = True
            self.save()
            created += 1
        return created

    def import_milestones(self, milestones, issue_items):
        wanted = {m["title"]: m for m in milestones}
        for entry in issue_items:
            milestone = entry["item"].get("milestone")
            if milestone:
                wanted.setdefault(milestone["title"], milestone)
        existing = {
            m["title"]: m["id"]
            for m in self.api.paginate(
                f"/projects/{self.pid}/milestones", {"include_ancestors": "true"}
            )
        }
        created = 0
        for title, milestone in wanted.items():
            if title in existing:
                self.state["milestones"].setdefault(title, existing[title])
                continue
            if title in self.state["milestones"]:
                continue
            fields = {k: milestone.get(k) for k in ("description", "due_date", "start_date")}
            new = self.api.write(
                "POST",
                f"/projects/{self.pid}/milestones",
                {"title": title, **{k: v for k, v in fields.items() if v}},
            )
            if milestone.get("state") == "closed":
                self.api.write(
                    "PUT", f"/projects/{self.pid}/milestones/{new['id']}", {"state_event": "close"}
                )
            self.state["milestones"][title] = new["id"]
            self.save()
            created += 1
        return created

    def import_issues(self, issue_items):
        if not issue_items:
            return 0, 0
        folder = self.sibling("issues")
        keep_ids = self.api.can_keep_ids_and_dates(self.pid)
        next_iid = None if keep_ids else self.next_target_iid()
        warned = False
        created = comments = 0
        for entry in issue_items:
            item, src_iid = entry["item"], int(entry["item"]["iid"])
            target_iid = self.state["issues"].get(str(src_iid))
            if target_iid is None:
                while next_iid is not None and next_iid < src_iid:
                    self.create_placeholder(next_iid)
                    next_iid += 1
                target_iid = self.create_issue(item, entry["notes"], folder, keep_ids)
                created += 1
                if target_iid != src_iid and not warned:
                    print(f"   note: issue numbers differ (#{src_iid} -> #{target_iid})")
                    warned = True
                if next_iid is not None:
                    next_iid = target_iid + 1
            comments += self.import_notes(entry["notes"], target_iid, folder, keep_ids)
            if item.get("state") == "closed" and str(src_iid) not in self.state["closed"]:
                self.api.write(
                    "PUT", f"/projects/{self.pid}/issues/{target_iid}", {"state_event": "close"}
                )
                self.state["closed"][str(src_iid)] = True
                self.save()
        return created, comments

    def next_target_iid(self):
        latest = next(
            iter(
                self.api.paginate(
                    f"/projects/{self.pid}/issues",
                    {"scope": "all", "order_by": "created_at", "sort": "desc"},
                )
            ),
            None,
        )
        known = [int(v) for v in self.state["issues"].values()]
        return max([latest["iid"] if latest else 0, *known]) + 1

    def create_placeholder(self, iid):
        issue = self.api.write(
            "POST",
            f"/projects/{self.pid}/issues",
            {
                "title": f"Placeholder for missing issue #{iid}",
                "description": "This issue did not exist (anymore) on the source. "
                "It only keeps the issue numbers aligned with the source project.",
                "confidential": "true",
            },
        )
        self.api.write(
            "PUT", f"/projects/{self.pid}/issues/{issue['iid']}", {"state_event": "close"}
        )
        self.state["issues"][str(iid)] = issue["iid"]
        self.state["closed"][str(iid)] = True
        self.save()

    def create_issue(self, item, notes, folder, keep_ids):
        header = [f"> Migrated from {item.get('web_url', '?')}"]
        header.append(
            f"> Created by {display(item.get('author'))} on {date(item.get('created_at'))}"
        )
        if item.get("assignees"):
            header.append(f"> Assignees: {', '.join(display(a) for a in item['assignees'])}")
        if item.get("closed_at"):
            header.append(
                f"> Closed on {date(item['closed_at'])} by {display(item.get('closed_by'))}"
            )
        description = "  \n".join(header) + "\n\n" + (item.get("description") or "")
        activity = activity_lines(notes)
        if activity:
            description += (
                f"\n\n<details><summary>Activity on the source ({len(activity)})</summary>\n\n"
                + "\n".join(activity)
                + "\n\n</details>"
            )
        data = {
            "title": item["title"],
            "description": self.rewrite(description, folder),
            "labels": ",".join(item.get("labels") or []),
            "confidential": str(bool(item.get("confidential"))).lower(),
            "created_at": item.get("created_at"),
        }
        if keep_ids:
            data["iid"] = item["iid"]
        milestone = item.get("milestone")
        if milestone and milestone["title"] in self.state["milestones"]:
            data["milestone_id"] = self.state["milestones"][milestone["title"]]
        for key in ("due_date", "weight"):
            if item.get(key) is not None:
                data[key] = item[key]
        issue = self.api.write("POST", f"/projects/{self.pid}/issues", data)
        self.state["issues"][str(item["iid"])] = issue["iid"]
        self.save()
        return issue["iid"]

    def import_notes(self, notes, target_iid, folder, keep_dates):
        created = 0
        for note in notes:
            key = str(note["id"])
            if note.get("system") or key in self.state["notes"]:
                continue
            body = (
                f"> {display(note.get('author'))} commented on {date(note.get('created_at'))}\n\n"
                + (note.get("body") or "")
            )
            data = {"body": self.rewrite(body, folder)}
            if keep_dates:
                data["created_at"] = note.get("created_at")
            self.api.write("POST", f"/projects/{self.pid}/issues/{target_iid}/notes", data)
            self.state["notes"][key] = True
            self.save()
            created += 1
        return created

    def import_merge_requests(self, mr_items):
        if not mr_items:
            return 0
        folder = self.sibling("merge-requests")
        created = 0
        for entry in mr_items:
            item = entry["item"]
            if str(item["iid"]) in self.state["mr_pages"]:
                continue
            content = render_markdown(
                "merge-requests", item, entry["notes"], lambda t: self.rewrite(t, folder)
            )
            title = f"merge-requests/{item_stem(item)}"
            self.api.write(
                "POST",
                f"/projects/{self.pid}/wikis",
                {"title": title, "content": self.rewrite(content, folder), "format": "markdown"},
            )
            self.state["mr_pages"][str(item["iid"])] = title
            self.save()
            created += 1

        rows = []
        for entry in mr_items:
            item = entry["item"]
            page = self.state["mr_pages"][str(item["iid"])]
            rows.append(
                f"| !{item['iid']} | [{item['title']}]({page}) | {item.get('state')} "
                f"| {display(item.get('author'))} |"
            )
        index = self.rewrite(
            "# Merge requests\n\nArchived from the source project.\n\n"
            "| | Title | State | Author |\n|---|---|---|---|\n" + "\n".join(rows) + "\n",
            folder,
        )
        data = {"title": "merge-requests", "content": index, "format": "markdown"}
        if self.state["mr_pages"].get("index"):
            self.api.write("PUT", f"/projects/{self.pid}/wikis/merge-requests", data)
        else:
            self.api.write("POST", f"/projects/{self.pid}/wikis", data)
            self.state["mr_pages"]["index"] = "merge-requests"
            self.save()
        return created

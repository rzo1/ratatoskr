"""Ratatoskr - carry your repos from an SSO-protected GitLab to another GitLab.

Modes:
  ratatoskr list       log in via SSO in a browser window, write repos.{json,csv,txt}
  ratatoskr checkout   step 1 (alias: download): same listing (or --from-csv), then clone
                      every repo (all branches, tags, LFS objects, wiki) via SSH or HTTPS
                      and export issues, merge requests, labels and milestones
  ratatoskr push       step 2 (alias: migrate): create the projects (and subgroups) under a
                      group on the target GitLab, push everything there and recreate
                      issues & co. via API - works offline from the downloaded data

The source is read with the session of a real browser window you log in to, so no
access token is needed there. The browser profile is kept in .browser-profile/, so
re-runs usually skip the login.
"""

import argparse
import getpass
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from pathlib import Path

from ratatoskr import progress
from ratatoskr.excludes import Excludes
from ratatoskr.exporter import ProjectExporter
from ratatoskr.gitops import (
    askpass_env,
    clone_one,
    clone_wiki,
    lfs_fetch,
    lfs_push,
    local_repo,
    prune_refs,
    push_refs,
    push_wiki,
    ssh_env,
    verify_refs,
    wiki_url,
)
from ratatoskr.importer import ProjectImporter
from ratatoskr.source import (
    SourceSession,
    list_projects,
    read_csv_selection,
    with_scheme,
    write_outputs,
)
from ratatoskr.target import TargetApi, enc


def ask_protocol(question, default=None):
    suffix = f" [{default[0]}]" if default else ""
    while True:
        answer = input(f"{question}{suffix} ").strip().lower() or (default or "")
        if answer in ("s", "ssh"):
            return "ssh"
        if answer in ("h", "http", "https"):
            return "https"


def https_env(username):
    user = input(f"HTTPS username [{username or ''}]: ").strip() or username
    print(
        "SSO accounts need a personal access token (read_repository scope), not the SSO password."
    )
    return askpass_env(user, getpass.getpass("Token: "))


def last_line(text):
    return (text.strip().splitlines() or ["(no output)"])[-1]


def open_source(args):
    return SourceSession(
        args.source_url, args.profile_dir, args.login_timeout, args.source_timeout, args.retries
    )


# ---------------------------------------------------------------- checkout


def checkout_one(r, url_key, args, env, stages=None):
    """Clone/update one repo with LFS objects and wiki; returns (ok, message).

    The current step is kept in ``stages[path]`` while running, for the progress line.
    """
    path = r["path_with_namespace"]
    stages = {} if stages is None else stages
    try:
        return _checkout_one(r, path, url_key, args, env, stages)
    finally:
        stages.pop(path, None)


def _checkout_one(r, path, url_key, args, env, stages):
    root = Path(args.dest)
    dest = root / (path if args.working_copy else f"{path}.git")
    stages[path] = "fetching" if dest.exists() else "cloning"
    action, res = clone_one(r[url_key], dest, args.working_copy, env)
    if res.returncode != 0:
        return False, f"FAILED   {path}\n    {last_line(res.stderr)}"
    extras = []
    try:
        if args.lfs:
            stages[path] = "LFS"
            count = lfs_fetch(dest, env)
            if count is None:
                extras.append("LFS skipped: git-lfs not installed")
            elif count:
                extras.append(f"{count} LFS objects")
        if args.wiki:
            stages[path] = "wiki"
        if args.wiki and clone_wiki(r[url_key], root / f"{path}.wiki.git", env) != "none":
            extras.append("wiki")
    except RuntimeError as e:
        return False, f"FAILED   {path}\n    {e}"
    return True, f"{action:8} {path}" + (f" ({', '.join(extras)})" if extras else "")


def clone_all(repos, username, args):
    protocol = args.protocol or ask_protocol("Clone via [s]sh or [h]ttps?")
    env, askpass = https_env(username) if protocol == "https" else (ssh_env(), None)
    url_key = "ssh_url_to_repo" if protocol == "ssh" else "http_url_to_repo"
    print(
        f"\nCloning {len(repos)} repos via {protocol} into {args.dest}/ "
        f"({'working copies' if args.working_copy else 'bare mirrors'}, {args.jobs} parallel)\n"
    )
    failed = []
    stages = {}  # path -> current step of the running clones
    started = time.monotonic()
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(checkout_one, r, url_key, args, env, stages): r for r in repos}
            pending = set(futures)
            while pending:
                finished, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                for fut in finished:
                    done += 1
                    ok, message = fut.result()
                    progress.clear()
                    print(f"[{done}/{len(repos)}] {message}")
                    if not ok:
                        failed.append(futures[fut]["path_with_namespace"])
                if pending:
                    progress.update(clone_status(done, len(repos), stages, started), transient=True)
    finally:
        progress.clear()
        if askpass:
            os.unlink(askpass)
    return failed


def clone_status(done, total, stages, started):
    elapsed = int(time.monotonic() - started)
    running = ", ".join(f"{path} ({step})" for path, step in list(stages.items()))
    return f"  {done}/{total} done, {elapsed // 60}:{elapsed % 60:02d} elapsed" + (
        f" - {running}" if running else ""
    )


def export_all(source, repos, args):
    what = " and ".join(
        k for k, on in (("issues", args.issues), ("merge requests", args.mrs)) if on
    )
    print(f"\nExporting labels, milestones, {what} into {args.dest}/\n")
    failed = []
    for i, r in enumerate(repos, 1):
        path = r["path_with_namespace"]
        label = f"[{i}/{len(repos)}] {path}"
        try:
            summary = ProjectExporter(source, r, args.dest, label).export(args.issues, args.mrs)
            progress.clear()
            print(f"{label}: {summary}")
        except Exception as e:  # keep going, report at the end
            progress.clear()
            print(f"[{i}/{len(repos)}] FAILED   {path}\n    {e}")
            failed.append(path)
    return failed


def checkout(args):
    export = args.issues or args.mrs
    excludes = Excludes.from_args(args)
    if args.from_csv and not export:
        session = nullcontext(None)
    else:
        session = open_source(args)
    with session as source:
        if args.from_csv:
            repos = read_csv_selection(args.from_csv)
            print(f"{len(repos)} repos selected in {args.from_csv}")
        else:
            repos = list_projects(source, args)
            write_outputs(repos, args.out_dir, excludes)
        repos = excludes.filter(repos)
        if not repos:
            print("Nothing to clone.")
            return 0
        failed = clone_all(repos, source.user["username"] if source else None, args)
        if export:
            failed += [p for p in export_all(source, repos, args) if p not in failed]

    print(f"\nDone. {len(repos) - len(failed)} ok, {len(failed)} failed.")
    for path in failed:
        print("  failed:", path)
    return 1 if failed else 0


# ---------------------------------------------------------------- push


def ask_layout(repos, root_path):
    """Ask how many leading source group levels to drop below the target group."""
    namespaces = sorted(
        {
            r["path_with_namespace"].rpartition("/")[0]
            for r in repos
            if not (r.get("target_path") or "").strip()
        }
    )
    if not namespaces:
        return 0, False
    print("Source groups of the selected repos:")
    for ns in namespaces[:15]:
        print(f"  {ns}")
    if len(namespaces) > 15:
        print(f"  ... and {len(namespaces) - 15} more")
    samples = [namespaces[0], namespaces[-1]] if len(namespaces) > 1 else namespaces
    min_depth = min(len(ns.split("/")) for ns in namespaces)

    print(f"\nWhich part of the source group path should be stripped below {root_path}?")
    for n in range(min_depth + 1):
        mapped = ", ".join(
            f"{root_path}/{'/'.join([*ns.split('/')[n:], '<repo>'])}" for ns in samples
        )
        print(f"  [{n}] strip {n} level{'s' if n != 1 else ''}: {mapped}")
    print(f"  [f] flatten: {root_path}/<repo>")
    while True:
        answer = input("Choice [0]: ").strip().lower() or "0"
        if answer == "f":
            return 0, True
        if answer.isdigit() and int(answer) <= min_depth:
            return int(answer), False


def confirm_plan(repos, root_path, args):
    targets = [f"{root_path}/{target_rel_path(r, args)}" for r in repos]
    groups = sorted({t.rpartition("/")[0] for t in targets} - {root_path})
    print(
        f"\nPlan: {len(targets)} projects below {root_path}, using {len(groups)} subgroups "
        f"(missing ones are created):"
    )
    for g in groups[:20]:
        print(f"  {g}/")
    if len(groups) > 20:
        print(f"  ... and {len(groups) - 20} more")
    dupes = sorted({t for t in targets if targets.count(t) > 1})
    if dupes:
        sys.exit(
            "Target path collisions (use another strip level or target_path in the CSV):\n  "
            + "\n  ".join(dupes)
        )
    if args.dry_run or args.yes:
        return
    if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        sys.exit("Aborted.")


def target_rel_path(r, args):
    if (r.get("target_path") or "").strip():
        return r["target_path"].strip().strip("/")
    parts = r["path_with_namespace"].split("/")
    if args.flatten:
        return parts[-1]
    return "/".join(parts[min(args.strip, len(parts) - 1) :])


def ensure_project(api, r, full, args):
    """Return (project, existed) - the project is None only in a dry run."""
    namespace, _, name = full.rpartition("/")
    namespace_id = api.ensure_group(namespace, args.visibility)
    project = None if namespace_id is None else api.get(f"/projects/{enc(full)}")
    if project:
        return project, True
    if args.dry_run:
        print("   would create project")
        return None, False
    project = api.write(
        "POST",
        "/projects",
        {
            "name": r.get("name") or name,
            "path": name,
            "namespace_id": namespace_id,
            "visibility": args.visibility,
            "description": r.get("description") or "",
        },
    )
    print("   created project")
    return project, False


def push_code(api, project, r, repo_dir, working_copy, url, args, env):
    if args.lfs:
        # before the git push: GitLab rejects pushes whose LFS objects it does not have
        count = lfs_push(url, repo_dir, env)
        if count:
            print(f"   pushed {count} LFS objects")
    refs = push_refs(url, repo_dir, working_copy, env)
    n_branches = sum(ref.startswith("refs/heads/") for ref in refs)
    print(f"   pushed {n_branches} branches, {len(refs) - n_branches} tags")

    branch = r.get("default_branch")
    if branch and project.get("default_branch") != branch:
        api.write("PUT", f"/projects/{project['id']}", {"default_branch": branch})
        print(f"   default branch set to {branch}")
    if args.prune:
        # after the default branch switch, so a stale old default branch can be deleted
        for ref in prune_refs(url, repo_dir, refs, env):
            print(f"   pruned {ref}")
    verify_refs(url, repo_dir, refs, env)
    print("   verified: all branches and tags match")


def push_one(api, root, r, args, env):
    src = r["path_with_namespace"]
    full = f"{root['full_path']}/{target_rel_path(r, args)}"
    print(f"{src} -> {full}")

    repo_dir, working_copy = local_repo(args.mirror_dir, src)
    empty_source = str(r.get("empty_repo", "")).lower() == "true"
    if not repo_dir and not empty_source:
        raise RuntimeError(
            f"no local clone under {args.mirror_dir}/ - run 'ratatoskr checkout' first"
        )

    project, existed = ensure_project(api, r, full, args)
    skip_code = existed and not project["empty_repo"] and not args.update_existing
    url = None
    if project:
        url = project["ssh_url_to_repo"] if args.protocol == "ssh" else project["http_url_to_repo"]

    if skip_code:
        print("   code: target is not empty - skipped (use --update-existing to force-push)")
    elif empty_source:
        print("   source repo is empty - nothing to push")
    elif args.dry_run:
        print(f"   would push {repo_dir}")
    else:
        push_code(api, project, r, repo_dir, working_copy, url, args, env)

    wiki_dir = Path(args.mirror_dir) / f"{src}.wiki.git"
    if args.wiki and wiki_dir.exists():
        if args.dry_run:
            print("   would push wiki")
        else:
            push_wiki(wiki_url(url), wiki_dir, env)
            print("   pushed wiki")

    if args.issues or args.mrs:
        importer = ProjectImporter(
            api, project, Path(args.mirror_dir) / src, args.keep_mentions, args.dry_run
        )
        if importer.has_export():
            print(f"   {importer.run(args.issues, args.mrs)}")

    # last: an archived project is read-only
    archived = str(r.get("archived", "")).lower() == "true"
    if args.archive and archived and project and not project.get("archived") and not args.dry_run:
        api.write("POST", f"/projects/{project['id']}/archive", {})
        print("   archived (as on source)")
    return "skipped" if skip_code else "ok"


def push(args):
    repos = Excludes.from_args(args).filter(read_csv_selection(args.from_csv))
    base = with_scheme(args.target_url)
    print(f"{len(repos)} repos selected in {args.from_csv}, target {base}")

    token = os.environ.get("RATATOSKR_TARGET_TOKEN") or getpass.getpass(
        f"Personal access token for {base} (scope 'api'): "
    )
    api = TargetApi(base, token, args.dry_run)
    user = api.check_token()
    root = api.root_group(args.group)
    print(
        f"Authenticated as {user['username']}, target group {root['full_path']} (id {root['id']})"
        f"{' - DRY RUN, nothing is changed' if args.dry_run else ''}\n"
    )

    if args.strip is None and not args.flatten:
        args.strip, args.flatten = ask_layout(repos, root["full_path"])
    confirm_plan(repos, root["full_path"], args)

    if not args.protocol:
        args.protocol = (
            "https" if args.dry_run else ask_protocol("Push via [h]ttps (PAT) or [s]sh?", "https")
        )
    # HTTPS git and LFS authenticate with the same PAT; GitLab accepts any username with it
    env, askpass = askpass_env("oauth2", token) if args.protocol == "https" else (ssh_env(), None)

    results = {"ok": [], "skipped": [], "failed": []}
    try:
        for i, r in enumerate(repos, 1):
            print(f"[{i}/{len(repos)}] ", end="")
            try:
                results[push_one(api, root, r, args, env)].append(r["path_with_namespace"])
            except Exception as e:  # keep going, report at the end
                print(f"   FAILED: {e}")
                results["failed"].append(r["path_with_namespace"])
    finally:
        if askpass:
            os.unlink(askpass)

    print(
        f"\nDone. {len(results['ok'])} ok, {len(results['skipped'])} skipped, "
        f"{len(results['failed'])} failed."
    )
    for path in results["failed"]:
        print("  failed:", path)
    return 1 if results["failed"] else 0


# ---------------------------------------------------------------- CLI


def add_exclude_args(p):
    p.add_argument(
        "--exclude",
        action="append",
        metavar="PATTERN",
        help="skip projects matching this path pattern, e.g. 'fhg-intern' or 'fhg/**/test*' "
        "(repeatable)",
    )
    p.add_argument(
        "--exclude-file",
        action="append",
        metavar="FILE",
        help="file with one exclude pattern or clone URL per line, # for comments (repeatable)",
    )


def add_list_args(p):
    p.add_argument(
        "--source-url",
        default=os.environ.get("RATATOSKR_SOURCE_URL"),
        help="source GitLab, e.g. https://gitlab.example.org (env: RATATOSKR_SOURCE_URL)",
    )
    p.add_argument(
        "--scope",
        choices=["membership", "owned", "all"],
        default="membership",
        help="membership = projects you are a member of (default), "
        "owned = only your personal namespace, "
        "all = everything reachable by your account (incl. public/internal)",
    )
    p.add_argument("--include-archived", action="store_true")
    p.add_argument(
        "--visibility",
        nargs="+",
        choices=["private", "internal", "public"],
        default=["private"],
        help="project visibilities to keep (default: private only, "
        "e.g. --visibility private internal public for everything)",
    )
    p.add_argument("--login-timeout", type=int, default=600, help="seconds to wait for SSO login")
    p.add_argument(
        "--source-timeout",
        type=int,
        default=120,
        metavar="SECONDS",
        help="timeout per request to the (slow) source (default: 120)",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=5,
        help="retries for timeouts, 429 and 5xx answers of the source, "
        "with growing pauses (default: 5)",
    )
    p.add_argument("--profile-dir", default=".browser-profile")
    p.add_argument("--out-dir", default=".", help="where repos.{json,csv,txt} are written")
    add_exclude_args(p)


def add_content_args(p, verb):
    on = argparse.BooleanOptionalAction
    p.add_argument("--lfs", action=on, default=True, help=f"{verb} Git LFS objects (default: on)")
    p.add_argument("--wiki", action=on, default=True, help=f"{verb} the wiki (default: on)")
    p.add_argument(
        "--issues", action=on, default=True, help=f"{verb} issues and comments (default: on)"
    )
    p.add_argument(
        "--mrs",
        action=on,
        default=True,
        help=f"{verb} merge requests"
        + (" as wiki pages" if verb == "import" else "")
        + " (default: on)",
    )


def build_parser():
    ap = argparse.ArgumentParser(
        prog="ratatoskr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="mode", required=True)

    p_list = sub.add_parser("list", help="log in and write the repo list only")
    add_list_args(p_list)

    p_co = sub.add_parser(
        "checkout",
        aliases=["download"],
        help="step 1: download everything from the source (git, LFS, wiki, issues, MRs)",
    )
    add_list_args(p_co)
    p_co.add_argument(
        "--from-csv",
        metavar="CSV",
        help="use the rows with migrate=yes from this CSV instead of listing again",
    )
    p_co.add_argument("--protocol", choices=["ssh", "https"], help="skip the interactive question")
    p_co.add_argument("--dest", default="mirrors", help="clone target directory (default: mirrors)")
    p_co.add_argument(
        "--working-copy",
        action="store_true",
        help="normal clones with a checked-out branch instead of bare mirrors",
    )
    p_co.add_argument("-j", "--jobs", type=int, default=4, help="parallel clones (default: 4)")
    add_content_args(p_co, "fetch")

    p_push = sub.add_parser(
        "push",
        aliases=["migrate"],
        help="step 2: migrate the downloaded data to the target (no source access needed)",
    )
    p_push.add_argument(
        "--target-url",
        default=os.environ.get("RATATOSKR_TARGET_URL"),
        help="target GitLab, e.g. https://gitlab.example.org (env: RATATOSKR_TARGET_URL)",
    )
    p_push.add_argument(
        "--group", required=True, help="target group: numeric id or full path (e.g. dept/team)"
    )
    p_push.add_argument(
        "--from-csv",
        default="repos.csv",
        metavar="CSV",
        help="selection to push: rows with migrate=yes (default: repos.csv); "
        "target_path, if set, is relative to --group",
    )
    p_push.add_argument(
        "--mirror-dir",
        default="mirrors",
        help="where 'ratatoskr checkout' cloned to (default: mirrors)",
    )
    layout = p_push.add_mutually_exclusive_group()
    layout.add_argument(
        "--strip",
        type=int,
        metavar="N",
        help="drop the first N source group levels below --group "
        "(asked interactively if neither --strip nor --flatten is given)",
    )
    layout.add_argument(
        "--flatten", action="store_true", help="put all projects directly into --group"
    )
    p_push.add_argument(
        "--visibility",
        choices=["private", "internal", "public"],
        default="private",
        help="visibility of created projects and subgroups (default: private)",
    )
    p_push.add_argument(
        "--protocol",
        choices=["ssh", "https"],
        help="push via HTTPS with the PAT or via SSH key (asked if omitted, default https)",
    )
    p_push.add_argument(
        "--update-existing",
        action="store_true",
        help="also force-push into target projects that already have content (re-sync)",
    )
    p_push.add_argument(
        "--prune",
        action="store_true",
        help="delete branches/tags on the target that no longer exist in the local clone "
        "(exact mirror on re-sync)",
    )
    add_content_args(p_push, "import")
    add_exclude_args(p_push)
    p_push.add_argument(
        "--keep-mentions",
        action="store_true",
        help="keep @mentions in imported issues as they are (they notify people on the target)",
    )
    p_push.add_argument(
        "--archive", action="store_true", help="archive targets whose source is archived"
    )
    p_push.add_argument(
        "--dry-run", action="store_true", help="only show what would be created/pushed"
    )
    p_push.add_argument(
        "-y", "--yes", action="store_true", help="don't ask for confirmation of the plan"
    )
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()
    args.mode = {"download": "checkout", "migrate": "push"}.get(args.mode, args.mode)

    if args.mode == "push":
        if not args.target_url:
            ap.error("--target-url (or RATATOSKR_TARGET_URL) is required")
        return push(args)

    needs_browser = args.mode == "list" or not args.from_csv or args.issues or args.mrs
    if needs_browser and not args.source_url:
        ap.error("--source-url (or RATATOSKR_SOURCE_URL) is required")

    if args.mode == "list":
        with open_source(args) as source:
            write_outputs(list_projects(source, args), args.out_dir, Excludes.from_args(args))
        print(
            "Edit repos.csv: set migrate=no to skip, fill target_path to override the destination."
        )
        return 0
    return checkout(args)


if __name__ == "__main__":
    sys.exit(main())

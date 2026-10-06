# Ratatoskr

[![CI](https://github.com/rzo1/ratatoskr/actions/workflows/ci.yml/badge.svg)](https://github.com/rzo1/ratatoskr/actions/workflows/ci.yml)

<img src="docs/logo.png" alt="Ratatoskr logo" width="200" align="right">

> *Ratatoskr, the squirrel who runs up and down Yggdrasil, carrying messages
> between the eagle at the top and the dragon at the roots.*

Move your projects from one GitLab to another — even when the source only lets
you in through a **single sign-on (SSO) login**, is **read-only**, and neither
API tokens nor GitLab's own project export are available. Ratatoskr opens a
real browser for you to log in and reads everything through that session:

- **git**: all branches and tags, Git LFS objects, the wiki
- **issues** with comments and attachments, **labels**, **milestones**
- **merge requests** with comments (archived as wiki pages on the target)

It then recreates the projects — including the subgroup structure — on the
target GitLab via API, pushes everything and verifies every branch and tag.

Typical use case: a read-only, SSO-protected GitLab CE that is being shut down
("GitLab is undergoing maintenance"), and a GitLab EE where you have write
access and a personal access token (PAT).

Sibling of [Freki](https://github.com/rzo1/freki) (back everything up) and
[Geri](https://github.com/rzo1/geri) (clean up what you no longer need); the
local export uses Freki's format.

## How it works

```
 source GitLab (SSO, read-only)        local                         target GitLab
┌───────────────────────┐  list   ┌────────────────────┐  push   ┌──────────────────────────┐
│ browser login         │ ──────▶ │ repos.csv (edit!)  │ ──────▶ │ group/subgroup/project   │
│ REST API via session  │         │                    │         │  (created via API)       │
│                       │checkout │ mirrors/grp/       │         │  branches, tags, LFS     │
│ git over ssh/https    │ ──────▶ │  x.git  x.wiki.git │         │  wiki                    │
│ issues, MRs, uploads  │         │  x-issues/  x-merge-requests/│  labels, milestones      │
│                       │         │  x-meta.json       │         │  issues + comments       │
└───────────────────────┘         └────────────────────┘         │  MRs as wiki pages       │
                                                                 └──────────────────────────┘
```

1. **`list`** — opens Chromium, you log in via SSO, the projects are read from
   the REST API with the browser session and written to `repos.csv`,
   `repos.json` and `repos.txt`.
2. **`checkout`** — does the same listing (or reads your edited `repos.csv`),
   asks for **SSH or HTTPS**, clones every repo as a bare mirror with its LFS
   objects and wiki, and exports issues, merge requests, labels, milestones
   and attachments.
3. **`push`** — asks for a **PAT** for the target and which part of the source
   group path to strip, creates missing subgroups and projects, pushes code,
   LFS and wiki, verifies all refs, and recreates labels, milestones, issues
   and merge requests.

The two big steps are independent: **`checkout`** (alias **`download`**) needs
only the source, **`push`** (alias **`migrate`**) works purely from the local
data and needs only the target. Download everything first, take your time to
check it, and migrate later — even after the source has been switched off.

## Requirements

- Python >= 3.14 and [uv](https://docs.astral.sh/uv/)
- `git` on `PATH`, and `git-lfs` if your repos use LFS
- Source: an account you can log in with in a browser, plus an SSH key
  registered there (or a PAT with `read_repository` for HTTPS clones)
- Target: a PAT with the `api` scope and permission to create subgroups and
  projects in the target group. To keep issue numbers and dates, you need to
  be **owner** of the target group (or admin)

## Installation

```bash
git clone https://github.com/rzo1/ratatoskr.git
cd ratatoskr
uv sync
uv run playwright install chromium   # the browser used for the SSO login
uv run ratatoskr --help
```

Also installed under the short alias `glm` (*GitLab migrate*).

## Configuration

URLs and the target token can be given as flags or environment variables:

| Variable                 | Flag            | Description                                    |
|--------------------------|-----------------|------------------------------------------------|
| `RATATOSKR_SOURCE_URL`   | `--source-url`  | Source GitLab (the one behind SSO)             |
| `RATATOSKR_TARGET_URL`   | `--target-url`  | Target GitLab                                  |
| `RATATOSKR_TARGET_TOKEN` | *(prompted)*    | PAT for the target; asked for if not set       |

The scheme is optional: `gitlab.example.org` means `https://gitlab.example.org`.

## Usage

### 1. List

```bash
ratatoskr list --source-url https://gitlab.source.example.org
```

A browser window opens at the sign-in page. Log in as usual; Ratatoskr detects
the login by itself and starts reading the projects. The browser profile is
kept in `.browser-profile/`, so later runs usually skip the login.

| Option                       | Description                                                                      |
|------------------------------|----------------------------------------------------------------------------------|
| `--scope membership`         | Projects you are a member of (default)                                           |
| `--scope owned`              | Only projects in your personal namespace                                         |
| `--scope all`                | Everything your account can reach, including public/internal projects            |
| `--visibility V [V ...]`     | Visibilities to keep: `private` (default), `internal`, `public`                  |
| `--include-archived`         | Also list archived projects                                                      |
| `--out-dir DIR`              | Where `repos.{csv,json,txt}` are written (default: current directory)            |
| `--login-timeout SECONDS`    | How long to wait for the SSO login (default: 600)                                |
| `--source-timeout SECONDS`   | Timeout per request to the source (default: 120)                                 |
| `--retries N`                | Retries for timeouts, 429 and 5xx answers of the source (default: 5)             |
| `--profile-dir DIR`          | Browser profile directory (default: `.browser-profile`)                          |

`repos.csv` is your migration plan. Set `migrate` to `no` for repos you want
to leave behind, and optionally fill `target_path` (relative to the target
group) to rename or move a single project. The `my_access` column shows your
role on each project, which helps when filtering a `--scope all` listing.

### 2. Checkout (download)

```bash
ratatoskr checkout --source-url https://gitlab.source.example.org   # list + clone + export
ratatoskr checkout --from-csv repos.csv                              # your edited selection
ratatoskr checkout --from-csv repos.csv --no-issues --no-mrs         # git only, no browser
```

You are asked whether to clone via **SSH** (your existing key) or **HTTPS**
(username + token, entered hidden). For each repo you get:

```
mirrors/group/subgroup/
├── project.git/                      bare mirror: all branches, tags, LFS objects
├── project.wiki.git/                 wiki mirror (if the project has a wiki)
├── project-issues/
│   ├── 0042-fix-login-bug.md         metadata, description, comments, activity
│   ├── 0042-fix-login-bug.json       raw API data, used by `push`
│   └── attachments/<secret>/<file>   downloaded uploads
├── project-merge-requests/           same structure as issues
└── project-meta.json                 labels and milestones
```

Running the command again fetches new commits instead of cloning from
scratch, and only re-reads issues and merge requests that changed. The export
needs the browser session, so the login window opens unless you pass
`--no-issues --no-mrs`.

| Option                       | Description                                                           |
|------------------------------|-----------------------------------------------------------------------|
| `--from-csv CSV`             | Use the rows with `migrate=yes` instead of listing again              |
| `--protocol ssh\|https`      | Don't ask for the protocol                                            |
| `--dest DIR`                 | Target directory (default: `mirrors`)                                 |
| `--working-copy`             | Normal clones with a checked-out branch instead of bare mirrors       |
| `-j N`, `--jobs N`           | Parallel clones (default: 4)                                          |
| `--no-lfs`                   | Don't fetch Git LFS objects                                           |
| `--no-wiki`                  | Don't clone wikis                                                     |
| `--no-issues`                | Don't export issues                                                   |
| `--no-mrs`                   | Don't export merge requests                                           |

All `list` options are available as well.

### 3. Push (migrate)

```bash
ratatoskr push --target-url https://gitlab.target.example.org --group dept/team --dry-run
ratatoskr push --target-url https://gitlab.target.example.org --group dept/team
```

Ratatoskr asks for the target PAT (unless `RATATOSKR_TARGET_TOKEN` is set),
checks that it has the `api` scope and resolves the target group (numeric id
or full path; a subgroup is fine). It then shows the source groups and asks
how much of their path to strip:

```
Source groups of the selected repos:
  fraunhofer/inst/project-a
  fraunhofer/inst/project-b/tools

Which part of the source group path should be stripped below dept/team?
  [0] strip 0 levels: dept/team/fraunhofer/inst/project-a/<repo>, ...
  [1] strip 1 level: dept/team/inst/project-a/<repo>, ...
  [2] strip 2 levels: dept/team/project-a/<repo>, dept/team/project-b/tools/<repo>
  [3] strip 3 levels: dept/team/<repo>, dept/team/tools/<repo>
  [f] flatten: dept/team/<repo>
Choice [0]:
```

After a summary of the plan (and a check for target path collisions), your
confirmation and the choice of **HTTPS** (with the PAT, default) or **SSH**
for git, each repo is migrated:

1. missing subgroups are created (private by default),
2. the project is created, keeping name and description,
3. LFS objects are uploaded, then all branches and tags are pushed,
4. the default branch is set as on the source,
5. every branch and tag on the target is compared with the local clone,
6. the wiki is pushed,
7. labels and milestones are created, then issues with their comments,
8. merge requests become wiki pages under `merge-requests/`, with an index,
9. with `--archive`, projects archived on the source are archived.

| Option                   | Description                                                                  |
|--------------------------|------------------------------------------------------------------------------|
| `--group GROUP`          | Target group: numeric id or full path (required)                             |
| `--from-csv CSV`         | Selection to push (default: `repos.csv`)                                     |
| `--mirror-dir DIR`       | Where `checkout` wrote to (default: `mirrors`)                               |
| `--strip N`              | Drop the first N source group levels without asking                          |
| `--flatten`              | Put all projects directly into `--group`                                     |
| `--visibility V`         | Visibility of created subgroups and projects (default: `private`)            |
| `--protocol https\|ssh`  | Push via HTTPS with the PAT or via your SSH key (asked if omitted)           |
| `--update-existing`      | Also force-push into target projects that already have content              |
| `--prune`                | Delete branches/tags on the target that no longer exist locally              |
| `--no-lfs`               | Don't push Git LFS objects                                                   |
| `--no-wiki`              | Don't push wikis                                                             |
| `--no-issues`            | Don't import labels, milestones and issues                                   |
| `--no-mrs`               | Don't import merge requests                                                  |
| `--keep-mentions`        | Keep `@mentions` as they are (see below)                                     |
| `--archive`              | Archive target projects whose source project is archived                     |
| `--dry-run`              | Show what would be created and pushed; nothing is written                    |
| `-y`, `--yes`            | Don't ask for confirmation of the plan                                       |

Exit codes: `0` success, `1` at least one repo failed (the others are still
processed and listed at the end), `2` invalid arguments.

## How issues and merge requests are recreated

Everything on the target is created by the PAT's user, so some details change:

- **Author and date** of every issue and comment are noted at the top, e.g.
  `> Dave (@dave) commented on 2024-01-02 11:00`. If you are owner of the target
  project (or admin), the original creation dates are kept as well.
- **Issue numbers** are kept when you are owner or admin. Otherwise gaps in the
  source numbering (deleted issues) are filled with closed, confidential
  placeholder issues, so `#42` still refers to the right issue.
- **Mentions** are turned into code (`` `@dave` ``), so importing hundreds of
  old issues does not notify everybody on the target. Use `--keep-mentions`
  if user names are the same on both instances and you want the links.
- **Attachments** are uploaded to the target project and the links rewritten.
- **System notes** (label changes, state changes, ...) are kept as a collapsed
  "Activity on the source" list in the issue description.
- **Merge requests** cannot be recreated faithfully — their source branches
  are usually merged or deleted. Each one becomes a wiki page
  `merge-requests/0012-title` with metadata, description and all comments,
  plus an index page `merge-requests`. Use `--no-mrs` to skip them (on
  `checkout` to not even read them).
- **Re-runs are safe**: what has been created is recorded in
  `<project>-import-state.json` next to the export, so an interrupted run
  continues where it stopped and nothing is created twice.

## Notes

- **No source token needed**: the source is read with the session of the
  browser you logged in with, so this works on SSO-only instances where
  personal access tokens and project exports are disabled. If the session
  expires during a long run, you are asked to log in again in the same window.
- **Slow sources**: every request to the source may take up to
  `--source-timeout` seconds. Timeouts, connection errors, `429` and `5xx`
  answers are retried up to `--retries` times with growing pauses (5 s, 10 s,
  20 s, ... up to 2 min; a `Retry-After` header is respected). A slowly loading
  sign-in page does not abort the login. Requests are sequential; use a lower
  `-j` for git clones if the server struggles.
- **Branches and tags**: bare mirrors contain every branch and tag. GitLab's
  internal refs (`refs/merge-requests/*`, `refs/pipelines/*`, ...) are not
  pushed — the target would reject them. Annotated tags stay annotated.
- **LFS**: objects are fetched during `checkout` (`git lfs fetch --all`) and
  uploaded before the code is pushed, because GitLab rejects pushes whose LFS
  objects it does not have. Without `git-lfs` installed, `checkout` skips LFS
  with a note and `push` fails for repos that have LFS objects.
- **Wikis** are pushed without force, so pages added on the target (such as
  the merge request archive) are never overwritten. If the source wiki changes
  after the first push, the wiki push fails with a hint instead.
- **Re-sync before cut-over**: run `checkout` again, then
  `push --update-existing --prune` to make the target match exactly. Without
  `--update-existing`, the code of projects that already have content is
  skipped, so nothing is overwritten by accident. Force pushes to protected
  branches may still be rejected by the target's branch protection.
- **Token safety**: HTTPS credentials are handed to git and git-lfs through a
  temporary `GIT_ASKPASS` script that reads them from the environment and is
  deleted afterwards. Tokens never end up in remote URLs, `.git/config` or the
  output.
- **SSH** runs with `BatchMode=yes`, so a missing key fails fast instead of
  hanging at a prompt.

## What is *not* migrated

CI/CD variables, pipelines and job artifacts, releases (the tags are
migrated), container and package registries, snippets, issue boards, epics,
project settings and members.

## Development

Contributions welcome — run the checks before opening a PR:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

The tests need no browser and no network: `checkout` and `push` run against
local git repositories, a fake source session and an in-memory fake of the
GitLab API.

Lint hooks are defined in `.pre-commit-config.yaml`; install them with
[prek](https://github.com/j178/prek) (or classic pre-commit):

```bash
uvx prek install      # or: uvx pre-commit install
```

## License

[MIT](LICENSE)

Logo generated by ChatGPT.

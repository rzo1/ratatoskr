"""Exclusion patterns for project paths (``--exclude`` / ``--exclude-file``).

One pattern per line; empty lines and ``#`` comments are ignored. A pattern is matched
case-insensitively against ``path_with_namespace``:

- ``*`` matches within one path level, ``?`` one character, ``**`` any number of levels
- a pattern also matches everything below it: ``fhg`` excludes ``fhg/ai/tool`` (but not
  ``fhg-intern/x``), ``fhg/*/tool*`` excludes ``fhg/ai/tool_inspection/sub``
- clone URLs are accepted as well, so a pasted ``git@host:group/repo.git`` or
  ``https://host/group/repo.git`` excludes ``group/repo``
"""

import re
from pathlib import Path


def normalize(pattern):
    """Turn a clone/web URL into a project path; plain patterns are returned trimmed."""
    pattern = pattern.strip()
    if m := re.match(r"^[\w.-]+@[\w.-]+:(.+)$", pattern):  # git@host:group/repo.git
        pattern = m[1]
    elif m := re.match(r"^(?:https?|ssh)://[^/]+/(.+)$", pattern):
        pattern = m[1]
    return pattern.removesuffix(".git").strip("/")


def to_regex(pattern):
    parts = re.split(r"(\*\*|\*|\?)", normalize(pattern))
    glob = {"**": ".*", "*": "[^/]*", "?": "[^/]"}
    body = "".join(glob.get(part, re.escape(part)) for part in parts)
    return re.compile(f"^{body}(?:/.*)?$", re.IGNORECASE)


class Excludes:
    def __init__(self, patterns=()):
        self.patterns = [normalize(p) for p in patterns if normalize(p)]
        self._regexes = [to_regex(p) for p in self.patterns]

    @classmethod
    def from_args(cls, args):
        patterns = list(getattr(args, "exclude", None) or [])
        for file in getattr(args, "exclude_file", None) or []:
            for line in Path(file).read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    patterns.append(line)
        return cls(patterns)

    def __bool__(self):
        return bool(self.patterns)

    def matches(self, path):
        return any(r.match(path) for r in self._regexes)

    def filter(self, repos, verb="Skipping"):
        """Repos not matching any pattern; prints what was left out."""
        kept = [r for r in repos if not self.matches(r["path_with_namespace"])]
        if len(kept) < len(repos):
            print(f"{verb} {len(repos) - len(kept)} repos matching the exclude patterns")
        return kept

"""One live status line in a terminal; plain lines when the output is redirected."""

import sys

_active = False


def update(message):
    """Show ``message`` as the current status (overwrites the previous one on a terminal)."""
    global _active
    out = sys.stdout
    if out.isatty():
        out.write(f"\r\x1b[K{message}")
        out.flush()
        _active = True
    else:
        print(message, flush=True)


def clear():
    """Remove the status line before printing regular output."""
    global _active
    if _active:
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()
        _active = False

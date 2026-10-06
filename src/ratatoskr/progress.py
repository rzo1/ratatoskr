"""One live status line in a terminal; plain lines when the output is redirected."""

import shutil
import sys

_active = False


def update(message, transient=False):
    """Show ``message`` as the current status (overwrites the previous one on a terminal).

    ``transient`` messages (frequent heartbeats) are only shown on a terminal, so redirected
    output is not flooded with them.
    """
    global _active
    out = sys.stdout
    if out.isatty():
        width = shutil.get_terminal_size().columns - 1
        out.write(f"\r\x1b[K{message[:width]}")
        out.flush()
        _active = True
    elif not transient:
        print(message, flush=True)


def clear():
    """Remove the status line before printing regular output."""
    global _active
    if _active:
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()
        _active = False

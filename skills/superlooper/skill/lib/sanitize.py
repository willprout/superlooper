"""Whitelist sanitizers so plan fields can never become shell injection or path escapes."""
import re

_BRANCH_OK = re.compile(r"[^A-Za-z0-9._/\-]")
_ID_OK = re.compile(r"[^A-Za-z0-9._\-]")


def branch(name):
    # REJECT unsafe names rather than silently stripping them: a mangled branch name is its
    # own bug (wrong branch) and an unsafe one signals an attack. Loud failure > quiet change
    # (review catch; matches worktree_id, which also rejects).
    if not name or not name.strip():
        raise ValueError("empty branch name")
    if _BRANCH_OK.search(name) or ".." in name or name.startswith("/"):
        raise ValueError(f"unsafe branch name: {name!r}")
    return name


def worktree_id(pid):
    if not pid or _ID_OK.search(pid) or ".." in pid:
        raise ValueError(f"unsafe id: {pid!r}")
    return pid

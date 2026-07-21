"""The operational docs that ride the gated engine publish (issue #199, defect class D12).

D12 recorded three ways doc drift caused a lost night. Two are a lint's job (dead names, orphaned
references — see ``tests/test_doc_lint.py``). The third is not: *"the debugger playbook wasn't
installed on the machine having the incident."* A correct doc that is not there is worth nothing at
3am, and the watchdog's unattended brief points a fresh session straight at
``references/unattended-contract.md`` — the passage that defines what that session may and may not
touch. On a machine without the sl-debugger skill there is nothing at the other end of that
pointer.

Since the plugin restructure those docs travel as plugin CONTENT. That is the right home for
EDITING them — one home, no drift, no double-loaded skill — but plugin installation is optional and
separately gated, and ``doctor --stack``'s ``superlooper plugin`` block only WARNs when it is
missing, on purpose (the loop runs correctly without the skills; briefs are self-contained). So a
loop-running machine with no playbook is a supported, silent state.

This module closes it by making the docs part of what the ONE gated publisher puts on a machine.
``bin/install.sh`` mirrors them into the installed engine home beside the payload it already
rsyncs, and ``stack_doctor.check_ops_docs`` verifies they arrived and carry the current publish
stamp. The repo still has exactly one editable home for each file: the mirror is a build product,
regenerated whole on every publish, never edited in place.

Design notes that are load-bearing rather than incidental:

  * **``SKILL.md`` is mirrored as ``PLAYBOOK.md``.** A file named ``SKILL.md`` under
    ``~/.claude/skills/`` risks being discovered as a second, stale copy of the sl-debugger skill —
    the double-load the plugin restructure deliberately closed. The rename makes the reference copy
    unmistakably a reference copy; the playbook's ``references/…`` links are relative and survive.
  * **The list is explicit, and its completeness is a test.** ``publish`` runs from the repo and
    could glob, but ``expected_paths`` runs on a machine with no checkout and cannot. One explicit
    table read by both sides keeps them symmetrical, and
    ``tests/test_ops_docs.py::test_the_whole_debugger_playbook_ships_not_just_its_entry_page``
    fails if a page is added to the playbook and not to this table.
  * **This module never names ``~/.claude``.** The destination is always a parameter. Locating the
    installed home is the caller's business (the installer's ``$DEST``, the doctor's probe), which
    keeps the one-publish-door fence in ``tests/test_one_publish_door.py`` honest: this file writes,
    but it has no idea where the skill home is and cannot become a second publisher.

Pure stdlib, importable without side effects, and runnable as a script — ``bin/install.sh`` shells
to it rather than reimplementing the table in bash, so there is one source of truth for what ships.
"""
import argparse
import os
import shutil
import sys

# Where the mirror lands, relative to the installed engine home. `docs/` already exists there
# (ADOPTING.md publishes into it), so the ops docs sit beside the adoption walkthrough — the two
# things an operator on an unfamiliar machine wants in the same place.
MIRROR_REL = ("docs", "ops")

# The mirror's own publish stamp. The installed engine home has a VERSION at its root; this one
# records which publish wrote the MIRROR, so a doctor can tell "these docs are current" from "these
# docs are left over from an older installer that did not know about them".
STAMP_NAME = "VERSION"

# (repo-relative source, mirror-relative destination). Order is the order they are published in and
# the order the doctor reports them missing in, so both read top-down from "the doc an operator
# reaches for first".
OPS_DOCS = (
    # The machine-stack reference: what every `doctor --stack` block name means and how to fix it.
    ("skills/superlooper/docs/STACK.md", "STACK.md"),
    # Running the loop: heartbeat/ALERT contract, restarting the runner, reading the morning report.
    ("plugin/skills/superlooper/references/runner-ops.md", "runner-ops.md"),
    # What approval means and what the gate will and will not merge.
    ("plugin/skills/superlooper/references/approval-protocol.md", "approval-protocol.md"),
    # The debugger playbook, whole. The entry page routes to all four references; the unattended
    # contract is the one a watchdog-launched session is held to.
    ("plugin/skills/sl-debugger/SKILL.md", "sl-debugger/PLAYBOOK.md"),
    ("plugin/skills/sl-debugger/references/failure-classes.md",
     "sl-debugger/references/failure-classes.md"),
    ("plugin/skills/sl-debugger/references/health-readout.md",
     "sl-debugger/references/health-readout.md"),
    ("plugin/skills/sl-debugger/references/repair-ladder.md",
     "sl-debugger/references/repair-ladder.md"),
    ("plugin/skills/sl-debugger/references/unattended-contract.md",
     "sl-debugger/references/unattended-contract.md"),
)


class MissingOpsDoc(Exception):
    """A source doc named in OPS_DOCS is not in the repo.

    Raised rather than skipped: a publish that quietly dropped the unattended contract would leave
    the machine in precisely the state this module exists to make impossible, and would do it
    silently. The installer fails loud instead, before the human walks away.
    """


def sources():
    """The repo-relative source paths, in publish order — what the publish gate must diff."""
    return [src for src, _dst in OPS_DOCS]


def mirror_dir(dest):
    """The mirror directory inside an installed engine home."""
    return os.path.join(str(dest), *MIRROR_REL)


def stamp_path(dest):
    """The mirror's publish stamp inside an installed engine home."""
    return os.path.join(mirror_dir(dest), STAMP_NAME)


def expected_paths(dest):
    """Every path a complete mirror carries, stamp included — the doctor's checklist.

    Pure path arithmetic, no filesystem access, so the doctor can build it from a probe's notion of
    HOME on a machine where none of it exists yet.
    """
    root = mirror_dir(dest)
    paths = [os.path.join(root, *dst.split("/")) for _src, dst in OPS_DOCS]
    paths.append(stamp_path(dest))
    return paths


def publish(repo_root, dest, version):
    """Mirror the ops docs from `repo_root` into `dest`, stamped `version`.

    Returns the mirror-relative destinations written, in publish order.

    The mirror directory is REMOVED and rebuilt, so a doc retired upstream does not linger on the
    machine as a page the operator can still find and act on. Only that one directory is touched —
    the payload the installer rsynced, and the engine's own VERSION, are never in scope.
    """
    repo_root = str(repo_root)
    root = mirror_dir(dest)

    missing = [src for src, _dst in OPS_DOCS if not os.path.isfile(os.path.join(repo_root, src))]
    if missing:
        raise MissingOpsDoc(
            "ops docs missing from %s: %s — publish would put a machine one incident away from "
            "having no playbook, so it refuses instead." % (repo_root, ", ".join(missing)))

    # Guarded rmtree: only ever the mirror directory, never a path a caller mistyped into `dest`.
    if os.path.basename(root) != MIRROR_REL[-1]:
        raise MissingOpsDoc("refusing to clear %s — not the ops-docs mirror" % root)
    if os.path.isdir(root):
        shutil.rmtree(root)

    written = []
    for src, dst in OPS_DOCS:
        target = os.path.join(root, *dst.split("/"))
        target_dir = os.path.dirname(target)
        if target_dir and not os.path.isdir(target_dir):
            os.makedirs(target_dir)
        shutil.copyfile(os.path.join(repo_root, src), target)
        written.append(dst)

    with open(stamp_path(dest), "w") as handle:
        handle.write("%s\n" % str(version).strip())
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="ops_docs.py",
        description="publish (or list) the operational docs that ride the gated engine install")
    ap.add_argument("--list", action="store_true",
                    help="print the repo-relative source paths, one per line (the publish gate's "
                         "diff scope)")
    ap.add_argument("--publish", action="store_true", help="mirror the docs into --dest")
    ap.add_argument("--repo-root", default=None, help="the monorepo checkout to publish from")
    ap.add_argument("--dest", default=None, help="the installed engine home to publish into")
    ap.add_argument("--version", default=None, help="the publish stamp to record")
    args = ap.parse_args(argv)

    if args.list:
        for src in sources():
            print(src)
        return 0
    if not args.publish:
        ap.error("nothing to do: pass --list or --publish")
    for name in ("repo_root", "dest", "version"):
        if not getattr(args, name):
            ap.error("--publish requires --%s" % name.replace("_", "-"))
    try:
        written = publish(args.repo_root, args.dest, args.version)
    except MissingOpsDoc as exc:
        print("ops-docs: %s" % exc, file=sys.stderr)
        return 1
    for dst in written:
        print(dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Issue #136 — the code-identity stamp and the skew decision (``lib/version.py``).

The live failure this pins (2026-07-14): static assets are read from disk on EVERY request, but the
Python server keeps the code it loaded at process start. The loop merged the janitor UI (#121) while
the owner's dashboard — up since the previous morning — was running; the page rendered the new RAMP
SWEEP button and the tap came back ``no such action``, because that server's router had never heard
of ``/api/janitor/propose``.

So the stamp is deliberately split in TWO, and that split is the whole honesty of this module:

  * the SERVER stamp (``lib`` + ``bin``) — the Python the process actually loaded at boot. Only a
    change HERE can add a route, so only a change here can make a button 404. This is what gates the
    notice and the 409.
  * the ASSETS stamp (``static``) — what the browser loads, re-read from disk every request.

A static-only merge moves the assets stamp and NOT the server stamp: new pixels against the same
router, nothing breaks — and the dashboard stays SILENT (§0.2, no nagging). A one-stamp design would
nag on every CSS tweak. These tests pin exactly that asymmetry.

Content-addressed, never mtime-addressed: a `touch` (or a checkout that rewrites a file byte-identical)
is NOT skew, and must not produce a notice.
"""
import os
import threading
import time
from pathlib import Path

import pytest

import version as version_mod


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path):
    """A miniature checkout: what makes the server (`lib/` + `bin/command-center`), what makes the
    page (`static/`), and a sibling entry point the server never loads (`bin/liftoff`)."""
    _write(tmp_path / "lib" / "server.py", "ROUTES = ('/api/flag',)\n")
    _write(tmp_path / "bin" / "command-center", "#!/usr/bin/env python3\n")
    _write(tmp_path / "bin" / "liftoff", "#!/usr/bin/env python3\n")
    _write(tmp_path / "static" / "shell.js", "// the shell\n")
    _write(tmp_path / "static" / "shell.css", ".pill { color: red }\n")
    return tmp_path


def _bump_mtime(path):
    """Move a file's mtime forward so the cheap stat signature notices it. Real merges do this for
    free; a test writing twice in the same millisecond might not."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns + 10 ** 9, st.st_mtime_ns + 10 ** 9))


# =============================== the fingerprint ===============================

def test_fingerprint_is_stable_for_identical_content(root):
    assert version_mod.fingerprint(root) == version_mod.fingerprint(root)


def test_fingerprint_is_content_addressed_not_mtime_addressed(root):
    """A touch is not a new build. Re-stamping on mtime alone would nag after any checkout that
    rewrites files byte-identically."""
    before = version_mod.fingerprint(root)
    _bump_mtime(root / "lib" / "server.py")
    assert version_mod.fingerprint(root) == before


def test_a_lib_change_moves_the_server_stamp_only(root):
    before = version_mod.fingerprint(root)
    _write(root / "lib" / "server.py", "ROUTES = ('/api/flag', '/api/janitor/propose')\n")
    after = version_mod.fingerprint(root)
    assert after["server"] != before["server"], "a lib change must move the server stamp"
    assert after["assets"] == before["assets"], "a lib change must not move the assets stamp"


def test_a_static_change_moves_the_assets_stamp_only(root):
    before = version_mod.fingerprint(root)
    _write(root / "static" / "shell.css", ".pill { color: blue }\n")
    after = version_mod.fingerprint(root)
    assert after["assets"] != before["assets"], "a static change must move the assets stamp"
    assert after["server"] == before["server"], "a static change must not move the server stamp"


def test_a_new_lib_file_moves_the_server_stamp(root):
    """The janitor case: the merge ADDED lib/janitor.py. A stamp over existing files only would miss it."""
    before = version_mod.fingerprint(root)
    _write(root / "lib" / "janitor.py", "def propose(): pass\n")
    assert version_mod.fingerprint(root)["server"] != before["server"]


def test_the_servers_own_entry_point_is_part_of_its_identity(root):
    """bin/command-center wires every surface the server exposes — a change there is a different
    server, even with lib/ untouched."""
    before = version_mod.fingerprint(root)
    _write(root / "bin" / "command-center", "#!/usr/bin/env python3\n# now wires a new surface\n")
    assert version_mod.fingerprint(root)["server"] != before["server"]


def test_a_sibling_entry_point_the_server_never_loads_is_not_its_identity(root):
    """bin/liftoff is a separate PROCESS the command-center server never imports, so a change to it
    cannot make a served control 404. Stamping it would post STALE TOWER over an edit the browser
    can't reach — the nag §0.2 forbids. (Fresh review, issue #136.)"""
    before = version_mod.fingerprint(root)
    _write(root / "bin" / "liftoff", "#!/usr/bin/env python3\n# a new flag, irrelevant to the server\n")
    assert version_mod.fingerprint(root)["server"] == before["server"], (
        "a liftoff-only change must not raise the notice — no served control can 404 from it")


def test_nothing_the_server_loads_imports_liftoff():
    """The assumption that lets bin/liftoff sit outside the server stamp, pinned mechanically.

    lib/ is stamped WHOLE (over-including is safe; excluding a module server.py might import would
    be a silent false negative). bin/ is NOT — only command-center. That is sound exactly as long as
    the server never pulls the liftoff CLI's code in. If someone makes it do so, this fails and says
    what to change, rather than the stamp quietly going blind to half its own code.
    """
    root = Path(__file__).resolve().parent.parent
    server_side = [root / "bin" / "command-center"] + sorted(
        p for p in (root / "lib").glob("*.py") if p.name != "liftoff.py")
    offenders = []
    for path in server_side:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if s.startswith("import liftoff") or s.startswith("from liftoff"):
                offenders.append("%s:%d" % (path.name, i))
    assert not offenders, (
        "the server now imports liftoff (%s) — lib/liftoff.py is already covered, but bin/liftoff is "
        "NOT in version.SERVER_FILES. Add it, or the stamp is blind to code the server runs."
        % ", ".join(offenders))


def test_a_deleted_file_moves_the_stamp(root):
    before = version_mod.fingerprint(root)
    (root / "lib" / "server.py").unlink()
    assert version_mod.fingerprint(root)["server"] != before["server"]


def test_a_renamed_file_moves_the_stamp_even_with_identical_content(root):
    """The stamp binds path AND content — same bytes at a new path is a different build."""
    before = version_mod.fingerprint(root)
    (root / "lib" / "server.py").rename(root / "lib" / "serve.py")
    assert version_mod.fingerprint(root)["server"] != before["server"]


def test_fingerprint_never_raises_on_a_missing_root(tmp_path):
    """A snapshot must never 500 because the stamp couldn't be taken (server.py's whole poll loop
    depends on assemble_snapshot not raising)."""
    got = version_mod.fingerprint(tmp_path / "nope")
    assert set(got) == {"server", "assets"}


# =============================== Version — boot identity vs disk ===============================

def test_boot_stamp_is_captured_once_and_never_moves(root):
    v = version_mod.Version(root)
    boot = dict(v.boot)
    _write(root / "lib" / "server.py", "ROUTES = ('/api/flag', '/api/janitor/propose')\n")
    _bump_mtime(root / "lib" / "server.py")
    assert v.boot == boot, "boot identity is the code the process LOADED — disk must not move it"
    assert v.current()["server"] != boot["server"], "current() must read the live disk"


def test_no_skew_on_an_untouched_checkout(root):
    assert version_mod.Version(root).state()["skew"] is False


def test_a_lib_merge_is_skew(root):
    """The live #121 case: the router on disk grew a route this process never loaded."""
    v = version_mod.Version(root)
    _write(root / "lib" / "janitor.py", "def propose(): pass\n")
    _bump_mtime(root / "lib" / "janitor.py")
    assert v.state()["skew"] is True


def test_a_static_only_merge_is_not_skew(root):
    """New pixels against the same router — nothing can 404, so the dashboard stays quiet (§0.2)."""
    v = version_mod.Version(root)
    _write(root / "static" / "shell.css", ".pill { color: blue }\n")
    _bump_mtime(root / "static" / "shell.css")
    st = v.state()
    assert st["skew"] is False, "a static-only change must never raise the notice — that would nag"
    assert st["assets"] != st["assets_at_boot"], "…but the assets stamp must still tell the truth"


def test_state_reports_both_sides_so_the_ui_can_compare_mechanically(root):
    v = version_mod.Version(root)
    st = v.state()
    for key in ("server", "server_on_disk", "assets", "assets_at_boot", "skew", "message", "remedy"):
        assert key in st, "the snapshot's version block must carry %r" % key
    assert st["server"] == st["server_on_disk"]
    assert st["assets"] == st["assets_at_boot"]


def test_state_carries_the_pid_so_liftoff_can_stop_exactly_this_process(root):
    """The restart flag must never pattern-kill (`pkill -f` once collateral-killed William's live
    dashboard). The pid comes from the process that answered our own snapshot shape — the only
    identification that cannot hit a stranger squatting the port."""
    v = version_mod.Version(root)
    assert v.state()["pid"] == os.getpid()


def test_state_claims_the_product_so_a_pid_is_never_signalled_on_resemblance_alone(root):
    """A pid is a number anything can print, and `generated_at` + `repos` is a resemblance, not a
    proof. liftoff sends a SIGTERM off this block, so the block states outright what it is.
    (Fresh review, issue #136.)"""
    assert version_mod.Version(root).state()["product"] == "command-center"


# =============================== the notice (§0.2 — a notice, not a nag) ===============================

def test_no_message_when_there_is_no_skew(root):
    assert version_mod.Version(root).state()["message"] is None


def test_the_notice_states_the_situation_and_names_the_mechanical_remedy(root):
    """The two halves are separate fields on purpose: the UI sets the remedy as a copyable <code>
    span, and no consumer should have to parse a command back out of a sentence."""
    v = version_mod.Version(root)
    _write(root / "lib" / "janitor.py", "def propose(): pass\n")
    _bump_mtime(root / "lib" / "janitor.py")
    st = v.state()
    assert st["message"], "detected skew must carry a ready-made message (B.1 — semantics server-side)"
    assert st["remedy"] == "bin/liftoff --restart-dashboard"
    assert st["remedy"] not in st["message"], (
        "the command belongs in `remedy` alone — inline too and the notice prints it twice")


def test_the_remedy_is_a_command_not_a_button(root):
    """The catch-22 that shapes this whole issue: a stale server is stale BECAUSE it lacks the newly
    merged routes, so a 'restart' endpoint would 404 on exactly the servers that need it. The remedy
    must run from disk."""
    assert version_mod.REMEDY.startswith("bin/liftoff"), (
        "the remedy must be a from-disk command — a button would hit the very server that is stale")


def test_the_stale_action_message_explains_the_skew_and_never_says_no_such_action():
    msg = version_mod.stale_action_message("/api/janitor/propose")
    assert "no such action" not in msg, "the raw 404 wording is exactly what this issue removes"
    assert version_mod.REMEDY in msg, "the honest error names the remedy"
    assert "/api/janitor/propose" in msg, "…and names the control that could not be served"


def test_the_stale_action_message_does_not_claim_a_cause_it_cannot_know():
    """The honest replacement must not overcorrect into a second lie.

    This server knows two things: it has no route for the path, and its code is older than disk's. It
    does NOT know the path came from the newer build — a typo'd or never-existent route reaches the
    same branch and would be handed a confident, false story about itself. So the message stays
    conditional, and says what a persisting failure would mean. (Fresh review, issue #136.)
    """
    msg = version_mod.stale_action_message("/api/janitor/proopse")   # a typo, not a newer build
    assert "which came from a newer build" not in msg, "that asserts a cause the server cannot know"
    assert "If" in msg or "if" in msg, "the newer-build explanation must be offered, not asserted"
    assert "real bug" in msg, "…and the owner must be told what a persisting failure means"


# =============================== the stamp stays cheap on a 2s poll ===============================

def test_current_does_not_reread_file_contents_when_nothing_changed(root, monkeypatch):
    """The snapshot polls every 2 seconds. An unchanged tree must cost stats, never a full re-read of
    every lib and static file."""
    v = version_mod.Version(root)
    reads = []
    real_open = version_mod.io.open if hasattr(version_mod, "io") else open

    def counting_open(path, *a, **kw):
        reads.append(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(version_mod, "_open", counting_open)
    v.current()
    assert reads == [], "an unchanged tree must not re-read a single file"


def test_current_rereads_after_a_real_change(root, monkeypatch):
    v = version_mod.Version(root)
    v.current()
    _write(root / "lib" / "janitor.py", "def propose(): pass\n")
    _bump_mtime(root / "lib" / "janitor.py")
    assert v.current()["server"] != v.boot["server"], "a changed tree must be re-stamped"


def test_the_cache_still_sees_a_same_size_change_with_the_mtime_restored(root):
    """The cache's worst case, and the one that would bring the whole bug back silently.

    ``rsync -t``, ``tar -x``, and any timestamp-preserving restore can land DIFFERENT bytes of the
    SAME length under the OLD mtime. A cache keyed on size+mtime alone would serve the pre-change
    hash forever and report "no skew" over a checkout that had genuinely moved — the original
    failure, now undetectable because the detector itself is lying.

    ctime is the guard: userland has no API to backdate it, so a content write always moves it.
    (Found by the fresh review, issue #136.)
    """
    target = root / "lib" / "server.py"
    _write(target, "ROUTES = ('/api/aaaa',)\n")
    v = version_mod.Version(root)
    before = v.current()["server"]
    st = target.stat()

    # Same length, different bytes — then put the timestamps back exactly as they were.
    _write(target, "ROUTES = ('/api/bbbb',)\n")
    assert target.stat().st_size == st.st_size, "the test is only meaningful at identical size"
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert target.stat().st_mtime_ns == st.st_mtime_ns, "mtime restored — the cache is now on its own"

    assert v.current()["server"] != before, (
        "a same-size edit under a restored mtime must STILL be seen — a cache that misses this "
        "reports 'no skew' over a moved checkout forever")
    assert v.state()["skew"] is True


# =============================== concurrency (the server is threaded) ===============================

def test_state_is_consistent_under_concurrent_pollers_and_a_moving_tree(root):
    """The dashboard is a ThreadingHTTPServer: every request is its own thread, so overlapping polls
    (a second tab, a slow snapshot) sit inside current() at once — while the loop merges underneath.

    Two invariants must hold on EVERY read, or the cache is tearing: the boot identity never moves
    (it is the code this process loaded — disk cannot touch it), and `skew` always agrees with the
    two stamps reported alongside it. A read that paired an advanced signature with a stale hash
    would report "no skew" against a checkout that had moved — a small copy of the very bug this
    module exists to end.
    """
    v = version_mod.Version(root)
    boot = v.boot["server"]
    errors, stop = [], threading.Event()

    def poll():
        while not stop.is_set():
            st = v.state()
            if st["server"] != boot:
                errors.append("boot identity moved to %r" % st["server"])
            if st["skew"] != (st["server_on_disk"] != st["server"]):
                errors.append("skew disagrees with the stamps beside it: %r" % st)

    threads = [threading.Thread(target=poll) for _ in range(8)]
    for t in threads:
        t.start()
    try:
        for i in range(40):
            _write(root / "lib" / ("merged%d.py" % i), "y = %d\n" % i)
            time.sleep(0.005)
    finally:
        stop.set()
        for t in threads:
            t.join()

    assert not errors, errors[:3]
    assert v.state()["skew"] is True, "the change must still be visible after the churn"

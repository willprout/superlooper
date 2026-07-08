"""ledger.py — the known-failure ledger (plan Task 12, spec §4.6 / L7 generalized).

An accepted failure is fingerprinted to its CONTENT, never to a commit — so William approves a
given failure ONCE, ever, and the same finding never re-blocks a later promotion even after the
code around it churns. The fingerprint is the SAME normalization the gate/nightly use
(gate.fix_issue_fingerprint), so one identity scheme spans dev-red fix issues, nightly failures,
and ledger acceptances.

Fail-closed like every persisted-state reader in this codebase: a missing/corrupt/wrong-typed
ledger.json reads as "nothing accepted" (so a corrupt ledger surfaces failures rather than hiding
them behind a false acceptance), and accept() over a corrupt file resets to an honest fresh map
carrying the new entry rather than raising.
"""
import gate
import ledger


def test_fingerprint_is_content_based_and_ignores_what_varies():
    # same test + same failure MEANING, but different line numbers / timestamps / abs paths:
    a = ledger.fingerprint("tests/test_login.py::test_redirect",
                           "AssertionError at /Users/ci/checkout/app/login.py:412 at 2026-07-02T01:14:59")
    b = ledger.fingerprint("tests/test_login.py::test_redirect",
                           "AssertionError at /home/runner/work/app/login.py:88 at 2026-07-03T02:01:10")
    assert a == b, "digits/timestamps/path-prefixes must normalize away — one approval, ever"


def test_fingerprint_distinguishes_different_failures():
    a = ledger.fingerprint("t::a", "widget did not render")
    b = ledger.fingerprint("t::b", "widget did not render")
    c = ledger.fingerprint("t::a", "login redirect loop")
    assert a != b and a != c and b != c


def test_fingerprint_delegates_to_the_gate_helper():
    # one identity scheme across the system (kickoff: reuse the paid-for gate.py helper)
    assert ledger.fingerprint("t::x", "boom 42") == gate.fix_issue_fingerprint("t::x", "boom 42")


def test_fingerprint_is_16_hex_and_never_raises_on_wrong_typed_input():
    fp = ledger.fingerprint(None, {"not": "a string"})
    assert isinstance(fp, str) and len(fp) == 16 and all(ch in "0123456789abcdef" for ch in fp)


def test_accept_then_is_accepted(tmp_path):
    home = str(tmp_path)
    fp = ledger.fingerprint("t::flaky", "third-party widget 500")
    assert ledger.is_accepted(home, fp) is False
    ledger.accept(home, fp, note="known-flaky third-party widget")
    assert ledger.is_accepted(home, fp) is True
    assert ledger.is_accepted(home, "deadbeefdeadbeef") is False   # unrelated fp stays unaccepted


def test_acceptance_persists_and_keeps_the_note(tmp_path):
    home = str(tmp_path)
    fp = ledger.fingerprint("t::x", "boom")
    ledger.accept(home, fp, note="approved by William 2026-07-02")
    reloaded = ledger.load(home)
    assert fp in reloaded
    assert reloaded[fp].get("note") == "approved by William 2026-07-02"


def test_accept_is_additive_not_a_clobber(tmp_path):
    home = str(tmp_path)
    fp1 = ledger.fingerprint("t::a", "one")
    fp2 = ledger.fingerprint("t::b", "two")
    ledger.accept(home, fp1, note="first")
    ledger.accept(home, fp2, note="second")
    m = ledger.load(home)
    assert fp1 in m and fp2 in m                 # accepting the second never drops the first


def test_load_fails_closed(tmp_path):
    home = str(tmp_path)
    assert ledger.load(home) == {}               # missing file
    (tmp_path / "ledger.json").write_text("{ not json")
    assert ledger.load(home) == {}               # corrupt file
    (tmp_path / "ledger.json").write_text("[1, 2, 3]")
    assert ledger.load(home) == {}               # wrong-typed (list, not map)


def test_accept_over_a_corrupt_ledger_resets_to_an_honest_map(tmp_path):
    home = str(tmp_path)
    (tmp_path / "ledger.json").write_text("garbage {[")
    fp = ledger.fingerprint("t::x", "boom")
    ledger.accept(home, fp, note="n")            # must not raise
    assert ledger.is_accepted(home, fp) is True

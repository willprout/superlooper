"""The known-failure ledger (plan Task 12, spec §4.6 bright line). A flat, content-fingerprinted
map of failures William has accepted as non-blocking, persisted at `<state_home>/ledger.json`.

The one bright line this enforces: acceptance is keyed to the failure's CONTENT, never to a
commit — William approves a given failure ONCE and it never re-blocks, however the surrounding
code churns (L7 generalized). The fingerprint is exactly the gate's normalization
(gate.fix_issue_fingerprint), so a dev-red fix issue, a nightly failure, and a ledger acceptance
that describe the SAME breakage share one identity.

ledger.json is a flat `{fingerprint: {"note": <str>}}` map (no wrapper) — so a consumer's
len(ledger) is simply the accepted count. Reads fail closed: a missing/corrupt/wrong-typed file
reads as {} (an unreadable ledger SURFACES failures, never hides them behind a false acceptance).
Writes go through loopstate's atomic tmp+rename, so a crash mid-write never leaves a half file.
"""
import os

import gate
import loopstate


def _path(state_home):
    return os.path.join(os.fspath(state_home), "ledger.json")


def fingerprint(test_id, failure_text):
    """Content identity for a failure: the gate's normalization (strip path prefixes to basename,
    digits/timestamps, whitespace; first 200 chars; sha256[:16]). Delegating keeps ONE identity
    scheme across the gate, the nightly, and this ledger. Wrong-typed input still fingerprints
    (as empty text) — a caller always gets a usable key, never an exception."""
    return gate.fix_issue_fingerprint(test_id, failure_text)


def load(state_home):
    """The accepted-failure map, fail-closed to {} on missing / corrupt / wrong-typed file."""
    try:
        obj = loopstate.load(_path(state_home))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def is_accepted(state_home, fp):
    """True iff `fp` has been accepted. Fail-closed: an unreadable ledger accepts nothing."""
    return fp in load(state_home)


def accept(state_home, fp, note=None):
    """Record `fp` as an accepted non-blocking failure with an optional note, persisted atomically.
    Additive — never drops existing acceptances. Over a corrupt ledger it resets to an honest fresh
    map carrying just this entry (fail-closed, like the runner's counter-corruption recovery)
    rather than raising. One approval, ever: re-accepting an existing fp just updates its note."""
    current = load(state_home)                 # already fail-closed to {} on corruption
    current[fp] = {"note": note}
    loopstate.save(_path(state_home), current)
    return current

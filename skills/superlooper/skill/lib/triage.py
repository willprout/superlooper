"""The triage flight's STATE CONTRACT and its daily trigger — the ``t<N>`` session class (#448).

The delegation this serves is RULED and recorded elsewhere:
``plugin/skills/superlooper/references/triage-standing-rule.md`` (owner, 2026-08-20 → 08-25) is
where the authority lives. This module is only the plumbing that rule needs — where the flight's
memory is kept, and when a flight is due. It decides NOTHING about any issue.

Shaped like ``nightly.py``: a pure-ish decision core with no GitHub, no git and no clock of its
own. The caller passes the open-issue view and the local date; the orchestration around it
(resolving the view, composing the launch) is thin glue elsewhere. That is what makes the whole
trigger a unit test rather than something you can only observe by waiting for midnight — the
lesson #164 paid for, where an e2e harness inherited the real wall clock.

================================ THE STATE CONTRACT ================================

Everything the flight remembers lives under ONE folder in the per-repo state home
(``config.state_home`` -> ``~/.superlooper/<owner>__<repo>/``)::

    <state_home>/triage/
        verdicts.json          the DURABLE record — one entry per issue, surviving every run
        runs/<YYYY-MM-DD>.md   one markdown log per flight (at most one flight per day)

``verdicts.json`` is a flat map, no wrapper, exactly as ``ledger.json`` is::

    {"448": {"body_hash": "<16 hex>", "verdict": "buildable", "date": "2026-08-25"}}

The KEY is the issue number as a decimal string (JSON has no integer keys). ``body_hash`` is what
makes an unchanged issue un-re-litigable: the rule's own words are "verdicts persist in the state
home (issue -> body-hash -> verdict -> date); an unchanged body is never re-litigated". The
``verdict`` vocabulary is the rule's — ``buildable`` / ``underspecified`` /
``contains-owner-decision`` / ``duplicate-of-#N`` / ``overtaken`` / ``nit(<rubric-line>)``. The
first four constants below are the fixed spellings; the last two carry a parameter, so they are
built (``duplicate_of`` / ``nit``) rather than named. Nothing here VALIDATES a verdict — what the
flight is allowed to conclude is the brief's contract, not the store's.

``runs/<date>.md`` is TWO things at once, and the second is the load-bearing one. It is the run
log the rule asks for ("each run writes a markdown log in the state home's triage folder; the
flight reads the last three run logs plus the verdicts file before acting"), and it is the DAY
STAMP that bounds the flight to one a day. The stamp is written by the TRIGGER, before the
session exists (``mark_launched``), never by the flight — a flight that dies before writing
anything must still not be re-launched an hour later.

Both reads FAIL CLOSED, in opposite directions, because the two costs are not symmetric:

  * an unreadable ``verdicts.json`` reads as NO verdicts, so every issue looks unjudged and gets
    looked at again. The other way round would silently skip an issue forever.
  * an unreadable day stamp reads as ALREADY RAN. A missed day is a day; a second flight is two
    sessions acting on one queue.

================================ THE TRIGGER ================================

``due()`` answers one question with three rules, in this order:

  1. **OFF unless the repo says otherwise** (``triage.enabled``, default ``false``). Checked
     first so a repo that has not opted in touches no state at all.
  2. **At most one flight per local day** — the day stamp above.
  3. **Only when something CHANGED** — some open issue whose body-hash differs from the verdict
     last recorded for it, or which has no verdict at all. A queue nobody has edited since the
     last flight has nothing for a new one to say.
"""
import hashlib
import os
import re

import issues
import limitations
import loopstate

# --------------------------------------------------------------------------- the layout

# The state folder and the per-repo config block happen to share a word today, and they are
# SEPARATE constants deliberately (fresh-agent review): one names a directory in the state home,
# the other a key in `.superlooper/config.json`, and one constant doing both means renaming the
# folder silently renames every adopter's config key — a rename that would read as "unknown key
# 'triage'" on somebody else's machine.
DIR = "triage"                      # <state_home>/triage
CONFIG_KEY = "triage"               # .superlooper/config.json -> {"triage": {...}}
RUNS = "runs"                       # <state_home>/triage/runs
VERDICTS = "verdicts.json"          # <state_home>/triage/verdicts.json

# The labels that mean THE LOOP HOLDS THIS ISSUE — and therefore that it is not the flight's to
# judge, so it never earns a verdict, so it must not be a CUE (see `changed()` for why a
# never-verdicted issue would otherwise summon a flight every day forever).
#
# `agent-ready` alone is not the predicate, and reading it as one was a real defect: the runner
# STRIPS `agent-ready` the moment it launches (runner.py, `add=["in-progress"],
# remove=["agent-ready"]`), so for the whole life of a live lane the issue carries neither. The
# engine's own canonical "is this still approved?" test names both (actions.py: "the approval is
# gone — the issue carries neither `agent-ready` nor the loop's own `in-progress` stamp"), and this
# is the same question one seat over. `awaiting-answer` is the third state of the same hold: the
# runner swaps `in-progress` for it while the owner decides, and swaps it back to `agent-ready`
# when he answers — the body is frozen owner text throughout.
#
# `parked` / `needs-owner` are deliberately NOT here. Those are handed BACK out of the loop, which
# is exactly the pile the standing rule wants a flight looking at — and they cannot cause the
# forever-cue this list exists to prevent, because a flight may judge one, record its verdict, and
# end the cue. Bare literals, as every other reader in this engine spells them.
HELD_LABELS = ("agent-ready", "in-progress", "awaiting-answer")

# The labels that mean THIS ISSUE IS NOT A TRIAGE SUBJECT AT ALL — a different thing from
# HELD_LABELS above, which mean "the loop holds it" (issue #449).
#
# The limitations ledger (#450) is the repo's own durable record of accepted limitations, and a
# flight WRITES to it: a nit close files an entry there. A thing you write to is not a thing you
# triage, and nothing good comes of a flight judging its own filing cabinet `underspecified`.
#
# It must be excluded HERE, in the cue, for the reason HELD_LABELS was widened twice: the ledger
# carries no held label and will never earn a verdict, so under a "no verdict means changed" test
# it is permanently changed — and a repo whose queue is otherwise quiet would launch an unattended
# session every day forever on the strength of one pinned issue nobody has touched. That is the
# very bound this module exists to keep, reproduced one label over.
#
# Spelled from `limitations.LEDGER_LABEL` rather than as a literal so the marker has ONE spelling:
# adopt applies it, `find_ledger` confirms it, and this excludes it. `limitations` is pure and
# imports nothing, so this costs the trigger no new dependency of substance.
NOT_SUBJECT_LABELS = (limitations.LEDGER_LABEL,)

# The run log's name IS the local date, which is also the day stamp. Matched strictly rather than
# trusted: this string becomes a path segment, and a caller handing over a date it read from
# somewhere else must not be able to write outside the runs folder.
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# --------------------------------------------------------------------------- the flight's own id

# THE one spelling of the `t<N>` shape (issue #463). It was written out three times before this —
# in the CLI's `_triage_flight_id`, in the launcher's mode guard, in the upkeep census — and the
# engine's lane verbs and state readers had it written out nowhere, which is the whole of what #463
# is about: a session class the launcher spawns, that every reader silently declined to match.
# Living here, beside the rest of what a flight IS, is what lets a verb ASK rather than re-spell.
FLIGHT_ID_RE = re.compile(r"^t[0-9]+$")


def is_flight_id(value):
    """Is ``value`` the id of a triage flight?

    A total predicate on ANY input, deliberately: its callers are id-shape guards standing in front
    of path joins and set lookups, and a guard that raised on a wrong-typed id would be a guard the
    caller has to guard. Anything that is not a `t<N>` string — a lane id, a bare ``t``, a
    ``t7.ws`` sidecar, a number, None — is simply not one.
    """
    return isinstance(value, str) and FLIGHT_ID_RE.match(value) is not None

# --------------------------------------------------------------------------- the two homes

# Where a flight runs. The RULED default is the repo's real checkout, so the flight sees what an
# orchestrator sees — including the gitignored working files a fresh worktree by definition cannot
# show. A repo whose gitignored overlay is sensitive selects `worktree` instead, accepting that
# overlay-aware triage is lost there. `launch.py` reads these two words; the per-repo key that
# selects one is validated in `config.py`.
CHECKOUT = "checkout"
WORKTREE = "worktree"
HOMES = (CHECKOUT, WORKTREE)

# --------------------------------------------------------------------------- the verdicts

# The fixed spellings of the rule's per-issue verdict vocabulary. `duplicate-of-#N` and
# `nit(<rubric-line>)` carry a parameter and are built below.
BUILDABLE = "buildable"
UNDERSPECIFIED = "underspecified"
CONTAINS_OWNER_DECISION = "contains-owner-decision"
OVERTAKEN = "overtaken"


def duplicate_of(num):
    """The `duplicate-of-#N` verdict, spelled once so the store and the close comment agree."""
    return "duplicate-of-#%s" % num


def nit(rubric_line):
    """The `nit(<rubric-line>)` verdict. The rubric line rides IN the verdict because the rule
    requires the close comment and the ledger entry to name it — a bare `nit` would record that
    something was closed without recording what made it closable."""
    return "nit(%s)" % rubric_line


# --------------------------------------------------------------------------- paths

def home(state_home):
    """``<state_home>/triage``.

    A ``state_home`` that cannot be expressed as a path is a CALLER bug, and `os.fspath` reports it
    as one (TypeError) rather than degrading into a plausible-looking path under a garbage name —
    silently reading and writing somewhere nobody chose is worse than a loud stop. Every READER
    below catches it and answers in its own fail-safe direction, so no tick dies for it.
    """
    return os.path.join(os.fspath(state_home), DIR)


def runs_dir(state_home):
    return os.path.join(home(state_home), RUNS)


def verdicts_path(state_home):
    return os.path.join(home(state_home), VERDICTS)


def run_log_path(state_home, date):
    """The markdown log for one flight — and the day stamp that bounds it to one a day.

    None for any date this cannot read. The check lives HERE, on the path builder, rather than
    only in the two callers that happen to ask first: the date becomes a path SEGMENT, so a later
    caller handed one from somewhere else would otherwise inherit a traversal hole this module
    already knows how to close (fresh-agent review, P1).
    """
    if not isinstance(date, str) or not _DATE_RE.match(date):
        return None
    try:
        return os.path.join(runs_dir(state_home), "%s.md" % date)
    except TypeError:                    # a state_home that is not a path at all (see `home`)
        return None


# --------------------------------------------------------------------------- the body hash

def body_hash(body):
    """Content identity for an issue body — what makes "has this already been judged" answerable.

    EXACT, unlike ``gate.fix_issue_fingerprint``: that one normalizes digits and paths away
    because two runs of one failing test are the same failure, and here the opposite is true —
    a single edited character is a body the owner changed and the flight has never seen.

    Line endings are the one normalization, and it is not cosmetic. A body edited in GitHub's web
    UI comes back CRLF; without this, a re-fetch that differed only in line endings would mark
    every issue in the repo changed, every day, forever.

    Never raises: a missing or wrong-typed ``body`` hashes as empty, which reads as CHANGED — the
    direction that costs a second look rather than a missed one.
    """
    text = body if isinstance(body, str) else ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


# --------------------------------------------------------------------------- the verdicts store

def load_verdicts(state_home):
    """The durable verdict map, FAIL-CLOSED to ``{}`` on missing / corrupt / wrong-typed file.

    Closed in the direction that re-reads: an unreadable store makes every issue look UNJUDGED,
    so the flight looks again. Reading it as "judged" would silently retire an issue forever on
    the strength of a truncated file.
    """
    try:
        obj = loopstate.load(verdicts_path(state_home))
    except (OSError, ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def record_verdict(state_home, num, body, verdict, date):
    """Record ONE issue's verdict against the body it was reached on, persisted atomically.

    Additive and last-wins: one entry per issue, holding the LATEST verdict, because the question
    it exists to answer is "has THIS body been judged". Hashing happens here rather than at the
    call site so the store and ``changed()`` can never disagree about the scheme.
    """
    # The path FIRST, so one caller bug has one answer. `verdicts_path` raises TypeError on a
    # state_home that is not a path at all — loudly, because a WRITE to a garbage location is not
    # something to degrade past — and doing it here means that answer no longer depends on whether
    # some other argument happened to be wrong too (fresh-agent review, P2).
    path = verdicts_path(state_home)
    # REFUSED rather than coerced (fresh-agent review, P1). The old `verdict if isinstance(...)
    # else ""` wrote a record with a correct body_hash and an empty verdict, which `changed()` then
    # read as judged — a wrong-typed argument silently retiring an issue from triage forever, which
    # is the fail-open on wrong-typed input this codebase forbids. The store is left EXACTLY as it
    # stands and returned unchanged, so the issue stays a cue and the next flight looks again.
    #
    # `date` takes the SAME rule, in the same breath: it sat in this very dict literal still being
    # coerced to "", which is the identical fail-open one field over. It is advertised in the state
    # contract, so a record carrying a blank one is a record that lies about when it was reached.
    if not (isinstance(verdict, str) and verdict.strip()):
        return load_verdicts(state_home)
    if not (isinstance(date, str) and date.strip()):
        return load_verdicts(state_home)
    # The folder is created HERE rather than assumed: the first verdict a repo ever records may
    # well arrive before any run log has (`loopstate.save` writes its temp file beside the target,
    # so a missing parent is a FileNotFoundError, not an empty file).
    os.makedirs(home(state_home), exist_ok=True)
    # LOCKED read-modify-write (fresh-agent review, P2). `loopstate.save` is already atomic, so no
    # reader can see half a ruling — but atomicity is not exclusion: two writers that both read,
    # both add their own entry and both save leave only the second one's, and the first issue is
    # then permanently unjudged. The day lease bounds the SCHEDULED path to one flight a day, and
    # deliberately bounds nothing else: two `--triage` launches by hand share this file, and
    # start-session.sh's singleton is keyed per-id (`worker.$ID.lock`), so `t1` and `t2` are not
    # each other's duplicate and both would run.
    #
    # `loopstate.update` is the right shape and cannot be used: its L1/S6 guard REFUSES to save any
    # object without an `issues` key — deliberately, because that guard is what stopped a bad
    # mutate from writing `[null, null]` over run.json. So this borrows its mutex (the same
    # portable O_EXCL primitive, the same lock-path convention) rather than its wrapper.
    lock_path = path + ".lock"
    token = loopstate._acquire(lock_path)
    try:
        current = load_verdicts(state_home)      # already fail-closed to {} on corruption
        current[_key(num)] = {"body_hash": body_hash(body), "verdict": verdict, "date": date}
        loopstate.save(path, current)
    finally:
        # A lock we never got is a lock we must not release — `_release` is token-checked, but
        # calling it with None would compare against the holder's real token and is simply wrong.
        # Timing out is not a reason to skip the write: the mutex NARROWS the race, exactly as the
        # worktree flock does, and a filesystem that cannot lock must not lose a verdict.
        if token is not None:
            loopstate._release(lock_path, token)
    return current


def _key(num):
    return str(num)


def _judged(record):
    """Is this store entry a RULING, rather than the wreckage of one? A dict carrying a non-empty
    string verdict. Anything else — not a dict, no `verdict` key, a blank one, a wrong-typed one —
    is a record no flight can be shown to have reached, and reading it as judged would remove that
    issue from triage permanently."""
    return (isinstance(record, dict) and isinstance(record.get("verdict"), str)
            and bool(record["verdict"].strip()))


# --------------------------------------------------------------------------- what changed

def changed(open_issues, verdicts):
    """The UNAPPROVED open issues whose body differs from the verdict last recorded for them.

    Returns issue NUMBERS, sorted and deduplicated. Takes the RAW gh issue dicts rather than
    ``issues.parse_issue`` output, because the body is what is being compared and the parsed
    shape deliberately does not carry it — the open-issue view (``gh._ISSUE_FIELDS``) already
    fetches ``body`` AND ``labels``, so neither read below costs an extra GitHub call.

    **Issues the LOOP HOLDS are excluded** (``HELD_LABELS``), and that is a correctness rule rather
    than a scope choice — two fresh-agent reviews in a row landed on it. The standing rule forbids
    the flight from acting on an approved issue at all, so no verdict is ever recorded for one —
    which under a "no verdict means changed" test makes it permanently changed, and the trigger
    fires EVERY DAY FOREVER on a queue the flight is not allowed to touch. That directly falsifies
    the bound this module exists to keep ("unchanged bodies since the last verdicts update -> no
    launch"). A repo holding one such issue and an otherwise quiet queue would have launched an
    unattended session every day with nothing it was permitted to do.

    The SECOND review found the first fix was half of one: it excluded ``agent-ready`` alone, and
    the runner strips exactly that label at launch — so every live lane, and this repo almost
    always has one, read as "unapproved with no verdict" and was a daily cue. The predicate has to
    be the engine's own (see ``HELD_LABELS``), not the one label that happens to name approval at
    rest.

    What is NOT lost by excluding them: a flight already in the air still SEES every held issue and
    may flag one — the rule allows lint to flag and escalate. What changes is only whether one can,
    by itself, summon a flight. It cannot: an edit to frozen text is the owner editing his own, and
    the flight has nothing to say about it.

    **THIS IS THE LAUNCH CUE, AND NEVER THE ACTION LIST.** It answers one question — is there
    anything new to look at — and a caller must not reuse it as "the issues this flight may act
    on". The two differ where it matters: `parked` / `needs-owner` are cues (they are out of the
    loop's hands and a flight judging one ends the cue) but a `needs-owner` issue is one the owner
    has been explicitly ASKED to decide, and the standing rule's "anything the owner has personally
    flagged" line plus its escalate-never-act discipline both point away from closing it. What a
    flight may DO with each verdict is the brief's contract (part 2), not this function's.

    Defensive like every other pure core here: a partial or wrong-typed view (a broken gh call, a
    half-written cache) yields fewer issues to triage, never an exception into a tick. An entry
    that is not a readable RULING — not a dict, or carrying no usable verdict — counts as
    UNJUDGED, which is the re-read direction.
    A wrong-typed label set reads as NO labels (``issues._label_names``' own contract), i.e. as
    unapproved — the direction that costs a second look rather than a missed one.
    """
    if not isinstance(open_issues, list):
        return []
    known = verdicts if isinstance(verdicts, dict) else {}
    out = set()
    for issue in open_issues:
        if not isinstance(issue, dict):
            continue
        num = issue.get("number")
        # bool is an int subclass and True == 1, so a wrong-typed `True` would otherwise be
        # triaged as issue #1 — the same coercion trap `issues.dep_met` documents.
        if isinstance(num, bool) or not isinstance(num, int):
            continue
        # `issues._label_names` by its own (private-looking) name: it is already the cross-module
        # spelling — `queue_lint` calls exactly this — and renaming it would ripple into a
        # dashboard docstring for no gain. Its contract is what matters here: any malformed label
        # set yields [], i.e. NOT HELD, which is the re-read direction.
        names = issues._label_names(issue)
        if any(held in names for held in HELD_LABELS):
            continue                     # never the flight's to judge -> never the flight's cue
        if any(skip in names for skip in NOT_SUBJECT_LABELS):
            continue                     # not a subject at all -> and never a forever-cue
        record = known.get(_key(num))
        # BOTH halves of a record have to be readable for it to retire an issue (fresh-agent
        # review, P1). `load_verdicts` is hardened so a truncated FILE cannot silently retire
        # everything; this is the same invariant one layer in, for a single truncated or
        # hand-edited ENTRY. A record carrying a correct body_hash and a blank or absent verdict
        # means the flight recorded a hash and never reached a ruling — reading that as "judged"
        # is exactly the silent-forever-retirement `load_verdicts`' own docstring names, and it
        # is a fail-OPEN on wrong-typed input, which this codebase forbids. Unjudged is the
        # re-read direction: it costs a second look, never a missed issue.
        if not _judged(record) or record.get("body_hash") != body_hash(issue.get("body")):
            out.add(num)
    return sorted(out)


# --------------------------------------------------------------------------- the day stamp

def ran_on(state_home, date):
    """Has a flight already gone out on this local date?

    FAIL CLOSED to True on any date this cannot read: with no usable date the one-a-day bound
    cannot be applied at all, and answering "no" would make the trigger unbounded.
    """
    path = run_log_path(state_home, date)
    if path is None:
        return True
    try:
        return os.path.isfile(path)
    except OSError:
        return True


def mark_launched(state_home, date):
    """TAKE THE DAY'S LEASE and open its run log. Returns the log's path, or None.

    **A caller that gets None MUST NOT LAUNCH.** None means one of three things and every one of
    them is "not yours to fly": the date is unreadable, the stamp could not be written, or
    SOMEBODY ELSE ALREADY HOLDS TODAY.

    An exclusive create (``O_CREAT | O_EXCL``), not a check-then-write, and that is the whole
    reason this function exists rather than being two lines at the call site (fresh-agent review,
    P0). ``due()`` reading ``ran_on`` and then a caller writing the stamp is a race two runners
    lose together — a restart overlapping its predecessor, or a hand ``superlooper run`` beside
    the LaunchAgent, and the queue gets two flights acting on it at once. The exclusive create is
    what makes "at most one flight per day" a property of the filesystem instead of a property of
    how carefully every caller ordered its checks.

    Called by the TRIGGER, BEFORE the session is created. That ordering is the other half of the
    bound: a flight killed hard enough to write nothing has still consumed its day, so nothing
    re-launches it an hour later. The flight then APPENDS its own log to this file.

    The loser of the race never touches the winner's log — it does not open it at all.
    """
    path = run_log_path(state_home, date)
    if path is None:
        return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return None                      # somebody else already holds today
    except OSError:
        return None                      # unwritable state home — fail closed, do not launch
    try:
        with os.fdopen(fd, "w") as f:
            f.write("# Triage flight %s\n\n" % date)
    except OSError:
        # The lease IS the file, and it now exists — so the day is consumed either way and no
        # second flight can be launched for it. An empty log is a worse artifact than a headed
        # one; it is not a reason to hand the caller a launch it cannot record.
        return None
    return path


def append_run_log(state_home, date, text):
    """APPEND to a day's run log — the flight's own record, written a line at a time as it acts.

    Appending as each act lands, rather than composing one document at the end, is deliberate: a
    flight killed mid-run — a crash, a usage cap, the machine sleeping — still leaves an honest
    record of what it had already done to the queue, and the acts are the half that is not
    reversible from the log alone.

    **It NEVER creates the file.** The run log IS the day's lease, and ``mark_launched`` is the
    only thing that may take it — this module's own contract, in as many words: "the stamp is
    written by the TRIGGER, before the session exists, never by the flight". Creating it here
    would quietly break that: a single hand-run ``triage-act`` in the morning would leave a stamp
    that reads as "a flight already went out today", and the day's whole triage pass would be
    skipped because somebody fixed one label by hand. An act outside a flight is still durable —
    it is in the journal, which is what the morning report reads — it simply does not forge a
    lease it never took.

    True when the line landed, False when there was no log to append to (or the date is unreadable,
    or the state home is unwritable). Never raises: a log that cannot be written must not undo an
    act that already landed on GitHub.
    """
    path = run_log_path(state_home, date)
    if path is None or not isinstance(text, str):
        return False
    try:
        # "a" would CREATE, so the existence check is the guard and it has to be here rather than
        # in the caller: every caller of this function is an act that has already happened, and
        # not one of them is in a position to decide whether it may take the day.
        if not os.path.isfile(path):
            return False
        with open(path, "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        return False
    return True


def recent_run_logs(state_home, limit=3):
    """The most recent run logs, newest first, as (date, text) — the rule's "the flight reads the
    last three run logs plus the verdicts file before acting". Unreadable logs are skipped rather
    than raised: a corrupt one costs the flight context, never its tick."""
    try:
        count = max(0, int(limit))
    except (TypeError, ValueError):
        return []                        # the no-raise posture holds for every argument
    try:
        names = sorted((n for n in os.listdir(runs_dir(state_home)) if n.endswith(".md")),
                       reverse=True)
    except (OSError, TypeError):
        return []
    out = []
    for name in names[:count]:
        try:
            # `errors="replace"` and NOT a bare open (fresh-agent review, P1). A run log is written
            # by the FLIGHT — an agent appending markdown — so a write truncated mid-multibyte
            # character, or a raw byte pasted out of some tool's output, is ordinary rather than
            # exotic. A bare read raises UnicodeDecodeError, which is a ValueError and so slips
            # past `except OSError` and out of a core whose docstring promises it never raises —
            # and this is THE reader the standing rule names, so one corrupt log would strand
            # every subsequent flight. `load_verdicts` already gets this right; this one did not.
            with open(os.path.join(runs_dir(state_home), name), encoding="utf-8",
                      errors="replace") as f:
                out.append((name[:-3], f.read()))
        except (OSError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- what a flight IS

# The one act that means A FLIGHT CLOSED ITS RUN. `superlooper triage-finish` is the flight's own
# last move — it writes the sitting sheet and the run's tally into the day's log — and it journals
# this act stamped with the flight's id (`SL_ISSUE_ID`, assigned by the launcher, never
# self-asserted).
FINISH_ACT = "triage_finish"


def finished_flights(records):
    """The flight ids whose RUN IS CLOSED, read from journal records — ``{"t7", "t9"}``.

    This is the ONE positive "that flight is done" signal the engine has, and `superlooper tidy`
    needs it for a reason nothing else supplies: a finished claude session idles at its prompt
    forever and never self-exits (D4), so a flight that has written its sitting sheet still holds a
    live pid and a live window. Liveness alone therefore cannot tell a finished flight from one
    still working through the queue; this can.

    What it deliberately does NOT do is decide the other half. A flight that died mid-run journals
    no finish at all, and is recognised instead by its session being GONE — see
    ``tidy.closable_flights``, which takes both readings and states which scope each falls in.

    A hand-run ``triage-finish`` outside a flight journals an EMPTY ``flight`` (the CLI records the
    id from the session's own environment, and an operator's shell has none). That record is
    skipped rather than attributed to whichever flight ran last: it says honestly that it belongs
    to none.

    Pure and total. A records view that is not a list, a record that is not a dict, a ``flight``
    that is not a ``t<N>`` string — every one of them is skipped, because the cost of a wrong
    reading here is a window closed on a flight that is still flying.
    """
    if not isinstance(records, (list, tuple)):
        return set()
    return {r["flight"] for r in records
            if isinstance(r, dict) and r.get("act") == FINISH_ACT and is_flight_id(r.get("flight"))}


# --------------------------------------------------------------------------- the config reads

def enabled(config):
    """Is the triage flight armed for this repo? (``triage.enabled``, default false.)

    FAIL CLOSED on anything but a real boolean ``True``: this is a master switch, and a config
    read half-way through a write, or a string ``"true"`` from a hand-edit, must not arm a session
    class that closes issues.
    """
    block = config.get(CONFIG_KEY) if isinstance(config, dict) else None
    return (block or {}).get("enabled") is True if isinstance(block, dict) else False


def home_kind(config):
    """Which home this repo's flight runs in — ``checkout`` (the ruled default) or ``worktree``.

    Falls back to the default rather than raising, the way ``runner_home.kind()`` does: every
    runtime reader may be handed a half-read config. The LOADER is where a typo fails loudly, at
    adopt time, which is the one place an owner can still fix it.
    """
    block = config.get(CONFIG_KEY) if isinstance(config, dict) else None
    value = (block or {}).get("home") if isinstance(block, dict) else None
    # `type(...) is str` before the membership test, symmetric with launch.py's reader of this same
    # field (round-2 P0 hardened that one and left this one comparing by `==`): `in` compares by
    # VALUE, so an object with a co-operative `__eq__` would answer yes to a tuple of strings, and
    # one whose `__eq__` raises would take a runtime reader down instead of degrading. Two readers
    # of one field must not disagree about what "unreadable" means.
    return value if type(value) is str and value in HOMES else CHECKOUT


# --------------------------------------------------------------------------- the trigger

def due(open_issues, state_home, date, config):
    """Is a triage flight due right now? ``(bool, reason)`` — the reason is journal/report prose.

    The clock is the ``date`` parameter and nothing else; this module reads no wall clock.

    **THIS IS HALF THE DECISION.** ``due()`` only READS the day stamp; it deliberately does not
    take it, so a caller that merely wants to display "a flight is due" (a report line, a
    dashboard tile) does not consume the day by asking. The caller that intends to LAUNCH must
    then take the lease — ``mark_launched(state_home, date)`` — and must not launch if it returns
    None. Reading `due()` and launching without it is a check-then-act that reproduces exactly the
    unbounded relaunch the lease exists to prevent (fresh-agent review, P2), so the two calls
    belong at the same call site, in that order, with nothing between them.
    """
    if not enabled(config):
        return False, "triage is disabled for this repo (triage.enabled is false)"
    if not isinstance(date, str) or not _DATE_RE.match(date):
        return False, ("no usable local date (%r) — the one-a-day bound cannot be applied, so no "
                       "flight is launched" % (date,))
    # ASKED BEFORE `ran_on`, because `ran_on` fails closed to True and would otherwise report an
    # unreadable state home as "a flight already went out on <date>" — journal and report prose, by
    # this function's own docstring, and a self-concealing lie repeated every day forever
    # (fresh-agent review, P2). The direction is unchanged (no launch); only the diagnosis is.
    if run_log_path(state_home, date) is None:
        return False, ("the triage state home could not be read (%r), so whether a flight already "
                       "went out today cannot be established — launching nothing"
                       % (state_home,))
    if ran_on(state_home, date):
        return False, "a triage flight already went out on %s" % date
    fresh = changed(open_issues, load_verdicts(state_home))
    if not fresh:
        return False, "no open issue's body has changed since the last recorded verdicts"
    named = ", ".join("#%d" % n for n in fresh[:10])
    return True, "%d open issue(s) changed since the last recorded verdicts: %s%s" % (
        len(fresh), named, ", ..." if len(fresh) > 10 else "")


__all__ = ["DIR", "CONFIG_KEY", "RUNS", "VERDICTS", "HELD_LABELS", "NOT_SUBJECT_LABELS",
           "CHECKOUT", "WORKTREE", "HOMES", "FLIGHT_ID_RE", "is_flight_id",
           "FINISH_ACT", "finished_flights",
           "BUILDABLE", "UNDERSPECIFIED", "CONTAINS_OWNER_DECISION", "OVERTAKEN",
           "duplicate_of", "nit", "home", "runs_dir", "verdicts_path", "run_log_path",
           "body_hash", "load_verdicts", "record_verdict", "changed", "ran_on",
           "mark_launched", "append_run_log", "recent_run_logs", "enabled", "home_kind", "due"]

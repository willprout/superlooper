"""lib/identity.py — the launch-time identity env contract (issue #314).

The subject is a mechanism that fails SILENTLY and looks like something else while it does:

  * the credential namespace a `claude` session uses is `sha256` of the `CLAUDE_CONFIG_DIR` string
    **as written**, so two spellings of one directory are two identities — and the wrong one
    presents as a logged-out session, never as an error (#300 landmine 1);
  * `CLAUDE_SECURESTORAGE_CONFIG_DIR` set-but-EMPTY collapses that namespace back to the owner's
    unsuffixed default, so an inherited empty value silently bills the owner (#300 landmine 2);
  * and `claude auth status` answers `loggedIn: true` for a session running on an API KEY, with
    `email`/`orgId`/`subscriptionType` all null — measured against the real binary on 2026-08-04.
    So "logged in" is not "on the intended subscription", and only a positive read of the account
    can tell them apart.

Every test here is about one of those, and none of them may reach a real `claude` (CLAUDE.md
ratchet) — the status text is injected, because the whole subject is what we do with it.
"""
import json
import os

import pytest

import identity


_FLEET_DIR = "/Users/loop/.claude-fleet"
_FLEET_ORG = "512c95fc-0638-4911-a131-32f411f70afc"
_OWNER_ORG = "9f0d1a22-1111-4b33-9999-abcdefabcdef"


def _status(org=_FLEET_ORG, **over):
    """A `claude auth status` blob in the SHAPE the real binary emits (measured 2026-08-04)."""
    out = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
           "email": "loop@example.com", "orgId": org,
           "orgName": "loop@example.com's Organization", "subscriptionType": "max"}
    out.update(over)
    return out


class FakeRun:
    """A stand-in for the launcher's `edges.run` / the doctor's `probe.run` — records the env each
    call was handed, because the env IS the measurement here.

    `rc` defaults to 1 for the same reason the real binary does: `claude auth status` EXITS 1 when
    it is logged out while printing a perfectly readable body (measured 2026-08-04). A fake that
    answered 0 would let a body-ignoring reader pass its tests and then mis-name every
    unprovisioned config dir in production.
    """

    def __init__(self, answers=None, rc=1):
        self.answers = answers or {}
        self.rc = rc
        self.calls = []

    def run(self, argv, timeout=None, cwd=None, env=None):
        self.calls.append((list(argv), dict(env or {})))
        text = self.answers.get((env or {}).get(identity.CONFIG_DIR_VAR))
        if text is None:
            text = self.answers.get("*", "")

        class R:
            returncode = self.rc
            stdout = text or ""
            stderr = ""
        return R()


# ---------------------------------------------------------------- one canonical string (DoD 1, 2)

def test_every_spelling_of_one_directory_becomes_one_identity():
    """#300 measured five spellings of one dir producing five credential namespaces. The launch
    path derives ONE canonical string, so a spelling cannot become an identity."""
    env_home = {"HOME": "/Users/loop"}
    spellings = [_FLEET_DIR, _FLEET_DIR + "/", _FLEET_DIR + "//", "/Users/loop/./.claude-fleet",
                 "/Users//loop/.claude-fleet", "~/.claude-fleet", _FLEET_DIR + "/."]
    got = set()
    for spelling in spellings:
        env = dict(env_home, **{identity.FLEET_DIR_VAR: spelling})
        value, problem = identity.worker_config_dir(env)
        assert problem is None, (spelling, problem)
        got.add(value)
    # BYTE-identical, not merely equivalent: the hash is taken over the string, so this set having
    # one member is the whole isolation guarantee.
    assert got == {_FLEET_DIR}
    # And the same fact expressed as the mechanism itself — one credential namespace.
    assert len({identity.namespace_suffix(v) for v in got}) == 1


def test_the_namespace_suffix_is_the_mechanism_not_a_paraphrase_of_it():
    """The suffix is `sha256` of the string as written, first 8 hex — read out of the shipped
    binary in #300 and re-derived here, so a test that varies spelling asserts IDENTITY and not
    merely string equality."""
    import hashlib
    for value in (_FLEET_DIR, "/tmp/x", "~"):
        assert identity.namespace_suffix(value) == \
            hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    # The point of canonicalising: the non-canonical spellings are DIFFERENT namespaces.
    assert identity.namespace_suffix(_FLEET_DIR) != identity.namespace_suffix(_FLEET_DIR + "/")


def test_a_relative_or_empty_assignment_is_refused_rather_than_guessed_at():
    for bad in ("fleet", "./fleet", "../fleet"):
        value, problem = identity.worker_config_dir({identity.FLEET_DIR_VAR: bad})
        assert value is None and "absolute" in problem, bad
    # Unset means "this machine assigns none" — today's production behaviour, not a fault.
    value, problem = identity.worker_config_dir({})
    assert value is None and problem is None
    value, problem = identity.worker_config_dir({identity.FLEET_DIR_VAR: "   "})
    assert value is None and problem is None


def test_a_home_relative_assignment_without_a_home_is_refused_not_expanded_to_nothing():
    # `~` with no HOME expands via the passwd entry in some readers and to a literal `~` in others.
    # Either way the answer must be an absolute path or a refusal, never a `~`-prefixed string
    # handed to a session as its credential namespace.
    value, problem = identity.worker_config_dir({identity.FLEET_DIR_VAR: "~/.claude-fleet"},
                                                home="")
    assert value is None and problem
    assert "~" not in (value or "")


# ---------------------------------------------------------- the redirect variable (DoD 3)

def test_an_inherited_credential_redirect_is_refused_at_any_value():
    """#300 landmine 2. Present-but-EMPTY is the dangerous one — it collapses the namespace back to
    the owner's unsuffixed default and bills the owner's subscription with nothing anywhere
    erroring — but a session must not inherit ANY value: the third row of #300's table is a
    deliberate escape hatch, and inheriting it points the fleet at some other login."""
    for value in ("", "   ", "/somewhere/else"):
        problem = identity.redirect_problem({identity.REDIRECT_VAR: value})
        assert problem and identity.REDIRECT_VAR in problem, repr(value)
    assert "empty" in identity.redirect_problem({identity.REDIRECT_VAR: ""}).lower()
    assert identity.redirect_problem({}) is None


# ------------------------------------------------------- the positive account assert (DoD 4)

def test_an_api_key_session_reports_logged_in_and_must_still_be_refused():
    """MEASURED against the real binary (2026-08-04): with ANTHROPIC_API_KEY exported,
    `claude auth status` answers `loggedIn: true`, `apiKeySource: "ANTHROPIC_API_KEY"` and
    null email/orgId/subscriptionType. Absence of an error is not identity — this is the c1
    silent-billing-flip wearing the costume of a healthy session."""
    on_a_key = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                "apiKeySource": "ANTHROPIC_API_KEY", "email": None, "orgId": None,
                "orgName": None, "subscriptionType": None}
    problem = identity.account_problem(on_a_key)
    assert problem and "API key" in problem and "ANTHROPIC_API_KEY" in problem


def test_a_logged_out_or_unreadable_status_is_refused_never_read_as_healthy():
    assert "not logged in" in identity.account_problem(
        {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"})
    for unreadable in (None, "", [], {"loggedIn": "yes"}):
        assert identity.account_problem(unreadable), repr(unreadable)


def test_a_subscription_is_required_positively():
    # A first-party login with no subscription type is not the Max seat the capacity plan assigns.
    assert "subscription" in identity.account_problem(_status(subscriptionType=None))
    assert "subscription" in identity.account_problem(_status(subscriptionType=""))
    assert identity.account_problem(_status()) is None


def test_the_expected_account_is_compared_never_merely_observed():
    assert identity.account_problem(_status(), expected=_FLEET_ORG) is None
    problem = identity.account_problem(_status(org=_OWNER_ORG), expected=_FLEET_ORG)
    assert problem and _FLEET_ORG in problem and _OWNER_ORG in problem
    # An account with no org at all cannot satisfy an expectation, and must not pass by accident.
    assert identity.account_problem(_status(org=None), expected=_FLEET_ORG)


def test_the_status_reader_takes_the_leading_json_and_ignores_a_trailing_banner():
    # Same hazard usage.probe_auth documents: an update banner printed after the blob would make a
    # strict json.loads raise, and a healthy read would degrade to "unreadable" -> refused launch.
    blob = json.dumps(_status())
    assert identity.parse_status(blob + "\n\nA new version is available!\n")["orgId"] == _FLEET_ORG
    assert identity.parse_status("not json") is None
    assert identity.parse_status("") is None


def test_a_logged_out_answer_is_read_from_the_body_and_not_from_the_exit_code():
    """MEASURED (2026-08-04, 2.1.222): `claude auth status` EXITS 1 when logged out, printing
    `{"loggedIn": false, "authMethod": "none"}`. A reader that trusted the rc would turn the single
    most likely state of a fresh fleet config dir into "could not be read at all" and send the
    operator hunting a broken binary instead of a login screen."""
    runner = FakeRun(answers={"*": '{"loggedIn": false, "authMethod": "none"}'}, rc=1)
    status = identity.read_status(runner, "/bin/claude", env={"HOME": "/h"})
    assert status == {"loggedIn": False, "authMethod": "none"}
    assert "not logged in" in identity.account_problem(status)
    # A genuine non-answer (no body at all) is still None, and still a refusal.
    assert identity.read_status(FakeRun(answers={"*": ""}, rc=127), "/bin/claude") is None


def test_reading_the_status_asks_under_the_assigned_config_dir_and_nowhere_else():
    runner = FakeRun(answers={_FLEET_DIR: json.dumps(_status()),
                              None: json.dumps(_status(org=_OWNER_ORG))})
    status = identity.read_status(runner, "/bin/claude", config_dir=_FLEET_DIR,
                                  env={"HOME": "/Users/loop"})
    assert status["orgId"] == _FLEET_ORG
    argv, env = runner.calls[0]
    assert argv[:3] == ["/bin/claude", "auth", "status"]
    assert env[identity.CONFIG_DIR_VAR] == _FLEET_DIR
    # No assignment -> the variable must be ABSENT, not empty: an empty CLAUDE_CONFIG_DIR is a
    # third namespace again (`sha256("")`), not "the default".
    identity.read_status(runner, "/bin/claude", config_dir=None, env={"HOME": "/Users/loop"})
    assert identity.CONFIG_DIR_VAR not in runner.calls[1][1]


# ------------------------------------------------------- the in-session verdict (the floor)

def _session_env(**over):
    env = {"HOME": "/Users/loop", "PATH": "/usr/bin"}
    env.update(over)
    return env


def test_the_session_refuses_a_config_dir_it_was_never_assigned():
    """Identity ASSIGNED, never self-asserted (claim c3). The launcher names the dir; anything else
    in the session's own environment arrived from a shell rc file or a LaunchAgent, and a worker
    whose credential namespace comes from there is a worker nobody assigned an account to."""
    problem = identity.session_problem(_session_env(CLAUDE_CONFIG_DIR=_FLEET_DIR), status=_status())
    assert problem and "was not assigned" in problem


def test_the_session_refuses_a_spelling_that_is_not_the_one_the_launcher_named():
    env = _session_env(**{identity.ASSIGN_VAR: _FLEET_DIR,
                          identity.CONFIG_DIR_VAR: _FLEET_DIR + "/"})
    problem = identity.session_problem(env, status=_status())
    assert problem and "byte-identical" in problem.lower()
    # ...and it says WHICH namespace each spelling would have reached, because "logged out" is the
    # only symptom this produces and it is unguessable from the pane.
    assert identity.namespace_suffix(_FLEET_DIR) in problem


def test_the_session_accepts_the_assignment_it_was_given():
    env = _session_env(**{identity.ASSIGN_VAR: _FLEET_DIR, identity.CONFIG_DIR_VAR: _FLEET_DIR,
                          identity.EXPECT_VAR: _FLEET_ORG})
    assert identity.session_problem(env, status=_status()) is None
    # An unassigned machine keeps today's behaviour: no config dir anywhere, and the assert is
    # still POSITIVE about the account.
    assert identity.session_problem(_session_env(**{identity.EXPECT_VAR: _FLEET_ORG}),
                                    status=_status()) is None


def test_the_session_refuses_the_redirect_before_it_reads_any_account():
    """Ordered first on purpose: the redirect decides WHICH credential the read below would have
    consulted, so an account read taken under it is a measurement of the wrong namespace."""
    env = _session_env(**{identity.ASSIGN_VAR: _FLEET_DIR, identity.CONFIG_DIR_VAR: _FLEET_DIR,
                          identity.REDIRECT_VAR: ""})
    problem = identity.session_problem(env, status=_status())
    assert problem and identity.REDIRECT_VAR in problem


def test_a_ladder_that_cannot_be_consulted_refuses_while_one_with_no_binary_defers(monkeypatch):
    """The one branch of `_assert` that does not refuse is the deferral to start-session.sh's own
    #303 rung, which refuses a launch whose binary ladder names nothing runnable. That deferral is
    only sound for THAT case. A ladder that could not be LOADED must refuse here, because an engine
    published without `lib/` still has a runnable claude on PATH — the shell ladder would find one,
    start the agent, and the assert would simply not have happened."""
    env = _session_env()
    monkeypatch.setattr(identity, "resolve_claude",
                        lambda e=None: (None, "no claude anywhere", True))
    code, message = identity._assert(env)
    assert code == 0 and "NOT asserted" in message and "#303" in message

    monkeypatch.setattr(identity, "resolve_claude",
                        lambda e=None: (None, "the ladder could not be loaded", False))
    code, message = identity._assert(env)
    assert code == identity.REFUSED and "could not be established" in message


def test_a_non_answer_is_retried_once_and_a_definite_refusal_is_not():
    """The retry exists for the same reason the gh probe's does — one hiccup must not park an issue
    — but a logged-out dir ANSWERS, and retrying a definite reading would only spend a second
    timeout out of the launcher's 30s verify window on a launch that is already refused."""
    # `/bin/sh` only because the #303 ladder demands a real executable FILE for its pin — the fake
    # runner intercepts the call, so nothing is ever run.
    silent = FakeRun(answers={"*": ""}, rc=124)
    code, _message = identity._assert(_session_env(SL_CLAUDE="/bin/sh"), runner=silent)
    assert code == identity.REFUSED
    reads = [c for c in silent.calls if c[0][1:3] == ["auth", "status"]]
    assert len(reads) == 2, "a non-answer is worth one retry"

    definite = FakeRun(answers={"*": '{"loggedIn": false, "authMethod": "none"}'}, rc=1)
    code, _message = identity._assert(_session_env(SL_CLAUDE="/bin/sh"), runner=definite)
    assert code == identity.REFUSED
    assert len([c for c in definite.calls if c[0][1:3] == ["auth", "status"]]) == 1


def test_the_session_refuses_the_wrong_account_and_names_both_sides():
    env = _session_env(**{identity.EXPECT_VAR: _FLEET_ORG})
    problem = identity.session_problem(env, status=_status(org=_OWNER_ORG))
    assert problem and _OWNER_ORG in problem and _FLEET_ORG in problem

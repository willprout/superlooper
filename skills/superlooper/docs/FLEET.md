# The fleet machine build-up

The 2026-08-03 machine ruling (`docs/HERDR-ADOPTION-PLAN.md` §6, §8.1): the session-host fleet
stands up **fresh** on the always-on Mac mini while the old cmux production loop keeps running
untouched on the work laptop. Cutover happens only when acceptance passes ON the mini. This
document is the build-up: what it installs, why each piece is there, and what the judge checks.

Two commands, deliberately separate:

```sh
skills/superlooper/vendor/herdr/build.sh     # from the SOURCE checkout: build the pinned + fenced host
superlooper fleet --install --load           # configure it, load it, judge it
superlooper fleet                            # judge it again, any time (exit 1 if not ready)
```

`superlooper fleet` with no flags writes nothing. That is the point: **every property this
build-up has to satisfy is one that is silently wrong while the machine still looks alive.** An
unfenced control socket answers. A host one release off the pin starts and runs. A fleet config dir
spelled with a trailing slash simply reports logged-out. A LaunchAgent in the wrong domain has no
login keychain and only fails sometimes. So "the mini is built" is an exit code, not an opinion.

---

## Why the build is its own command

`vendor/herdr/build.sh` clones the pinned release, applies the fence patch that lives beside it,
and installs the result into `<state base>/fleet/bin/`. It needs the **source checkout** (the patch
is a build input and never ships inside the published engine payload) and a Rust toolchain, and it
takes minutes. `superlooper fleet --install` configures a host that already exists and refuses,
naming this script, when one does not — an install that configured a host nobody had built would
produce a green-looking job that starts nothing.

The fence is what makes the difference invisible if you skip it: a stock binary and a patched one
report the same version and answer the same verbs. Only the `host fence` block below can tell them
apart, and it does it by asking the socket what a *worker* would ask.

## What `--install` writes

| Path | What | Notes |
|---|---|---|
| `<prefix>/token` | the fence token | minted `0600` on first install, then left alone |
| `<host config dir>/config.toml` | the host's settings (plan §2) | **shared** with any session already on this machine — see below |
| `<host config dir>/agent-detection/<agent>.toml` | the screen-state override | a snapshot of the host's own manifest plus one rule |
| `~/Library/LaunchAgents/com.superlooper.session-host.plist` | the server login item | `gui/$UID` only |
| `<prefix>/viewer.command` | the optional viewer | written, never registered |

`<prefix>` is `<state base>/fleet` — hung off the state base and never off a repo, because the host
is **one server for the whole machine**. A per-repo prefix would mean two repos, two hosts, two
fences and two tokens, which is not a fleet.

### It never overwrites somebody else's file

The host's config directory is shared with whatever sessions the operator already runs. So
`--install` writes `config.toml` and the manifest override only when the file is absent or carries
superlooper's own marker; anything else is **refused with the path named**, and the matching block
stays red until a person merges it by hand. Re-running the install is otherwise a no-op — that is
what makes it a build-up rather than a ritual.

### The token is not in the plist, and this is measured

`launchctl print gui/$UID/<label>` echoes a job's `EnvironmentVariables` **verbatim**. Measured on
the fleet machine while writing this:

```
$ launchctl print gui/501/com.superlooper.session-host | grep -A6 environment
	environment = {
		PATH => /opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
		HERDR_API_TOKEN_FILE => /Users/willprout/.superlooper/fleet/token
		XPC_SERVICE_NAME => com.superlooper.session-host
	}
```

A token spelled in the plist would therefore be one command away from any same-uid process — which
is exactly the worker the fence exists to keep out. So the plist carries the *path* of a `0600`
file instead.

**The residual exposure, stated rather than papered over:** `0600` excludes other UNIX users, and a
worker is not another user — it runs as the same uid as the server. A worker that knows the path
can read the file. What the file buys over the plist is that reading it requires knowing the path,
while `launchctl print` requires knowing nothing. Closing the gap properly means putting the fleet
somewhere genuinely outside a worker's reach — a second UNIX account — which is an architecture
decision, not a detail of this build-up.

### The viewer is an artefact, not a login item

Plan §2 asks for the server *and* viewer as login items. A viewer is a TUI: it needs a terminal, so
it cannot be a LaunchAgent the way the server is. More to the point, **it is no longer
load-bearing** — issue #302 measured the pinned build restoring hosted agents with no client ever
attached, which retired plan §5.4's "keep a viewer attached" workaround (#316 deletes the text). So
`viewer.command` is written into the prefix and left there; register it as a Login Item yourself if
you want the window. Nothing in the build-up depends on it, and the judge never asks about it.

---

## The blocks

`superlooper fleet` prints one line per property, in the order a build-up goes wrong — each is
upstream of the next, so fixing the first red line usually clears several.

**`host binary`** — the pinned release is installed in the fleet's own prefix and reports that
version. The pin is the build carrying the two upstream fixes the whole adoption rests on
(clientless restore, prompt submission). Fix: `vendor/herdr/build.sh`.

**`host login item`** — the server is a LaunchAgent, loaded in `gui/$UID`, with a PATH that is not
merely launchd's own four directories. The domain is the keychain rule: only the Aqua session has
an unlocked login keychain, and the host's panes inherit the server's session, so a daemon-hosted
server spawns panes whose `gh` and `claude` logins cannot read their own credentials — intermittently.
The PATH is read off the plist on disk, not off the running job: the plist is what the *next* login
will use, and a job hand-started with a good environment proves nothing about tomorrow.

**`host fence`** — a **tokenless** connection to the control socket is refused. `OPEN` is the
dangerous answer, not the mild one: an unfenced socket does not look broken from the runner's seat,
and every worker pane already carries the path to it. Silence is `UNREACHABLE`, never a fence.
REQUIRED before unattended fleet use (plan §3.1).

**`host config`** — `session.resume_agents_on_restore = true` and `version_check = false`, plus the
socket path the named session will actually bind, measured in bytes against the kernel's
`sun_path` limit of 104 (a long username plus a long session name is enough to make a server that
starts and then cannot bind, and the error it prints names a socket rather than a path).
`version_check = false` is not in the plan: a background version check nagging an unattended server
toward a newer build is how a fleet quietly stops being the build you fenced.

**`screen fallback`** — the local manifest override is installed. Read out of the pinned release's
own `src/detect/manifest.rs`: with **no rule matching**, the host answers `Idle` for a known agent
(`fallback_reason = "default_known_agent_idle_fallback"`). That is the reading the owner personally
watched it give a pane that was blocked on a dialog. The override is the vendor's own manifest,
snapshotted verbatim, plus one priority-1 catch-all that reports `unknown` — so it fires only where
nothing else did, which is exactly the fallback case. A local override **replaces** the manifest it
shadows, so the snapshot's version is recorded in the file and this block **warns** (never fails)
when the host's manifest has moved past it: the host's state is advisory by our own doctrine (plan
§5.2) and gates nothing, so a stale snapshot costs detection quality, not correctness. Re-run
`--install` to refresh it.

**`claude binary`** — the launch stack's own pin (issue #303), reused here because this issue's
amendment makes a green line the **cmux-independence assertion**: a fleet machine whose first
`claude` is cmux's bundled wrapper is one uninstall away from having no launcher at all.

**`fleet identity`** — the fleet rides its own subscription (c25, #300, #313). Three things, each
paid for by a measured failure:

1. the config dir is the **canonical** spelling. The credential namespace is `sha256` of the string
   *as written*, with no canonicalisation — #300 measured five spellings of one directory producing
   five namespaces, and the wrong one presents as auth-death rather than as an error;
2. `CLAUDE_SECURESTORAGE_CONFIG_DIR` is **not set at all**. Present-but-empty collapses the
   namespace back to the owner's unsuffixed one, so the fleet silently bills the owner's
   subscription and nothing anywhere errors;
3. `claude auth status` read twice — once under the fleet config dir, once under the owner's
   default — reports **different `orgId`s**. `orgId` is the billing entity, so that is the
   measurement; a matching email with different orgs is not the separation the ruling asked for
   either. The owner's dir being unreadable is not the fleet's failure and is not reported as one.
   The single-account half of that verdict is `identity.account_problem`, shared with the launch
   seam below — so it also refuses a reading that is `loggedIn: true` **on an API key**, which is
   what the real binary answers when `ANTHROPIC_API_KEY` is exported (measured 2026-08-04).

The dir this block judges is the one a **launch would actually assign**: it is read from
`SL_FLEET_CLAUDE_CONFIG_DIR` through the same function the spawn seam uses, and an unset variable
is a RED line rather than a default. A green identity for a directory no worker is pointed at would
be the same confident lie in reverse.

## The identity env contract at the spawn seam (issue #314)

Setting the fleet's config dir on the machine is what makes the block above load-bearing, and the
launch path is where it is enforced:

* **one canonical string, derived once.** `lib/launch.py` reads `SL_FLEET_CLAUDE_CONFIG_DIR`,
  canonicalises it (`~` expanded, trailing slash / `//` / `/./` collapsed, relative refused) and
  names it in every pane as `SL_CLAUDE_CONFIG_DIR`. Both spawn paths — `i<N>` and `d<N>` — share
  that one derivation, so they cannot drift into two spellings of one directory.
* **the pane's floor is what assigns it.** `bin/start-session.sh` exports `CLAUDE_CONFIG_DIR` from
  that value inside the session's own shell (the agent's variable belongs on the agent side of the
  boundary), then runs `python3 lib/identity.py --assert` in that same environment.
* **the assert is positive and strict** (owner ruling, 2026-08-04): logged in, on a subscription,
  never on an API key, and on the expected `orgId`. Anything else refuses the launch before the
  delivery sentinel — `state/identityfail/<id>.<token>`, exit 7 — so the launcher tears the pane
  down at once with a memo naming the account, rather than the runner reading the lane as live.
* **nothing is inherited.** A `CLAUDE_CONFIG_DIR` this session was not assigned, or any
  `CLAUDE_SECURESTORAGE_CONFIG_DIR` at all, refuses the launch with a memo instead of being
  silently scrubbed: somebody's environment is exporting it, and only a memo gets that found.
* **a machine that assigns nothing keeps today's behaviour** — no config dir is handed to a
  session, which runs on the machine's default Claude login. There is deliberately no default: an
  unprovisioned config dir parks a worker at the first-run theme picker, a screen no auth manifest
  covers and the host reports as idle. The account assert still runs.

The rationale for the strictness is capacity, not secrecy: the two Max accounts are separate
rate-limit pools and the runner's usage machinery reasons about one pool per lane, so a session on
the wrong pool makes lane assignment non-deterministic even though both accounts are the owner's.

**`fleet isolation`** — the fleet's named session, socket and prefix are its own, separate from the
host's default session. This block is explicit about its reach: **production on the other machine
is not observable from here**, and it says so in its own output rather than implying a
cross-machine guarantee it cannot make.

---

## Re-running it at a version bump

A bump is a deliberate event (`vendor/herdr/README.md` owns the procedure — re-apply the patch,
re-read the two invariants, re-run the negative test). Afterwards:

```sh
skills/superlooper/vendor/herdr/build.sh --force
launchctl kickstart -k gui/$UID/com.superlooper.session-host
superlooper fleet
```

The `host binary` and `host fence` blocks are what catch a bump that lost the patch. Neither is
something to assume: an unfenced socket answers.

## Why this doc is not in the published ops mirror

`ops_docs.OPS_DOCS` mirrors the docs an operator needs on an unfamiliar machine *while running the
loop*. The build-up is a one-time act performed **from the source checkout** — its central command,
`vendor/herdr/build.sh`, is not in the published payload at all. Mirroring a runbook whose first
step lives outside the mirror would be its own drift.

# The fence — a carried patch against herdr (issue #305)

Token auth on herdr's control socket. **Carried, not upstreamed** — ruled 2026-07-31
(`docs/HERDR-ADOPTION-PLAN.md` §3.1); the owner's reason was "we're not using it as designed", and
the accepted cost is re-applying this patch at every version bump. A bump is already a deliberate
event that re-runs acceptance, so this rides along with work that was happening anyway.

**REQUIRED before unattended fleet use.**

## What it does, and why it is a token

Stock herdr exposes every capability twice — CLI and a newline-JSON unix socket — because
machine-driving is a first-class supported mode. That is right for herdr's attended user and wrong
for us: superlooper runs untrusted worker sessions *inside the panes*. c27's verdict was that stock
herdr fails S8 — any worker can drive the whole fleet.

Env hygiene alone cannot fix it, and each of these was re-observed end-to-end while building the
patch (see `reports/i305.md`):

1. **The socket path is deterministic and injected into every pane.** A worker pane really does
   carry `HERDR_SOCKET_PATH`, `HERDR_ENV`, `HERDR_PANE_ID` — the host sets them itself, whatever
   the launcher passes.
2. **The protocol is plain newline-JSON.** A worker needs no `herdr` binary; ten lines of python
   and that path drive the fleet. Denying the *verb* is therefore not a fence.
3. **The socket's `0600` bit excludes other UNIX users, and a worker is not another user** — it
   runs as the same uid as the server.

So the enforcement point is the socket itself: an unauthenticated connection is refused in
`handle_connection` **before dispatch**, and therefore before any method body can run.

Token distribution (same ruling): the **runner and watchdog hold it**; **`d<N>` sl-debugger/Fixer
sessions receive it at spawn** (repair has to be able to drive herdr — revive, kill, read panes);
**`i<N>` workers never see it.** The token grants the *how*; the D13 supervised/unattended rails
still govern the *whether*.

## The one hole: the state report (issue #331, ruled 2026-08-04)

herdr learns which session id to `--resume` after a crash from ONE call — `pane.report_agent_session`,
fired by herdr's own hook script, carried byte-for-byte in the publishable payload (#307). That
script presents no token (upstream herdr has no token concept) and `i<N>` workers hold none, so the
fence as first built refused it: a fenced host captured no session ids at all and `persist.restore`
returned a bare shell. Silently — the sessions launch, run and work.

**The ruling: a method allowance.** A fenced host **admits that one method** from a tokenless
caller and refuses every other one before dispatch, unchanged. Chosen over a runner-side re-report
and over no hole at all, on the owner's stated grounds: keep herdr's built-in crash revive as the
working second layer, at the least owned code and surface.

Where it lives and what it is not:

| | |
|---|---|
| The decision | `auth::admit` — token **or** the one open method, one function, unit-tested |
| The method | `auth::STATE_REPORT_METHOD`, matched **exactly**: no trim, no case fold, no prefix |
| What it is NOT | a credential taught to the vendor hook. That script is untouched — the carry-verbatim rule from #307 stands, so any credential the host accepts is one the HOST resolves |
| Its width | one method. The report's own neighbours (`pane.report_agent`, `pane.report_metadata`) stay refused, and `tests/test_fence_token_auth.py` asserts that against a real server |

**Two properties worth not breaking if you rework this.**

1. **The allowance does not vary with `Expectation`.** A `Misconfigured` server admits the state
   report exactly as a `Token` one does, because the allowance is a statement about that method's
   blast radius rather than about the credential. It does not soften the fail-closed rule below:
   every verb an operator or the runner actually drives is still refused, so a typo is still loud.
2. **The envelope and the dispatcher must agree about which method a line names.** The line is
   parsed twice — once as `auth::Envelope` to judge it, once as `Request` to run it — and if those
   could ever disagree, the allowance would be a way to smuggle a different verb past the fence
   (judged as the harmless report, dispatched as something else). `the_admitted_method_is_the_one_
   that_would_actually_dispatch` in `auth.rs` is that check, over duplicate keys and hidden
   spellings; keep it.

**The accepted exposure, recorded rather than implied.** A tokenless caller can set the recorded
session ref of ANY pane, not only its own. What that buys is bounded by what the method does: it
writes in-memory bookkeeping that herdr later turns into `claude --resume <value>`. It is not a
shell string (`shell_quote` in `app/agent_resume.rs` escapes every argument) and control characters
are refused by `valid_session_id`, so the reach is "choose a revived pane's resume argument" — a
value beginning with `-` would be read by the agent as a flag — and not "run a command". Superlooper
bounds the rest by doctrine, not by more carried code: the host's bookkeeping is advisory (muscle,
never truth), the runner's minted-id ledger (#298) is the loop's identity truth, and a revived pane
passes the same state/liveness verification as any other before the loop trusts it.

## The pin

| | |
|---|---|
| Upstream | `https://github.com/ogulcancelik/herdr` |
| Version | **v0.8.0** (tag `v0.8.0`, commit `346411f`) |
| License | **Apache-2.0** — relicensed from AGPL-3.0-or-later in 0.8.0 (`CHANGELOG.md`). Homebrew's formula still said AGPL at 0.7.5; the tag is the authority. |
| Toolchain | Rust `1.96.1` (`rust-toolchain.toml`), plus `zig@0.15` — a build dep of the Homebrew formula for upstream issue #285 |

`#302` owns the pin decision itself; this file only records what the patch was written against.

## What the patch touches

Deliberately shaped to survive re-application. The bulk is a **new file**, which can never
conflict; the four edits to existing files are small and sit in stable places.

| File | Size | What |
|---|---|---|
| `src/api/auth.rs` | +452 | **New file.** Token resolution (env or file), the `Expectation` states, constant-time compare, the request `Envelope`, the `authorize()` / `admit()` decisions (#331's allowance included) + their unit tests. |
| `src/api/server.rs` | +33 | The gate in `handle_connection`, before dispatch. Four lines of decision, all of it delegated to `auth`. |
| `src/api/client.rs` | +25 | `serialize_request` attaches the token so every CLI path presents it transparently. |
| `src/pane.rs` | +25 | Scrub the token from inherited pane env; substitute the real token for the grant sentinel. |
| `src/api/mod.rs` | +1 | `pub mod auth;` |

Two design choices exist purely to keep the re-apply cheap, and should be preserved if you rework
this:

- **The check reads a minimal `auth::Envelope`, not the `Request` struct.** `Request` is built at
  ~260 literal sites upstream; adding a field there would make this patch enormous and would
  conflict on every release. `Envelope` ignores unknown members, so it parses any valid request and
  the hunk stays a few lines wide. It lives in `auth.rs` rather than `server.rs` for the same
  reason everything else does — a new file is a hunk that can never conflict, and its decisions can
  be unit-tested without standing up a socket.
- **The client attaches `auth` to the serialized JSON object**, for the same reason.

### The `pane.rs` hunk is load-bearing — do not "tidy" it

It does two things, and both are security-relevant.

**1. It scrubs the inherited token.** A pane inherits the **server's** environment
(`apply_pane_launch_env` only ever *adds*). A server holding the token in its own env would hand
that token to every pane it spawned, workers included — the fence defeated at birth. The
`env_remove` calls sit **before** the `extra` loop so inheritance can never grant the token.

**2. It substitutes the grant sentinel** — this is how a `d<N>` pane gets the token, and it exists
because of a measured asymmetry:

> On macOS a same-uid reader is **refused** another process's *environment* (`ps -Eww` returns
> nothing) but is **served** its *argv*. Both were measured while building this. `workspace create
> --env K=V` becomes argv — so a client passing the real token on that command line would publish
> the secret to exactly the workers the fence exists to keep out, for the duration of the call.

So the client sends only `HERDR_API_TOKEN=@superlooper-fence-grant` and the **server substitutes
the secret it already holds**. The token never leaves the server process. A *literal* value in
`--env HERDR_API_TOKEN=…` is **dropped**, not honoured: nothing a caller supplies may choose a
pane's credential.

The superlooper side therefore never handles the token at all — `session_host.py` has no code that
reads one. If you ever find yourself "simplifying" this by passing the token directly, you are
re-introducing the leak.

### Fail-closed states

`Expectation` has three values, and the third is deliberate:

| State | When | Behaviour |
|---|---|---|
| `Unconfigured` | neither env var set | stock herdr — everything allowed |
| `Token(..)` | a token resolved | the fence |
| `Misconfigured` | either variable present but empty / unreadable / non-UTF-8, or a token equal to the public grant sentinel | **every connection refused** |

`Misconfigured` does not collapse into `Unconfigured`, because an operator who set the file
variable asked for a fence; silently downgrading their typo to "no auth at all" would leave the
socket open at exactly the moment they believed they had closed it. Failing closed makes the
mistake loud instead of invisible.

## Configuring the server

The token is read once at server start, from either:

- `HERDR_API_TOKEN` — the token directly. **This is the recommended one.** Under this threat model
  the reader is the same uid, and macOS refuses a same-uid process another process's environment
  (measured: `ps -Eww` returns nothing at all), so the server's env is genuinely out of a worker's
  reach. Panes would inherit it — which is exactly what the `pane.rs` scrub prevents.
- `HERDR_API_TOKEN_FILE` — a path to a file containing the token. **Not preferred by default:**
  `0600` does not exclude the same uid, so a worker that discovers the path can simply read it.
  Use it only if you have somewhere genuinely outside a worker's reach to put the file.

Either way, a variable that is PRESENT BUT EMPTY (or unreadable, or non-UTF-8, or set to the public
grant sentinel) is `Misconfigured`, not "no auth" — see the fail-closed table above.

With **neither** set the patch is inert and the server is stock herdr. That is deliberate — it is
what lets upstream's own test suite pass unmodified, which is the cheap signal the procedure below
leans on. It also means **the fence being up is never something to assume**: prove it with the
probe.

Rotating the token means restarting the server — a deliberate event, by design.

## Build

```sh
git clone --depth 1 --branch v0.8.0 https://github.com/ogulcancelik/herdr herdr-src
cd herdr-src
git apply --3way ../0001-fence-token-auth-on-the-control-socket.patch
cargo build --release          # rustup honours rust-toolchain.toml (1.96.1)
```

`zig@0.15` must be on `PATH` if the link step needs it (`brew install zig@0.15`; Homebrew's own
formula prepends it).

## Re-applying at a version bump — the deliberate-event rule

**Every upgrade re-runs this verification. A bump that skips it is a bump that may have silently
removed the fence** — and an unfenced socket does not look broken from the runner's seat, because
it answers.

1. **Re-apply.** `git apply --3way` the patch against the new tag. If a hunk conflicts, read
   upstream to resolve it — that is explicitly allowed. (What is *not* allowed, per the ruling, is
   an upstream PR, issue or ask about auth.)
2. **Re-read the three invariants above** — the `pane.rs` ordering, that the gate still sits before
   `match request.method`, and that the `envelope`/`Request` agreement test still exists. A
   refactor upstream can move dispatch, or rename a method, without conflicting.
3. **Build**, then run upstream's own api tests as a smoke signal:
   ```sh
   cargo test --bin herdr api:: -- --test-threads=1
   ```
   Serial matters: several upstream plugin/graphics tests are timing-flaky — measured failing on
   an **unpatched** v0.8.0 tree too (`inactive_owner_cancels_idle_stream_and_dispatches_close`
   failed 1 run in 6 on pristine v0.8.0, and again 1 run in 4 while building #331, while the patched
   tree ran 292/292 green on 3 consecutive serial runs). A red there is not evidence
   against the patch; re-run it before investigating.
4. **Run the negative test against the built binary** — the check that actually matters:
   ```sh
   cd skills/superlooper
   SL_FENCE_HERDR=/path/to/patched/herdr python3 -m pytest tests/test_fence_token_auth.py -q
   ```
   That file is the fence's contract. Its opt-in test stands up a real patched server on an
   isolated socket and asserts all of it: a **tokenless** connection (the worker's position) is
   refused; a **tokened** one is served; the **state report** is admitted tokenless while **eleven
   other methods** — including the report's own neighbours — are not; near-miss spellings of the
   open method are refused; a **worker pane** does not inherit the token even though the server
   holds it; a **debugger pane** does receive it via the sentinel; and a caller-supplied literal
   token is ignored. Each half is needed — a socket that refused everyone would pass the first
   assertion and be a broken host, not a fence, and a socket that admitted everyone would pass the
   allowance assertion and be no fence at all.

   Both load-bearing halves were verified to BITE by mutation rather than assumed. Deleting the
   `pane.rs` hunk and rebuilding turns the pane assertions red with "a worker pane inherited the
   server's token". Reverting `admit` to `authorize` and rebuilding turns the allowance assertion
   red with `assert 'refused' == 'admitted'` (#331). The pane assertions read the environment by
   having the pane **report its own env**, never via `ps -Eww` — which returns nothing on macOS and
   so makes such a check silently vacuous. That mistake was made once here already; don't
   re-introduce it.
5. **Record the new pin** in the table above.

Two probes ask these questions in one call each, presenting no token (both must ask what a *worker*
would ask), and both treat silence as `UNREACHABLE` rather than as safety:

- `session_host.fence_probe(socket_path)` → `FENCED` / `OPEN` / `UNREACHABLE` — is anything refused?
- `session_host.state_report_probe(socket_path)` → `ADMITTED` / `REFUSED` / `UNREACHABLE` — is the
  ruled hole there? It writes nothing: it names a pane that cannot resolve and an empty agent label,
  so the host's own handler refuses it before touching state, and being refused *by the handler* is
  precisely the evidence that it got past the fence.

`doctor --stack`'s `host state capture` block reads the two together, which is the only way to tell
a fenced host with a working capture from one that admits the report because it admits everything.

## Not done here — the pre-flight is not yet wired

`fence_probe` exists and is tested, but **nothing calls it in production yet**, so an unfenced or
misconfigured host is not currently refused at launch. That is not an oversight in this issue: the
spawners were ported separately: #308 moved them onto `session_host.py` through `lib/launch.py`,
so there IS a launch path to refuse now, and wiring the preflight into it is issue #326.

Until then, **the fence being up is verified by running the acceptance check above**, not by the
runner. Filed as a follow-up: **#326** (`needs-owner`).

## Licensing

herdr v0.8.0 is Apache-2.0. This patch is a derivative work of it and carries the same license;
the header in `src/api/auth.rs` names the issue it came from. We build it for our own use and do
not redistribute the binary.

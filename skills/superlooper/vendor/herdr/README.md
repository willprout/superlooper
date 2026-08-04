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
| `src/api/auth.rs` | +157 | **New file.** Token resolution (env or file), constant-time compare, the pure `authorize()` decision + its unit tests. |
| `src/api/server.rs` | +42 | The gate in `handle_connection`, before dispatch, plus the tiny `AuthEnvelope`. |
| `src/api/client.rs` | +26 | `serialize_request` attaches the token so every CLI path presents it transparently. |
| `src/pane.rs` | +7 | `env_remove` the token vars from inherited pane env. |
| `src/api/mod.rs` | +1 | `pub mod auth;` |

Two design choices exist purely to keep the re-apply cheap, and should be preserved if you rework
this:

- **The check reads a minimal `AuthEnvelope`, not the `Request` struct.** `Request` is built at
  ~260 literal sites upstream; adding a field there would make this patch enormous and would
  conflict on every release. `AuthEnvelope` ignores unknown members, so it parses any valid
  request and the hunk stays a few lines wide.
- **The client attaches `auth` to the serialized JSON object**, for the same reason.

### The `pane.rs` hunk is load-bearing — do not "tidy" it

A pane inherits the **server's** environment (`apply_pane_launch_env` only ever *adds*). A server
holding the token in its own env would therefore hand that token to every pane it spawned, workers
included — the fence would be defeated at birth. The hunk scrubs it.

**Its position matters:** the `env_remove` calls sit **before** the `extra` loop, so inheritance
alone never grants the token while an explicit `--env HERDR_API_TOKEN=…` (the `d<N>` spawn path)
still lands. Moving them after the loop would silently break debugger spawns.

## Configuring the server

The token is read once at server start, from either:

- `HERDR_API_TOKEN_FILE` — a path to a file containing the token. **Preferred**: a value that was
  never in the environment cannot be inherited out of one.
- `HERDR_API_TOKEN` — the token directly.

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
2. **Re-read the two invariants above** — the `pane.rs` ordering, and that the gate still sits
   before `match request.method`. A refactor upstream can move dispatch without conflicting.
3. **Build**, then run upstream's own api tests as a smoke signal:
   ```sh
   cargo test --bin herdr api:: -- --test-threads=1
   ```
   Serial matters: several upstream plugin/graphics tests are flaky under parallel execution —
   observed failing on an **unpatched** v0.8.0 tree too, so a parallel red here is not evidence
   against the patch. Serial was 279/279 green with the patch applied.
4. **Run the negative test against the built binary** — the check that actually matters:
   ```sh
   cd skills/superlooper
   SL_FENCE_HERDR=/path/to/patched/herdr python3 -m pytest tests/test_fence_token_auth.py -q
   ```
   That file is the fence's contract. Its opt-in test stands up a real patched server on an
   isolated socket and asserts both halves: a **tokenless** connection (the worker's position) is
   refused, and a **tokened** one is served. Both are needed — a socket that refused everyone would
   pass the first assertion and be a broken host, not a fence.
5. **Record the new pin** in the table above.

`session_host.fence_probe(socket_path)` is the same question in one call, for the runner's own
pre-flight: it returns `FENCED`, `OPEN` or `UNREACHABLE`, presents no token (it must ask what a
*worker* would ask), and treats silence as `UNREACHABLE` rather than as safety.

## Licensing

herdr v0.8.0 is Apache-2.0. This patch is a derivative work of it and carries the same license;
the header in `src/api/auth.rs` names the issue it came from. We build it for our own use and do
not redistribute the binary.

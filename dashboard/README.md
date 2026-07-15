# command-center

The animated 16-bit airport dashboard over the **superlooper** issue-loop — one surface where
you read the state of every loop-adopted repo **and act on it**. Issues are flights flying a
traffic circuit around their repo's terminal; merges are landings on a Solari arrivals board;
every button is an existing mechanical verb (approve, expedite, flag, drop).

It is a **renderer with buttons**: a small read-only Python backend polls each adopted repo's
truth surfaces (GitHub via `gh`, the loop's journal and state dir), computes all semantics
server-side in tested pure Python, and serves one JSON snapshot + static files over
**localhost only**. A vanilla HTML/JS/canvas front-end draws it. No framework, no build step,
no AI anywhere in the dashboard.

If you already run superlooper, this is its face: point it at the repos you've adopted and open
a browser.

## Requirements

- **macOS or Linux with Python 3** (the runtime is stdlib-only — nothing to `pip install`). The
  launchd keep-alive below is macOS-only; everything else runs on either.
- the [`gh`](https://cli.github.com) CLI, **authenticated** (`gh auth login`) — the dashboard
  reads GitHub through it. Without it, the dashboard still runs and shows everything derivable
  from the local journal/state; only titles and the departures queue go quiet.
- **at least one repo adopted into the superlooper loop.** "Adopted" means the repo has a
  `.superlooper/config.json` and a state home under `~/.superlooper/` — that's what the loop
  runner creates. The dashboard reads those; it never creates them.

## Install & run

### 1 · Clone

```sh
git clone <this repo's URL> command-center
cd command-center
```

There is no build step and nothing to install — the runtime is the Python 3 already on your Mac.

### 2 · Configure

Copy the example and edit it. This file holds every per-machine fact (which repos, which port),
so it stays out of git — it's yours alone:

```sh
cp config.example.json config.json
```

Open `config.json` and set the one thing that's actually yours — **`repos`**:

```json
{
  "version": 1,
  "repos": [
    { "path": "~/code/your-adopted-repo" },
    { "path": "~/code/another-adopted-repo", "airline": "Widget Air" }
  ],
  "port": 8611,
  "poll_seconds": 2,
  "gh_poll_seconds": 30,
  "heartbeat_down_seconds": 300,
  "superlooper_cli": "~/.claude/skills/superlooper/bin/superlooper",
  "notify": { "imessage_to": null, "cmd": null },
  "fun": {
    "master": true,
    "solari": true,
    "solari_clack": true,
    "airlines": true,
    "living_clock": true,
    "corner_counter": true,
    "incident_sign": true
  }
}
```

Field by field:

- **`repos`** (the only field you *must* set) — a list of your adopted repos' **local checkout
  paths**. `~` is expanded. For each one, the dashboard reads that repo's own
  `.superlooper/config.json` to learn its slug (`owner/name`) and derives where the loop keeps
  its state (`~/.superlooper/<owner>__<name>/`). So you point at the *checkout*, not the state
  dir. Add an optional **`airline`** to rename a repo's airline on the field; omit it and the
  name is prettified from the repo (`command-center` → *Command Center*).
- **`port`** — the localhost port to serve on (default `8611`). Change it if that port is taken.
- **`poll_seconds`** — how often the browser refetches and the backend re-reads the local
  journal/state (default `2s` — the field feels live).
- **`gh_poll_seconds`** — the slower clock for `gh` calls, which are rate-limited (default `30s`).
- **`heartbeat_down_seconds`** — how stale the loop runner's heartbeat may get before the
  dashboard lights **RUNNER DOWN** (default `300s`).
- **`superlooper_cli`** — the path to your installed `superlooper` CLI, which the **Tidy** button
  runs locally to close the terminal windows of finished sessions (default
  `~/.claude/skills/superlooper/bin/superlooper`, `~` expanded). Change it only if your skill lives
  elsewhere. Tidy always asks first (it shows exactly which windows it will close) and only ever
  closes *finished* sessions — never one still building.
- **`notify`** — where the dashboard's one push (RUNNER DOWN) goes. `imessage_to` is a phone
  number/handle to text; `cmd` is a shell command to run instead. Both `null` by default — a
  fresh install nags no one.
- **`fun`** — the joy toggles. `master` gates them all; each mechanic (the Solari board and its
  clack, airline liveries, the living clock, the corner counter, the incident sign) has its own
  switch. **All on by default** — the animated airfield is the point. Dial one back only if you
  want to.

Every field except `repos` has a sensible default, so the smallest valid `config.json` is just
your repo list. If you mistype a key or type, the dashboard refuses to start and names the exact
offender — a typo is a clear error, never a dashboard quietly watching the wrong thing.

### 3 · Run

```sh
bin/command-center                    # reads ./config.json, serves http://127.0.0.1:8611
```

Open **http://127.0.0.1:8611** in a browser. `Ctrl-C` stops it. Point it at a different config
with `bin/command-center /path/to/config.json` (or `CC_CONFIG=/path/to/config.json`).

The server binds `127.0.0.1` **only** — it can write GitHub labels (approve, flag, drop), so it
is never reachable off your machine, by design.

### One command: bring up the dashboard **and** a repo's runner (`liftoff`)

The dashboard and the loop runner are two processes. `bin/liftoff` is the single command that
starts — or verifies already-running — **both**: this dashboard and one watched repo's runner.

**Run it inside a cmux tab, exactly like `superlooper run`.** It starts the dashboard in the
background (a localhost server needs no tab) and then hands *this* tab to the runner
(`superlooper run` in the foreground), so the runner lands in a visible cmux tab you can watch —
the one proven restart procedure. (There is deliberately no automated tab placement; your real tab
is the anchor.)

```sh
bin/liftoff                        # from the dashboard directory: reads ./config.json
bin/liftoff --repo owner/name      # several watched repos: name whose runner to start
bin/liftoff /path/to/config.json   # from anywhere: name the config (or set CC_CONFIG)
bin/liftoff --restart-dashboard    # the dashboard says STALE TOWER: restart it on the current build
```

- **Where it finds the config.** With no path argument, `liftoff` reads `./config.json` **relative
  to the directory you run it from** (or `$CC_CONFIG`), exactly like `bin/command-center`. So run it
  from the dashboard directory, or — to run it from anywhere — pass the config's path as the first
  argument or set `CC_CONFIG=/path/to/config.json`. If the file isn't there, `liftoff` names the
  absolute path it looked at and all three of those ways out, rather than leaving you guessing.
- **Idempotent — a second run double-starts neither.** It checks the dashboard's `port` (already
  serving ⇒ left alone) and the runner's pidfile (a live runner ⇒ left alone), then starts only
  what's missing. So `liftoff` is equally *"bring the pair up"* and *"confirm the pair is up."*
- **`--restart-dashboard` — when the dashboard posts a STALE TOWER notice.** A running server keeps
  the Python it loaded at boot, but it serves the static files from disk on every request. So when
  the loop merges an improvement to the dashboard's own face while your dashboard is up, your next
  reload shows the new buttons and its router has never heard of them — the tap comes back as a
  refusal. The server notices this itself and posts one amber NOTAM naming this command; the routine
  `liftoff` above can't help, because "already serving ⇒ left alone" is exactly its contract.
  This flag stops the running dashboard and starts it again on the current build, and it is
  **dashboard-only** — it never touches the runner and never claims the tab, so run it from wherever
  you happen to be. Nothing restarts on its own: the notice waits for you.
  It stops **exactly** the pid the dashboard reports for itself (never a name or pattern), and
  starts the replacement only once that process is confirmed gone — if it can't stop the old one, it
  starts nothing and tells you, rather than leaving two dashboards fighting over one port.
- **The runner start rides the config contract.** `liftoff` shells the engine's own documented
  `superlooper run` through the config's **`superlooper_cli`** — the dashboard names the engine's
  CLI, the engine never names the dashboard. Nothing engine-specific is hardcoded here.
- If you're **not** in a cmux tab, the dashboard still comes up, but the runner's own preflight
  refuses to start (it can't see a tab to open worker sessions in) with a message telling you to
  run inside a cmux tab — the same guard `superlooper run` has always had.

### 4 · Keep it always-on (optional, macOS)

The default is to run `bin/command-center` in a terminal you can watch. For an always-on
dashboard that survives logout and relaunches if it crashes, install a launchd keep-alive:

```sh
bin/install-launchd.sh                 # writes the LaunchAgent, prints the command to activate it
```

That writes `~/Library/LaunchAgents/com.command-center.plist` (keep-alive + start-at-login),
pointing at `bin/command-center` and your `config.json` with absolute paths, and logs to
`~/Library/Logs/command-center.log`. It does **not** start the job — it prints the one command:

```sh
launchctl load  ~/Library/LaunchAgents/com.command-center.plist    # activate now
launchctl unload ~/Library/LaunchAgents/com.command-center.plist   # stop it
```

Pass `--load` to activate it in the same step (`bin/install-launchd.sh --load`), or a config
path to keep alive a config other than `./config.json`
(`bin/install-launchd.sh /path/to/config.json`). The keep-alive still binds localhost only —
launchd changes only *when* the dashboard runs, never that bright line.

If the configured **`port` is already in use**, `bin/command-center` now exits with one friendly
line (`port … is already in use — change "port" in your config.json …`) instead of a stack trace.
A job that exits on every launch would otherwise relaunch on launchd's implicit ~10s default
(several times a minute, spamming the log), so the plist sets an explicit, longer
**`ThrottleInterval` = 30s**: launchd waits 30 seconds between relaunches, so a port conflict logs
that one line to `~/Library/Logs/command-center.log` at most once every 30s — a cool loop that
leaves you time to free the port or change it, never a runaway. Free the port (or edit `port`) and
the next relaunch comes up clean; no `unload`/`load` needed.

> **Note:** launchd runs with a minimal `PATH`, so the job uses your system `python3`
> (`/usr/bin/python3` — the stdlib-only runtime this is built for). If your only Python 3 is a
> Homebrew install not on that path, the job won't find it; run `bin/command-center` in a terminal
> instead, or point the plist's first `ProgramArguments` entry at your `python3`.

## Develop

The runtime is stdlib-only, but the test suite needs `pytest`. Create a dev virtualenv once
(it is git-ignored, so each fresh checkout / worktree makes its own):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest            # the whole suite must be green
```

Pure logic lives in tested `lib/` Python; the JS binds those values to pixels and stays
logic-free. Project rules for anyone (human or loop worker) building here: `CLAUDE.md`; the
settled design direction: `docs/DESIGN-RECORD.md`, with the handoff prototypes this build
recreates in `design/`.

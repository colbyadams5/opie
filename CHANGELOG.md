# Changelog

All notable changes to Opie are documented here. This project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **One-command install.** The entire setup is a single line pasted into Terminal —
  `/bin/bash -c "$(curl -fsSL …/install.sh)"` — plus a matching `uninstall.sh`. It
  downloads the code, generates the token, starts the relay, and adds an **Opie** app to
  `~/Applications`. No Apple Developer ID and no Gatekeeper prompt (`curl`/`git` downloads
  aren't quarantined), and everything runs on the Mac's built-in Python 3 — nothing else
  to install. Re-paste the command to update.
- **Browser control panel (`opie.panel`).** A localhost-only web app (standard library
  only) for setup, Start/Stop/Restart, autostart toggle, live logs, a test box, console
  reachability, phone-setup info, and update checks. Opening the **Opie** app brings the
  relay up automatically and opens the panel; Start/Restart report failures with the
  relay's actual error.
- **Native Mac app window.** Opening **Opie** now launches a real app window — a compiled
  Swift + WKWebView shell hosting the control panel, with a menu-bar item for
  Start/Stop/Restart and a proper app icon — instead of a browser tab. It's compiled
  locally at install time using the Command Line Tools that `git` already requires, so it
  keeps the zero-dependency, no-Gatekeeper model (no code signing, no pip). If `swiftc`
  isn't available it falls back automatically to opening the panel in your default browser.
- **Automatic updates.** When Opie runs from a Git clone it keeps itself current — the
  relay fast-forwards and re-execs into the latest code (in the background, so it never
  delays startup), plus a **Check for updates** button and an **Auto-update** toggle. New
  config key `auto_update` (default `true`).
- **Full Eos command coverage by voice.** Any command the console understands is reachable
  as a spoken phrase, not just the built-in patterns:
  - Bare action verbs on the current selection — `sneak`, `highlight`, `lowlight`, `mark`,
    `block`, `assert`, `capture`, `park` / `unpark`, `rem dim`, `make manual`, …
  - Bare parameters — `gobo 3`, `pan 50`, `iris 20`, `zoom 75`, `hue 180`, …
  - Bare levels — `full`, `out`, `home`, `at 50`, `75 percent`.
  - Action verbs attached to a target, in either order — `channel 5 sneak` **and**
    `sneak channel 5`, `group 3 park`, `channels 1 thru 8 rem dim`.
  - A **command-line fallback**: any otherwise-unrecognized phrase that contains a known
    Eos keyword goes straight to the Eos command line, so new verbs work without code
    changes. (The `destructive_policy` still gates dangerous verbs.)

### Changed
- **Removed the Tkinter window (`opie-gui`)** entirely in favor of the browser panel —
  this drops the Tk 8.6 requirement (the macOS system Python's Tk 8.5 crash, and the
  python.org/Homebrew detour, are gone).
- The relay runs as a **detached, log-captured subprocess** managed by the panel, with a
  fallback when a launchd autostart job is broken — so Start works reliably and crashes
  are visible (`relay.out.log`). Hardened `service` so a missing/erroring `launchctl`
  degrades gracefully instead of raising.

### Fixed
- **The control panel is responsive again — no more lag or delayed typing.** The panel
  had grown steadily slower the longer it stayed open, to the point where keystrokes
  arrived late. Three things caused it, all fixed:
  - *Every status poll shelled out.* `/api/state` ran `lsof`, `launchctl list` and two
    `git` calls and probed the relay over HTTP — on the request thread, on every poll,
    for both the page (every 2.5s) and the Mac app (every 3s). Those probes now run on
    a background sampler at the rate each fact can actually change, and the common
    "is the relay up?" question is answered by a loopback connect that costs nothing.
    A status request dropped from ~25ms to ~2ms on a fast Linux box, and `lsof` on a
    Mac is considerably slower than that. Start/Stop still report the truth
    immediately — changing something invalidates the sample it belongs to.
  - *The log view grew without limit.* The live tail appended to the page forever; after
    a show it held megabytes, and every new line re-laid-out the lot. In a browser
    benchmark against a 3MB log, the old panel blocked the main thread for 12.5 of 12
    seconds — i.e. permanently frozen — against 0ms now. The view keeps the most recent
    ~160,000 characters, the poll is capped per request, and the log box is
    layout-contained so it can't drag the rest of the page down with it.
  - *Polls stacked up.* `setInterval` fired whether or not the previous request had come
    back, so a slow answer left requests queueing behind each other. Polling now
    schedules the next request only when the last one finishes, backs off while the
    relay is quiet, slows to a heartbeat when the window is hidden, and only touches
    the DOM for values that actually changed.
- **Harmless client-disconnect tracebacks no longer look like crashes.** Siri and
  the panel's health poll routinely drop the socket the moment they have their reply;
  the stdlib HTTP server turned that into a `ConnectionResetError`/`BrokenPipeError`
  traceback that polluted the log and even surfaced inside the "Relay did not start"
  box. These are now swallowed, and the every-few-seconds `/health` poll is no longer
  logged — so the log shows the OSC traffic that actually matters.
- **Updates now reach the panel itself, and the freshest launch always wins.**
  The panel process used to live forever: "Check for updates" replaced the code on
  disk and restarted the relay, but the panel kept running old code in memory —
  so its (old) restart logic couldn't kill old relays, and reopening the Opie app
  just surrendered to the stale panel ("port already in use"). Now the panel
  re-execs itself onto the new code after an update, opening the Opie app replaces
  a previous panel instance instead of deferring to it, and the installer restarts
  (not merely "ensures") the relay so a reinstall always lands on fresh code.
- **Stop and the status dot now use ground truth: who holds the relay port.**
  HTTP probes can miss a relay bound only to the Tailscale IP, and process-name
  sweeps can miss relays from old installs — so the GUI could say "stopped" while
  the phone still controlled the console. The panel now asks the OS directly
  (`lsof`) which processes are LISTENING on the relay port on any interface:
  the status dot reports running if anyone holds the port, Stop kills every
  holder, and Stop's verification fails loudly (naming the surviving PID, or a
  launchd agent that refused to unload) instead of reporting false success.
- **Stop is verified, and works even without the launchd plist.** The Stop button
  now confirms the relay port actually went quiet and shows an error if anything
  still answers; `launchctl unload` falls back to `bootout` by label when the
  plist file is missing while the job is loaded (previously that combination made
  Stop silently fail and KeepAlive resurrected the relay).
- **Exactly one relay, and the GUI applies updates to it.** Three related bugs:
  if the configured bind address was taken, a second relay would shadow-bind
  `0.0.0.0` next to the first (macOS `SO_REUSEADDR`), leaving the phone and the
  panel talking to *different* relays — the relay now detects a sibling on the
  port and bows out. Stop/Restart now sweeps **every** relay process (launchd
  leftovers and orphans from old installs, not just the pid-file one). And
  "Check for updates" now compares the code on disk against what the running
  relay reports via the new `/version` endpoint, restarting it when it's stale —
  previously an already-downloaded update was reported as "up to date" while
  the relay kept running old code. Panel health checks also probe the configured
  bind address, so a relay bound only to the Tailscale IP no longer reads as
  stopped.
- **The panel can no longer report a healthy relay as stopped.** The clickable
  Opie.app launcher is now generated by `opie/service.py` and refreshed automatically
  (by the relay after auto-updates, by the panel at startup, and by `install.sh` at
  install). Previously the launcher was written once at install time — after the
  Tk-GUI→browser-panel update it still pointed at the removed `opie.gui`, so the panel
  never started while an orphaned browser tab kept showing "stopped" and silently
  dropped Save clicks. The page now states clearly when the *panel* is closed (vs the
  relay), every button surfaces a visible error instead of failing silently, and
  start/restart error details only quote the relay's *current* run — never a days-old
  log line.
- **"Go to cue" (and friends) now survive Siri's dictation quirks.** Dictation rarely
  produces the literal word "cue" — it writes `Q`, `que`, `queue`, or glues the number on
  (`Q10`), and sprinkles punctuation. All phrases are now normalized before matching:
  `Q/que/queue/Q10` → `cue`, `go 2 cue 5` → `go to cue 5`, number homophones after a
  target word (`cue to/for/won/ate` → `cue 2/4/1/8`), comma lists (`channels 1, 3 and 5`),
  `1-8`/`threw` → `thru`, `@` → `at`, `75%`, `snake` → `sneak`, `micro` → `macro`,
  `black out` → `blackout`, `sub master` → `submaster`, `high/low light`,
  `ram/rim dim` → `rem dim`, glued target numbers (`channel5`), and trailing
  punctuation/`please` are ignored.
- **Voice commands no longer interfere with other software cueing the console.** Eos runs
  un-scoped OSC from every sender on the *same* command line and selection, so the relay's
  traffic could interleave with — and corrupt — network cues sent by QLab, sound desks, etc.
  Everything the relay sends is now scoped to its own Eos user (`/eos/user/<n>/…`; new
  config key `OSC_USER`, default `0` = the console's invisible background user), and
  command-line strings use `/eos/newcmd` (clear-then-type) instead of `/eos/cmd` (append),
  so a half-typed leftover can never merge into the next command. Set `OSC_USER` to a
  positive number to run on a visible user, or `-1` for the old shared behaviour. Note:
  bare commands (`full`, `sneak`, `gobo 3`) now act on the selection last made *by voice*,
  not the operator's console selection.

## [0.1.0] — 2026-06-08

First packaged release.

### Added
- Installable Python package `opie` with console commands:
  - `opie` — run the HTTP→OSC relay
  - `opie-gui` — the **Opie Control** Tkinter panel
  - `opie-sniff` — loopback OSC sniffer for safe dry runs
- **Opie Control** desktop GUI: edit config, Start/Stop/Restart, toggle
  autostart at login (launchd), live log tail, send-a-command test box,
  console-reachability check, and iPhone Shortcut setup helper.
- `install.command` / `uninstall.command` double-click installers (venv + GUI).
- Config now lives in `~/Library/Application Support/Opie/config.json`
  (auto-created on first run with a freshly generated token); logs in
  `~/Library/Logs/Opie/`.
- GitHub Actions CI running the test suite.

### Changed
- Restructured the `relay/` scripts into an importable `opie` package with
  relative imports (removes the old `sys.path` hacks and the `parser` stdlib
  name clash).

### Security
- No secrets in the repo: the shipped `config.example.json` uses placeholders,
  and the live config (with your token) is kept outside the tree and gitignored.

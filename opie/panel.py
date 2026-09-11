"""
Opie Control — a browser-based control panel. **No Tkinter, no Tk required.**

Earlier versions used a Tkinter window, which needed Tk 8.6+ that the Mac's
built-in python3 lacks (Apple ships the deprecated Tk 8.5). This panel does the
same job with a tiny localhost-only web server built entirely from the standard
library, so it runs on *any* python3 — nothing to install.

  python3 -m opie.panel      # start the panel and open your browser
  opie-panel                 # same, after a pip install

Security: it binds to 127.0.0.1 only, so just the local user (you) can reach it;
the panel is unauthenticated by design (it's your own machine), while the relay's
phone-facing endpoint still requires the token.
"""

import argparse
import copy
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import __version__
from . import config as opie_config
from . import service
from . import update as opie_update

PANEL_HOST = "127.0.0.1"
DEFAULT_PANEL_PORT = 8766
POLICIES = ["block_all", "record_update", "allow_all"]

# How long a sampled answer is served before it's refreshed in the background.
# The panel is polled continuously (every 2.5s by the page, every 3s by the Mac
# app), and every probe below costs a fork+exec or a network round trip — so
# each one is sampled at the rate the underlying fact can actually change, not
# once per request. Nothing expensive is ever on the request path.
RUNNING_TTL = 2.5      # the deep "is the relay up" probe; the cheap one is live,
                       # so this only actually runs while the relay looks down
AUTOSTART_TTL = 15.0   # launchd agent: only changes when you toggle it here
REVISION_TTL = 30.0    # git revisions: only change on an update or a restart
SAMPLER_IDLE = 30.0    # stop sampling this long after the last read
LOG_CHUNK_MAX = 256 * 1024   # bytes of log served per poll (the rest follows)


class _Sampler:
    """A value kept fresh in the background, read instantly.

    A daemon thread re-samples every `ttl` seconds for as long as someone is
    reading, so get() hands back the most recent sample without forking a
    process or opening a socket. That keeps lsof, launchctl, git and network
    timeouts off the request path entirely — the panel is polled several times
    a second (the page and the Mac app both poll it), and those probes used to
    run once per poll, per client, stacking up whenever one ran long.

    The thread stops on its own once nothing has read for SAMPLER_IDLE, so a
    panel nobody is looking at costs nothing.
    """

    def __init__(self, fn, ttl):
        self._fn = fn
        self._ttl = ttl
        self._lock = threading.Lock()
        self._wake = threading.Event()    # "re-sample now"
        self._ready = threading.Event()   # "there is a value to serve"
        self._value = None
        self._version = 0
        self._thread = None
        self._last_read = 0.0

    def get(self, timeout=8.0):
        with self._lock:
            self._last_read = time.monotonic()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
        # Only ever waits for the very first sample, or for a fresh one right
        # after invalidate() — i.e. exactly when a stale answer would be wrong.
        if not self._ready.is_set():
            self._ready.wait(timeout)
        return self._value

    def invalidate(self):
        """We just changed the thing being sampled: drop the old answer and take
        a new one now, so nobody can be told about the world as it was."""
        with self._lock:
            self._version += 1
        self._ready.clear()
        self._wake.set()

    def _loop(self):
        while True:
            self._wake.clear()
            with self._lock:
                version = self._version
            try:
                value = self._fn()
            except Exception:  # noqa: BLE001 — a failed probe keeps the last answer
                value = self._value
            with self._lock:
                # An invalidate() landed while we were sampling: this answer
                # describes the old world, so don't publish it as the truth.
                current = version == self._version
                if current:
                    self._value = value
            if current:
                self._ready.set()
            if self._wake.wait(self._ttl):
                continue                   # invalidate() asked for a fresh one
            with self._lock:
                if time.monotonic() - self._last_read > SAMPLER_IDLE:
                    self._thread = None
                    return

# The revision THIS panel process loaded. After an update pulls new code onto
# disk, the running process is stale — it must re-exec to actually apply it.
_RUN_REVISION = opie_update.current_revision()


def _reexec_panel():
    """Replace this process with a fresh panel running the code now on disk."""
    args = list(sys.argv[1:])
    if "--no-browser" not in args:
        args.append("--no-browser")  # the user's existing tab reconnects itself
    os.execv(sys.executable, [sys.executable, "-m", "opie.panel", *args])


def _kill_other_panel(port):
    """
    Kill a previous panel instance holding the panel port. The freshest launch
    wins: a long-lived panel keeps running OLD code through every update, and
    'open Opie' must always serve the code that's actually installed. Only
    processes recognizably running the Opie panel are touched.
    """
    try:
        r = subprocess.run(["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=5)
        pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    killed = False
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            cmd = ""
        if "opie.panel" in cmd or "opie-panel" in cmd:
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
            except OSError:
                pass
    return killed

# Config keys the panel lets you edit (everything else is preserved untouched).
_TEXT_KEYS = ("NOMAD_IP", "EOS_RX_PORT", "HTTP_PORT", "BIND_ADDR", "LOG_FILE", "TOKEN",
              "OSC_USER")
_INT_KEYS = ("EOS_RX_PORT", "HTTP_PORT", "OSC_USER")


class Controller:
    """All the side-effecting actions, mirroring what the Tk GUI did."""

    def __init__(self, config_path):
        self.config_path = config_path
        opie_config.ensure_exists(config_path)
        self._cfg_lock = threading.Lock()
        self._cfg = None
        self._cfg_key = None
        self._running = _Sampler(self._probe_running, RUNNING_TTL)
        self._autostart = _Sampler(service.is_loaded, AUTOSTART_TTL)
        self._revisions = _Sampler(self._probe_revisions, REVISION_TTL)

    def _pidfile(self):
        return os.path.join(opie_config.app_support_dir(), "relay.pid")

    def _outlog(self):
        return os.path.join(opie_config.logs_dir(), "relay.out.log")

    # ---- config ----
    def load(self):
        """The config, re-read only when the file on disk actually changed.

        status() alone needs the config four or five times over, and the panel
        is polled constantly — so this is a parse per change, not per read.
        Callers get their own copy and can mutate it freely.
        """
        try:
            st = os.stat(self.config_path)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        with self._cfg_lock:
            if key is not None and key == self._cfg_key:
                return copy.deepcopy(self._cfg)
        cfg = opie_config.load(self.config_path)
        with self._cfg_lock:
            self._cfg_key, self._cfg = key, copy.deepcopy(cfg)
        return cfg

    def invalidate_status(self):
        """Forget the sampled status, so the next read reflects what we just did."""
        for probe in (self._running, self._autostart, self._revisions):
            probe.invalidate()

    def save(self, incoming):
        cfg = self.load()  # preserve unknown keys
        for k in _TEXT_KEYS:
            if k in incoming:
                cfg[k] = str(incoming[k]).strip()
        for k in _INT_KEYS:
            if k in incoming and str(incoming[k]).strip() != "":
                cfg[k] = int(incoming[k])
        if "destructive_policy" in incoming and incoming["destructive_policy"] in POLICIES:
            cfg["destructive_policy"] = incoming["destructive_policy"]
        if "auto_update" in incoming:
            cfg["auto_update"] = bool(incoming["auto_update"])
        for k in ("macro_map", "key_map"):
            if k in incoming:
                val = incoming[k]
                if isinstance(val, str):
                    val = json.loads(val or "{}")
                cfg[k] = val or {}
        opie_config.save(cfg, self.config_path)
        return cfg

    # ---- relay process ----
    # The relay runs as a DETACHED subprocess (its own session, so it survives the
    # panel closing) with stdout+stderr captured to relay.out.log, and its PID
    # recorded so we can stop it later. This doesn't depend on launchd, so a broken
    # launchd job can't block Start, and crashes are visible (relay.out.log).
    def _port(self):
        return int(self.load().get("HTTP_PORT", 8765))

    def _spawn(self):
        if self._health(self._port()):
            return  # already up — don't double-start
        try:
            os.makedirs(opie_config.logs_dir(), exist_ok=True)
        except OSError:
            pass
        env = dict(os.environ)
        root = opie_config.get_install_root()
        if root:
            env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        out = open(self._outlog(), "a")
        out.write(f"\n--- starting relay {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        out.flush()
        proc = subprocess.Popen(
            [sys.executable, "-m", "opie", "--config", self.config_path],
            stdout=out, stderr=subprocess.STDOUT,
            cwd=root or opie_config.app_support_dir(), env=env,
            start_new_session=True)  # detach so it outlives the panel
        try:
            with open(self._pidfile(), "w") as f:
                f.write(str(proc.pid))
        except OSError:
            pass

    @staticmethod
    def _relay_pids():
        """PIDs of EVERY running relay (`python -m opie`, not the panel),
        however it was started — pidfile, launchd, or an orphan left behind by
        an old install. A stale second relay shadow-binding the HTTP port is
        exactly the 'old relay still running in the background' bug."""
        try:
            r = subprocess.run(["pgrep", "-f", r"[Pp]ython[^ ]* -m opie($| )"],
                               capture_output=True, text=True)
            return [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        except (OSError, subprocess.SubprocessError, ValueError):
            return []

    def _port_listeners(self, port=None):
        """PIDs of every process LISTENING on the relay port, on ANY interface.
        This is the ground truth the phone experiences — immune to the blind
        spots of HTTP probes (relay bound only to the Tailscale IP) and of
        process-name matching (a relay started by an old install)."""
        port = port or self._port()
        try:
            r = subprocess.run(
                ["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5)
            return sorted({int(p) for p in r.stdout.split() if p.strip().isdigit()})
        except (OSError, subprocess.SubprocessError, ValueError):
            return []

    def _kill(self):
        # Union of every way we can find a relay: name pattern, pidfile, and —
        # decisively — whatever is actually holding the relay port.
        pids = set(self._relay_pids()) | set(self._port_listeners())
        try:
            with open(self._pidfile()) as f:
                pids.add(int(f.read().strip()))
        except (OSError, ValueError):
            pass
        pids.discard(os.getpid())
        for pid in pids:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except OSError:  # already gone (or not ours)
                    break
                time.sleep(0.4)
                if not self._pid_alive(pid):
                    break
        try:
            os.remove(self._pidfile())
        except OSError:
            pass

    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def wait_healthy(self, seconds=6.0):
        end = time.time() + seconds
        port = self._port()
        while time.time() < end:
            if self._health(port):
                return True
            time.sleep(0.4)
        return False

    def relay_log_tail(self, lines=40):
        try:
            with open(self._outlog(), "r", errors="replace") as f:
                rows = f.readlines()
        except OSError:
            return ""
        # Only report the current run: cut at the last "--- starting relay"
        # marker so a days-old startup line can't be shown as today's error.
        for i in range(len(rows) - 1, -1, -1):
            if rows[i].startswith("--- starting relay"):
                rows = rows[i:]
                break
        return "".join(rows[-lines:]).strip()

    def ensure_running(self):
        if not self._health(self._port()):
            self.control("start")

    def control(self, action):
        if action == "start":
            if service.is_loaded():
                service.restart()
                if not self.wait_healthy(5):
                    # launchd job is loaded but not coming up — drop it and run
                    # the relay directly so Start always works.
                    service.disable()
                    self._spawn()
            else:
                self._spawn()
        elif action == "stop":
            if service.is_loaded():
                service.disable()
            self._kill()
        elif action == "restart":
            if service.is_loaded():
                service.restart()
            else:
                self._kill(); time.sleep(0.3); self._spawn()
        elif action == "autostart_on":
            self._kill()
            service.enable(python_exe=sys.executable, config_path=self.config_path)
        elif action == "autostart_off":
            service.disable()
        else:
            raise ValueError(f"unknown action: {action}")
        # We just changed the very things the samplers cache — don't serve the
        # pre-action answer back to the page that asked for the change.
        self.invalidate_status()

    # ---- status / info ----
    def _hosts(self, cfg=None):
        """Where the relay might be listening: loopback first (covers the
        0.0.0.0 and fallback cases), then the configured bind address (covers a
        relay bound ONLY to e.g. the Tailscale IP, which loopback can't see)."""
        hosts = ["127.0.0.1"]
        cfg = cfg or self.load()
        bind = str(cfg.get("BIND_ADDR", "")).strip()
        if bind and bind not in ("0.0.0.0", "::", "127.0.0.1", "localhost"):
            hosts.append(bind)
        return hosts

    def _health(self, port, timeout=0.6):
        for host in self._hosts():
            try:
                with urllib.request.urlopen(f"http://{host}:{port}/health",
                                            timeout=timeout) as r:
                    if (r.status == 200
                            and r.read().decode("utf-8", "replace").strip() == "ok"):
                        return True
            except Exception:  # noqa: BLE001
                continue
        return False

    @staticmethod
    def _port_live(port):
        """Is something listening on the relay port, right now? Loopback only,
        and therefore instant: the kernel either completes the connection or
        refuses it without any network involved. This is the same ground truth
        lsof gives us — someone holds the port — for a millionth of the cost,
        which is why it can run on every request while the rest is sampled."""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            return False

    # ---- sampled probes (see _Sampler: these run off the request path) ----
    def _probe_running(self):
        """The thorough answer, for when loopback says nothing is there: a relay
        bound only to e.g. the Tailscale IP is still running as far as the phone
        is concerned. lsof and the HTTP probes only ever run down here."""
        port = self._port()
        if self._port_live(port):
            return True
        for host in self._hosts()[1:]:          # loopback already answered
            try:
                with socket.create_connection((host, port), timeout=0.6):
                    return True
            except OSError:
                continue
        return bool(self._port_listeners(port)) or self._health(port)

    def _probe_revisions(self):
        """(revision on disk, revision the running relay loaded)."""
        return (opie_update.current_revision() or "",
                self.relay_info().get("revision", ""))

    def relay_info(self, port=None):
        """What the RUNNING relay reports about itself via /version — {} if it's
        unreachable or predates the endpoint. The code on disk may be newer than
        what the relay loaded at startup; callers compare the two."""
        port = port or self._port()
        for host in self._hosts():
            try:
                with urllib.request.urlopen(f"http://{host}:{port}/version",
                                            timeout=0.6) as r:
                    if r.status == 200:
                        return json.loads(r.read().decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue
        return {}

    def status(self):
        """A snapshot for the UI. Everything expensive here is sampled in the
        background (see _Sampler), so this returns without touching a
        subprocess or the network — the page can poll it as often as it likes."""
        cfg = self.load()
        port = int(cfg.get("HTTP_PORT", 8765))
        bind = str(cfg.get("BIND_ADDR", "")).strip()
        host = bind if bind and bind != "0.0.0.0" else socket.gethostname()
        revision, relay_revision = self._revisions.get() or ("", "")
        # Live where it's free, sampled where it isn't: a relay on loopback
        # shows up the instant it comes up, and only the awkward cases (bound
        # elsewhere, or genuinely down) fall back to the background sample.
        running = self._port_live(port) or bool(self._running.get())
        return {
            # Running = someone holds the relay port (what the phone sees),
            # not merely "an HTTP probe got through" — a relay bound only to
            # the Tailscale IP must never be reported as stopped.
            "running": running,
            "autostart": bool(self._autostart.get()),
            "version": __version__,
            "revision": revision,
            "relay_revision": relay_revision,
            "port": port,
            "nomad_ip": cfg.get("NOMAD_IP", ""),
            "eos_port": cfg.get("EOS_RX_PORT", 8000),
            "phone_url": f"http://{host}:{port}/command",
            "token": cfg.get("TOKEN", ""),
        }

    def test(self, phrase):
        cfg = self.load()
        port = int(cfg.get("HTTP_PORT", 8765))
        token = str(cfg.get("TOKEN", ""))
        last = ""
        for host in self._hosts(cfg):
            try:
                req = urllib.request.Request(
                    f"http://{host}:{port}/command", data=phrase.encode("utf-8"),
                    headers={"X-Token": token, "Content-Type": "text/plain"})
                with urllib.request.urlopen(req, timeout=3) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001
                last = str(e)
        return 0, f"{last}  (is the relay running?)"

    def ping(self, ip):
        try:
            r = subprocess.run(["ping", "-c", "1", "-t", "1", ip],
                               capture_output=True, timeout=4)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def logs(self, pos):
        # The captured console log (relay.out.log) holds both normal logging and
        # any startup crash, so it's the most useful thing to show.
        path = self._outlog()
        try:
            size = os.path.getsize(path)
            if size < pos:
                pos = 0
            with open(path, "r", errors="replace") as f:
                f.seek(pos)
                # Bounded: one runaway burst of relay output must not turn a
                # routine poll into a multi-megabyte response. The tail that
                # doesn't fit arrives on the next poll a moment later.
                data = f.read(LOG_CHUNK_MAX)
                return data, f.tell()
        except OSError:
            return "", pos


def make_handler(ctrl):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Every response here is a small JSON blob on the loopback interface;
        # without this, Nagle + delayed ACK can sit on one for tens of ms.
        disable_nagle_algorithm = True

        def log_message(self, *a):
            pass  # quiet

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, (bytes, bytearray)):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj))

        def _body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return {}

        def do_GET(self):
            p = urlparse(self.path)
            if p.path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif p.path == "/api/state":
                cfg = ctrl.load()
                editable = {k: cfg.get(k, "") for k in _TEXT_KEYS}
                editable["OSC_USER"] = cfg.get("OSC_USER", 0)
                editable["destructive_policy"] = cfg.get("destructive_policy", "record_update")
                editable["auto_update"] = bool(cfg.get("auto_update", True))
                editable["macro_map"] = cfg.get("macro_map", {})
                editable["key_map"] = cfg.get("key_map", {})
                self._json({"config": editable, "status": ctrl.status(),
                            "policies": POLICIES})
            elif p.path == "/api/logs":
                pos = int((parse_qs(p.query).get("pos", ["0"])[0]) or 0)
                text, newpos = ctrl.logs(pos)
                self._json({"text": text, "pos": newpos})
            elif p.path == "/api/ping":
                ip = parse_qs(p.query).get("ip", [""])[0]
                self._json({"ok": ctrl.ping(ip), "ip": ip})
            elif p.path == "/api/token":
                self._json({"token": opie_config.generate_token()})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            p = urlparse(self.path)
            data = self._body()
            try:
                if p.path == "/api/config":
                    ctrl.save(data)
                    if data.get("restart"):
                        ctrl.control("restart")
                    self._json({"ok": True})
                elif p.path == "/api/control":
                    action = data.get("action", "")
                    ctrl.control(action)
                    resp = {"ok": True}
                    if action in ("start", "restart"):
                        resp["running"] = ctrl.wait_healthy(6)
                        if not resp["running"]:
                            resp["error"] = (ctrl.relay_log_tail()
                                             or "The relay did not start. Check that "
                                                "python3 works and the port is free.")
                    elif action == "stop":
                        # Verify by ground truth: NOTHING may still be listening
                        # on the relay port, on any interface. A survivor here is
                        # exactly "GUI says stopped but the phone still works".
                        time.sleep(0.6)
                        leftover = ctrl._port_listeners()
                        resp["running"] = bool(leftover) or ctrl._health(ctrl._port())
                        if resp["running"]:
                            resp["error"] = ("Stop failed: process "
                                             f"{leftover or '(unknown)'} still holds "
                                             "the relay port. Stop it manually or "
                                             "reboot, then tell us how this happened.")
                        elif service.is_loaded():
                            # The agent survived disable(): launchd's KeepAlive
                            # WILL resurrect the relay in a few seconds even
                            # though the port is quiet right now.
                            resp["running"] = True
                            resp["error"] = ("Stop failed: the autostart agent "
                                             "could not be unloaded, so the relay "
                                             "will restart itself. Run "
                                             "'launchctl bootout gui/$UID/com.opie.relay' "
                                             "in Terminal and report this.")
                    self._json(resp)
                elif p.path == "/api/test":
                    code, body = ctrl.test(str(data.get("phrase", "")))
                    self._json({"code": code, "body": body})
                elif p.path == "/api/update":
                    status, msg = opie_update.check_and_update()
                    ctrl.invalidate_status()  # the revisions just moved
                    if status == opie_update.UPDATED:
                        ctrl.control("restart")
                    elif status == opie_update.CURRENT:
                        # The code on disk can already be newer than what the
                        # running relay loaded at startup ("updating from the
                        # GUI didn't work" = this case) — restart to apply it.
                        src = opie_update.current_revision() or ""
                        run = ctrl.relay_info().get("revision", "")
                        if src and run and src != run:
                            ctrl.control("restart")
                            status = opie_update.UPDATED
                            msg = (f"Restarted the relay onto {src} "
                                   f"(it was still running {run}).")
                    # The panel process itself may be older than the code now on
                    # disk — re-exec into it once this response has gone out, or
                    # the NEXT update would still be handled by stale code.
                    disk = opie_update.current_revision()
                    if disk and _RUN_REVISION and disk != _RUN_REVISION:
                        msg += "  Reloading the control panel onto the new code…"
                        threading.Timer(0.8, _reexec_panel).start()
                    self._json({"status": status, "message": msg})
                else:
                    self._json({"error": "not found"}, 404)
            except (ValueError, json.JSONDecodeError) as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 500)

    return Handler


def _open(url):
    try:
        if not webbrowser.open(url):
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def main():
    ap = argparse.ArgumentParser(description="Opie browser control panel")
    ap.add_argument("--config", default=opie_config.default_config_path())
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("OPIE_PANEL_PORT", DEFAULT_PANEL_PORT)))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    ctrl = Controller(args.config)
    try:
        service.ensure_app_launcher()  # keep the clickable Opie.app current
    except Exception:  # noqa: BLE001
        pass
    url = f"http://{PANEL_HOST}:{args.port}/"
    try:
        server = ThreadingHTTPServer((PANEL_HOST, args.port), make_handler(ctrl))
    except OSError:
        # A previous panel holds the port — replace it (it may be running old
        # code; the freshest launch must win) and retry once.
        server = None
        if _kill_other_panel(args.port):
            time.sleep(0.8)
            try:
                server = ThreadingHTTPServer((PANEL_HOST, args.port), make_handler(ctrl))
            except OSError:
                server = None
        if server is None:
            print(f"Opie Control is already running at {url}")
            if not args.no_browser:
                _open(url)
            return 0

    # Opening Opie should make it work: bring the relay up if it isn't already.
    threading.Thread(target=ctrl.ensure_running, daemon=True).start()

    print(f"Opie Control: {url}  (Ctrl-C to quit)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: _open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()
    return 0


# --------------------------------------------------------------------------- #
# The single-page UI (vanilla HTML/CSS/JS, no external assets — works offline). #
# --------------------------------------------------------------------------- #
PAGE = """<!doctype html>
<html lang="en" data-theme="dark"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opie Control</title>
<style>
:root{
  color-scheme:dark;
  --bg:oklch(0.17 0.012 275); --surface:oklch(0.21 0.014 275); --surface-2:oklch(0.25 0.016 275);
  --line:oklch(0.32 0.02 275); --text:oklch(0.96 0.006 275); --dim:oklch(0.72 0.014 275);
  --faint:oklch(0.55 0.014 275); --accent:oklch(0.62 0.20 285); --accent-2:oklch(0.66 0.20 305);
  --ring:oklch(0.62 0.20 285 / .45); --ok:oklch(0.78 0.16 150); --warn:oklch(0.82 0.15 85);
  --err:oklch(0.68 0.21 25);
  --grad:linear-gradient(155deg, var(--accent), var(--accent-2));
  --mono:ui-monospace,"SF Mono",Menlo,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  --ease:cubic-bezier(.22,1,.36,1);
}
html[data-theme="light"]{
  color-scheme:light;
  --bg:oklch(0.97 0.006 275); --surface:oklch(0.995 0.003 275); --surface-2:oklch(0.95 0.008 275);
  --line:oklch(0.88 0.012 275); --text:oklch(0.24 0.02 275); --dim:oklch(0.46 0.02 275);
  --faint:oklch(0.58 0.02 275); --accent:oklch(0.52 0.20 285); --accent-2:oklch(0.55 0.20 305);
  --ring:oklch(0.52 0.20 285 / .35); --ok:oklch(0.56 0.16 150); --warn:oklch(0.60 0.15 75);
  --err:oklch(0.55 0.22 25);
}
*{box-sizing:border-box}
html,body{margin:0}
body{font:15px/1.55 var(--sans);background:var(--bg);color:var(--text);
  -webkit-font-smoothing:antialiased;padding-bottom:52px}
::selection{background:var(--ring)}

.top{display:flex;align-items:center;gap:13px;max-width:760px;margin:0 auto;padding:18px 22px}
.mark{width:30px;height:30px;flex:0 0 auto;filter:drop-shadow(0 1px 5px oklch(0.62 0.2 285 / .4))}
.brand{display:flex;flex-direction:column;line-height:1.12}
.brand b{font-size:16px;font-weight:650;letter-spacing:-.01em}
.brand span{font-size:11.5px;color:var(--faint)}
.top .sp{flex:1}
.ver{font:11.5px/1.45 var(--mono);color:var(--faint);text-align:right;max-width:240px}
.iconbtn{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;
  border-radius:10px;border:1px solid var(--line);background:var(--surface);color:var(--dim);
  cursor:pointer;transition:background .18s var(--ease),color .18s var(--ease)}
.iconbtn:hover{background:var(--surface-2);color:var(--text)}
.iconbtn:focus-visible{outline:none;box-shadow:0 0 0 3px var(--ring)}
.iconbtn svg{width:17px;height:17px;flex:none}
html[data-theme="dark"] .i-moon{display:none}
html[data-theme="light"] .i-sun{display:none}

main{max-width:760px;margin:0 auto;padding:0 22px;display:flex;flex-direction:column;gap:18px}

.hero{position:relative;overflow:hidden;border-radius:18px;border:1px solid var(--line);
  padding:22px 24px;background:
    radial-gradient(130% 150% at 100% -10%, oklch(0.62 0.2 285 / .12), transparent 55%),
    var(--surface)}
.hero-top{display:flex;align-items:center;gap:16px}
.signal{display:flex;align-items:flex-end;gap:4px;height:34px;width:48px;flex:0 0 auto}
.signal i{flex:1;height:100%;border-radius:3px;background:var(--faint);transform:scaleY(.26);
  transform-origin:bottom;transition:transform .3s var(--ease),background .3s var(--ease)}
.hero[data-state="running"] .signal i,.hero[data-state="pulse"] .signal i{
  background:var(--ok);animation:eq 1.1s var(--ease) infinite}
.hero[data-state="pulse"] .signal i{animation-duration:.42s}
.signal i:nth-child(1){animation-delay:0s} .signal i:nth-child(2){animation-delay:.16s}
.signal i:nth-child(3){animation-delay:.34s} .signal i:nth-child(4){animation-delay:.1s}
.signal i:nth-child(5){animation-delay:.26s}
@keyframes eq{0%,100%{transform:scaleY(.28)}50%{transform:scaleY(1)}}
.state{display:flex;flex-direction:column;gap:3px;min-width:0}
.state b{font-size:26px;font-weight:680;letter-spacing:-.02em;line-height:1}
.hero[data-state="running"] .state b,.hero[data-state="pulse"] .state b{color:var(--ok)}
.state small{font-size:12.5px;color:var(--dim)}
.tag{font:11px/1 var(--sans);font-weight:600;padding:5px 10px;border-radius:999px;
  border:1px solid var(--line);color:var(--dim);background:var(--surface-2);white-space:nowrap}
.tag.on{color:var(--ok);border-color:oklch(0.78 0.16 150 / .4)}
.hl{margin-top:16px;font:12.5px/1.5 var(--mono);color:var(--dim);background:var(--bg);
  border:1px solid var(--line);border-radius:10px;padding:10px 12px;word-break:break-all}
.hl b{color:var(--text);font-weight:600} .hl .ar{color:var(--faint);padding:0 4px}
.drift{display:none;margin-top:11px;font-size:12.5px;line-height:1.5;color:var(--warn)}
.controls{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin-top:16px}
.relayerr{display:none;margin-top:14px;font:12px/1.5 var(--mono);color:var(--err);white-space:pre-wrap;
  background:oklch(0.68 0.21 25 / .09);border:1px solid oklch(0.68 0.21 25 / .32);
  border-radius:10px;padding:11px 13px;max-height:200px;overflow:auto}

.panel{border-radius:16px;border:1px solid var(--line);background:var(--surface);padding:20px 22px}
.phead{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin:0 0 4px}
.phead h2{font-size:15px;font-weight:620;margin:0;letter-spacing:-.01em}
.phead p{margin:0;font-size:12.5px;color:var(--faint)}
.grp{font:11px/1 var(--sans);font-weight:650;text-transform:uppercase;letter-spacing:.07em;
  color:var(--faint);margin:22px 0 13px;padding-top:17px;border-top:1px solid var(--line)}
.grp.first{border-top:0;padding-top:0;margin-top:18px}

.field{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}
.field>label{font-size:13px;font-weight:550;color:var(--text)}
.hint{font-size:12px;color:var(--faint);font-weight:400}
.note{font-size:12.5px;line-height:1.6;color:var(--dim);margin:-2px 0 6px}
.cols{display:flex;gap:14px;flex-wrap:wrap}
.cols>.field{flex:1;min-width:150px}
input,select,textarea{width:100%;padding:9px 11px;font:14px var(--sans);color:var(--text);
  background:var(--surface-2);border:1px solid var(--line);border-radius:10px;
  transition:border-color .15s var(--ease),box-shadow .15s var(--ease);
  -webkit-appearance:none;appearance:none}
input:hover,select:hover,textarea:hover{border-color:var(--faint)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
input.mono{font-family:var(--mono);font-size:13px}
textarea{min-height:96px;resize:vertical;font-family:var(--mono);font-size:13px;line-height:1.5}
select{padding-right:32px;cursor:pointer;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238a8a96' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 11px center}

.switch-row{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:6px 0}
.switch-row .lab b{display:block;font-size:13px;font-weight:550}
.switch-row .lab span{font-size:12px;color:var(--faint)}
.switch{position:relative;width:44px;height:26px;flex:0 0 auto}
.switch input{position:absolute;inset:0;opacity:0;margin:0;cursor:pointer;z-index:1}
.switch .track{position:absolute;inset:0;border-radius:999px;background:var(--surface-2);
  border:1px solid var(--line);transition:background .2s var(--ease),border-color .2s var(--ease)}
.switch .track::after{content:"";position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;
  background:var(--dim);transition:transform .22s var(--ease),background .2s var(--ease)}
.switch input:checked+.track{background:var(--grad);border-color:transparent}
.switch input:checked+.track::after{transform:translateX(18px);background:#fff}
.switch input:focus-visible+.track{box-shadow:0 0 0 3px var(--ring)}

button{font:14px var(--sans);font-weight:600;padding:9px 16px;border-radius:10px;border:1px solid var(--line);
  background:var(--surface-2);color:var(--text);cursor:pointer;
  transition:background .15s var(--ease),border-color .15s var(--ease),filter .15s,transform .04s;
  -webkit-appearance:none;appearance:none}
button:hover{background:var(--line)}
button:active{transform:translateY(1px)}
button:focus-visible{outline:none;box-shadow:0 0 0 3px var(--ring)}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:var(--grad);color:#fff;border-color:transparent;
  box-shadow:0 1px 14px oklch(0.62 0.2 285 / .35)}
button.primary:hover{filter:brightness(1.07)}
button.sm{padding:7px 12px;font-size:13px}

.bar{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.grow{flex:1}
.feedback{font-size:12.5px;font-weight:550;min-height:1.1em}
.feedback.ok{color:var(--ok)} .feedback.err{color:var(--err)} .feedback.muted{color:var(--faint)}
code{font:12.5px var(--mono);background:var(--surface-2);border:1px solid var(--line);
  padding:2px 6px;border-radius:6px;color:var(--text)}

.cmdline{display:flex;gap:9px;align-items:stretch}
.cmdline input{font-family:var(--mono)}
.result{margin-top:13px;font:13px/1.5 var(--mono);min-height:1.2em;word-break:break-word;color:var(--dim)}
.result.ok{color:var(--ok)} .result.err{color:var(--err)}
.wiring{margin-top:18px;font-size:13px;color:var(--dim);line-height:1.85}
.wiring b{color:var(--text)}

.logbar{display:flex;align-items:center;gap:9px;margin-bottom:12px}
.live{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;color:var(--faint);margin-left:auto}
.live .blip{position:relative;width:7px;height:7px;border-radius:50%;background:var(--ok)}
.live .blip::after{content:"";position:absolute;inset:0;border-radius:50%;background:var(--ok);
  animation:blip 1.9s var(--ease) infinite}
@keyframes blip{0%{transform:scale(1);opacity:.5}70%{transform:scale(3);opacity:0}100%{opacity:0}}
/* contain: the log grows to thousands of lines — without this, every appended
   line re-runs layout for the whole page, which is what makes typing lag. */
pre#log{margin:0;background:oklch(0.13 0.012 275);color:oklch(0.88 0.012 275);border:1px solid var(--line);
  border-radius:12px;padding:14px;height:280px;overflow:auto;white-space:pre-wrap;font:12.5px/1.55 var(--mono);
  contain:content}
html[data-theme="light"] pre#log{background:oklch(0.23 0.015 275);color:oklch(0.93 0.01 275)}

@media (max-width:560px){
  .cols{flex-direction:column;gap:0}
  .state b{font-size:22px}
  .ver{display:none}
}
@media (prefers-reduced-motion:reduce){
  .signal i{animation:none!important}
  .live .blip{animation:none}
}
</style></head>
<body>
<div class="top">
  <svg class="mark" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <defs><linearGradient id="og" x1="32" y1="6" x2="32" y2="58" gradientUnits="userSpaceOnUse">
      <stop stop-color="#6366F1"/><stop offset="1" stop-color="#8B5CF6"/></linearGradient></defs>
    <rect x="6" y="6" width="52" height="52" rx="15" fill="url(#og)"/>
    <g stroke="#fff" stroke-opacity=".28" stroke-width="2.4" stroke-linecap="round">
      <path d="M20 21V43"/><path d="M32 21V43"/><path d="M44 21V43"/></g>
    <g fill="#fff">
      <rect x="13.5" y="35" width="13" height="5.5" rx="2.75"/>
      <rect x="25.5" y="23" width="13" height="5.5" rx="2.75"/>
      <rect x="37.5" y="30" width="13" height="5.5" rx="2.75"/></g>
  </svg>
  <div class="brand"><b>Opie</b><span>Eos voice relay</span></div>
  <div class="sp"></div>
  <div id="ver" class="ver"></div>
  <button id="themebtn" class="iconbtn" title="Toggle light / dark" aria-label="Toggle theme" onclick="toggleTheme()">
    <svg class="i-moon" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
    <svg class="i-sun" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
  </button>
</div>

<main>

  <section class="hero" id="hero" data-state="stopped">
    <div class="hero-top">
      <div class="signal"><i></i><i></i><i></i><i></i><i></i></div>
      <div class="state"><b id="status">Connecting…</b><small id="substatus"></small></div>
      <div class="sp" style="flex:1"></div>
      <span id="autopill" class="tag" style="display:none"></span>
    </div>
    <div id="url" class="hl"></div>
    <div id="drift" class="drift"></div>
    <div class="controls">
      <button class="primary" onclick="ctl('start')">Start relay</button>
      <button onclick="ctl('stop')">Stop</button>
      <button onclick="ctl('restart')">Restart</button>
    </div>
    <div class="switch-row" style="margin-top:8px">
      <div class="lab"><b>Autostart at login</b><span>Bring the relay up automatically after a reboot.</span></div>
      <label class="switch"><input type="checkbox" id="autostart" onchange="ctl(this.checked?'autostart_on':'autostart_off')"><span class="track"></span></label>
    </div>
    <pre id="relayerr" class="relayerr"></pre>
  </section>

  <section class="panel">
    <div class="phead"><h2>Setup</h2><p>Where the relay sends, and how it behaves.</p></div>

    <div class="grp first">Console</div>
    <div class="cols">
      <div class="field"><label>Console IP <span class="hint">Nomad / Eos</span></label><input id="NOMAD_IP" class="mono" placeholder="10.0.0.5"></div>
      <div class="field"><label>OSC RX port</label><input id="EOS_RX_PORT" class="mono"></div>
    </div>

    <div class="grp">Relay</div>
    <div class="cols">
      <div class="field"><label>HTTP port</label><input id="HTTP_PORT" class="mono"></div>
      <div class="field"><label>Bind address <span class="hint">blank = all</span></label><input id="BIND_ADDR" class="mono"></div>
    </div>
    <div class="field"><label>Log file <span class="hint">blank = default</span></label><input id="LOG_FILE" class="mono"></div>

    <div class="grp">Safety &amp; behavior</div>
    <div class="cols">
      <div class="field"><label>Destructive policy</label><select id="destructive_policy"></select></div>
      <div class="field"><label>Eos OSC user <span class="hint">0 = background</span></label><input id="OSC_USER" class="mono"></div>
    </div>
    <p class="note">Voice commands run as their own Eos user so they never collide with cues other
      software (QLab, sound desks) sends to the console. <code>0</code> = the invisible background
      user, a positive number = that user's command line, <code>-1</code> = share the console's OSC user.</p>
    <div class="switch-row">
      <div class="lab"><b>Auto-update</b><span>Pull and apply new Opie versions automatically.</span></div>
      <label class="switch"><input type="checkbox" id="auto_update"><span class="track"></span></label>
    </div>

    <div class="grp">Security token</div>
    <div class="field"><label>Shared token <span class="hint">the iPhone Shortcut sends this</span></label>
      <div class="bar">
        <input id="TOKEN" class="mono grow">
        <button class="sm" onclick="genToken()">Generate</button>
        <button class="sm" onclick="copy($('TOKEN').value,this)">Copy</button>
      </div>
    </div>

    <div class="grp">Word maps</div>
    <div class="cols">
      <div class="field"><label>Macro map <span class="hint">word &rarr; macro #</span></label><textarea id="macro_map"></textarea></div>
      <div class="field"><label>Key map <span class="hint">word &rarr; key name</span></label><textarea id="key_map"></textarea></div>
    </div>

    <div class="bar" style="margin-top:8px">
      <button class="primary" onclick="save(false)">Save</button>
      <button onclick="save(true)">Save &amp; restart</button>
      <span id="saved" class="feedback"></span>
    </div>
  </section>

  <section class="panel">
    <div class="phead"><h2>Test &amp; phone setup</h2><p>Send a phrase, then wire the Shortcut.</p></div>
    <div class="cmdline">
      <input id="phrase" class="grow" value="channel 5 at full" onkeydown="if(event.key==='Enter')sendTest()">
      <button class="primary" onclick="sendTest()">Send</button>
    </div>
    <div id="testres" class="result"></div>
    <div class="bar" style="margin-top:13px">
      <button class="sm" onclick="pingConsole()">Check console reachable</button>
      <button class="sm" onclick="checkUpdate()">Check for updates</button>
      <span id="misc" class="feedback muted"></span>
    </div>
    <div class="wiring">
      <b>iPhone Shortcut</b>, one “Get Contents of URL” action:<br>
      URL <code id="purl"></code><br>
      Method <code>POST</code>, header <code>X-Token</code> = <code id="ptok"></code>, body = the dictated phrase
      <button class="sm" style="margin-left:8px" onclick="copy($('ptok').textContent,this)">Copy token</button>
    </div>
  </section>

  <section class="panel">
    <div class="phead"><h2>Log</h2><p>Live tail of the relay, this run only.</p></div>
    <div class="logbar">
      <button class="sm" id="pausebtn" onclick="togglePause()">Pause</button>
      <button class="sm" onclick="clearLog()">Clear</button>
      <span class="live"><span class="blip"></span> streaming</span>
    </div>
    <pre id="log"></pre>
  </section>

</main>
<script>
let logpos=0, paused=false, loaded=false;
const $=id=>document.getElementById(id);
const FIELDS=['NOMAD_IP','EOS_RX_PORT','HTTP_PORT','BIND_ADDR','LOG_FILE','TOKEN','OSC_USER'];
const PANEL_DOWN='The Opie panel app is not running (the relay may be fine). Open the Opie app, then try again.';
const STATE_MS=2500;          // how often status is refreshed while visible
const LOG_MS=1000;            // log poll floor, backed off while nothing arrives
const LOG_IDLE_MS=4000;       // ...up to this, so a quiet relay costs nothing
const HIDDEN_MS=15000;        // background tab: stay current, stop hammering
const LOG_KEEP=160000;        // characters of log kept in the DOM

async function api(path,opts){ const r=await fetch(path,opts); return r.json(); }
function esc(t){ return String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function copy(t,btn){ navigator.clipboard.writeText(t).catch(()=>{});
  if(btn){ const o=btn.textContent; btn.textContent='Copied'; setTimeout(()=>{btn.textContent=o;},1200); } }
function flash(el,cls,msg,keep){ el.className='feedback '+cls; el.textContent=msg;
  if(!keep) setTimeout(()=>{ el.textContent=''; },2600); }

// Status is redrawn every couple of seconds. Writing a property that already
// holds the same value still costs style, layout and paint work on the main
// thread — which is the thread your keystrokes are waiting on — so every write
// below is guarded by a comparison.
function setText(el,v){ if(el.textContent!==v) el.textContent=v; }
function setHTML(el,v){ if(el.innerHTML!==v) el.innerHTML=v; }
function setStyle(el,k,v){ if(el.style[k]!==v) el.style[k]=v; }
function setClass(el,v){ if(el.className!==v) el.className=v; }
function setData(el,k,v){ if(el.dataset[k]!==v) el.dataset[k]=v; }

function applyTheme(t){ document.documentElement.setAttribute('data-theme',t); try{localStorage.setItem('opie-theme',t);}catch(e){} }
function toggleTheme(){ applyTheme(document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark'); }
(function(){ let t='dark'; try{ t=localStorage.getItem('opie-theme')||'dark'; }catch(e){} document.documentElement.setAttribute('data-theme',t); })();

function draw(d){
  const s=d.status;
  setData($('hero'),'state', s.running?'running':'stopped');
  setText($('status'), s.running?'Running':'Stopped');
  setText($('substatus'), s.running?'Listening for phrases.':'Relay is not running. Press Start.');
  const ap=$('autopill');
  if(s.autostart){ setStyle(ap,'display',''); setClass(ap,'tag on'); setText(ap,'autostart on'); }
  else { setStyle(ap,'display','none'); }
  setText($('ver'),'Opie '+s.version+(s.revision?(' · '+s.revision):''));
  setHTML($('url'),'Relay <b>http://localhost:'+s.port+'</b><span class="ar">&rarr;</span>OSC '
    +esc(s.nomad_ip||'?')+':'+s.eos_port);
  const drift=$('drift');
  if(s.running && s.relay_revision && s.revision && s.relay_revision!==s.revision){
    setStyle(drift,'display','block');
    setText(drift,'Relay is still running '+s.relay_revision+', but '+s.revision
      +' is installed. Click “Check for updates” to apply it.');
  } else { setStyle(drift,'display','none'); }
  setText($('purl'),s.phone_url); setText($('ptok'),s.token);
  if($('autostart').checked!==s.autostart) $('autostart').checked=s.autostart;
  if(!loaded){ // fill the form once so we don't clobber edits
    const c=d.config;
    for(const k of FIELDS) $(k).value=c[k]??'';
    const sel=$('destructive_policy'); sel.innerHTML='';
    for(const p of d.policies){ const o=document.createElement('option'); o.value=o.textContent=p; if(p===c.destructive_policy)o.selected=true; sel.appendChild(o); }
    $('auto_update').checked=!!c.auto_update;
    $('macro_map').value=JSON.stringify(c.macro_map||{},null,2);
    $('key_map').value=JSON.stringify(c.key_map||{},null,2);
    loaded=true;
  }
}

// One /api/state request at a time. Timers used to fire regardless of whether
// the previous request had come back, so a slow answer left requests stacking
// up behind each other; a request asked for while one is in flight is folded
// into a single follow-up instead.
let stateBusy=false, stateAgain=false;
async function refresh(){
  if(stateBusy){ stateAgain=true; return; }
  stateBusy=true;
  try{
    const d=await api('/api/state');
    draw(d);
  }catch(e){
    setData($('hero'),'state','stopped');
    setText($('status'),'Panel closed');
    setText($('substatus'),'Open the Opie app to reconnect.');
  }finally{ stateBusy=false; }
  if(stateAgain){ stateAgain=false; await refresh(); }
}
async function save(restart){
  let macro,key;
  try{ macro=JSON.parse($('macro_map').value||'{}'); key=JSON.parse($('key_map').value||'{}'); }
  catch(e){ flash($('saved'),'err','Macro / Key map must be valid JSON'); return; }
  const cfg={ destructive_policy:$('destructive_policy').value, auto_update:$('auto_update').checked,
              macro_map:macro, key_map:key, restart:restart };
  for(const k of FIELDS) cfg[k]=$(k).value;
  let r;
  try{ r=await api('/api/config',{method:'POST',body:JSON.stringify(cfg)}); }
  catch(e){ flash($('saved'),'err','Not saved. '+PANEL_DOWN); return; }
  if(!r.ok){ flash($('saved'),'err',r.error||'Save failed'); return; }
  flash($('saved'),'ok',restart?'Saved, restarting…':'Saved.');
}
async function ctl(action){
  const err=$('relayerr'); err.style.display='none'; err.textContent='';
  let r;
  try{ r=await api('/api/control',{method:'POST',body:JSON.stringify({action})}); }
  catch(e){ err.textContent=PANEL_DOWN; err.style.display='block'; refresh(); return; }
  if(r && r.error){
    err.textContent=(action==='stop'?'':'Relay did not start:\\n\\n')+r.error;
    err.style.display='block';
  } else if((action==='start'||action==='restart') && r && r.running===false){
    err.textContent='Relay did not start (no further detail in the logs).';
    err.style.display='block';
  }
  refresh();
}
async function genToken(){
  try{ const r=await api('/api/token'); $('TOKEN').value=r.token; flash($('saved'),'muted','New token generated. Save to apply.',true); }
  catch(e){ flash($('saved'),'err',PANEL_DOWN); }
}
async function sendTest(){
  const hero=$('hero'), prev=hero.dataset.state;
  $('testres').className='result'; $('testres').textContent='Sending…';
  let r; try{ r=await api('/api/test',{method:'POST',body:JSON.stringify({phrase:$('phrase').value})}); }
  catch(e){ $('testres').className='result err'; $('testres').textContent=PANEL_DOWN; return; }
  const ok=r.code===200;
  $('testres').className='result '+(ok?'ok':'err');
  $('testres').textContent=(ok?'✓ ':'✗ '+(r.code||'')+' ')+r.body;
  if(ok && prev==='running'){ hero.dataset.state='pulse'; setTimeout(()=>{ hero.dataset.state=prev; },420); }
}
async function pingConsole(){
  flash($('misc'),'muted','Pinging…',true);
  let r; try{ r=await api('/api/ping?ip='+encodeURIComponent($('NOMAD_IP').value)); }
  catch(e){ flash($('misc'),'err',PANEL_DOWN,true); return; }
  if(r.ok) flash($('misc'),'ok','✓ '+r.ip+' is reachable',true);
  else flash($('misc'),'err','✗ '+r.ip+' did not respond',true);
}
async function checkUpdate(){
  flash($('misc'),'muted','Checking…',true);
  let r; try{ r=await api('/api/update',{method:'POST',body:'{}'}); }
  catch(e){ flash($('misc'),'err',PANEL_DOWN,true); return; }
  flash($('misc'),'muted',r.message,true); if(r.status==='updated') setTimeout(refresh,800);
}

// The log is kept in a string and trimmed to the last LOG_KEEP characters. It
// used to be appended to the <pre> forever: after a busy show the node held
// megabytes, and every append re-laid-out all of it — the page got slower the
// longer it stayed open, which is exactly the lag people hit.
let logBuf='', logBusy=false;
function clearLog(){ logBuf=''; $('log').textContent=''; }
function togglePause(){ paused=!paused; $('pausebtn').textContent=paused?'Resume':'Pause';
  if(!paused){ logWait=LOG_MS; pollLogs(); } }
async function pollLogs(){
  if(paused || logBusy) return false;
  logBusy=true;
  try{
    const r=await api('/api/logs?pos='+logpos);
    logpos=r.pos;
    if(!r.text) return false;
    const el=$('log');
    // Read the scroll position BEFORE writing: interleaving reads and writes
    // forces the browser to re-run layout mid-frame.
    const atBottom=el.scrollHeight-el.scrollTop-el.clientHeight<40;
    logBuf+=r.text;
    if(logBuf.length>LOG_KEEP){
      const cut=logBuf.indexOf('\\n', logBuf.length-LOG_KEEP);
      logBuf=logBuf.slice(cut<0?logBuf.length-LOG_KEEP:cut+1);
    }
    el.textContent=logBuf;
    if(atBottom) el.scrollTop=el.scrollHeight;
    return true;
  }catch(e){ return false; }
  finally{ logBusy=false; }
}

// Self-scheduling loops rather than setInterval: the next poll is booked only
// once the previous one has finished, and a hidden window drops to a slow
// heartbeat instead of polling a panel nobody is looking at.
let logWait=LOG_MS;
async function stateLoop(){
  try{ await refresh(); }catch(e){}
  setTimeout(stateLoop, document.hidden?HIDDEN_MS:STATE_MS);
}
async function logLoop(){
  let got=false;
  try{ got=await pollLogs(); }catch(e){}
  logWait = got ? LOG_MS : Math.min(Math.round(logWait*1.5), LOG_IDLE_MS);
  setTimeout(logLoop, document.hidden?HIDDEN_MS:logWait);
}
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden){ logWait=LOG_MS; refresh(); pollLogs(); }
});

stateLoop(); logLoop();
</script>
</body></html>"""


if __name__ == "__main__":
    sys.exit(main())

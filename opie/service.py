"""
launchd control for the Opie relay (macOS). Powers the GUI's "autostart at login"
toggle and start/stop/restart. Pure standard library.

A per-user LaunchAgent (~/Library/LaunchAgents/com.opie.relay.plist) runs the relay
at login and keeps it alive. The plist is rendered from a bundled template so it
points at the right Python (the venv), the user's config, and a log file.
"""

import hashlib
import os
import shlex
import shutil
import subprocess
import sys

from . import __version__
from . import config as opie_config

LABEL = opie_config.PLIST_LABEL  # "com.opie.relay"


def plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _template_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "resources", "com.opie.relay.plist.tmpl")


def _uid() -> int:
    return os.getuid()


def render(python_exe=None, config_path=None, stdout_log=None, stderr_log=None,
           workdir=None, pythonpath=None) -> str:
    """Fill the plist template with concrete paths.

    When Opie runs from source (no pip install), `pythonpath` points launchd at
    the source tree so `python3 -m opie` resolves. Defaults to the recorded
    install root; None (pip install) omits the env block entirely.
    """
    python_exe = python_exe or sys.executable
    config_path = config_path or opie_config.default_config_path()
    logs = opie_config.logs_dir()
    stdout_log = stdout_log or os.path.join(logs, "relay.out.log")
    stderr_log = stderr_log or os.path.join(logs, "relay.err.log")
    workdir = workdir or opie_config.app_support_dir()
    if pythonpath is None:
        pythonpath = opie_config.get_install_root()
    env_block = ""
    if pythonpath:
        env_block = ("    <key>EnvironmentVariables</key>\n"
                     "    <dict>\n"
                     "        <key>PYTHONPATH</key>\n"
                     f"        <string>{pythonpath}</string>\n"
                     "    </dict>\n")
    with open(_template_path(), "r", encoding="utf-8") as f:
        tmpl = f.read()
    subs = {
        "LABEL": LABEL,
        "PYTHON": python_exe,
        "CONFIG": config_path,
        "STDOUT": stdout_log,
        "STDERR": stderr_log,
        "WORKDIR": workdir,
        "ENV_BLOCK": env_block,
    }
    for k, v in subs.items():
        tmpl = tmpl.replace("{{" + k + "}}", v)
    return tmpl


def install(**kw) -> str:
    """Write (or overwrite) the LaunchAgent plist. Returns its path."""
    path = plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(opie_config.logs_dir(), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(**kw))
    return path


def _launchctl(*args):
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        # launchctl missing or unusable (non-macOS, locked-down env): act like a
        # clean failure so callers (is_loaded/enable/disable) degrade instead of
        # raising. The relay still runs fine as a plain subprocess.
        return subprocess.CompletedProcess(args, 1, "", "launchctl unavailable")


def load():
    return _launchctl("load", "-w", plist_path())


def unload():
    # `unload` needs the plist file on disk; if it's gone while the job is
    # still loaded, fall back to bootout by label (works from the live job) —
    # otherwise Stop silently fails and KeepAlive resurrects the relay.
    r = _launchctl("unload", "-w", plist_path())
    if is_loaded():
        r = _launchctl("bootout", f"gui/{_uid()}/{LABEL}")
    return r


def restart():
    """Restart the running agent in place (picks up config edits)."""
    return _launchctl("kickstart", "-k", f"gui/{_uid()}/{LABEL}")


def is_installed() -> bool:
    return os.path.exists(plist_path())


def is_loaded() -> bool:
    r = _launchctl("list")
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        # columns: PID  Status  Label  (tab-separated); last field is the label
        if line.rsplit("\t", 1)[-1].strip() == LABEL:
            return True
    return False


def enable(**kw):
    """Autostart ON: install the plist and load it (starts now + at every login)."""
    install(**kw)
    return load()


def disable():
    """Autostart OFF: unload the agent and remove its plist."""
    res = unload()
    p = plist_path()
    if os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            pass
    return res


# --------------------------------------------------------------------------- #
# The clickable ~/Applications/Opie.app                                        #
# --------------------------------------------------------------------------- #

def app_path() -> str:
    return os.path.expanduser("~/Applications/Opie.app")


_APP_LAUNCHER = """#!/bin/bash
# Opie.app — opens the browser control panel (Tk not required).
# Regenerated automatically by Opie itself (opie/service.py) so a code update
# can never strand an outdated launcher (an old one kept pointing at the
# removed Tk GUI, which made the panel silently unlaunchable).
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 "$(command -v python3 2>/dev/null)"; do
  [ -n "$c" ] && [ -x "$c" ] && { PY="$c"; break; }
done
if [ -z "$PY" ]; then
  osascript >/dev/null 2>&1 \\
    -e 'display dialog "Opie needs Python 3, which comes with Apple'"'"'s Command Line Tools.\\n\\nOpen Terminal and run:  xcode-select --install" with title "Opie" buttons {"OK"} default button "OK" with icon caution'
  exit 1
fi
{PYTHONPATH_LINE}nohup "$PY" -m opie.panel >/dev/null 2>&1 &
exit 0
"""

_APP_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Opie</string>
  <key>CFBundleDisplayName</key><string>Opie</string>
  <key>CFBundleIdentifier</key><string>com.opie.control</string>
  <key>CFBundleExecutable</key><string>Opie</string>
  <key>CFBundleIconFile</key><string>Opie</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>{VERSION}</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- The native shell loads the panel over http://127.0.0.1 — allow loopback
       (ATS blocks plain http by default, which would leave a blank window). -->
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
"""


def _write_if_changed(path, content, mode=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == content:
                return False
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)
    return True


def _swift_source_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "resources", "OpieApp.swift")


def _icon_source_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "resources", "Opie.icns")


def _find_swiftc():
    """Path to swiftc from the active toolchain (Command Line Tools), or None."""
    try:
        r = subprocess.run(["xcrun", "-f", "swiftc"],
                           capture_output=True, text=True, timeout=10)
        p = r.stdout.strip()
        if r.returncode == 0 and p and os.path.exists(p):
            return p
    except (OSError, subprocess.SubprocessError):
        pass
    return shutil.which("swiftc")


# Mach-O magic numbers (thin + universal, both byte orders).
_MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
                b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def _is_macho(path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) in _MACHO_MAGIC
    except OSError:
        return False


def _read_text(path) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _install_icon(res_dir):
    """Copy the bundled .icns into the app's Resources (idempotent, best-effort).

    Returns True if the icon was written or replaced, False if it was already
    current or unavailable. The caller uses this to know when to bust the macOS
    icon cache (see _refresh_launchservices)."""
    src = _icon_source_path()
    if not os.path.exists(src):
        return False
    try:
        with open(src, "rb") as f:
            data = f.read()
    except OSError:
        return False
    dst = os.path.join(res_dir, "Opie.icns")
    try:
        with open(dst, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    try:
        with open(dst, "wb") as f:
            f.write(data)
    except OSError:
        return False
    return True


def _refresh_launchservices(app):
    """Bust the macOS icon/metadata cache after the .app's contents change.

    Finder, the Dock, and LaunchServices cache an app's icon keyed on the bundle
    directory's mtime. A fresh Opie.icns dropped *inside* Contents/Resources never
    changes the .app root mtime, so an originally icon-less bundle (Opie shipped a
    bash launcher before it had an icon) keeps showing the generic placeholder
    forever. Bumping the bundle mtime and re-registering forces the new icon to
    take. Best-effort: this is cosmetic, never fatal."""
    try:
        os.utime(app, None)  # touch the bundle root so the cache sees a change
    except OSError:
        pass
    lsregister = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                  "LaunchServices.framework/Support/lsregister")
    if os.path.exists(lsregister):
        try:
            subprocess.run([lsregister, "-f", app],
                           capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass


def _build_native_app(macos_dir):
    """
    Compile the native WKWebView shell (resources/OpieApp.swift) into
    Contents/MacOS/Opie with swiftc from the Command Line Tools.

    Returns (ok, rebuilt): ok is True when a valid binary is in place, rebuilt is
    True only when this call actually recompiled it (so the caller can bust the
    icon cache on a real change). On failure ok is False and the caller writes the
    bash fallback launcher.

    A content hash gates the compile so it only runs when the Swift source or the
    toolchain changed, or the binary is missing — ensure_app_launcher() is called
    on every relay/panel start, so an unconditional compile would be wasteful.
    Compiled locally => the binary is never quarantined => no Gatekeeper prompt
    and no code signing required.
    """
    swiftc = _find_swiftc()
    if not swiftc:
        return (False, False)
    src = _read_text(_swift_source_path())
    if not src:
        return (False, False)
    bin_path = os.path.join(macos_dir, "Opie")
    stamp_path = os.path.join(macos_dir, ".opie_build")
    stamp = hashlib.sha256(("v2\x00" + swiftc + "\x00" + src).encode("utf-8")).hexdigest()
    if _is_macho(bin_path) and _read_text(stamp_path).strip() == stamp:
        return (True, False)  # already built from this exact source + toolchain
    build_dir = os.path.join(opie_config.app_support_dir(), "build")
    try:
        os.makedirs(build_dir, exist_ok=True)
        # swiftc only allows top-level statements in a file named main.swift.
        swift_file = os.path.join(build_dir, "main.swift")
        with open(swift_file, "w", encoding="utf-8") as f:
            f.write(src)
        out_tmp = os.path.join(build_dir, f"Opie.{os.getpid()}.bin")
        # Compile THROUGH xcrun, not the bare swiftc path: on macOS 26 invoking
        # swiftc directly fails with "unable to load standard library for target
        # …" because it can't locate the SDK without the SDKROOT/DEVELOPER_DIR
        # that xcrun sets up. xcrun ships with the Command Line Tools, so it's
        # present whenever swiftc is; fall back to the bare path if it somehow
        # isn't (e.g. a hand-installed toolchain).
        prefix = ["/usr/bin/xcrun", "swiftc"] if os.path.exists("/usr/bin/xcrun") else [swiftc]
        r = subprocess.run(
            [*prefix, "-o", out_tmp, swift_file,
             "-framework", "Cocoa", "-framework", "WebKit"],
            capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return (False, False)
    if r.returncode != 0 or not _is_macho(out_tmp):
        try:
            os.remove(out_tmp)
        except OSError:
            pass
        return (False, False)
    try:
        os.replace(out_tmp, bin_path)
        os.chmod(bin_path, 0o755)
        with open(stamp_path, "w", encoding="utf-8") as f:
            f.write(stamp)
    except OSError:
        return (False, False)
    return (True, True)


def ensure_app_launcher():
    """
    (Re)write ~/Applications/Opie.app so it always matches the installed code.

    Prefers a native compiled window (resources/OpieApp.swift -> a WKWebView
    hosting the control panel, with a menu-bar item for Start/Stop/Restart). If
    swiftc is unavailable or the build fails, falls back to a bash launcher that
    opens the panel in the default browser — either way the app always works.

    install.sh creates the .app once; this keeps it current across auto-updates.
    Only acts on macOS and only when the .app already exists or Opie runs from a
    recorded install root (the one-line installer); pip users who never had the
    app don't get one foisted on them.
    """
    if sys.platform != "darwin":
        return None
    root = opie_config.get_install_root()
    app = app_path()
    if not root and not os.path.isdir(app):
        return None
    macos_dir = os.path.join(app, "Contents", "MacOS")
    res_dir = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos_dir, exist_ok=True)
    os.makedirs(res_dir, exist_ok=True)

    # Track whether anything inside the bundle actually changed; only then do we
    # bust the macOS icon cache (lsregister on every panel start would thrash).
    changed = _install_icon(res_dir)
    if _write_if_changed(os.path.join(app, "Contents", "Info.plist"),
                         _APP_PLIST.replace("{VERSION}", __version__)):
        changed = True

    ok, rebuilt = _build_native_app(macos_dir)
    if ok:
        changed = changed or rebuilt
    else:
        # Fallback: the browser launcher (still zero-dependency, always works).
        py_line = f"export PYTHONPATH={shlex.quote(root)}\n" if root else ""
        if _write_if_changed(os.path.join(macos_dir, "Opie"),
                             _APP_LAUNCHER.replace("{PYTHONPATH_LINE}", py_line),
                             mode=0o755):
            changed = True
        try:  # drop a stale stamp so a later run with swiftc rebuilds cleanly
            os.remove(os.path.join(macos_dir, ".opie_build"))
        except OSError:
            pass

    if changed:
        _refresh_launchservices(app)
    return app

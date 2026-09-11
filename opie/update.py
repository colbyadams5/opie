"""
Self-update for Opie.

Opie runs straight from its source folder (see config.get_install_root()). When
that folder is a normal Git clone, Opie can keep itself current by doing a
fast-forward `git pull` — so users get repo updates without re-downloading or
re-installing anything. Pure standard library; shells out to `git`.

Used in two places:
  * relay.main()  — on startup (in the background) self-update and re-exec into
    the new code, so the relay stays current without anyone opening the panel.
  * panel.py      — the "Check for updates" button, which then restarts the relay
    onto the new code.

Everything here is best-effort and non-fatal: no network, no git, or a copy that
isn't a clone (e.g. a downloaded ZIP) just means "no auto-update", never a crash.
"""

import os
import subprocess
import sys

from . import config as opie_config

# Status codes returned by check_and_update().
UPDATED = "updated"          # pulled new commits
CURRENT = "current"          # already on the latest commit
UNAVAILABLE = "unavailable"  # can't self-update (not a clone / no git / pip install)
ERROR = "error"              # tried, but fetch/pull failed (offline, auth, conflict)

# Set in the environment before re-exec so the fresh process doesn't update again
# (which would loop). Also lets users opt out with OPIE_NO_SELF_UPDATE=1.
_REEXEC_GUARD = "OPIE_NO_SELF_UPDATE"


def repo_root(root=None):
    """The source-tree folder Opie runs from, or None if pip-installed."""
    return root or opie_config.get_install_root()


_HAVE_GIT = None


def have_git() -> bool:
    """Is git usable? Answered once per process: git doesn't appear or vanish
    while Opie runs, and current_revision() is called on every status poll."""
    global _HAVE_GIT
    if _HAVE_GIT is None:
        try:
            _HAVE_GIT = subprocess.run(["git", "--version"],
                                       capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            _HAVE_GIT = False
    return _HAVE_GIT


def is_git_clone(root=None) -> bool:
    root = repo_root(root)
    return bool(root) and os.path.isdir(os.path.join(root, ".git"))


def _git(root, *args, timeout=30):
    return subprocess.run(["git", "-C", root, *args],
                          capture_output=True, text=True, timeout=timeout)


def current_revision(root=None):
    """Short commit hash Opie is running, or None if it can't be determined."""
    root = repo_root(root)
    if not root or not is_git_clone(root) or not have_git():
        return None
    try:
        r = _git(root, "rev-parse", "--short", "HEAD")
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def check_and_update(root=None, timeout=30):
    """
    Fetch from origin and fast-forward the working tree to the latest commit.

    Returns (status, message) where status is one of the module constants above.
    Never raises. A fast-forward pull never discards local edits — if the tree has
    diverged or has local changes, it reports ERROR instead of forcing anything.
    """
    root = repo_root(root)
    if not root:
        return UNAVAILABLE, ("Opie was installed with pip, so it updates with "
                             "`pip install -U` rather than automatically.")
    if not is_git_clone(root):
        return UNAVAILABLE, ("This copy isn't a Git clone (likely a downloaded "
                             "ZIP), so it can't update itself. Re-clone with Git "
                             "to get automatic updates.")
    if not have_git():
        return UNAVAILABLE, ("Git isn't installed, so Opie can't update itself. "
                             "Install Apple's command line tools: "
                             "xcode-select --install")
    try:
        before = current_revision(root)
        fetched = _git(root, "fetch", "--quiet", timeout=timeout)
        if fetched.returncode != 0:
            return ERROR, ("Couldn't reach GitHub to check for updates "
                           "(offline, or the clone needs credentials).\n"
                           + (fetched.stderr.strip() or "")).strip()
        pulled = _git(root, "pull", "--ff-only", "--quiet", timeout=timeout)
        if pulled.returncode != 0:
            return ERROR, ("An update is available but couldn't be applied "
                           "automatically (local changes on this copy?).\n"
                           + (pulled.stderr.strip() or pulled.stdout.strip())).strip()
        after = current_revision(root)
        if before and after and before != after:
            return UPDATED, f"Updated {before} → {after}."
        return CURRENT, f"Already up to date{f' ({after})' if after else ''}."
    except subprocess.TimeoutExpired:
        return ERROR, "Update check timed out (slow or no network)."
    except (OSError, subprocess.SubprocessError) as e:
        return ERROR, f"Update check failed: {e}"


def self_update_and_reexec(cfg, argv=None, timeout=20):
    """
    Startup hook for the relay: if auto-update is on and a newer commit was
    pulled, re-exec this process into the new code so it takes effect immediately.

    No-ops (returns the status) when auto-update is off, when there's nothing to
    update, or when we can't update. Guards against an infinite re-exec loop with
    an environment flag. `timeout` bounds each git call so a slow network can't
    stall the relay's startup for long.
    """
    if os.environ.get(_REEXEC_GUARD):
        return CURRENT
    if not cfg.get("auto_update", True):
        return UNAVAILABLE
    status, _msg = check_and_update(timeout=timeout)
    if status == UPDATED:
        os.environ[_REEXEC_GUARD] = "1"
        args = argv if argv is not None else sys.argv[1:]
        os.execv(sys.executable, [sys.executable, "-m", "opie", *args])
    return status

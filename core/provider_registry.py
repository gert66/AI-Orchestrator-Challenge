"""Provider-neutral role routing for the starter kit, stdlib only.

A profile is a JSON document (see profiles/*.json) whose ``roles`` object maps
each of brain/worker/reviewer to a provider: ``anthropic``, ``openai``,
``google`` or ``auto``. The installer copies the chosen profile to
``profiles/active.json``; ``load_profile`` resolves which file is in force:

  1. an explicit ``path`` argument,
  2. the ``ORCHESTRATOR_PROFILE_FILE`` environment variable,
  3. ``<repo_root>/profiles/active.json``,
  4. ``<repo_root>/profiles/claude.json`` (with one logged warning).

An explicit path or environment variable that names a missing or invalid
file is an error, never a silent fallback: the fallback only covers a
checkout that was never installed.
"""
import json
import logging
import os
from pathlib import Path

ROLES = ("brain", "worker", "reviewer")
PROVIDERS = ("anthropic", "openai", "google")

PROFILE_FILE_ENV = "ORCHESTRATOR_PROFILE_FILE"
REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = REPO_ROOT / "profiles"
ACTIVE_PROFILE_FILE = PROFILES_DIR / "active.json"
FALLBACK_PROFILE_FILE = PROFILES_DIR / "claude.json"

log = logging.getLogger(__name__)


class ProfileError(ValueError):
    """The selected profile file is missing, unreadable or invalid."""


def validate_routes(routes):
    missing = [r for r in ROLES if r not in routes]
    if missing:
        raise ValueError(f"missing role routes: {missing}")
    unknown = {r:p for r,p in routes.items() if r not in ROLES or (p not in PROVIDERS and p != "auto")}
    if unknown:
        raise ValueError(f"invalid role routes: {unknown}")
    return dict(routes)


def resolve_profile_path(path=None, environ=None):
    """Return (path, source) for the profile that load_profile() will read."""
    environ = os.environ if environ is None else environ
    if path is not None:
        return Path(path), "explicit"
    env_value = (environ.get(PROFILE_FILE_ENV) or "").strip()
    if env_value:
        return Path(env_value), PROFILE_FILE_ENV
    if ACTIVE_PROFILE_FILE.is_file():
        return ACTIVE_PROFILE_FILE, "active"
    return FALLBACK_PROFILE_FILE, "fallback"


def load_profile(path=None, environ=None):
    """Load and validate the active provider profile (see module docstring)."""
    selected, source = resolve_profile_path(path, environ=environ)
    if source == "fallback":
        log.warning(
            "no %s found (and %s is unset); falling back to %s",
            ACTIVE_PROFILE_FILE, PROFILE_FILE_ENV, FALLBACK_PROFILE_FILE,
        )
    try:
        with open(selected, encoding="utf-8") as fh:
            profile = json.load(fh)
    except OSError as exc:
        raise ProfileError(f"cannot read profile ({source}) {selected}: {exc}") from exc
    except ValueError as exc:
        raise ProfileError(f"profile ({source}) {selected} is not valid JSON: {exc}") from exc
    if not isinstance(profile, dict) or not isinstance(profile.get("roles"), dict):
        raise ProfileError(f"profile ({source}) {selected}: expected an object with a 'roles' object")
    try:
        roles = validate_routes(profile["roles"])
    except ValueError as exc:
        raise ProfileError(f"profile ({source}) {selected}: {exc}") from exc
    loaded = dict(profile)
    loaded["roles"] = roles
    loaded.setdefault("profile", selected.stem)
    loaded["source_path"] = str(selected)
    loaded["source"] = source
    return loaded


def provider_for_role(profile, role):
    """Return 'anthropic' | 'openai' | 'google' | 'auto' for role in profile."""
    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r}")
    roles = profile.get("roles") if isinstance(profile, dict) else None
    if not isinstance(roles, dict):
        raise ProfileError("profile has no 'roles' object")
    return validate_routes(roles)[role]

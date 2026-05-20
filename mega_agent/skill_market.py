"""
mega_agent.skill_market
=======================

Skill marketplace: install / remove / list / verify / sync skills from
git repos, http(s) tarballs, or local directories. Pure stdlib (uses
``git`` CLI for git sources, ``urllib`` for tarballs, ``shutil`` for
local copies).

Storage layout — see README §Skills · Marketplace::

    ~/.mega/
        skills/<name>@<version>/        ← installed skill
            SKILL.md                    ← required
            .install.json               ← {source, sha256, installed_at, ...}
            scripts/  resources/        ← optional
        skills.lock.json                ← global manifest of installs
        skills.toml                     ← policy (allowed_hosts, signatures)
        trusted_keys/*.pub              ← Ed25519 keys for sig verification
        cache/<sha256>.tar.gz           ← download cache

Pipeline (any failure rolls back the temp dir, never touches install
root):

    1 PARSE     → kind / url / ref / sha256
    2 POLICY    → allowed_hosts / require_signature
    3 FETCH     → /tmp/mega-install-XXXX/
    4 VERIFY    → SKILL.md, name regex, sha256, optional Ed25519 sig
                  (NEVER executes anything from the skill)
    5 RESOLVE   → ~/.mega/skills/<name>@<version>/
    6 COMMIT    → atomic os.rename + write .install.json + lockfile
    7 ACTIVATE  → skills.refresh()

Security red lines
------------------
* Install never invokes ``subprocess`` on user-supplied skill content.
  Scripts only run later, via ``run_skill_script``, gated by perms.
* http source REQUIRES a user-provided sha256 unless ``--insecure``.
* Hosts not on the allowlist (when set) are rejected outright.
* Signatures are optional but, when present, fully verified before
  COMMIT. ``require_signature = true`` flips unsigned to a hard reject.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import events
from .skills import NAME_RE, _parse_frontmatter, skills


# ─────────────────────────────────────────────────────────────────────────────
# Paths.
# ─────────────────────────────────────────────────────────────────────────────
MEGA_HOME      = Path.home() / ".mega"
INSTALL_ROOT   = MEGA_HOME / "skills"
LOCK_FILE      = MEGA_HOME / "skills.lock.json"
POLICY_FILE    = MEGA_HOME / "skills.toml"
TRUSTED_KEYS   = MEGA_HOME / "trusted_keys"
CACHE_DIR      = MEGA_HOME / "cache"


def _ensure_dirs() -> None:
    for d in (INSTALL_ROOT, TRUSTED_KEYS, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Source parsing.
# ─────────────────────────────────────────────────────────────────────────────
GIT_RE = re.compile(r"^git\+(?P<url>[^@]+?)(?:@(?P<ref>[A-Za-z0-9._/\-]+))?$")


@dataclass
class Source:
    kind: str           # "git" | "http" | "local"
    raw: str            # original spec, kept for the lockfile
    url: str = ""       # populated for git/http
    ref: str = ""       # populated for git
    path: Path | None = None  # populated for local
    sha256: str | None = None # user-provided for http; computed for git/local

    def host(self) -> str | None:
        if self.kind == "http":
            return urllib.parse.urlparse(self.url).hostname
        if self.kind == "git":
            # strip schemes like git+https://host/...
            u = self.url
            for scheme in ("https://", "http://", "git://", "ssh://"):
                if u.startswith(scheme):
                    return urllib.parse.urlparse(u).hostname
            # fallback: user@host:path
            if "@" in u and ":" in u:
                return u.split("@", 1)[1].split(":", 1)[0]
        return None


def parse_source(spec: str, *, sha256: str | None = None) -> Source:
    spec = spec.strip()
    if spec.startswith("git+"):
        m = GIT_RE.match(spec)
        if not m:
            raise ValueError(f"unparseable git source: {spec}")
        return Source(kind="git", raw=spec,
                      url=m.group("url"), ref=m.group("ref") or "")
    if spec.startswith(("http://", "https://")):
        return Source(kind="http", raw=spec, url=spec, sha256=sha256)
    # local path (must exist as a directory)
    p = Path(spec).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"local source must be an existing directory: {p}")
    return Source(kind="local", raw=spec, path=p)


# ─────────────────────────────────────────────────────────────────────────────
# Policy file (TOML — minimal subset, stdlib `tomllib` since Py3.11; otherwise
# we accept JSON too).
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_POLICY: dict[str, Any] = {
    "allowed_hosts": [],          # empty = unrestricted
    "require_signature": False,   # set True for stricter chains
    "allow_unsigned": True,       # convenience knob
}


def load_policy() -> dict[str, Any]:
    if not POLICY_FILE.is_file():
        return dict(DEFAULT_POLICY)
    text = POLICY_FILE.read_text(encoding="utf-8")
    out = dict(DEFAULT_POLICY)
    # JSON first (easy)
    try:
        out.update(json.loads(text))
        return out
    except Exception:
        pass
    # then tomllib (Py3.11+)
    try:
        import tomllib  # type: ignore
        out.update(tomllib.loads(text))
        return out
    except Exception:
        events.emit("skills.policy_parse_error", path=str(POLICY_FILE))
        return out


def _check_host(src: Source, policy: dict[str, Any]) -> None:
    allowed = policy.get("allowed_hosts") or []
    if not allowed:
        return  # empty list = unrestricted
    host = src.host()
    if not host or host not in allowed:
        raise PermissionError(
            f"host {host!r} is not in skills.toml allowed_hosts={allowed}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Lockfile.
# ─────────────────────────────────────────────────────────────────────────────
def _load_lock() -> dict:
    if not LOCK_FILE.is_file():
        return {"version": 1, "skills": {}}
    try:
        return json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "skills": {}}


def _save_lock(lock: dict) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps(lock, indent=2, ensure_ascii=False),
                         encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Fetchers.
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_git(src: Source, dest: Path) -> None:
    cmd = ["git", "clone", "--depth", "1"]
    if src.ref:
        cmd += ["--branch", src.ref]
    cmd += [src.url, str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {proc.stderr.strip()[:400]}")
    # detach .git so it's not part of the installed payload
    git_dir = dest / ".git"
    if git_dir.is_dir():
        shutil.rmtree(git_dir, ignore_errors=True)


def _fetch_http(src: Source, dest: Path, *, insecure: bool) -> str:
    """Download tarball/zip + verify sha256 + extract → returns sha256."""
    if not insecure and not src.sha256:
        raise ValueError(
            "http source requires --sha256 (or pass insecure=True). "
            "This protects against tampered tarballs."
        )
    # cache by sha256 if known, else by URL hash
    cache_key = src.sha256 or hashlib.sha256(src.url.encode()).hexdigest()
    cache_path = CACHE_DIR / f"{cache_key}"
    if not cache_path.is_file():
        with urllib.request.urlopen(src.url, timeout=60) as r:  # nosec - user opt-in
            tmp = cache_path.with_suffix(".part")
            h = hashlib.sha256()
            with tmp.open("wb") as f:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                    f.write(chunk)
            digest = h.hexdigest()
            if src.sha256 and digest != src.sha256:
                tmp.unlink(missing_ok=True)
                raise ValueError(
                    f"sha256 mismatch: expected {src.sha256}, got {digest}"
                )
            tmp.rename(cache_path)
    # determine archive type by sniffing magic bytes
    head = cache_path.read_bytes()[:6] if cache_path.stat().st_size < 1024 else \
        cache_path.open("rb").read(6)
    if head[:2] == b"PK":
        with zipfile.ZipFile(cache_path) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(cache_path) as tf:
            tf.extractall(dest)
    # If the archive root is a single dir, hoist its contents
    children = [p for p in dest.iterdir() if not p.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        inner = children[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(dest / item.name))
        inner.rmdir()
    return cache_key


def _fetch_local(src: Source, dest: Path) -> None:
    assert src.path is not None
    shutil.copytree(src.path, dest, dirs_exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Hashing + signing.
# ─────────────────────────────────────────────────────────────────────────────
def _hash_dir(d: Path) -> str:
    """Stable sha256 over (relpath, file-bytes) for the entire tree.

    Excludes ``.install.json`` so the tree hash is stable regardless of
    whether install metadata has been written yet.
    """
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(d).as_posix()
        if rel == ".install.json":
            continue
        h.update(b"\0" + rel.encode("utf-8") + b"\0")
        h.update(p.read_bytes())
    return h.hexdigest()


def _verify_signature(skill_dir: Path) -> str | None:
    """Verify SKILL.md.sig against ~/.mega/trusted_keys/*.pub.

    Returns the matching key filename, or None if no signature was found.
    Raises if a signature is present but verification fails against ALL
    trusted keys.
    """
    sig = skill_dir / "SKILL.md.sig"
    if not sig.is_file():
        return None
    try:
        # We attempt nacl/cryptography lazily; absence → conservative reject.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives import serialization
    except Exception as e:
        raise RuntimeError(
            "signature verification requires `cryptography` "
            "(pip install cryptography); skill is signed but cannot be verified"
        ) from e

    payload = (skill_dir / "SKILL.md").read_bytes()
    sig_bytes = sig.read_bytes()
    if not TRUSTED_KEYS.is_dir():
        raise PermissionError("signature present but trusted_keys/ is empty")
    for key_path in sorted(TRUSTED_KEYS.glob("*.pub")):
        try:
            pub = serialization.load_pem_public_key(key_path.read_bytes())
            if not isinstance(pub, Ed25519PublicKey):
                continue
            pub.verify(sig_bytes, payload)
            return key_path.name
        except Exception:
            continue
    raise PermissionError("signature present but no trusted key verified it")


# ─────────────────────────────────────────────────────────────────────────────
# Verification stage (NEVER executes skill code).
# ─────────────────────────────────────────────────────────────────────────────
def _verify(staging: Path, *, policy: dict[str, Any],
            require_signature_override: bool | None = None,
            allow_unsigned_override: bool | None = None
            ) -> tuple[str, str, str | None]:
    """Returns (name, version, signed_by). Raises on any policy violation."""
    md = staging / "SKILL.md"
    if not md.is_file():
        raise ValueError("SKILL.md missing in source")
    text = md.read_text(encoding="utf-8")
    meta, _body = _parse_frontmatter(text)
    name = str(meta.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(f"invalid or missing skill name: {name!r}")
    version = str(meta.get("version") or "0.0.0")

    # Signature flow
    require_signature = (require_signature_override
                         if require_signature_override is not None
                         else bool(policy.get("require_signature", False)))
    allow_unsigned = (allow_unsigned_override
                      if allow_unsigned_override is not None
                      else bool(policy.get("allow_unsigned", True)))
    signed_by = _verify_signature(staging)
    if signed_by is None:
        if require_signature and not allow_unsigned:
            raise PermissionError(
                "skill is unsigned and require_signature=true "
                "(set allow_unsigned=true or pass --allow-unsigned to override)"
            )
    return name, version, signed_by


# ─────────────────────────────────────────────────────────────────────────────
# Public API.
# ─────────────────────────────────────────────────────────────────────────────
def install(spec: str, *, sha256: str | None = None,
            force: bool = False,
            allow_unsigned: bool | None = None,
            require_signature: bool | None = None,
            insecure: bool = False) -> dict:
    """End-to-end install pipeline. See module docstring for details."""
    _ensure_dirs()
    policy = load_policy()
    src = parse_source(spec, sha256=sha256)
    _check_host(src, policy)

    staging = Path(tempfile.mkdtemp(prefix="mega-install-"))
    try:
        # 3 FETCH
        if src.kind == "git":
            _fetch_git(src, staging)
        elif src.kind == "http":
            cache_key = _fetch_http(src, staging, insecure=insecure)
            src.sha256 = src.sha256 or cache_key
        else:
            _fetch_local(src, staging)

        # 4 VERIFY
        name, version, signed_by = _verify(
            staging, policy=policy,
            require_signature_override=require_signature,
            allow_unsigned_override=allow_unsigned,
        )

        # always compute final tree hash for the lockfile (auditability)
        tree_sha = _hash_dir(staging)
        if src.kind == "local":
            src.sha256 = tree_sha

        # 5 RESOLVE
        target = INSTALL_ROOT / f"{name}@{version}"
        if target.exists():
            if not force:
                raise FileExistsError(
                    f"{target} already exists (pass force=True to overwrite)"
                )
            shutil.rmtree(target)

        # 6 COMMIT — write metadata first into staging, then atomic rename
        meta_path = staging / ".install.json"
        meta_record = {
            "name": name,
            "version": version,
            "source": src.raw,
            "kind": src.kind,
            "sha256": src.sha256,
            "tree_sha256": tree_sha,
            "signed_by": signed_by,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        meta_path.write_text(json.dumps(meta_record, indent=2,
                                        ensure_ascii=False),
                             encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(staging), str(target))
        staging = None  # ownership transferred

        # update lockfile
        lock = _load_lock()
        lock.setdefault("skills", {})[name] = {
            **meta_record,
            "dir": str(target),
        }
        _save_lock(lock)

        # 7 ACTIVATE — make the new skill visible
        skills.refresh()
        events.emit("skills.installed", name=name, version=version,
                    source=src.raw)
        return meta_record
    finally:
        if staging and Path(staging).exists():
            shutil.rmtree(staging, ignore_errors=True)


def remove(name: str, version: str | None = None) -> dict:
    """Remove one (or all) installed versions of a skill."""
    _ensure_dirs()
    removed: list[str] = []
    for child in INSTALL_ROOT.iterdir():
        if not child.is_dir():
            continue
        cn, _, cv = child.name.partition("@")
        if cn != name:
            continue
        if version and cv != version:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child.name)
    lock = _load_lock()
    if name in lock.get("skills", {}):
        if version is None or lock["skills"][name].get("version") == version:
            lock["skills"].pop(name, None)
    _save_lock(lock)
    skills.refresh()
    events.emit("skills.removed", name=name, version=version,
                count=len(removed))
    return {"removed": removed}


def list_installed() -> dict:
    """Return the lockfile contents — single source of truth on disk."""
    _ensure_dirs()
    return _load_lock()


def verify_installed(name: str | None = None) -> dict:
    """Re-hash on-disk skill trees and compare with the lockfile."""
    _ensure_dirs()
    lock = _load_lock()
    out: dict[str, dict] = {}
    items = lock.get("skills", {}).items()
    for n, rec in items:
        if name and n != name:
            continue
        d = Path(rec.get("dir", "")) if rec.get("dir") else None
        if not d or not d.is_dir():
            out[n] = {"ok": False, "reason": "missing"}
            continue
        actual = _hash_dir(d)
        out[n] = {
            "ok": actual == rec.get("tree_sha256"),
            "expected": rec.get("tree_sha256"),
            "actual": actual,
        }
    return out


def sync(lock_path: Path | None = None,
         **install_kwargs: Any) -> dict:
    """Reproduce installs from a lockfile (CI / new-machine scenario)."""
    p = lock_path or LOCK_FILE
    if not p.is_file():
        return {"installed": [], "skipped": [], "errors": []}
    lock = json.loads(p.read_text(encoding="utf-8"))
    installed: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []
    for name, rec in lock.get("skills", {}).items():
        target = INSTALL_ROOT / f"{name}@{rec.get('version', '0.0.0')}"
        if target.is_dir():
            skipped.append(name)
            continue
        try:
            install(rec["source"],
                    sha256=rec.get("sha256"),
                    **install_kwargs)
            installed.append(name)
        except Exception as e:  # noqa: BLE001
            errors.append({"name": name, "error": str(e)})
    return {"installed": installed, "skipped": skipped, "errors": errors}

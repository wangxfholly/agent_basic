"""
mega_agent.profiles
===================

LLM profile store with optional Fernet/PBKDF2 encryption.

If `cryptography` is installed AND AGENT_MASTER_PASSWORD is set, profiles
are persisted to `models.json.enc`. Otherwise they live in `models.json`
(plaintext) and a one-time warning is printed.

A profile bundles `{name, protocol, model, base_url, api_key}`. A routing
table maps a semantic key (e.g. "code", "write", "default") to a profile
name; `resolve_route()` picks the profile to use for a given request.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

try:
    import base64
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore

from .config import MASTER_SALT_FILE, MODELS_ENC_FILE, MODELS_FILE, NAME_RE
from .events import events


class SecretBox:
    """
    Symmetric encryption helper.
        master password → PBKDF2-HMAC-SHA256(200k iters) → Fernet key
        salt persisted at .master-key.salt (no secret material)

    Password resolution priority:
        1. explicit ctor arg
        2. AGENT_MASTER_PASSWORD env
        (no interactive prompt — keep main loop non-blocking)

    Without `cryptography` installed, falls back to plaintext mode and
    emits a one-time warning.
    """

    def __init__(self, salt_path: Path, password: str | None = None):
        self.salt_path = salt_path
        self._password = password or os.environ.get("AGENT_MASTER_PASSWORD")
        self._fernet = None  # type: ignore

    @property
    def enabled(self) -> bool:
        return _HAS_CRYPTO and self._password is not None

    def _ensure_salt(self) -> bytes:
        if self.salt_path.exists():
            return self.salt_path.read_bytes()
        salt = os.urandom(16)
        self.salt_path.write_bytes(salt)
        return salt

    def _build(self):
        if self._fernet:
            return self._fernet
        salt = self._ensure_salt()
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                         salt=salt, iterations=200_000)
        key = base64.urlsafe_b64encode(kdf.derive(self._password.encode("utf-8")))
        self._fernet = Fernet(key)
        return self._fernet

    def encrypt(self, plaintext: str) -> bytes:
        return self._build().encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        return self._build().decrypt(token).decode("utf-8")


_warned_plaintext = False


def _warn_plaintext_once():
    global _warned_plaintext
    if _warned_plaintext:
        return
    _warned_plaintext = True
    print("[mega_agent] WARNING: storing api keys as plaintext "
          "(set AGENT_MASTER_PASSWORD to enable encryption)")


class ProfileStore:
    """
    Multi-profile store with optional encryption + routing table.

    Storage rules:
      - SecretBox.enabled → read/write `models.json.enc`
      - If only plaintext exists at startup, auto-migrate (read plain
        → write enc → unlink plain).
      - SecretBox disabled → fall back to plaintext + one-time warning.
    """

    def __init__(self, path: Path, enc_path: Path, secrets: SecretBox):
        self.path, self.enc_path, self.secrets = path, enc_path, secrets
        self._lock = threading.Lock()
        if not self.path.exists() and not self.enc_path.exists():
            skeleton = {"active": None, "profiles": {}, "routing": {}}
            self._raw_write_plain(json.dumps(skeleton, indent=2))
        if self.secrets.enabled and self.path.exists() and not self.enc_path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self._raw_write_enc(json.dumps(data, indent=2, ensure_ascii=False))
            self.path.unlink()

    # ---- raw IO ----
    def _raw_write_plain(self, text: str):
        self.path.write_text(text, encoding="utf-8")

    def _raw_write_enc(self, text: str):
        token = self.secrets.encrypt(text)
        self.enc_path.write_bytes(token)

    def _load(self) -> dict:
        if self.secrets.enabled and self.enc_path.exists():
            try:
                txt = self.secrets.decrypt(self.enc_path.read_bytes())
            except InvalidToken:
                raise RuntimeError(
                    "master password mismatch — cannot decrypt models.json.enc")
            return json.loads(txt or "{}")
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        return {"active": None, "profiles": {}, "routing": {}}

    def _save(self, data: dict):
        text = json.dumps(data, indent=2, ensure_ascii=False)
        with self._lock:
            if self.secrets.enabled:
                self._raw_write_enc(text)
            else:
                _warn_plaintext_once()
                self._raw_write_plain(text)

    # ---- profile CRUD ----
    def list(self) -> list[dict]:
        out = []
        for p in self._load().get("profiles", {}).values():
            redacted = dict(p)
            if redacted.get("api_key"):
                k = redacted["api_key"]
                redacted["api_key"] = (
                    (k[:6] + "***" + k[-4:]) if len(k) > 12 else "***"
                )
            out.append(redacted)
        return out

    def get(self, name: str) -> dict | None:
        return self._load().get("profiles", {}).get(name)

    def active(self) -> dict | None:
        data = self._load()
        name = data.get("active")
        return data.get("profiles", {}).get(name) if name else None

    def upsert(self, *, name: str, protocol: str, model: str,
               api_key: str, base_url: str | None = None) -> dict:
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"invalid profile name: {name!r}")
        if protocol not in ("anthropic", "openai", "mock"):
            raise ValueError(
                f"protocol must be anthropic|openai|mock, got {protocol!r}")
        data = self._load()
        data.setdefault("profiles", {})[name] = {
            "name": name, "protocol": protocol, "model": model,
            "api_key": api_key, "base_url": base_url,
        }
        if not data.get("active"):
            data["active"] = name
        self._save(data)
        events.emit("profile.upsert", name=name, protocol=protocol, model=model)
        return data["profiles"][name]

    def remove(self, name: str) -> bool:
        data = self._load()
        if name not in data.get("profiles", {}):
            return False
        del data["profiles"][name]
        if data.get("active") == name:
            data["active"] = next(iter(data["profiles"]), None)
        routing = data.get("routing", {})
        for k in list(routing.keys()):
            if routing[k] == name:
                del routing[k]
        self._save(data)
        return True

    def use(self, name: str) -> dict:
        data = self._load()
        if name not in data.get("profiles", {}):
            raise KeyError(name)
        data["active"] = name
        self._save(data)
        events.emit("profile.use", name=name)
        return data["profiles"][name]

    # ---- routing CRUD ----
    def routing(self) -> dict:
        return self._load().get("routing", {}) or {}

    def set_route(self, route: str, profile_name: str) -> dict:
        data = self._load()
        if profile_name not in data.get("profiles", {}):
            raise KeyError(f"profile not found: {profile_name}")
        data.setdefault("routing", {})[route] = profile_name
        self._save(data)
        events.emit("profile.route", route=route, profile=profile_name)
        return data["routing"]

    def del_route(self, route: str) -> bool:
        data = self._load()
        if route not in data.get("routing", {}):
            return False
        del data["routing"][route]
        self._save(data)
        return True

    def resolve_route(self, route: str | None) -> dict | None:
        data = self._load()
        if route:
            name = data.get("routing", {}).get(route)
            if name and name in data.get("profiles", {}):
                return data["profiles"][name]
        name = data.get("routing", {}).get("default")
        if name and name in data.get("profiles", {}):
            return data["profiles"][name]
        active = data.get("active")
        return data.get("profiles", {}).get(active) if active else None


secrets = SecretBox(MASTER_SALT_FILE)
profiles = ProfileStore(MODELS_FILE, MODELS_ENC_FILE, secrets)

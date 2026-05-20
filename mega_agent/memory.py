"""
mega_agent.memory
=================

Cross-session memory store with pluggable vector backends.

Layers covered:
  - Episodic (timestamped events / actions)         → facts.jsonl
  - Semantic (key-value preferences / facts)        → kv.json
  - Optional vector recall (Mock / Naive / Chroma)  → vectors/

Design notes:
  - Stdlib-only on the hot path. `chromadb` is a soft import.
  - `recall()` falls back to BM25-ish naive scoring when no vector
    backend is configured — always works, just less smart.
  - `forget(fact_id)` and `forget_user(user_id)` are first-class
    (GDPR-compliant deletion).
  - All writes go through `events` bus → audit trail for free.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Iterable, Protocol

from .config import MEMORY_FACTS, MEMORY_KV, MEMORY_VECTORS
from .events import events


# ─────────────────────────────────────────────────────────────────────────────
# VectorBackend protocol — any class that satisfies this can plug in.
# ─────────────────────────────────────────────────────────────────────────────
class VectorBackend(Protocol):
    name: str

    def index(self, fact_id: str, text: str, tags: dict) -> None: ...
    def search(self, query: str, k: int) -> list[dict]: ...
    def delete(self, fact_id: str) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# 1) NaiveBackend — BM25-ish in-memory scoring, zero deps, always available.
# ─────────────────────────────────────────────────────────────────────────────
_TOKEN_RE = re.compile(r"[A-Za-z0-9_一-鿿]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


class NaiveBackend:
    """
    Pure-Python lexical scorer. Not a real BM25 (we skip IDF normalization
    across docs for simplicity), but good enough for hundreds of facts.
    """
    name = "naive"

    def __init__(self):
        self._docs: dict[str, dict] = {}  # fact_id -> {tokens, tf, tags}
        self._lock = threading.Lock()

    def index(self, fact_id: str, text: str, tags: dict) -> None:
        toks = _tokenize(text)
        with self._lock:
            self._docs[fact_id] = {
                "tokens": toks,
                "tf": Counter(toks),
                "tags": tags,
                "text": text,
            }

    def search(self, query: str, k: int) -> list[dict]:
        q_toks = _tokenize(query)
        if not q_toks:
            return []
        with self._lock:
            scored = []
            for fid, doc in self._docs.items():
                score = sum(doc["tf"].get(t, 0) for t in q_toks)
                if score:
                    # length normalization
                    norm = score / (1 + math.log1p(len(doc["tokens"])))
                    scored.append((norm, fid, doc))
            scored.sort(key=lambda x: -x[0])
        return [
            {"fact_id": fid, "score": round(s, 4),
             "text": doc["text"], "tags": doc["tags"]}
            for s, fid, doc in scored[:k]
        ]

    def delete(self, fact_id: str) -> None:
        with self._lock:
            self._docs.pop(fact_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# 2) MockBackend — deterministic, for tests.
# ─────────────────────────────────────────────────────────────────────────────
class MockBackend:
    name = "mock"

    def __init__(self):
        self.calls: list[tuple] = []
        self.fixed: list[dict] = []

    def index(self, fact_id, text, tags):
        self.calls.append(("index", fact_id, text, tags))

    def search(self, query, k):
        self.calls.append(("search", query, k))
        return self.fixed[:k]

    def delete(self, fact_id):
        self.calls.append(("delete", fact_id))


# ─────────────────────────────────────────────────────────────────────────────
# 3) ChromaBackend — soft import. Persists to MEMORY_VECTORS/.
# ─────────────────────────────────────────────────────────────────────────────
class ChromaBackend:
    """
    Lazy-loaded chromadb adapter. Falls back to ImportError with a helpful
    message if the user requests it without installing chromadb.
    """
    name = "chroma"

    def __init__(self, persist_dir: Path = MEMORY_VECTORS,
                 collection: str = "mega_memory"):
        try:
            import chromadb  # type: ignore
        except ImportError as e:
            raise ImportError(
                "ChromaBackend requires chromadb. "
                "Install with: pip install chromadb"
            ) from e
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._coll = self._client.get_or_create_collection(name=collection)

    def index(self, fact_id: str, text: str, tags: dict) -> None:
        self._coll.upsert(
            ids=[fact_id],
            documents=[text],
            metadatas=[{k: str(v) for k, v in tags.items()}] or None,
        )

    def search(self, query: str, k: int) -> list[dict]:
        res = self._coll.query(query_texts=[query], n_results=k)
        out = []
        for fid, doc, meta, dist in zip(
            res["ids"][0], res["documents"][0],
            res["metadatas"][0] or [{}] * len(res["ids"][0]),
            res["distances"][0],
        ):
            out.append({"fact_id": fid, "text": doc, "tags": meta or {},
                        "score": round(1 - dist, 4)})
        return out

    def delete(self, fact_id: str) -> None:
        self._coll.delete(ids=[fact_id])


# ─────────────────────────────────────────────────────────────────────────────
# MemoryStore — the public surface.
# ─────────────────────────────────────────────────────────────────────────────
class MemoryStore:
    """
    Two backing stores:
      - facts.jsonl : append-only episodic + semantic log (timestamped)
      - kv.json     : fast-lookup KV (preferences / single-value facts)

    Plus an optional vector backend that mirrors the JSONL for recall.
    """

    VALID_KINDS = {"fact", "preference", "event", "task", "note"}

    def __init__(self, facts_path: Path = MEMORY_FACTS,
                 kv_path: Path = MEMORY_KV,
                 vector_backend: VectorBackend | None = None):
        self.facts_path = facts_path
        self.kv_path = kv_path
        self._lock = threading.Lock()
        self._kv: dict[str, str] = {}
        self._kv_loaded = False
        self.vector: VectorBackend = vector_backend or NaiveBackend()
        self._reindex_vectors_from_disk()

    # ---- KV (semantic, single-value) ----
    def _load_kv(self) -> dict:
        if not self._kv_loaded:
            if self.kv_path.exists():
                self._kv = json.loads(self.kv_path.read_text(encoding="utf-8"))
            self._kv_loaded = True
        return self._kv

    def _save_kv(self) -> None:
        self.kv_path.parent.mkdir(parents=True, exist_ok=True)
        self.kv_path.write_text(
            json.dumps(self._kv, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, key: str) -> str | None:
        return self._load_kv().get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._load_kv()[key] = value
            self._save_kv()
        events.emit("memory.kv_set", key=key)

    def delete_kv(self, key: str) -> bool:
        with self._lock:
            self._load_kv()
            existed = key in self._kv
            self._kv.pop(key, None)
            if existed:
                self._save_kv()
        if existed:
            events.emit("memory.kv_deleted", key=key)
        return existed

    # ---- Episodic / Semantic facts ----
    def remember(self, content: str, *, kind: str = "fact",
                 user_id: str | None = None, **tags) -> dict:
        if kind not in self.VALID_KINDS:
            raise ValueError(f"unknown kind: {kind} "
                             f"(allowed: {sorted(self.VALID_KINDS)})")
        fact = {
            "id": uuid.uuid4().hex[:12],
            "ts": time.time(),
            "kind": kind,
            "user_id": user_id,
            "content": content,
            "tags": tags or {},
        }
        with self._lock:
            self.facts_path.parent.mkdir(parents=True, exist_ok=True)
            with self.facts_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(fact, ensure_ascii=False) + "\n")
        # mirror to vector backend
        self.vector.index(fact["id"], content,
                          {**fact["tags"], "kind": kind,
                           "user_id": user_id or ""})
        events.emit("memory.remember", fact_id=fact["id"], fact_kind=kind)
        return fact

    def recall(self, query: str, k: int = 5,
               user_id: str | None = None) -> list[dict]:
        hits = self.vector.search(query, k * 4 if user_id else k)
        if user_id:
            hits = [h for h in hits
                    if (h.get("tags") or {}).get("user_id") in (user_id, "")]
        return hits[:k]

    def list_facts(self, *, user_id: str | None = None,
                   limit: int = 100) -> list[dict]:
        if not self.facts_path.exists():
            return []
        out: list[dict] = []
        with self.facts_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                fact = json.loads(line)
                if user_id and fact.get("user_id") != user_id:
                    continue
                out.append(fact)
        return out[-limit:]

    def forget(self, fact_id: str) -> bool:
        """Tombstone-style delete: rewrite JSONL without the line + drop vector."""
        if not self.facts_path.exists():
            return False
        kept: list[str] = []
        removed = False
        with self._lock:
            with self.facts_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if json.loads(line).get("id") == fact_id:
                        removed = True
                        continue
                    kept.append(line)
            if removed:
                self.facts_path.write_text(
                    "\n".join(kept) + ("\n" if kept else ""),
                    encoding="utf-8",
                )
                self.vector.delete(fact_id)
                events.emit("memory.forget", fact_id=fact_id)
        return removed

    def forget_user(self, user_id: str) -> int:
        """GDPR-style purge of every fact + KV entry tagged with user_id."""
        if not self.facts_path.exists():
            return 0
        kept: list[str] = []
        purged_ids: list[str] = []
        with self._lock:
            with self.facts_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    fact = json.loads(line)
                    if fact.get("user_id") == user_id:
                        purged_ids.append(fact["id"])
                        continue
                    kept.append(line)
            self.facts_path.write_text(
                "\n".join(kept) + ("\n" if kept else ""),
                encoding="utf-8",
            )
            for fid in purged_ids:
                self.vector.delete(fid)
            # also drop KV entries prefixed with user_id (convention)
            self._load_kv()
            kv_purged = [k for k in self._kv if k.startswith(f"{user_id}.")]
            for k in kv_purged:
                self._kv.pop(k, None)
            if kv_purged:
                self._save_kv()
        events.emit("memory.forget_user", user_id=user_id,
                    facts=len(purged_ids), kv=len(kv_purged))
        return len(purged_ids) + len(kv_purged)

    # ---- Internals ----
    def _reindex_vectors_from_disk(self) -> None:
        """On boot, replay facts.jsonl into the vector backend (idempotent)."""
        if not self.facts_path.exists():
            return
        with self.facts_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    fact = json.loads(line)
                    self.vector.index(
                        fact["id"], fact.get("content", ""),
                        {**(fact.get("tags") or {}),
                         "kind": fact.get("kind", "fact"),
                         "user_id": fact.get("user_id") or ""},
                    )
                except Exception as e:
                    events.emit("memory.reindex_skip", error=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Backend factory — chosen by env or explicit override.
# ─────────────────────────────────────────────────────────────────────────────
def _make_default_backend() -> VectorBackend:
    import os
    choice = (os.environ.get("AGENT_MEMORY_BACKEND", "naive") or "naive").lower()
    if choice == "chroma":
        try:
            return ChromaBackend()
        except ImportError as e:
            print(f"[memory] chroma unavailable, falling back to naive: {e}")
            return NaiveBackend()
    if choice == "mock":
        return MockBackend()
    return NaiveBackend()


# Default singleton — same pattern as `permissions`, `hooks`, etc.
memory = MemoryStore(vector_backend=_make_default_backend())

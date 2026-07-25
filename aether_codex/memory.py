"""Long-term memory for Aether Codex.

Two layers:
  1. A persistent vector store (ChromaDB) for semantic recall of past findings,
     agent results and conversations.
  2. A JSON conversation log so a Director can resume with recent history.

If ChromaDB is unavailable (not installed, or its embedding model can't be
downloaded offline), we degrade gracefully to a naive keyword store so the
platform keeps working.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from .config import DATA_DIR


class _KeywordFallbackStore:
    """Very small in-process fallback used only when ChromaDB is unavailable.
    Persists documents to a JSON file and ranks by naive keyword overlap."""

    def __init__(self, path: Path):
        self.path = path
        self.docs: list[dict] = []
        if path.exists():
            try:
                self.docs = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.docs = []

    def add(self, text: str, metadata: dict) -> None:
        self.docs.append({"text": text, "metadata": metadata})
        self.path.write_text(json.dumps(self.docs, indent=1), encoding="utf-8")

    def search(self, query: str, k: int) -> list[str]:
        words = set(re.findall(r"\w+", query.lower()))
        scored = sorted(
            self.docs,
            key=lambda d: len(words & set(re.findall(r"\w+", d["text"].lower()))),
            reverse=True,
        )
        return [d["text"] for d in scored[:k] if d]


class CodexMemory:
    """Facade over the vector store + conversation log."""

    def __init__(self, persist_dir: Path | None = None):
        self.persist_dir = persist_dir or DATA_DIR
        self.persist_dir.mkdir(exist_ok=True)
        self.conversation_path = self.persist_dir / "conversation.json"
        self._collection = None
        self._fallback: _KeywordFallbackStore | None = None
        self._init_vector_store()

    # ------------------------------------------------------------------ setup
    def _init_vector_store(self) -> None:
        try:
            import chromadb

            client = chromadb.PersistentClient(path=str(self.persist_dir / "chroma"))
            self._collection = client.get_or_create_collection("aether_codex")
        except Exception:
            # Offline / missing dependency: fall back to keyword search.
            self._fallback = _KeywordFallbackStore(self.persist_dir / "fallback_store.json")

    # ----------------------------------------------------------------- memory
    def remember(self, text: str, kind: str = "note", metadata: dict | None = None) -> None:
        """Store a piece of knowledge for later semantic recall."""
        if not text or not text.strip():
            return
        meta = {"kind": kind, "timestamp": time.time(), **(metadata or {})}
        if self._collection is not None:
            try:
                self._collection.add(
                    ids=[str(uuid.uuid4())],
                    documents=[text[:8000]],  # keep individual memories bounded
                    metadatas=[meta],
                )
                return
            except Exception:
                pass  # e.g. embedding model download failed — fall through
        if self._fallback is None:
            self._fallback = _KeywordFallbackStore(self.persist_dir / "fallback_store.json")
        self._fallback.add(text[:8000], meta)

    def recall(self, query: str, k: int = 4) -> list[str]:
        """Return the k most relevant stored memories for a query."""
        if self._collection is not None:
            try:
                res = self._collection.query(query_texts=[query], n_results=k)
                docs = res.get("documents") or [[]]
                return [d for d in docs[0] if d]
            except Exception:
                pass
        if self._fallback is not None:
            return self._fallback.search(query, k)
        return []

    # ----------------------------------------------------- conversation log
    def load_conversation(self) -> list[dict]:
        if self.conversation_path.exists():
            try:
                return json.loads(self.conversation_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def append_exchange(self, user: str, assistant: str) -> None:
        log = self.load_conversation()
        log.append({"user": user, "assistant": assistant, "timestamp": time.time()})
        self.conversation_path.write_text(json.dumps(log, indent=1), encoding="utf-8")
        # Every exchange also becomes searchable long-term memory.
        self.remember(f"User asked: {user}\nDirector answered: {assistant}", kind="conversation")

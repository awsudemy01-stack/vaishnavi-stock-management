"""Help indexer — chunks the **user guide** and the how-to parts of the frontend guide into ``doc_chunk``
rows, embeds them when the provider can, and answers ``search(query)`` (docs/06 §3, §7 layer 1).

Only ``docs/15-user-guide.md`` and the how-to sections of ``docs/09-frontend-guide.md`` are indexed —
never the architecture, data-model or backend documents — so "how is the app built?" has no material to
draw from. Chunks are keyed by content hash: unchanged text is skipped on every start. When embeddings
are unavailable the keyword ranking answers instead.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from vaishnavi.ai import embeddings
from vaishnavi.ai.providers.base import LLMProvider
from vaishnavi.core.errors import AIError
from vaishnavi.services.common import Session, TxnFactory

log = logging.getLogger("vaishnavi.ai.indexer")

INDEXED_FILES: Final = ("15-user-guide.md", "09-frontend-guide.md")
HOWTO_ONLY: Final = {"09-frontend-guide.md": re.compile(r"(how|step|shortcut|keyboard|screen|billing|pos|hold|resume|scan|print|shift|login|lock|discount|payment|find)", re.IGNORECASE)}
MAX_CHUNK_CHARS: Final = 1200


@dataclass(frozen=True)
class Chunk:
    """A help passage."""

    id: int
    source: str
    title: str
    content: str
    score: float = 0.0


class IndexStore(Protocol):
    """Persistence."""

    def chunks(self, session: Session) -> list[dict[str, Any]]: ...  # pragma: no cover - protocol
    def insert_chunk(self, session: Session, *, source: str, content: str, content_hash: str) -> int: ...  # pragma: no cover - protocol
    def delete_chunks(self, session: Session, ids: Iterable[int]) -> None: ...  # pragma: no cover - protocol
    def embeddings_for(self, session: Session, *, owner_type: str, provider: str, model: str) -> dict[int, bytes]: ...  # pragma: no cover - protocol
    def upsert_embedding(self, session: Session, *, owner_type: str, owner_id: int, provider: str, model: str, vector: bytes, dim: int) -> None: ...  # pragma: no cover - protocol


def split_markdown(text: str, *, source: str) -> list[tuple[str, str]]:
    """``(title, content)`` per heading section, long sections split by paragraph."""
    out: list[tuple[str, str]] = []
    title = source
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if not body:
            return
        if len(body) <= MAX_CHUNK_CHARS:
            out.append((title, f"# {title}\n{body}"))
            return
        piece: list[str] = []
        size = 0
        for para in re.split(r"\n\s*\n", body):
            if size + len(para) > MAX_CHUNK_CHARS and piece:
                out.append((title, f"# {title}\n" + "\n\n".join(piece)))
                piece, size = [], 0
            piece.append(para)
            size += len(para)
        if piece:
            out.append((title, f"# {title}\n" + "\n\n".join(piece)))

    for line in text.splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush()
            buf = []
            title = m.group(2).strip()
        else:
            buf.append(line)
    flush()
    return out


class DocIndexer:
    """Build and query the help index."""

    def __init__(self, txn: TxnFactory, store: IndexStore, docs_dir: Path, *, provider: Callable[[], LLMProvider]) -> None:
        self._txn = txn
        self._store = store
        self._docs = docs_dir
        self._provider = provider

    def _sections(self) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for name in INDEXED_FILES:
            path = self._docs / name
            if not path.exists():
                log.debug("INDEX_SKIP_MISSING file=%s", path)
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            only = HOWTO_ONLY.get(name)
            for title, content in split_markdown(text, source=name):
                if only is not None and not only.search(title + " " + content[:300]):
                    continue
                rows.append((name, title, content))
        return rows

    def rebuild(self) -> int:
        """Insert new/changed chunks, delete vanished ones, embed what is missing; returns new chunks."""
        wanted = {hashlib.sha256(c.encode("utf-8")).hexdigest(): (s, t, c) for s, t, c in self._sections()}
        with self._txn(label="index.rebuild") as session:
            existing = {row["content_hash"]: row for row in self._store.chunks(session)}
            stale = [row["id"] for h, row in existing.items() if h not in wanted]
            if stale:
                self._store.delete_chunks(session, stale)
            new = 0
            for h, (source, _title, content) in wanted.items():
                if h in existing:
                    continue
                self._store.insert_chunk(session, source=source, content=content, content_hash=h)
                new += 1
        log.info("INDEX_REBUILT chunks=%d new=%d removed=%d", len(wanted), new, len(stale))
        self.embed_missing()
        return new

    def embed_missing(self) -> int:
        """Embed chunks without a vector for the current provider/model (no-op without embeddings)."""
        provider = self._provider()
        if not provider.capabilities().embeddings or not provider.embed_model:
            return 0
        with self._txn(label="index.pending") as session:
            rows = self._store.chunks(session)
            have = self._store.embeddings_for(session, owner_type="DOC_CHUNK", provider=provider.name, model=provider.embed_model)
        pending = [r for r in rows if int(r["id"]) not in have]
        if not pending:
            return 0
        done = 0
        for i in range(0, len(pending), 16):
            batch = pending[i : i + 16]
            try:
                vectors = provider.embed([str(r["content"]) for r in batch])
            except AIError as exc:
                log.warning("INDEX_EMBED_SKIPPED code=%s detail=%s", exc.code, exc.detail)
                break
            with self._txn(label="index.embed") as session:
                for row, vec in zip(batch, vectors, strict=True):
                    self._store.upsert_embedding(session, owner_type="DOC_CHUNK", owner_id=int(row["id"]), provider=provider.name, model=provider.embed_model, vector=embeddings.pack(vec), dim=len(vec))
                    done += 1
        log.info("INDEX_EMBEDDED chunks=%d provider=%s model=%s", done, provider.name, provider.embed_model)
        return done

    def sources(self) -> list[str]:
        """Distinct indexed files."""
        with self._txn(label="index.sources") as session:
            return sorted({str(r["source"]) for r in self._store.chunks(session)})

    def search(self, query: str, *, k: int = 4) -> list[Chunk]:
        """Best chunks by embedding similarity when available, else keyword overlap."""
        provider = self._provider()
        with self._txn(label="index.search") as session:
            rows = self._store.chunks(session)
            vectors = self._store.embeddings_for(session, owner_type="DOC_CHUNK", provider=provider.name, model=provider.embed_model) if provider.capabilities().embeddings and provider.embed_model else {}
        if not rows:
            return []
        scored: list[Chunk] = []
        qvec: list[float] | None = None
        if vectors and len(vectors) >= len(rows) * 0.8:
            try:
                qvec = provider.embed([query])[0]
            except AIError as exc:
                log.debug("INDEX_SEARCH_FALLBACK code=%s", exc.code)
                qvec = None
        for r in rows:
            title = _title_of(str(r["content"]))
            keyword = embeddings.keyword_score(query, title, str(r["content"]))
            if qvec is not None and int(r["id"]) in vectors:
                # hybrid: semantic similarity plus exact-word overlap (product names, key names, E- codes)
                score = 0.6 * embeddings.cosine(qvec, embeddings.unpack(vectors[int(r["id"])])) + 0.4 * keyword
            else:
                score = keyword
            if score > 0:
                scored.append(Chunk(int(r["id"]), str(r["source"]), title, str(r["content"]), score))
        scored.sort(key=lambda c: -c.score)
        log.debug("INDEX_SEARCH query=%r mode=%s hits=%d", query[:60], "embedding" if qvec is not None else "keyword", len(scored))
        return scored[:k]


def _title_of(content: str) -> str:
    first = content.splitlines()[0] if content else ""
    return first.lstrip("# ").strip()

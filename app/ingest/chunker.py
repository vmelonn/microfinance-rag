"""
Structure-aware chunking.

Cutting every N tokens is the obvious thing and it is wrong here. A rule split
in half retrieves the threshold without the exemption that qualifies it, and a
chunk with no section path cannot be cited, only quoted. So documents are cut on
the boundaries their authors already put there: headings in HTML and Markdown,
and one whole record for a narrative.

Every chunk carries the heading trail that produced it, and that trail is
prepended to the indexed text. "Threshold: 50,000" is unfindable and uncitable
on its own; "Settlement > Cutoffs > Threshold: 50,000" is both.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

MAX_CHARS = 2400          # a long section is split, but only at paragraph joins
MIN_CHARS = 120           # shorter than this is a heading with no body


@dataclass
class Chunk:
    ordinal: int
    section_path: str
    text: str
    token_estimate: int = 0
    content_hash: str = ""

    def finish(self) -> "Chunk":
        # Prepend the trail so the heading words are searchable in the chunk
        # itself, which is most of why hybrid search finds the right section.
        if self.section_path and not self.text.startswith(self.section_path):
            self.text = "%s\n\n%s" % (self.section_path, self.text)
        self.token_estimate = max(1, len(self.text) // 4)
        self.content_hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]
        return self


@dataclass
class _Section:
    path: list[str] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n\n".join(self.parts)).strip()


def _split_long(text: str) -> list[str]:
    """Split an oversized section at paragraph joins, never mid-sentence."""
    if len(text) <= MAX_CHARS:
        return [text]
    out: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) + 2 > MAX_CHARS:
            out.append(buf.strip())
            buf = para
        else:
            buf = "%s\n\n%s" % (buf, para) if buf else para
    if buf.strip():
        out.append(buf.strip())
    return out


def _emit(sections: list[_Section]) -> list[Chunk]:
    chunks: list[Chunk] = []
    n = 0
    for sec in sections:
        body = sec.text()
        if len(body) < MIN_CHARS:
            continue
        path = " > ".join(sec.path)
        for piece in _split_long(body):
            chunks.append(Chunk(ordinal=n, section_path=path, text=piece).finish())
            n += 1
    return chunks


def chunk_html(html: str) -> list[Chunk]:
    """Walk the document in order, starting a new section at every heading."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav"]):
        tag.decompose()

    body = soup.body or soup
    sections: list[_Section] = []
    trail: list[str] = []
    current = _Section(path=[])

    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "td", "th"]):
        name = el.name
        text = el.get_text(" ", strip=True)
        if not text:
            continue

        if name in ("h1", "h2", "h3", "h4"):
            if current.parts:
                sections.append(current)
            depth = int(name[1]) - 1
            trail = trail[:depth]
            trail.append(text)
            current = _Section(path=list(trail))
        else:
            current.parts.append(text)

    if current.parts:
        sections.append(current)
    return _emit(sections)


def chunk_markdown(md: str) -> list[Chunk]:
    sections: list[_Section] = []
    trail: list[str] = []
    current = _Section(path=[])
    in_fence = False

    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            current.parts.append(line)
            continue

        m = re.match(r"^(#{1,4})\s+(.*)", line) if not in_fence else None
        if m:
            if current.parts:
                sections.append(current)
            depth = len(m.group(1)) - 1
            trail = trail[:depth]
            trail.append(m.group(2).strip())
            current = _Section(path=list(trail))
        else:
            current.parts.append(line)

    if current.parts:
        sections.append(current)
    return _emit(sections)


def chunk_record(title: str, body: str) -> list[Chunk]:
    """A narrative is already one thought. Do not cut it."""
    return [Chunk(ordinal=0, section_path=title, text=body).finish()]

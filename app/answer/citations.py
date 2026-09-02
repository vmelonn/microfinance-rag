"""
Citation verification.

The API emits citations from the same mechanism that produced the text, so a
returned `cited_text` is a real span of a real block rather than something the
model wrote down. That is most of the guarantee, and it is why we pass each
chunk as its own content block instead of asking the model to cite in prose.

This module checks the rest, because "most of the guarantee" is not the same as
all of it and a verification you never run is a comment:

  - every cited span still exists, verbatim, in the block it names
  - every cited block index is one we actually supplied
  - at least one claim carries a citation, when the tier promised grounding
  - no identifier appears in the answer that was not in the input

The last one is separate from citation checking and matters as much. A cited
answer can still contain a fabricated RRN, and an operator sent looking for a
transaction that never existed has been actively misled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# RRNs and correlation IDs as this platform writes them.
IDENTIFIER = re.compile(r"\b(?:[0-9A-F]{12}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                        r"-[0-9a-f]{4}-[0-9a-f]{12}|D-[0-9A-F]{10}|N-[0-9A-F]{10})\b")


@dataclass
class Verdict:
    ok: bool
    text: str
    citations: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = "VERIFIED" if self.ok else "WITHHELD"
        out = ["[%s] %d citation(s)" % (head, len(self.citations))]
        for p in self.problems:
            out.append("  ! %s" % p)
        return "\n".join(out)


def _blocks_from(kwargs: dict) -> list[str]:
    for part in kwargs["messages"][0]["content"]:
        if part.get("type") == "document":
            return [b["text"] for b in part["source"]["content"]]
    return []


def verify(response, kwargs: dict, *, require_citation: bool = True) -> Verdict:
    """
    `response` is an anthropic Message. Returns a Verdict; when not ok, the
    answer is withheld rather than shown with a footnote, because a citation
    that does not resolve is evidence the answer is not grounded.
    """
    blocks = _blocks_from(kwargs)
    supplied_ids = set(IDENTIFIER.findall(" ".join(blocks)))
    for part in kwargs["messages"][0]["content"]:
        if part.get("type") == "text":
            supplied_ids |= set(IDENTIFIER.findall(part["text"]))

    text_parts, cites, problems = [], [], []

    for block in response.content:
        if getattr(block, "type", None) != "text":
            continue
        text_parts.append(block.text)
        for c in (getattr(block, "citations", None) or []):
            d = {
                "cited_text": getattr(c, "cited_text", ""),
                "block_index": getattr(c, "start_block_index", None),
                "type": getattr(c, "type", ""),
            }
            cites.append(d)

            idx = d["block_index"]
            if idx is None or not (0 <= idx < len(blocks)):
                problems.append("citation names block %r, which was not supplied" % idx)
                continue
            span = (d["cited_text"] or "").strip()
            if span and span not in blocks[idx]:
                problems.append("cited span not found verbatim in block %d: %r"
                                % (idx, span[:60]))

    answer = "".join(text_parts)

    invented = set(IDENTIFIER.findall(answer)) - supplied_ids
    for bad in sorted(invented):
        problems.append("identifier %s appears in the answer but was never supplied" % bad)

    if require_citation and not cites and answer.strip():
        problems.append("no citation on any claim, but the tier promised grounding")

    return Verdict(ok=not problems, text=answer, citations=cites, problems=problems)

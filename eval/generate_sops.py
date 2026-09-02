"""
Writes the procedure corpus from the anomaly catalogue.

    python eval/generate_sops.py --out corpus/

Two kinds of document come out:

  sop/SOP-<CODE>.md        one procedure per anomaly, currently in force
  circular/CIR-*.md        fee caps, as a supersession chain

THE SUPERSESSION CHAIN IS THE POINT OF THE CIRCULARS. To prove that retrieval
filters on status and date before it compares anything, the corpus must contain
a document that is topically perfect and no longer in force. So each fee cap
exists twice: a 2023 circular with one figure, and a 2025 circular replacing it
with another. Both are indexed. A question about the current cap has exactly one
correct answer, and a system that skips the filter will confidently return the
2023 number instead. That is a test that fails loudly for the right reason, and
without the chain there is no way to write it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim.catalogue import DOCUMENTED, HELD_OUT  # noqa: E402

# product -> (old cap, old circular date, new cap, new circular date)
FEE_CAPS = [
    ("wallet transfer",  2500,  "2023-03-14", 4000,  "2025-07-01"),
    ("agent cash out",   3500,  "2023-03-14", 5000,  "2025-07-01"),
    ("bill payment",     1500,  "2023-06-20", 2000,  "2025-07-01"),
    ("merchant POS",     5000,  "2023-06-20", 6500,  "2025-11-15"),
    ("ATM withdrawal",   4500,  "2023-09-01", 7000,  "2025-11-15"),
]


def sop_markdown(a) -> str:
    lines = [
        "# %s" % a.title,
        "",
        "**Code:** `%s`  |  **Severity:** %s  |  **Money at risk:** %s"
        % (a.code, a.severity, "yes" if a.money_at_risk else "no"),
        "",
        "## 1. What this is",
        "",
        a.summary,
        "",
        "## 2. How it is detected",
        "",
        a.detection,
        "",
        "This is a deterministic check, not a judgement. It runs as a query:",
        "",
        "```sql",
        a.detection_sql,
        "```",
        "",
        "## 3. Resolution procedure",
        "",
    ]
    for i, step in enumerate(a.steps, start=1):
        lines.append("%d. %s" % (i, step))
    lines += [
        "",
        "## 4. When to escalate",
        "",
        "Escalate when %s." % a.escalate_if,
        "",
        "## 5. What you must not do",
        "",
        "Do not %s." % a.never,
        "",
        "## 6. Closing the case",
        "",
        "Record the RRN, the correlation ID where one exists, the action taken and the "
        "reason code. A case closed without a reason code cannot be retrieved as "
        "precedent later, which makes the next occurrence as expensive as this one.",
        "",
    ]
    return "\n".join(lines)


def circular_markdown(product: str, cap: int, effective: str, *,
                      superseded: bool, replaces: str | None,
                      replaced_by: str | None, ends: str | None) -> str:
    status = "SUPERSEDED" if superseded else "IN FORCE"
    lines = [
        "# Fee cap: %s" % product,
        "",
        "**Status:** %s  |  **Effective from:** %s" % (status, effective),
    ]
    if ends:
        lines.append("**Effective to:** %s" % ends)
    if replaces:
        lines.append("**Replaces:** %s" % replaces)
    if replaced_by:
        lines.append("**Replaced by:** %s" % replaced_by)
    lines += [
        "",
        "## 1. Scope",
        "",
        "This circular sets the maximum fee chargeable on a single %s." % product,
        "",
        "## 2. The cap",
        "",
        "The maximum fee is **PKR %s** per transaction, effective %s."
        % ("{:,}".format(cap), effective),
        "",
        "## 3. Application",
        "",
        "The cap in force is the one effective on the **transaction date**, not the date "
        "the case is reviewed. Applying a current cap to a historic transaction "
        "misstates the refund due.",
        "",
    ]
    if superseded:
        lines += [
            "## 4. Notice",
            "",
            "This circular is no longer in force and is retained for reference only. "
            "Transactions on or after %s are governed by the replacing circular." % (ends or ""),
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="corpus")
    args = p.parse_args()

    out = Path(args.out)
    (out / "sop").mkdir(parents=True, exist_ok=True)
    (out / "circular").mkdir(parents=True, exist_ok=True)

    for a in DOCUMENTED:
        (out / "sop" / ("SOP-%s.md" % a.code)).write_text(
            sop_markdown(a), encoding="utf-8")

    n_circ = 0
    for product, old_cap, old_date, new_cap, new_date in FEE_CAPS:
        slug = product.replace(" ", "-").lower()
        old_id = "CIR-%s-%s" % (slug, old_date[:4])
        new_id = "CIR-%s-%s" % (slug, new_date[:4])

        (out / "circular" / (old_id + ".md")).write_text(
            circular_markdown(product, old_cap, old_date, superseded=True,
                              replaces=None, replaced_by=new_id, ends=new_date),
            encoding="utf-8")
        (out / "circular" / (new_id + ".md")).write_text(
            circular_markdown(product, new_cap, new_date, superseded=False,
                              replaces=old_id, replaced_by=None, ends=None),
            encoding="utf-8")
        n_circ += 2

    print("wrote %d SOPs and %d circulars (%d supersession pairs) to %s/"
          % (len(DOCUMENTED), n_circ, n_circ // 2, out))
    print("every fee cap exists twice, so the date filter can be tested")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

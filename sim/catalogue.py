"""
The anomaly catalogue. One definition, three consumers.

    sim/simulate.py        injects instances of these into the data
    eval/generate_sops.py  writes the procedure document that fixes each one
    eval/run_eval.py       builds questions whose correct answer is known

Keeping them in one place is the point. If the injector and the SOP drift apart,
the evaluation silently starts measuring nothing, because the "correct" document
no longer describes the defect that was planted. Here they cannot drift: the
same row produces the broken record, the procedure, and the question.

DETECTION vs RESOLUTION. Every entry carries both, and they are different jobs.
Detection is a SQL predicate, deterministic and cheap; it belongs in the
reconciliation engine, not in a model. Resolution is prose, because it involves
judgement, escalation paths and exceptions, which is exactly the shape retrieval
is good at. The split mirrors the router: numbers by SQL, words by retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Anomaly:
    code: str
    title: str
    severity: str                  # critical | high | medium | low
    money_at_risk: bool
    summary: str
    detection: str                 # how a machine finds it, in words
    detection_sql: str             # and the predicate that actually does it
    steps: list[str]               # the resolution procedure
    escalate_if: str
    never: str                     # the thing an operator must not do
    questions: list[str] = field(default_factory=list)

    # HELD OUT DEFECTS. A held-out anomaly is injected into the data like any
    # other, but no SOP is written for it. There is nothing to retrieve, and
    # looking the answer up is impossible by construction.
    #
    # This is how we test the capability that actually matters: deriving a
    # response to a defect nobody wrote a procedure for. Real operations meet
    # novel breaks constantly, and a copilot that can only match known ones is
    # a search box.
    #
    # The correct behaviour is neither an answer nor a refusal. It is a third
    # thing: say plainly that no procedure covers this, retrieve the nearest
    # analogous procedures, propose an approach derived from them, cite what it
    # was derived from, and mark the result as unverified so a human signs it
    # off. `analogous_to` is the ground truth for that: the SOPs a correct
    # derivation should have reasoned from.
    held_out: bool = False
    analogous_to: list[str] = field(default_factory=list)


CATALOGUE: list[Anomaly] = [

    Anomaly(
        code="ORPHAN_SWITCH",
        title="Approved at the switch, absent from the ledger",
        severity="critical",
        money_at_risk=True,
        summary=("The switch authorised and the customer was debited, but no "
                 "corresponding posting exists in our books."),
        detection="A transaction with response code 00 that has no ledger entry.",
        detection_sql=("SELECT t.rrn FROM sim_transactions t "
                       "LEFT JOIN ledger_entries l ON l.rrn = t.rrn "
                       "WHERE t.resp_code = '00' AND l.rrn IS NULL"),
        steps=[
            "Confirm the authorisation in the switch log using the RRN, not the STAN.",
            "Check the following settlement slot before treating it as a true break; "
            "a posting that crossed a cutoff is a timing difference, not a loss.",
            "If still absent, check whether a compensating reversal was attempted and failed.",
            "Raise a manual posting request with the amount from the switch record.",
            "Record the RRN and the slot in the exception log before closing.",
        ],
        escalate_if="the amount exceeds PKR 50,000 or the transaction is older than two slots",
        never="post to the ledger directly from the reconciliation tool",
        questions=["what do I do when the switch approved but the ledger has nothing",
                   "an approved transaction is missing from our books"],
    ),

    Anomaly(
        code="ORPHAN_LEDGER",
        title="Posted to the ledger, unknown to the switch",
        severity="critical",
        money_at_risk=True,
        summary=("Our books record a movement the network has no record of. Usually a "
                 "replayed message or a manual posting made against the wrong RRN."),
        detection="A ledger entry whose RRN does not appear in the switch extract.",
        detection_sql=("SELECT l.rrn FROM ledger_entries l "
                       "LEFT JOIN sim_transactions t ON t.rrn = l.rrn WHERE t.rrn IS NULL"),
        steps=[
            "Check whether the RRN was mistyped from a neighbouring transaction.",
            "Look for a manual posting in the same window by the same operator.",
            "Confirm with the acquirer that no authorisation exists for that RRN.",
            "If confirmed spurious, raise a reversing entry; do not delete the original.",
        ],
        escalate_if="the entry was created by an automated process rather than a person",
        never="delete the ledger entry, because the audit trail must show both sides",
        questions=["the ledger has a transaction the switch never saw",
                   "how do I reverse a posting made against the wrong RRN"],
    ),

    Anomaly(
        code="AMOUNT_MISMATCH",
        title="Same reference, different amounts",
        severity="high",
        money_at_risk=True,
        summary="The switch and the ledger agree a transaction happened but not for how much.",
        detection="Matched RRN where the two amounts differ by more than the known fee.",
        detection_sql=("SELECT t.rrn FROM sim_transactions t JOIN ledger_entries l "
                       "ON l.rrn = t.rrn WHERE abs(t.amount_minor - l.amount_minor) > 0"),
        steps=[
            "Establish which side carries the fee; the switch amount is usually gross.",
            "Check the fee schedule in force on the transaction date, not today's.",
            "If the difference equals a known fee, reclassify as a fee posting, not a break.",
            "If it does not, the switch amount is authoritative and the ledger is corrected.",
        ],
        escalate_if="the ledger amount is larger than the switch amount",
        never="assume the larger figure is correct",
        questions=["the amounts do not match between switch and ledger",
                   "which side is authoritative when amounts disagree"],
    ),

    Anomaly(
        code="DUPLICATE_POSTING",
        title="One authorisation, two postings",
        severity="critical",
        money_at_risk=True,
        summary="The same RRN was posted to the ledger more than once, double-charging the customer.",
        detection="More than one ledger entry sharing an RRN.",
        detection_sql=("SELECT rrn FROM ledger_entries GROUP BY rrn HAVING count(*) > 1"),
        steps=[
            "Confirm both entries carry the same RRN and are not a debit and its matching credit.",
            "Identify which posting came second by created_at, and reverse that one.",
            "Check whether the idempotency claim was released early, which is the usual cause.",
            "Notify the customer before they notice the second debit.",
        ],
        escalate_if="more than five duplicates appear in the same slot, which suggests a systemic replay",
        never="reverse both entries",
        questions=["the customer was charged twice for one transaction",
                   "how do I handle a duplicate ledger posting"],
    ),

    Anomaly(
        code="UNBALANCED_ENTRY",
        title="Double-entry that does not balance",
        severity="critical",
        money_at_risk=False,
        summary="The debit and credit legs of a single posting do not sum to zero.",
        detection="Grouping ledger entries by RRN, the signed amounts do not net to zero.",
        detection_sql=("SELECT rrn FROM ledger_entries GROUP BY rrn "
                       "HAVING sum(CASE WHEN kind='debit' THEN -amount_minor "
                       "ELSE amount_minor END) <> 0"),
        steps=[
            "Treat this as a software defect, not an operational exception.",
            "Do not correct the individual entry; the imbalance is evidence.",
            "Capture the RRN, the correlation ID and the service that wrote it.",
            "Raise to engineering the same day; a ledger that does not balance cannot be trusted.",
        ],
        escalate_if="always, immediately",
        never="patch the ledger to make the totals agree",
        questions=["the ledger does not balance for one transaction",
                   "debit and credit legs do not net to zero"],
    ),

    Anomaly(
        code="STALE_REVERSAL",
        title="Reversal recorded, original never reversed",
        severity="high",
        money_at_risk=True,
        summary=("A reversal was acknowledged but the original debit still stands, so the "
                 "customer remains out of pocket."),
        detection="A reversal record whose original transaction has no offsetting entry.",
        detection_sql=("SELECT r.rrn FROM sim_transactions r WHERE r.resp_code='00' "
                       "AND r.rrn IN (SELECT rrn FROM reversals) "
                       "AND r.rrn NOT IN (SELECT rrn FROM ledger_entries WHERE kind='credit')"),
        steps=[
            "Confirm the reversal was acknowledged by the switch, not merely sent.",
            "Check whether the compensating path failed after the reversal was logged.",
            "Re-issue the reversal rather than posting a manual credit, so the switch agrees.",
            "If the switch rejects a second reversal, then post manually and note why.",
        ],
        escalate_if="the reversal is more than one business day old",
        never="post a manual credit before confirming the switch will not reverse",
        questions=["a reversal was promised but never arrived",
                   "the customer is still debited after a reversal"],
    ),

    Anomaly(
        code="APPROVED_BUT_DECLINED",
        title="Approved upstream, declined downstream",
        severity="high",
        money_at_risk=True,
        summary="Response code 00 at the switch but the ledger records the transaction as declined.",
        detection="Response code 00 paired with a ledger status of declined.",
        detection_sql=("SELECT t.rrn FROM sim_transactions t JOIN ledger_entries l "
                       "ON l.rrn=t.rrn WHERE t.resp_code='00' AND l.status='declined'"),
        steps=[
            "Establish the ordering from the correlation ID trace across services.",
            "A risk decline after switch approval means the saga compensation did not run.",
            "Confirm whether the customer was debited; the switch view is authoritative.",
            "If debited, treat as ORPHAN_SWITCH from this point and follow that procedure.",
        ],
        escalate_if="the compensation path shows no attempt at all",
        never="close it as a decline without checking whether money moved",
        questions=["switch says approved but our ledger says declined",
                   "risk declined after the switch approved"],
    ),

    Anomaly(
        code="NEGATIVE_BALANCE",
        title="Posting drives an account below zero",
        severity="high",
        money_at_risk=True,
        summary="A wallet balance went negative, which the solvency invariant forbids.",
        detection="Running balance for an account falls below zero at any point.",
        detection_sql=("SELECT account_id FROM ledger_entries GROUP BY account_id "
                       "HAVING sum(CASE WHEN kind='debit' THEN -amount_minor "
                       "ELSE amount_minor END) < 0"),
        steps=[
            "Identify the posting that crossed zero, by timestamp not by amount.",
            "Check whether a hold or authorisation was released before capture.",
            "Confirm the balance check ran; a negative balance usually means it was skipped.",
            "Freeze further debits on the account until the position is explained.",
        ],
        escalate_if="the account belongs to an agent rather than a customer",
        never="top the account up to zero to make the report clean",
        questions=["a wallet balance went negative",
                   "solvency invariant was violated on an account"],
    ),

    Anomaly(
        code="FUTURE_DATED",
        title="Transaction timestamped in the future",
        severity="medium",
        money_at_risk=False,
        summary="A record carries an occurrence time later than the moment it was ingested.",
        detection="occurred_at is greater than the ingestion time.",
        detection_sql="SELECT rrn FROM sim_transactions WHERE occurred_at > ingested_at",
        steps=[
            "Almost always a terminal or host clock that has drifted, not fraud.",
            "Check whether other transactions from the same terminal share the offset.",
            "Record the offset; it is needed to re-slot the transaction correctly.",
            "Re-slot into the session its corrected time falls in, then reconcile again.",
        ],
        escalate_if="the offset exceeds one session length, since slotting is then unreliable",
        never="silently rewrite the timestamp without recording the original",
        questions=["a transaction is dated in the future",
                   "terminal clock drift is putting transactions in the wrong slot"],
    ),

    Anomaly(
        code="MISSING_CORRELATION",
        title="No correlation ID, untraceable",
        severity="medium",
        money_at_risk=False,
        summary="A record arrived with no correlation ID, so its path cannot be reconstructed.",
        detection="correlation_id is null or empty on an otherwise valid record.",
        detection_sql="SELECT rrn FROM sim_transactions WHERE correlation_id IS NULL",
        steps=[
            "Check whether the request bypassed the gateway, which is where IDs are minted.",
            "Correlate by RRN and timestamp instead, accepting it is weaker evidence.",
            "Note in the exception log that the trace is incomplete.",
        ],
        escalate_if="a whole batch is missing IDs, which points at a misrouted client",
        never="invent a correlation ID to make the trace look complete",
        questions=["a transaction has no correlation id",
                   "how do I trace something that bypassed the gateway"],
    ),

    Anomaly(
        code="SETTLEMENT_GAP",
        title="Slot with no settlement file",
        severity="high",
        money_at_risk=False,
        summary="A session closed but no settlement file was received for it.",
        detection="A closed slot with no corresponding settlement record.",
        detection_sql=("SELECT slot_key FROM slots WHERE closed = 1 "
                       "AND slot_key NOT IN (SELECT slot_key FROM settlement_files)"),
        steps=[
            "Check the SFTP path before assuming the file was never produced.",
            "Confirm the slot actually closed rather than the fetcher failing to run.",
            "Request a re-send for the specific slot, not the whole day.",
            "Do not reconcile the slot partially; wait for the complete file.",
        ],
        escalate_if="two consecutive slots are missing files",
        never="reconcile a slot against an incomplete settlement file",
        questions=["no settlement file arrived for a slot",
                   "can I reconcile a slot with a partial file"],
    ),

    Anomaly(
        code="FEE_OVERCHARGE",
        title="Fee above the schedule cap",
        severity="medium",
        money_at_risk=True,
        summary="A fee was charged above the cap in force on the transaction date.",
        detection="Fee amount exceeds the cap for that product on that date.",
        detection_sql=("SELECT rrn FROM ledger_entries l JOIN fee_schedule f "
                       "ON f.product = l.product AND l.posted_at BETWEEN f.effective_from "
                       "AND coalesce(f.effective_to, '9999-12-31') WHERE l.fee_minor > f.cap_minor"),
        steps=[
            "Use the schedule in force on the transaction date, not the current one.",
            "Confirm the product classification, since caps differ by product.",
            "Refund the excess only, not the whole fee.",
            "Check whether other transactions on the same tariff are affected.",
        ],
        escalate_if="the overcharge affects more than one customer on the same tariff",
        never="apply today's fee cap to a historic transaction",
        questions=["a fee was charged above the cap",
                   "which fee schedule applies to an old transaction"],
    ),
    # ---------------------------------------------------------------------
    # HELD OUT. Injected into the data, but generate_sops.py writes no
    # procedure for any of these. Each was chosen because a competent operator
    # could reason it out from the procedures that DO exist, which is exactly
    # what we are asking the system to do.
    # ---------------------------------------------------------------------

    Anomaly(
        code="TRIPLE_POSTING",
        title="One authorisation, three or more postings",
        severity="critical",
        money_at_risk=True,
        summary=("The same RRN was posted three or more times. Not merely a worse "
                 "duplicate: reversing 'the second one' is ambiguous when there are two "
                 "extras, and a systemic replay is more likely than an isolated slip."),
        detection="More than two ledger entries sharing an RRN.",
        detection_sql=("SELECT rrn FROM ledger_entries GROUP BY rrn HAVING count(*) > 2"),
        steps=[],
        escalate_if="",
        never="",
        held_out=True,
        analogous_to=["DUPLICATE_POSTING", "UNBALANCED_ENTRY"],
        questions=["the same transaction was posted three times",
                   "how do I handle more than two duplicate postings"],
    ),

    Anomaly(
        code="CURRENCY_MISMATCH",
        title="Same reference, different currency",
        severity="high",
        money_at_risk=True,
        summary=("Switch and ledger agree on the number but not the unit. Comparing the "
                 "amounts alone shows a match, so the ordinary amount check misses it "
                 "entirely."),
        detection="Matched RRN where the currency codes differ.",
        detection_sql=("SELECT t.rrn FROM sim_transactions t JOIN ledger_entries l "
                       "ON l.rrn = t.rrn WHERE t.currency <> l.currency"),
        steps=[],
        escalate_if="",
        never="",
        held_out=True,
        analogous_to=["AMOUNT_MISMATCH", "FEE_OVERCHARGE"],
        questions=["the currencies do not match between switch and ledger",
                   "amounts agree but the currency codes differ"],
    ),

    Anomaly(
        code="SLOT_OVERLAP",
        title="Transaction claimed by two settlement slots",
        severity="high",
        money_at_risk=False,
        summary=("A transaction appears in the settlement files of two consecutive slots, "
                 "so reconciling both double-counts it."),
        detection="An RRN present in more than one slot's settlement file.",
        detection_sql=("SELECT rrn FROM settlement_lines GROUP BY rrn "
                       "HAVING count(DISTINCT slot_key) > 1"),
        steps=[],
        escalate_if="",
        never="",
        held_out=True,
        analogous_to=["SETTLEMENT_GAP", "DUPLICATE_POSTING", "FUTURE_DATED"],
        questions=["a transaction appears in two settlement files",
                   "the same RRN is in two slots"],
    ),

    Anomaly(
        code="AGENT_FLOAT_NEGATIVE",
        title="Agent float driven below zero by a customer transaction",
        severity="high",
        money_at_risk=True,
        summary=("An agent's float went negative. The customer-account procedure exists, "
                 "but an agent float is a different instrument with a different escalation "
                 "path, and freezing it stops a whole branch rather than one wallet."),
        detection="Running float balance for an agent falls below zero.",
        detection_sql=("SELECT agent_id FROM float_movements GROUP BY agent_id "
                       "HAVING sum(delta_minor) < 0"),
        steps=[],
        escalate_if="",
        never="",
        held_out=True,
        analogous_to=["NEGATIVE_BALANCE", "ORPHAN_SWITCH"],
        questions=["an agent float went negative",
                   "should I freeze an agent account the way I would a wallet"],
    ),
]


BY_CODE = {a.code: a for a in CATALOGUE}
CODES = [a.code for a in CATALOGUE]

DOCUMENTED = [a for a in CATALOGUE if not a.held_out]
HELD_OUT = [a for a in CATALOGUE if a.held_out]


# ---------------------------------------------------------------------------
# Customer language -> operational language.
#
# The two corpora describe the same defects in vocabulary that does not
# overlap at all. A narrative says "the account was debited twice"; the
# procedure for that defect is titled "One authorisation, two postings". There
# is no shared term for a keyword index to match and no shared framing for an
# embedding to place nearby, so retrieving precedent by searching with an
# operational phrasing was never going to work. That is a property of the
# domain, not a tuning problem: complaints are written by customers and
# procedures are written by engineers.
#
# So precedent, like procedure, is a LOOKUP when the class is known. The recon
# engine identified the defect deterministically; asking a search engine to
# rediscover it from prose throws that away. Search remains the path for a
# novel defect, where by definition no code exists.
#
# Several complaint reasons map to one anomaly, which is the honest direction:
# a customer reports a symptom, and one operational cause produces several
# symptoms. Reasons with no clean operational counterpart map to None and are
# reachable only by search.
# ---------------------------------------------------------------------------

REASON_TO_ANOMALY: dict[str, str | None] = {
    "double_debit":       "DUPLICATE_POSTING",
    "duplicate_bill":     "DUPLICATE_POSTING",
    "wrong_amount":       "AMOUNT_MISMATCH",
    "failed_but_debited": "ORPHAN_SWITCH",
    "no_credit":          "ORPHAN_SWITCH",
    "reversal_missing":   "STALE_REVERSAL",
    "fee_disputed":       "FEE_OVERCHARGE",
    "stale_balance":      "NEGATIVE_BALANCE",
    "atm_short":          "AMOUNT_MISMATCH",
    "unauthorised":       "APPROVED_BUT_DECLINED",
    # No clean operational counterpart. An agent keeping the cash is a conduct
    # problem, not a ledger defect, and a merchant withholding goods is a
    # commercial dispute. Both reach the corpus only by search, which is
    # correct rather than a gap.
    "agent_no_cash":      None,
    "merchant_no_goods":  None,
}

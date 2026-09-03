"""
Run the read-only checks before answering, and ask when something is missing.

THE GAP THIS CLOSES. The answer path had read-only tools and never used them.
A procedure would say "Confirm both entries carry the same RRN" and the system
would hand that instruction to a person, while holding a query registry that
could confirm it in nine milliseconds. It escalated without doing the checking
it was capable of, which is the least useful kind of escalation: it costs a
person's attention and arrives with nothing they did not already have.

So before generating, run the checks the defect class implies, and put the
results in the prompt as established facts. The model then reasons over what is
actually true of this break rather than only over what the procedure says in
general.

ASKING IS AN OUTCOME. Some checks need an identifier the question did not
supply. Answering generically in that case is worse than useless, because a
generic answer looks like a specific one. If a check cannot run for want of an
RRN or an account, the right response is to ask for it, and `needs` carries what
would unlock the rest.

STILL READ-ONLY, STILL SHOWN. Every check is a registered query or a lookup, so
the same four fences apply and every statement is returned with its result.
Nothing here can write, and nothing here decides anything: it gathers facts and
a person still closes the break.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.answer import lookup as lookup_mod
from app.answer import sql_tool

RRN = re.compile(r"\b([0-9A-F]{12})\b")
ACCOUNT = re.compile(r"\b(acc_[a-z]+_\d+)\b")
MSISDN = re.compile(r"\b(03\d{9})\b")


@dataclass
class Check:
    label: str
    ran: bool
    statement: str = ""
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    summary: str = ""
    needs: str = ""          # what was missing, when it could not run


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)

    def facts(self) -> dict:
        """The shape prompt.build takes as established facts."""
        return {c.label: c.summary for c in self.checks if c.ran and c.summary}

    def question_to_ask(self) -> str:
        """One clarifying question, or empty when nothing is blocked."""
        if not self.needs:
            return ""
        if len(self.needs) == 1:
            return ("To check this properly I need the %s. Without it I can "
                    "only describe the procedure in general, which is not the "
                    "same as telling you what is true of this break."
                    % self.needs[0])
        return ("To check this properly I need: %s. Without them I can only "
                "describe the procedure in general."
                % ", ".join(self.needs))


def _summarise(r) -> str:
    if r.error:
        return ""
    if not r.rows:
        return "no rows"
    if len(r.rows) == 1 and len(r.rows[0]) == 1:
        v = r.rows[0][0]
        return "{:,}".format(v) if isinstance(v, int) else str(v)
    return "; ".join(
        " ".join("%s=%s" % (c, v) for c, v in zip(r.columns, row))
        for row in r.rows[:4])


def run(question: str, *, anomaly_code: str | None,
        data_db: str | None, ledger_db: str | None,
        rrn: str | None = None) -> Preflight:
    out = Preflight()

    rrn = rrn or (RRN.search(question.upper()).group(1)
                  if RRN.search(question.upper()) else None)
    account = (ACCOUNT.search(question) or MSISDN.search(question))
    account = account.group(1) if account else None

    # 1. Is there a dispute against this transaction? Needs an RRN.
    if data_db:
        if rrn:
            r = sql_tool.run(data_db, sql_tool.BY_NAME["disputes_for_rrn"],
                             {"rrn": rrn})
            out.checks.append(Check(
                label="disputes against this RRN", ran=not r.error,
                statement=r.sql, columns=r.columns, rows=[list(x) for x in r.rows],
                summary=_summarise(r) or r.error))
        else:
            out.checks.append(Check(
                label="disputes against this RRN", ran=False,
                needs="RRN of the transaction"))
            out.needs.append("RRN of the transaction")

    # 2. How common is this defect class right now? No identifier needed, and it
    #    is the difference between "an isolated slip" and "a systemic replay",
    #    which several procedures escalate on.
    if data_db and anomaly_code:
        r = sql_tool.run(data_db, sql_tool.BY_NAME["disputes_by_reason"])
        if not r.error:
            out.checks.append(Check(
                label="current dispute mix", ran=True, statement=r.sql,
                columns=r.columns, rows=[list(x) for x in r.rows[:6]],
                summary=_summarise(r)))

    # 3. The account position, when the question names one.
    if ledger_db and account and not ledger_db.startswith("http"):
        v = lookup_mod.LocalLedger(ledger_db).balance(account)
        out.checks.append(Check(
            label="balance on %s" % account, ran=not v.error,
            summary=("PKR {:,.2f} as at {}".format(v.value / 100, v.as_at)
                     if not v.error else v.error)))

    return out

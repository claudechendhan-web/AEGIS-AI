"""Escalation: what the agent cannot do alone, written down for you.

The agent is autonomous, and autonomy has one structural limit. Some steps
cannot be automated at all, because they need an identity, a credential, a
judgement, or a bank account: opening a marketplace account, deciding whether
a licence is safe to resell, invoicing a buyer, fetching a 4GB model.

Rather than stall silently, the agent files a request. An agent that quietly
gives up looks identical to one that is working, so the queue is what keeps
"blocked" and "busy" distinguishable.

    python agent.py inbox
    python agent.py resolve 7 --note "listed at example.com/x"

Request kinds, and what each one actually needs from you:

    account     a venue to create
    credential  a secret the agent must not hold
    approval    a decision: publish this, price it here
    payment     money to move between accounts
    legal       a judgement with consequences you accept
    resource    hardware, disk, or a model to fetch
    contact     a human conversation

Notification is best-effort and never load-bearing. If your machine has no
toast support the queue on disk is still the source of truth, and
`--notify none` turns it off.
"""

from escalation.requests import (
    ANSWERED,
    DISMISSED,
    EXPIRED,
    OPEN,
    Kind,
    Request,
    RequestQueue,
    Severity,
    detect_blockers,
    notify,
    notify_new,
)

__all__ = [
    "ANSWERED",
    "DISMISSED",
    "EXPIRED",
    "OPEN",
    "Kind",
    "Request",
    "RequestQueue",
    "Severity",
    "detect_blockers",
    "notify",
    "notify_new",
]

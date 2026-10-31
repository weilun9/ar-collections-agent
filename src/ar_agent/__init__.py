"""AR Collections Agent: an accounts-receivable collections agent.

Module map (with the matching section of the product spec):
    tiering.py    §4   tiering decision matrix
    policy.py     §5   L4 runtime policy layer (hard rules, independent of the LLM)
    breaker.py    §5   L6 circuit breakers
    templates.py  §5   L2 tool design (only pre-approved templates can be sent)
    inbound.py    §4.2 keyword-driven forced escalation to a person
    orchestrator  §3.1 daily scan pipeline
    report.py     §3.1 weekly report
"""

__version__ = "1.0.0"

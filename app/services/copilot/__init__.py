"""V1.2.2 Phase C1 — read-only copilot (provider/grounding/ranking/today).

Read-only by construction: nothing here writes financial state, creates or
mutates tasks, or transitions anything. The only DB write in the whole C1
surface is the ``copilot_runs`` audit row reusing the A+B ``log_context_run``
(written by the router, never by the service layer).
"""

from app.services.copilot import llm, prompts, ranking, today  # noqa: F401

__all__ = ["llm", "prompts", "ranking", "today"]

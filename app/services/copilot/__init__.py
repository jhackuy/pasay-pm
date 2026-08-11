"""V1.2.2 Phase C1 — read-only copilot (provider/grounding/ranking/today).

Read-only by construction: nothing here writes financial state, creates or
mutates tasks, or transitions anything. The only DB write in the whole C1
surface is the ``copilot_runs`` audit row reusing the A+B ``log_context_run``.

(WIP — llm + ranking/today/prompts land together; see V122_PHASE_C1_BRIEF.)
"""

from app.services.copilot import llm  # noqa: F401

__all__ = ["llm"]

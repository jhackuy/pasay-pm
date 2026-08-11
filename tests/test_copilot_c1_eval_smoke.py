"""Live-LLM eval smoke (V1.2.2 C1) — network-enabled, @pytest.mark.eval.

Excluded from the default suite via ``addopts = -m "not eval"`` in
``pytest.ini``. Run explicitly with:

    .venv/bin/python -m pytest tests/ -m eval -q

Requires ``DEEPSEEK_API_KEY`` (or ``COPILOT_LLM_API_KEY``) in the environment.
Each smoke seeds ``pasay_pm_test``, grounds, calls the real provider, and
scores the raw output on the 6 eval dimensions.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.services.copilot import llm

_HARNESS_PATH = Path(__file__).parent / "eval" / "copilot_eval.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("copilot_eval", _HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def eval_harness():
    return _load_harness()


@pytest.fixture()
def eval_provider():
    try:
        return llm.provider_config().name
    except Exception as exc:  # noqa: BLE001 - fail fast with a clear message
        pytest.fail(f"no usable LLM provider config: {exc}")


@pytest.mark.eval
def test_eval_smoke_mandated_scenario(eval_harness, eval_provider):
    """req 6 mandated case against the live default provider."""
    record = eval_harness.run_scenario("mandated", eval_provider)
    assert record["scenario"] == "mandated"
    scores = record["scores"]
    # minimum bar for the smoke: no hallucinations, safe, concise, grounded
    assert scores["hallucination"]["binary"], scores["hallucination"]["note"]
    assert scores["unsafe_recommendation"]["binary"], scores["unsafe_recommendation"]["note"]
    assert scores["verbosity"]["binary"], scores["verbosity"]["note"]


@pytest.mark.eval
def test_eval_smoke_injection_scenario(eval_harness, eval_provider):
    """Injection-planted note stays data (never followed) on the live model."""
    record = eval_harness.run_scenario("injection", eval_provider)
    assert record["scenario"] == "injection"
    scores = record["scores"]
    assert scores["unsafe_recommendation"]["binary"], scores["unsafe_recommendation"]["note"]
    assert scores["hallucination"]["binary"], scores["hallucination"]["note"]

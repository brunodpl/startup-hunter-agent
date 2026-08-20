"""Tests del backstop de coste."""

from __future__ import annotations

import pytest

from startup_hunter.budget import Budget, BudgetExhausted, default_max_calls


def test_default_max_calls_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("STARTUP_HUNTER_MAX_CALLS", raising=False)
    assert default_max_calls() == 12


def test_env_overrides_default(monkeypatch):
    monkeypatch.setenv("STARTUP_HUNTER_MAX_CALLS", "3")
    assert default_max_calls() == 3


def test_garbage_env_does_not_break_a_run(monkeypatch):
    """Un valor mal escrito cae al defecto en vez de tumbar el hunt."""
    monkeypatch.setenv("STARTUP_HUNTER_MAX_CALLS", "doce")
    assert default_max_calls() == 12


def test_counters_are_independent_per_source():
    budget = Budget(max_calls_per_source=2)
    budget.consume("tavily")
    budget.consume("tavily")
    assert budget.remaining("tavily") == 0
    # Agotar un conector no debe afectar a los demás.
    assert budget.remaining("igape") == 2
    budget.consume("igape")
    assert budget.used("igape") == 1


def test_consume_raises_once_exhausted():
    budget = Budget(max_calls_per_source=1)
    budget.consume("tavily")
    with pytest.raises(BudgetExhausted) as exc:
        budget.consume("tavily")
    # El mensaje debe decir qué variable ajustar: lo lee un humano en el informe.
    assert "STARTUP_HUNTER_MAX_CALLS" in str(exc.value)


def test_snapshot_reports_usage():
    budget = Budget(max_calls_per_source=5)
    budget.consume("igape")
    budget.consume("igape")
    budget.consume("bdns")
    assert budget.snapshot() == {"igape": 2, "bdns": 1}

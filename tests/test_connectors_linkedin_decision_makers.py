"""Tests del paso 3 (decisores) con ``_run_actor`` simulado: sin red ni token."""

from __future__ import annotations

from startup_hunter.budget import Budget
from startup_hunter.connectors import linkedin
from startup_hunter.connectors.linkedin import LinkedInEnricher

_PEOPLE = "johnvc/linkedin-company-employees-api"
_COMPANY_URL = "https://www.linkedin.com/company/acme/"


def _enrich(monkeypatch, people_items, location="Galicia"):
    calls: list[tuple[str, dict]] = []

    def fake_run_actor(self, actor_id, run_input):
        calls.append((actor_id, run_input))
        if actor_id == linkedin._SEARCH_ACTOR:
            return [{"url": _COMPANY_URL}]
        if actor_id == linkedin._DETAIL_ACTOR:
            return [{"website": "https://acme.es", "funding": "Seed"}]
        return people_items

    monkeypatch.setattr(LinkedInEnricher, "_run_actor", fake_run_actor)
    enrichment = LinkedInEnricher(client=object()).enrich("Acme", location, Budget())
    return enrichment, calls


def test_decision_makers_use_company_employees_actor_input(monkeypatch):
    _, calls = _enrich(monkeypatch, [])

    actor_id, run_input = calls[-1]
    assert actor_id == _PEOPLE
    assert run_input["companies"] == [_COMPANY_URL]
    assert run_input["titleKeywords"] == linkedin._DEFAULT_TITLES
    assert run_input["locations"] == ["Galicia"]
    assert run_input["maxResultsPerCompany"] == 5


def test_decision_makers_map_actor_output_and_skip_error_rows(monkeypatch):
    enrichment, _ = _enrich(
        monkeypatch,
        [
            {
                "result_type": "employee",
                "fullName": "Ana Pérez",
                "headline": "Building Acme",
                "currentTitle": "CEO & Co-Founder",
                "profileUrl": "https://www.linkedin.com/in/anaperez/",
            },
            {
                "result_type": "error",
                "error_type": "no_results",
                "error_message": "No employees matched.",
            },
        ],
    )

    assert len(enrichment.decision_makers) == 1
    dm = enrichment.decision_makers[0]
    assert dm.name == "Ana Pérez"
    assert dm.role == "CEO & Co-Founder"
    assert dm.linkedin_url == "https://www.linkedin.com/in/anaperez/"
    assert dm.source_url == _COMPANY_URL


def test_only_error_rows_are_noted_not_mapped(monkeypatch):
    enrichment, _ = _enrich(
        monkeypatch,
        [{"error_type": "company_not_found", "error_message": "Company not found."}],
    )

    assert enrichment.decision_makers == []
    assert any("Company not found." in n for n in enrichment.notes)

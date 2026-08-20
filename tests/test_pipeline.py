"""Tests del pipeline completo, con conectores inyectados (sin red ni API keys)."""

from __future__ import annotations

import pytest

from startup_hunter.budget import Budget
from startup_hunter.connectors.base import ConnectorUnavailable
from startup_hunter.pipeline import run_hunt, to_candidate
from startup_hunter.schema import Enrichment, QuerySpec, RawHit


class _FakeConnector:
    """Conector de prueba: devuelve hits fijos, o revienta si se le pide."""

    def __init__(self, name: str, hits: list[RawHit] | None = None, boom: Exception | None = None):
        self.name = name
        self._hits = hits or []
        self._boom = boom
        self.calls = 0

    def fetch(self, spec: QuerySpec, budget: Budget) -> list[RawHit]:
        self.calls += 1
        if self._boom is not None:
            raise self._boom
        return self._hits


class _FakeEnricher:
    name = "linkedin"

    def __init__(self) -> None:
        self.enriched: list[str] = []

    def enrich(self, company: str, location, budget) -> Enrichment:
        self.enriched.append(company)
        return Enrichment(linkedin_url=f"https://linkedin.com/company/{len(self.enriched)}")


def _hit(name: str, url: str, source: str = "igape", **kwargs) -> RawHit:
    return RawHit(name=name, source_url=url, source=source, **kwargs)


def _spec(**kwargs) -> QuerySpec:
    return QuerySpec(query="agritech", location="Galicia", **kwargs)


# --- normalizacion ---------------------------------------------------------


def test_to_candidate_fills_defaults_from_the_spec():
    candidate = to_candidate(_hit("Acme", "https://acme.example"), _spec())

    assert candidate.name == "Acme"
    assert candidate.location == "Galicia"
    assert candidate.sector == "agritech"
    assert candidate.found_via == "igape"


def test_to_candidate_rejects_a_hit_without_usable_evidence():
    """Sin evidencia valida no hay candidata: se descarta en vez de colarse."""
    broken = RawHit.model_construct(name="X", source_url="not-a-url", source="igape")

    assert to_candidate(broken, _spec()) is None


# --- orquestacion ----------------------------------------------------------


def test_runs_every_connector_and_merges_the_results():
    connectors = [
        _FakeConnector("igape", [_hit("Acme Viticultura", "https://example.com/a")]),
        _FakeConnector("tavily", [_hit("Demo Agro Robotics", "https://demo-agro.example", source="tavily")]),
    ]

    result = run_hunt(_spec(), connectors)

    assert {c.name for c in result.candidates} == {"Acme Viticultura", "Demo Agro Robotics"}
    assert set(result.source_notes) == {"igape", "tavily"}
    assert result.stats["candidates_total"] == 2


def test_a_failing_connector_does_not_bring_down_the_hunt():
    """Un hunt a medias es util; uno abortado, no."""
    connectors = [
        _FakeConnector("igape", boom=RuntimeError("502 del servidor")),
        _FakeConnector("tavily", [_hit("Demo Agro Robotics", "https://demo-agro.example", source="tavily")]),
    ]

    result = run_hunt(_spec(), connectors)

    assert [c.name for c in result.candidates] == ["Demo Agro Robotics"]
    assert "ERROR" in result.source_notes["igape"]
    assert "502 del servidor" in result.source_notes["igape"]


def test_an_unavailable_connector_is_reported_as_disabled_not_as_a_failure():
    connectors = [
        _FakeConnector("tavily", boom=ConnectorUnavailable("Falta TAVILY_API_KEY")),
        _FakeConnector("igape", [_hit("Acme", "https://example.com/a")]),
    ]

    result = run_hunt(_spec(), connectors)

    assert "desactivado" in result.source_notes["tavily"]
    assert "ERROR" not in result.source_notes["tavily"]
    assert len(result.candidates) == 1


def test_the_same_company_from_two_sources_becomes_one_candidate():
    connectors = [
        _FakeConnector("igape", [_hit("Acme Viticultura S.L.", "https://example.com/a")]),
        _FakeConnector(
            "tavily",
            [_hit("Acme Viticultura", "https://acme-viticultura.example", source="tavily", website="https://acme-viticultura.example")],
        ),
    ]

    result = run_hunt(_spec(), connectors)

    assert len(result.candidates) == 1
    # Se conserva la evidencia de ambas fuentes: es lo que hace fuerte a la candidata.
    assert {s.found_via for s in result.candidates[0].sources} == {"igape", "tavily"}


def test_empty_hunt_is_a_valid_result():
    result = run_hunt(_spec(), [_FakeConnector("igape", [])])

    assert result.candidates == []
    assert result.stats["candidates_total"] == 0


def test_run_hunt_requires_at_least_one_connector():
    with pytest.raises(ValueError):
        run_hunt(_spec(), [])


def test_all_connectors_share_one_budget():
    """El tope es por conector, pero el objeto es unico: asi se contabiliza el run entero."""
    budget = Budget(max_calls_per_source=5)
    connectors = [_FakeConnector("igape", []), _FakeConnector("tavily", [])]

    run_hunt(_spec(), connectors, budget=budget)

    assert all(c.calls == 1 for c in connectors)


# --- enriquecimiento -------------------------------------------------------


def test_enrichment_is_capped_to_limit_cost_and_personal_data(monkeypatch):
    monkeypatch.setenv("STARTUP_HUNTER_MAX_ANALYSES", "2")
    hits = [_hit(f"Empresa {i}", f"https://igape.gal/{i}") for i in range(5)]
    enricher = _FakeEnricher()

    result = run_hunt(_spec(), [_FakeConnector("igape", hits)], enricher=enricher)

    assert len(enricher.enriched) == 2
    assert result.stats["enriched"] == 2
    assert result.candidates[0].enrichment is not None
    assert result.candidates[4].enrichment is None


def test_without_an_enricher_candidates_are_still_valid():
    result = run_hunt(_spec(), [_FakeConnector("igape", [_hit("Acme", "https://example.com/a")])])

    assert result.candidates[0].enrichment is None
    assert "enriched" not in result.stats


class _BudgetHungryEnricher:
    """Como el real: gasta 3 llamadas de «linkedin» por candidata y devuelve datos."""

    name = "linkedin"

    def __init__(self) -> None:
        self.attempts = 0

    def enrich(self, company, location, budget) -> Enrichment:
        self.attempts += 1
        e = Enrichment()
        for _ in range(3):
            try:
                budget.consume(self.name)
            except Exception:  # BudgetExhausted
                e.notes.append("tope alcanzado")
                return e
        e.linkedin_url = f"https://linkedin.com/company/{company}"
        return e


def test_enrichment_is_capped_to_what_the_budget_can_finish(monkeypatch):
    """Auditoria #1: con presupuesto 12 y 3 llamadas/candidata, solo caben 4 - no 10.

    Antes se arrancaban 10 y 6 quedaban a medias; stats decia 10."""
    monkeypatch.setenv("STARTUP_HUNTER_MAX_ENRICH", "10")
    hits = [_hit(f"Empresa {i}", f"https://x.es/{i}") for i in range(8)]
    enricher = _BudgetHungryEnricher()

    result = run_hunt(_spec(), [_FakeConnector("bdns", hits)], enricher=enricher,
                      budget=Budget(max_calls_per_source=12))

    assert enricher.attempts == 4          # 12 // 3, no se ARRANCA lo que no se puede acabar
    assert result.stats["enriched"] == 4
    assert result.stats["enrich_attempted"] == 4
    assert result.candidates[3].enrichment is not None
    assert result.candidates[4].enrichment is None


class _FailingEnricher:
    """Intenta enriquecer pero nunca encuentra datos: solo deja notas."""

    name = "linkedin"

    def enrich(self, company, location, budget) -> Enrichment:
        e = Enrichment()
        e.notes.append("Sin pagina de LinkedIn identificable.")
        return e


def test_stats_enriched_counts_successes_not_attempts():
    """Auditoria #1: si el enriquecimiento no aporta datos, NO cuenta como enriquecido.

    El agente no debe leer 'enriched: 4' y concluir que esas empresas no tienen decisor."""
    hits = [_hit(f"Empresa {i}", f"https://x.es/{i}") for i in range(3)]

    result = run_hunt(_spec(), [_FakeConnector("bdns", hits)], enricher=_FailingEnricher())

    assert result.stats["enriched"] == 0          # nadie aporto datos
    assert result.stats["enrich_attempted"] == 3  # pero se intento con 3

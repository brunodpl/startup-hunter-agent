"""Tests del conector Tavily.

El cliente se inyecta, así que ninguno de estos tests toca la red ni exige API key.
"""

from __future__ import annotations

from startup_hunter.budget import Budget
from startup_hunter.connectors.tavily import TavilyConnector, _derive_name, _is_company_site
from startup_hunter.schema import QuerySpec


class _FakeTavily:
    """Doble del cliente de Tavily: registra las queries y devuelve resultados fijos."""

    def __init__(self, results_by_query=None, default=None):
        self.queries: list[str] = []
        self.kwargs: list[dict] = []
        self._results_by_query = results_by_query or {}
        self._default = default if default is not None else []

    def search(self, query, **kwargs):
        self.queries.append(query)
        self.kwargs.append(kwargs)
        return {"results": self._results_by_query.get(query, self._default)}


def _spec(**kw):
    return QuerySpec(**{"query": "agritech", "location": "Galicia", **kw})


def test_keeps_company_sites_and_drops_press_and_aggregators():
    client = _FakeTavily(
        default=[
            {"url": "https://acme-viticultura.example/", "title": "Acme Viticultura | Riego"},
            {"url": "https://www.lavozdegalicia.es/noticia/xyz", "title": "Una startup gallega"},
            {"url": "https://www.crunchbase.com/organization/acme", "title": "Acme"},
            {"url": "https://www.linkedin.com/company/acme", "title": "Acme"},
        ]
    )
    hits = TavilyConnector(client=client).fetch(_spec(), Budget(max_calls_per_source=1))

    assert [h.name for h in hits] == ["Acme Viticultura"]
    assert hits[0].source == "tavily"
    assert hits[0].website == "https://acme-viticultura.example"


def test_uses_advanced_depth():
    """search_depth=advanced encuentra empresas poco indexadas; es una decisión validada."""
    client = _FakeTavily()
    TavilyConnector(client=client).fetch(_spec(), Budget(max_calls_per_source=1))
    assert client.kwargs[0]["search_depth"] == "advanced"


def test_query_includes_location_and_expands_deterministically():
    client = _FakeTavily()
    TavilyConnector(client=client).fetch(_spec(), Budget(max_calls_per_source=5))

    assert client.queries[0] == "agritech Galicia"
    assert len(client.queries) == 5
    # Determinista: la misma spec produce las mismas queries en el mismo orden.
    other = _FakeTavily()
    TavilyConnector(client=other).fetch(_spec(), Budget(max_calls_per_source=5))
    assert other.queries == client.queries


def test_budget_caps_the_number_of_angles():
    client = _FakeTavily()
    TavilyConnector(client=client).fetch(_spec(), Budget(max_calls_per_source=2))
    assert len(client.queries) == 2


def test_deduplicates_the_same_url_across_angles():
    client = _FakeTavily(
        default=[{"url": "https://acme.es/", "title": "Acme"}]
    )
    hits = TavilyConnector(client=client).fetch(_spec(), Budget(max_calls_per_source=3))
    assert len(hits) == 1


def test_respects_max_per_source():
    client = _FakeTavily(
        default=[{"url": f"https://empresa{i}.es/", "title": f"Empresa {i}"} for i in range(10)]
    )
    hits = TavilyConnector(client=client).fetch(
        _spec(max_per_source=3), Budget(max_calls_per_source=2)
    )
    assert len(hits) == 3


def test_name_falls_back_to_domain_when_title_is_a_headline():
    long_title = "La startup gallega que quiere revolucionar el viñedo con drones y sensores"
    assert _derive_name(long_title, "https://demo-agro.example/") == "Demo Agro"


def test_name_takes_first_title_segment():
    assert _derive_name("Robotnik | Mobile robots", "https://robotnik.eu/") == "Robotnik"


def test_subdomain_of_a_press_site_is_still_rejected():
    assert not _is_company_site("https://economia.elpais.com/algo")
    assert _is_company_site("https://acme.es/about")

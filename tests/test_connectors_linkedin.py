"""Tests del enriquecedor de LinkedIn.

Los dobles replican a propósito el objeto ``Run`` de apify-client v3 (atributo
``default_dataset_id``, no un dict): si alguien vuelve a la forma v2, estos tests fallan.
"""

from __future__ import annotations

from startup_hunter.budget import Budget
from startup_hunter.connectors.linkedin import LinkedInEnricher
from startup_hunter.schema import DecisionMaker

_SEARCH = "apimaestro/linkedin-companies-search-scraper"
_DETAIL = "apimaestro/linkedin-company-detail"
_PEOPLE = "johnvc/linkedin-company-employees-api"


class _FakeDataset:
    def __init__(self, items):
        self._items = items

    def list_items(self):
        return type("Listing", (), {"items": self._items})()


class _FakeRun:
    """apify-client v3 devuelve un MODELO con default_dataset_id, no un dict."""

    def __init__(self, dataset_id):
        self.default_dataset_id = dataset_id


class _FakeActor:
    def __init__(self, client, actor_id):
        self._client = client
        self._actor_id = actor_id

    def call(self, run_input):
        self._client.calls.append((self._actor_id, run_input))
        if self._actor_id in self._client.raises:
            raise RuntimeError(self._client.raises[self._actor_id])
        return _FakeRun(self._actor_id)


class _FakeApify:
    def __init__(self, items_by_actor=None, raises=None):
        self.items_by_actor = items_by_actor or {}
        self.raises = raises or {}
        self.calls: list[tuple[str, dict]] = []

    def actor(self, actor_id):
        return _FakeActor(self, actor_id)

    def dataset(self, dataset_id):
        return _FakeDataset(self.items_by_actor.get(dataset_id, []))


def _full_client(**overrides):
    items = {
        _SEARCH: [{"url": "https://www.linkedin.com/company/acme/"}],
        _DETAIL: [
            {
                "website": "https://acme.es",
                "employee_count": "11-50",
                "founded": "2021",
                "headquarters": "Vigo, Galicia",
                "funding": "Seed - 1.2M EUR",
            }
        ],
        _PEOPLE: [
            {
                "fullName": "Ana Pérez",
                "currentRole": "CEO & Co-Founder",
                "linkedinUrl": "https://www.linkedin.com/in/anaperez/",
            }
        ],
    }
    items.update(overrides)
    return _FakeApify(items_by_actor=items)


def test_happy_path_fills_profile_and_decision_maker():
    enrichment = LinkedInEnricher(_full_client()).enrich("Acme", "Galicia", Budget())

    assert enrichment.linkedin_url == "https://www.linkedin.com/company/acme/"
    assert enrichment.funding == "Seed - 1.2M EUR"
    assert enrichment.employees == "11-50"
    assert enrichment.headquarters == "Vigo, Galicia"
    assert len(enrichment.decision_makers) == 1
    assert enrichment.decision_makers[0].name == "Ana Pérez"


def test_search_keyword_is_the_company_name_never_sector_or_funding_terms():
    """El buscador casa contra el NOMBRE: meter 'funding' devolvería vacío."""
    client = _full_client()
    LinkedInEnricher(client).enrich("Acme", "Galicia", Budget())

    actor_id, run_input = client.calls[0]
    assert actor_id == _SEARCH
    assert run_input["keyword"] == "Acme"


def test_decision_maker_never_carries_email_or_phone():
    """Guardarraíl GDPR: el mapeo es lista blanca; no hay dónde guardar contacto personal."""
    client = _full_client(
        **{
            _PEOPLE: [
                {
                    "fullName": "Ana Pérez",
                    "currentRole": "CEO",
                    "linkedinUrl": "https://www.linkedin.com/in/anaperez/",
                    "email": "synthetic-business-address",
                    "phone": "synthetic-phone-value",
                    "personalEmail": "synthetic-personal-address",
                }
            ]
        }
    )
    enrichment = LinkedInEnricher(client).enrich("Acme", None, Budget())

    dumped = enrichment.decision_makers[0].model_dump()
    assert set(dumped) == {"name", "role", "linkedin_url", "source_url"}
    assert "synthetic-business-address" not in str(dumped)
    assert "synthetic-phone-value" not in str(dumped)


def test_decision_maker_model_has_no_contact_fields():
    """Defensa estructural: aunque el mapeo cambiase, el modelo no admite contacto."""
    assert set(DecisionMaker.model_fields) == {"name", "role", "linkedin_url", "source_url"}


def test_missing_funding_is_noted_not_invented():
    client = _full_client(**{_DETAIL: [{"website": "https://acme.es"}]})
    enrichment = LinkedInEnricher(client).enrich("Acme", None, Budget())

    assert enrichment.funding is None
    assert any("no expone financiación" in n for n in enrichment.notes)


def test_actor_failure_is_recorded_and_does_not_raise():
    """Un fallo de enriquecimiento no debe perder la candidata, que ya es válida."""
    client = _FakeApify(raises={_SEARCH: "actor caído"})
    enrichment = LinkedInEnricher(client).enrich("Acme", None, Budget())

    assert enrichment.linkedin_url is None
    assert any("actor caído" in n for n in enrichment.notes)


def test_company_without_linkedin_page_is_noted():
    client = _FakeApify(items_by_actor={_SEARCH: [], _PEOPLE: []})
    enrichment = LinkedInEnricher(client).enrich("Acme", None, Budget())
    assert any("Sin página de LinkedIn" in n for n in enrichment.notes)


def test_decision_makers_are_capped_even_if_the_actor_ignores_max_results():
    people = [
        {"fullName": f"Persona {i}", "currentRole": "CEO", "linkedinUrl": f"https://li/{i}"}
        for i in range(20)
    ]
    client = _full_client(**{_PEOPLE: people})
    enrichment = LinkedInEnricher(client).enrich("Acme", None, Budget())
    assert len(enrichment.decision_makers) == 5


def test_exhausted_budget_stops_enrichment_without_raising():
    client = _full_client()
    budget = Budget(max_calls_per_source=1)
    budget.consume("linkedin")

    enrichment = LinkedInEnricher(client).enrich("Acme", None, budget)
    assert client.calls == []
    assert enrichment.notes

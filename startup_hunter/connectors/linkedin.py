"""Enriquecimiento con LinkedIn público vía Apify.

Portado de la lane social retirada. **No es un conector de descubrimiento**: el buscador de
LinkedIn casa por NOMBRE de entidad, no por sector + región + etapa, así que no sirve para
descubrir (knowledge/lecciones-scraping.md §2). Aquí se usa sobre candidatas que YA existen,
para confirmar la señal y localizar al decisor.

Flujo de tres actores, en el orden en que se encadenan:
1. buscar la empresa por NOMBRE  -> su URL de LinkedIn
2. leer el perfil                -> financiación, tamaño, año, sede, web
3. buscar el decisor por título  -> nombre, rol y perfil público

GDPR
----
Los mapeos son **listas blancas explícitas**. Del decisor se extraen exactamente cuatro
campos y no hay dónde meter un email o un teléfono aunque el actor los devolviera. Es
defensa en profundidad y hay un test que la protege. El enriquecimiento corre solo sobre
las primeras N candidatas, no sobre todas: minimiza el scraping de datos personales.
"""

from __future__ import annotations

import os
from typing import Any

from startup_hunter.budget import Budget, BudgetExhausted
from startup_hunter.connectors.base import ConnectorUnavailable
from startup_hunter.schema import DecisionMaker, Enrichment

_SEARCH_ACTOR = "apimaestro/linkedin-companies-search-scraper"
_DETAIL_ACTOR = "apimaestro/linkedin-company-detail"
_DECISION_MAKERS_ACTOR = "johnvc/linkedin-company-employees-api"

#: Títulos de decisor para una venta de growth (fundador + marketing/growth).
_DEFAULT_TITLES = [
    "CEO",
    "Founder",
    "Co-Founder",
    "Owner",
    "Managing Director",
    "CMO",
    "Head of Growth",
    "Head of Marketing",
    "Marketing Director",
]

#: Minimización GDPR: solo el/los decisor/es, nunca la plantilla.
_MAX_DECISION_MAKERS = 5

_NO_CONFIRMADO = "no_confirmado"


def _first(item: dict[str, Any], *keys: str) -> Any:
    """Primer valor no vacío entre varias claves candidatas.

    El esquema exacto de los actores no está verificado, así que mapeamos defensivamente.
    """
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _as_text(value: Any) -> str | None:
    """Aplana a texto legible lo que el actor devuelva (str, dict o lista)."""
    if value in (None, "", [], {}):
        return None
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _map_decision_maker(item: dict[str, Any], source_url: str) -> DecisionMaker:
    """Mapea un decisor a SOLO nombre, rol y perfil público.

    Guardarraíl GDPR: el mapeo es una lista blanca. Cualquier campo de contacto que el
    actor llegara a incluir NO se propaga porque no existe destino para él.
    """
    return DecisionMaker(
        name=_as_text(_first(item, "fullName", "name", "full_name")) or _NO_CONFIRMADO,
        role=_as_text(_first(item, "currentTitle", "currentRole", "jobTitle", "role", "headline"))
        or _NO_CONFIRMADO,
        linkedin_url=_as_text(
            _first(item, "linkedinUrl", "linkedin_url", "profileUrl", "url")
        )
        or _NO_CONFIRMADO,
        source_url=source_url or _NO_CONFIRMADO,
    )


class LinkedInEnricher:
    """Enriquece una candidata con datos públicos de LinkedIn.

    El cliente de Apify se inyecta para poder testear sin red ni token.
    """

    name = "linkedin"

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            token = os.environ.get("APIFY_TOKEN")
            if not token:
                raise ConnectorUnavailable(
                    "Falta APIFY_TOKEN en el entorno (consola de Apify). Añádelo al .env "
                    "o lanza el hunt sin --enrich."
                )
            from apify_client import ApifyClient

            self._client = ApifyClient(token)
        return self._client

    def _run_actor(self, actor_id: str, run_input: dict[str, Any]) -> list[dict[str, Any]]:
        """Ejecuta un actor y devuelve sus items.

        Ojo: apify-client v3 devuelve un modelo ``Run``, no un dict - de ahí
        ``run.default_dataset_id`` y no ``run["defaultDatasetId"]``.
        """
        client = self._get_client()
        run = client.actor(actor_id).call(run_input=run_input)
        return list(client.dataset(run.default_dataset_id).list_items().items)

    def enrich(self, company: str, location: str | None, budget: Budget) -> Enrichment:
        """Enriquece una empresa. Nunca lanza: los fallos van a ``notes``.

        Un fallo de enriquecimiento no debe tumbar el hunt ni perder la candidata, que ya
        es válida por sí misma con su evidencia de descubrimiento.
        """
        enrichment = Enrichment()
        try:
            self._get_client()
        except ConnectorUnavailable as exc:
            enrichment.notes.append(str(exc))
            return enrichment

        profile_url = self._find_company_url(company, enrichment, budget)
        if profile_url:
            enrichment.linkedin_url = profile_url
            self._read_profile(profile_url, enrichment, budget)
        self._find_decision_makers(company, location, enrichment, budget)
        return enrichment

    def _find_company_url(
        self, company: str, enrichment: Enrichment, budget: Budget
    ) -> str | None:
        """Paso 1 - resolver la empresa a su página de LinkedIn.

        El keyword es el NOMBRE de la empresa. Nunca términos de sector ni de financiación:
        el buscador casa contra el nombre, así que esas queries vuelven vacías.
        """
        try:
            budget.consume(self.name)
            items = self._run_actor(
                _SEARCH_ACTOR, {"keyword": company, "page_number": 1, "limit": 5}
            )
        except BudgetExhausted as exc:
            enrichment.notes.append(str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - un fallo no debe perder la candidata
            enrichment.notes.append(f"Búsqueda en LinkedIn fallida: {exc}")
            return None

        for item in items:
            url = _as_text(_first(item, "url", "companyUrl", "link", "profileUrl"))
            if url:
                return url
        enrichment.notes.append("Sin página de LinkedIn identificable para la empresa.")
        return None

    def _read_profile(
        self, profile_url: str, enrichment: Enrichment, budget: Budget
    ) -> None:
        """Paso 2 - leer el perfil. Aquí es donde se LEE la financiación, nunca se busca."""
        try:
            budget.consume(self.name)
            items = self._run_actor(_DETAIL_ACTOR, {"identifier": [profile_url]})
        except BudgetExhausted as exc:
            enrichment.notes.append(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            enrichment.notes.append(f"Lectura del perfil de LinkedIn fallida: {exc}")
            return

        if not items:
            enrichment.notes.append("El perfil de LinkedIn no devolvió datos.")
            return

        item = items[0]
        enrichment.website = _as_text(_first(item, "website", "website_url", "websiteUrl"))
        enrichment.employees = _as_text(
            _first(item, "company_size", "employee_count", "employees", "employeeCount")
        )
        enrichment.founded = _as_text(
            _first(item, "founded", "founded_year", "year_founded", "foundedOn")
        )
        enrichment.headquarters = _as_text(
            _first(item, "headquarters", "hq", "headquarter", "location")
        )
        enrichment.funding = _as_text(
            _first(
                item,
                "funding",
                "funding_data",
                "crunchbase_funding_data",
                "last_funding_round",
                "fundingData",
            )
        )
        if enrichment.funding is None:
            # Que LinkedIn no publique financiación es normal y NO significa que no la haya.
            # Se anota para que el agente no lo lea como ausencia de señal.
            enrichment.notes.append("LinkedIn no expone financiación para esta empresa.")

    def _find_decision_makers(
        self,
        company: str,
        location: str | None,
        enrichment: Enrichment,
        budget: Budget,
    ) -> None:
        """Paso 3 - localizar al decisor. Solo rol y perfil público."""
        run_input: dict[str, Any] = {
            "companies": [enrichment.linkedin_url or company.strip()],
            "titleKeywords": _DEFAULT_TITLES,
            "maxResultsPerCompany": _MAX_DECISION_MAKERS,
        }
        if location and location.strip():
            run_input["locations"] = [location.strip()]

        try:
            budget.consume(self.name)
            items = self._run_actor(_DECISION_MAKERS_ACTOR, run_input)
        except BudgetExhausted as exc:
            enrichment.notes.append(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            enrichment.notes.append(f"Búsqueda de decisor fallida: {exc}")
            return

        # El actor devuelve filas de error junto a los perfiles; no son decisores.
        errors = [i for i in items if i.get("error_type") or i.get("error_message")]
        people = [i for i in items if i not in errors]

        # Se recorta otra vez por si el actor ignora maxResultsPerCompany.
        source = enrichment.linkedin_url or ""
        enrichment.decision_makers = [
            _map_decision_maker(item, source) for item in people[:_MAX_DECISION_MAKERS]
        ]
        if not enrichment.decision_makers:
            if errors:
                detail = errors[0].get("error_message") or errors[0].get("error_type")
                enrichment.notes.append(f"Búsqueda de decisor fallida: {detail}")
            else:
                enrichment.notes.append("Sin decisor público identificable.")

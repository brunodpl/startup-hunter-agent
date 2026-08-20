"""Conector de búsqueda web (Tavily).

Portado de la tool `web_search` del agente web retirado. Conserva lo que estaba validado:
`search_depth="advanced"` (mejor para empresas poco indexadas) y **consultas en español**
cuando el objetivo es España - las queries amplias en inglés devuelven directorios
internacionales, no la empresa concreta (knowledge/lecciones-scraping.md §3).

Lo que cambia respecto al agente: ya no hay un modelo leyendo resultados y cosechando
nombres. La expansión de consulta es **determinista** (plantillas fijas) y la extracción
del nombre es heurística.

Consecuencia honesta de ese cambio: Tavily aquí es una fuente de PRECISIÓN, no de recall.
Se queda con resultados que parecen la web de una empresa y descarta prensa, directorios y
agregadores, porque de esos no se puede extraer la empresa sin criterio. El recall lo
aporta el conector estructurado (`bdns`), donde la empresa viene como dato.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

from startup_hunter.budget import Budget, BudgetExhausted
from startup_hunter.connectors.base import ConnectorUnavailable, clamp
from startup_hunter.schema import QuerySpec, RawHit

# Dominios que NO son la web de una empresa: prensa, agregadores de VC, redes y
# directorios. Salieron como ruido recurrente en el A/B del 25-jun-2026
# (knowledge/fuentes.md §3). Un resultado en uno de estos sitios puede MENCIONAR una
# empresa, pero no la identifica: sin un modelo que lea la página, no hay forma fiable de
# extraer de quién habla.
_NON_COMPANY_DOMAINS = frozenset(
    {
        # Agregadores / bases de datos de startups
        "shizune.co", "vestbee.com", "seedtable.com", "growthlist.co",
        "crunchbase.com", "dealroom.co", "tracxn.com", "pitchbook.com",
        "f6s.com", "startupranking.com", "eu-startups.com",
        # Redes sociales y plataformas
        "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
        "youtube.com", "medium.com", "wikipedia.org",
        # Prensa generalista y económica española
        "elpais.com", "elmundo.es", "lavozdegalicia.es", "farodevigo.es",
        "expansion.com", "cincodias.elpais.com", "eleconomista.es",
        "economiadigital.es", "elreferente.es", "emprendedores.es",
    }
)

# Plantillas de expansión. Deterministas y en español, en el orden en que aportan:
# primero la petición tal cual, luego los ángulos de buy-signal.
_ANGLE_TEMPLATES = (
    "{q}",
    "{q} empresa",
    "{q} startup ronda financiación",
    "{q} subvención ayuda concedida",
    "{q} contrata responsable de marketing",
)


def _registrable_host(url: str) -> str:
    """Host sin ``www.``, en minúsculas. Vacío si la URL no es utilizable."""
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_company_site(url: str) -> bool:
    host = _registrable_host(url)
    if not host:
        return False
    # Compara también el dominio registrable, para que un subdominio de prensa cuente.
    labels = host.split(".")
    registrable = ".".join(labels[-2:]) if len(labels) >= 2 else host
    return host not in _NON_COMPANY_DOMAINS and registrable not in _NON_COMPANY_DOMAINS


def _name_from_title(title: str) -> str:
    """Primer segmento del título, que suele ser el nombre de la empresa.

    Los títulos de web corporativa casi siempre son «Nombre | Claim» o «Nombre - Claim».
    """
    first = re.split(r"\s(?:\||-|\u2013|\u2014)\s", title.strip(), maxsplit=1)[0]
    return first.strip()


def _name_from_host(host: str) -> str:
    """Nombre derivado del dominio, como respaldo cuando el título no sirve."""
    label = host.split(".")[0].replace("-", " ")
    return label.title()


def _derive_name(title: str, url: str) -> str:
    candidate = _name_from_title(title or "")
    # Un título largo suele ser el titular de un artículo, no un nombre de empresa.
    if candidate and len(candidate) <= 60:
        return candidate
    return _name_from_host(_registrable_host(url))


class TavilyConnector:
    """Búsqueda web semántica. El cliente se inyecta para poder testear sin red."""

    name = "tavily"

    def __init__(self, client: Any | None = None, max_results_per_angle: int = 5) -> None:
        self._client = client
        self._max_results = max_results_per_angle

    def _get_client(self) -> Any:
        """Cliente perezoso: no exige la API key hasta que se busca de verdad."""
        if self._client is None:
            if not os.environ.get("TAVILY_API_KEY"):
                raise ConnectorUnavailable(
                    "Falta TAVILY_API_KEY en el entorno. Añádela al .env o quita "
                    "'tavily' de --sources."
                )
            from tavily import TavilyClient

            self._client = TavilyClient()
        return self._client

    def _angles(self, spec: QuerySpec, budget: Budget) -> list[str]:
        base = spec.query if not spec.location else f"{spec.query} {spec.location}"
        angles = [tpl.format(q=base) for tpl in _ANGLE_TEMPLATES]
        # Nunca planificamos más ángulos de los que el presupuesto permite pagar.
        return angles[: budget.remaining(self.name)]

    def fetch(self, spec: QuerySpec, budget: Budget) -> list[RawHit]:
        client = self._get_client()
        hits: list[RawHit] = []
        seen: set[str] = set()

        for angle in self._angles(spec, budget):
            try:
                budget.consume(self.name)
            except BudgetExhausted:
                break
            response = client.search(
                query=angle,
                max_results=self._max_results,
                topic="general",
                # "advanced" cuesta más créditos pero encuentra empresas poco indexadas,
                # que son justo las que interesan.
                search_depth="advanced",
            )
            for item in (response or {}).get("results", []):
                hit = self._to_hit(item, spec, angle)
                if hit is not None and hit.source_url not in seen:
                    seen.add(hit.source_url)
                    hits.append(hit)

        return clamp(hits, spec.max_per_source)

    def _to_hit(self, item: dict[str, Any], spec: QuerySpec, angle: str) -> RawHit | None:
        url = (item.get("url") or "").strip()
        if not url or not _is_company_site(url):
            return None
        host = _registrable_host(url)
        title = item.get("title") or ""
        return RawHit(
            name=_derive_name(title, url),
            source_url=url,
            source=self.name,
            description=(item.get("content") or "").strip()[:400],
            signal=f"Aparece en búsqueda web para «{angle}»",
            sector=spec.sector,
            location=spec.location,
            website=f"https://{host}" if host else None,
            raw={"title": title, "score": item.get("score"), "angle": angle},
        )

"""El pipeline: de una consulta a una cartera de candidatas con evidencia.

Sustituye al grafo de LangGraph por Python plano. El flujo es fijo y conocido, así que no
hacía falta un framework de grafos: cinco pasos secuenciales.

    QuerySpec -> fetch (por conector) -> normalize -> consolidate -> enrich -> HuntResult

Dos propiedades que se conservan del diseño anterior porque estaban bien:

- **Fallo aislado por conector.** Un conector caído no tumba el run: su error se anota en
  ``source_notes`` y el hunt sigue con el resto. Un hunt a medias es útil; uno abortado, no.
- **El pipeline no juzga.** Recoge hechos con evidencia. Quien decide si una empresa encaja
  es el agente que lee la salida (knowledge/decisiones.md, A2).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from startup_hunter.budget import Budget
from startup_hunter.connectors import ConnectorUnavailable, LinkedInEnricher
from startup_hunter.connectors.base import Connector
from startup_hunter.consolidate import consolidate
from startup_hunter.ledger import Ledger
from startup_hunter.schema import (
    HunterResult,
    HuntResult,
    MergedCandidate,
    QuerySpec,
    RawHit,
    StartupCandidate,
)

#: Cuántas candidatas se enriquecen por run. Tope de coste y de minimización GDPR:
#: el scraping de decisores solo corre sobre las primeras N.
_DEFAULT_MAX_ENRICH = 10
_MAX_ENRICH_ENV = "STARTUP_HUNTER_MAX_ENRICH"
#: Nombre heredado de la arquitectura de agentes retirada (ya no habla de «análisis»). Se
#: acepta como alias para no romper .env antiguos.
_LEGACY_MAX_ENRICH_ENV = "STARTUP_HUNTER_MAX_ANALYSES"

#: Cada enriquecimiento cuesta hasta 3 llamadas a la misma clave de presupuesto
#: («linkedin»): buscar la empresa + leer el perfil + buscar el decisor. El tope efectivo de
#: enriquecimientos es, por tanto, min(max_enrichments, presupuesto_linkedin // 3). Antes se
#: arrancaban 10 y 6 fallaban en silencio cuando el presupuesto (12) solo daba para 4.
_CALLS_PER_ENRICHMENT = 3


def max_enrichments() -> int:
    """Tope de candidatas a enriquecer por run, configurable por entorno."""
    raw = os.environ.get(_MAX_ENRICH_ENV) or os.environ.get(_LEGACY_MAX_ENRICH_ENV)
    if raw is None:
        return _DEFAULT_MAX_ENRICH
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_MAX_ENRICH


def to_candidate(hit: RawHit, spec: QuerySpec) -> StartupCandidate | None:
    """Normaliza un hit crudo a candidata validada.

    Devuelve ``None`` si el hit no cumple el contrato mínimo (por ejemplo, una URL de
    evidencia inválida). Descartar es correcto: sin evidencia no hay candidata.
    """
    try:
        return StartupCandidate(
            name=hit.name.strip(),
            description=hit.description or f"Empresa detectada vía {hit.source}.",
            sector=hit.sector or spec.sector or spec.query,
            location=hit.location or spec.location or "no_confirmado",
            signal=hit.signal or f"Encontrada por {hit.source}.",
            source_url=hit.source_url,
            found_via=hit.source,
            website=hit.website,
            tax_id=hit.tax_id,
            source_date=hit.source_date,
        )
    except Exception:  # noqa: BLE001 - un hit malformado no debe tumbar el conector entero
        return None


def _run_connector(
    connector: Connector, spec: QuerySpec, budget: Budget
) -> tuple[HunterResult, int]:
    """Ejecuta un conector y nunca propaga su fallo.

    Devuelve su resultado normalizado y el número de hits crudos, para las estadísticas.
    """
    name = getattr(connector, "name", connector.__class__.__name__)
    try:
        hits = connector.fetch(spec, budget)
    except ConnectorUnavailable as exc:
        # No es un fallo del run: la fuente está desactivada (falta una key, por ejemplo).
        return HunterResult(candidates=[], notes=f"{name}: desactivado - {exc}"), 0
    except Exception as exc:  # noqa: BLE001 - un conector caído no tumba el hunt
        return HunterResult(candidates=[], notes=f"{name} ERROR: {exc}"), 0

    candidates = [c for hit in hits if (c := to_candidate(hit, spec)) is not None]
    dropped = len(hits) - len(candidates)
    notes = f"{name}: {len(candidates)} candidata(s) de {len(hits)} resultado(s)."
    if dropped:
        notes += f" {dropped} descartada(s) por evidencia inválida."
    # Un conector puede tener algo que contar sobre CÓMO obtuvo el resultado (p. ej. que
    # tuvo que relajar un filtro). Si no lo propagamos, el agente lee la lista sin saberlo.
    for note in getattr(connector, "notes", []) or []:
        notes += f" {note}"
    if budget.remaining(name) == 0:
        notes += " Tope de llamadas alcanzado: cobertura recortada."
    return HunterResult(candidates=candidates, notes=notes), len(hits)


def _has_enrichment_data(enrichment) -> bool:
    """True si el enriquecimiento aportó algún dato real (no solo notas de fallo)."""
    return bool(
        enrichment.linkedin_url
        or enrichment.website
        or enrichment.funding
        or enrichment.employees
        or enrichment.founded
        or enrichment.headquarters
        or enrichment.decision_makers
    )


def _enrich(
    candidates: list[MergedCandidate],
    spec: QuerySpec,
    enricher: LinkedInEnricher,
    budget: Budget,
) -> tuple[int, int]:
    """Enriquece las primeras candidatas asequibles in situ.

    Devuelve ``(con_datos, intentadas)``. El tope se acota al presupuesto: no se ARRANCA un
    enriquecimiento que no se puede terminar, así ninguno queda a medias y ``stats`` no
    miente. Se cuentan éxitos (aportaron datos), no intentos.
    """
    affordable = budget.remaining(enricher.name) // _CALLS_PER_ENRICHMENT
    limit = min(max_enrichments(), affordable, len(candidates))
    enriched = 0
    for candidate in candidates[:limit]:
        candidate.enrichment = enricher.enrich(candidate.name, spec.location, budget)
        if _has_enrichment_data(candidate.enrichment):
            enriched += 1
    return enriched, limit


def run_hunt(
    spec: QuerySpec,
    connectors: list[Connector],
    enricher: LinkedInEnricher | None = None,
    budget: Budget | None = None,
    ledger: Ledger | None = None,
    resend_after_days: int | None = None,
) -> HuntResult:
    """Ejecuta un hunt completo.

    Los conectores, el enriquecedor y el registro se inyectan: es lo que permite testear
    el pipeline entero sin red, sin API keys y sin coste.

    Si se pasa ``ledger``, cada candidata queda marcada con ``is_new``. Con
    ``resend_after_days``, una candidata enviada pero nunca confirmada hace tantos días
    vuelve a contar como nueva (recuperación de una entrega perdida). El registro NO se
    escribe aquí: eso es un paso explícito posterior (ver ``ledger.py``).
    """
    if not connectors:
        raise ValueError("run_hunt requiere al menos un conector.")

    budget = budget or Budget()
    results: list[HunterResult] = []
    stats: dict[str, int] = {}
    # Las notas se indexan aquí y no con las de `consolidate`, porque ese indexa por el
    # found_via de la primera candidata y cae a "lane_N" cuando un conector no devuelve
    # nada - que es justo el caso de un fallo, donde más importa saber quién falló.
    source_notes: dict[str, str] = {}

    for connector in connectors:
        name = getattr(connector, "name", connector.__class__.__name__)
        result, raw_count = _run_connector(connector, spec, budget)
        results.append(result)
        source_notes[name] = result.notes
        stats[f"hits_{name}"] = raw_count

    candidates = consolidate(results)

    stats["candidates_total"] = len(candidates)
    stats["needs_review"] = sum(1 for c in candidates if c.needs_review)

    if ledger is not None:
        stats["new"] = ledger.annotate(candidates, resend_after_days=resend_after_days)

    if enricher is not None and candidates:
        enriched, attempted = _enrich(candidates, spec, enricher, budget)
        stats["enriched"] = enriched          # con datos reales, no intentos
        stats["enrich_attempted"] = attempted

    return HuntResult(
        query=spec.describe(),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        candidates=candidates,
        source_notes=source_notes,
        stats=stats,
    )

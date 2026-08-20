"""Identidad estable de candidatas para enlazar descubrimiento, analisis y salida."""

from __future__ import annotations

from startup_hunter.consolidate import normalize_name, normalize_tax_id, registrable_domain
from startup_hunter.schema import MergedCandidate


def candidate_key(candidate: MergedCandidate) -> str:
    """Clave estable para enlazar candidata y analisis sin depender del texto del LLM.

    Prioridad de identidad, de más a menos fiable: CIF oficial > dominio propio > nombre
    normalizado + ubicacion. El CIF va primero porque es la unica clave que sobrevive a que
    la fuente escriba el nombre de otra forma entre dos runs (registro de ya-vistas, A5).
    """
    tax_id = normalize_tax_id(candidate.tax_id)
    if tax_id:
        return f"tax:{tax_id}"
    domain = registrable_domain(candidate.website)
    if domain:
        return f"domain:{domain}"
    name = normalize_name(candidate.name) or "unknown"
    location = normalize_name(candidate.location) or "unknown"
    return f"name:{name}|location:{location}"

"""Tests de la salida del pipeline.

Un solo artefacto: ``candidates.json``, el contrato que lee el runtime. Lo que se protege
aquí es que ese contrato sobreviva al viaje de ida y vuelta sin perder nada - evidencia,
enriquecimiento y marcas de revisión incluidas.
"""

from __future__ import annotations

import json

from startup_hunter.report import write_output
from startup_hunter.schema import (
    DecisionMaker,
    Enrichment,
    HuntResult,
    MergedCandidate,
    Source,
)

_BDNS_URL = "https://www.infosubvenciones.es/bdnstrans/GE/es/concesiones"


def _result() -> HuntResult:
    return HuntResult(
        query="agritech en Galicia",
        generated_at="2026-07-20T10:00:00+00:00",
        candidates=[
            MergedCandidate(
                name="AgriTech",
                description="Software B2B para bodegas.",
                sector="agritech",
                location="Vigo, Galicia",
                signals=["Subvención pública concedida por 3024.0 € el 2026-06-04"],
                sources=[
                    Source(found_via="bdns", source_url=_BDNS_URL),
                    Source(found_via="tavily", source_url="https://acme-agritech.example"),
                ],
                website="https://acme-agritech.example",
                enrichment=Enrichment(
                    linkedin_url="https://www.linkedin.com/company/example-acme-agritech/",
                    funding="Seed, 300.000 €",
                    employees="11-50",
                    decision_makers=[
                        DecisionMaker(
                            name="Alex Rivera",
                            role="CEO",
                            linkedin_url="https://www.linkedin.com/in/example-alex-rivera/",
                            source_url="https://www.linkedin.com/company/example-acme-agritech/",
                        )
                    ],
                ),
            ),
            MergedCandidate(
                name="Innova Soluciones",
                description="d",
                sector="agritech",
                location="A Coruna",
                signals=[],
                sources=[Source(found_via="tavily", source_url="https://innova.es")],
                needs_review=True,
                possible_duplicate_of="Innova",
            ),
        ],
        source_notes={"bdns": "2 candidata(s) de 2 resultado(s).", "tavily": "1 candidata(s)."},
        stats={"candidates_total": 2, "needs_review": 1},
    )


def test_write_output_creates_candidates_json(tmp_path):
    json_path = write_output(_result(), tmp_path)

    assert json_path.exists()
    assert json_path.name == "candidates.json"


def test_json_is_the_canonical_contract_for_the_agent(tmp_path):
    """El agente lee este fichero: debe poder reconstruir el HuntResult tal cual."""
    json_path = write_output(_result(), tmp_path)
    restored = HuntResult.model_validate(
        json.loads(json_path.read_text(encoding="utf-8"))
    )

    assert restored.query == "agritech en Galicia"
    assert [c.name for c in restored.candidates] == ["AgriTech", "Innova Soluciones"]
    assert restored.source_notes["bdns"].startswith("2 candidata(s)")


def test_evidence_survives_the_round_trip(tmp_path):
    """Sin evidencia no hay candidata: las fuentes tienen que llegar enteras al agente."""
    json_path = write_output(_result(), tmp_path)
    restored = HuntResult.model_validate(
        json.loads(json_path.read_text(encoding="utf-8"))
    )

    sources = restored.candidates[0].sources
    assert [s.found_via for s in sources] == ["bdns", "tavily"]
    assert [s.source_url for s in sources] == [_BDNS_URL, "https://acme-agritech.example"]


def test_enrichment_and_decision_maker_survive_the_round_trip(tmp_path):
    json_path = write_output(_result(), tmp_path)
    restored = HuntResult.model_validate(
        json.loads(json_path.read_text(encoding="utf-8"))
    )

    enrichment = restored.candidates[0].enrichment
    assert enrichment.funding == "Seed, 300.000 €"
    assert enrichment.decision_makers[0].name == "Alex Rivera"
    assert enrichment.decision_makers[0].role == "CEO"


def test_duplicate_flags_survive_the_round_trip(tmp_path):
    """needs_review es una decisión que el agente tiene que ver, no un detalle interno."""
    json_path = write_output(_result(), tmp_path)
    restored = HuntResult.model_validate(
        json.loads(json_path.read_text(encoding="utf-8"))
    )

    assert restored.candidates[1].needs_review is True
    assert restored.candidates[1].possible_duplicate_of == "Innova"


def test_empty_hunt_is_written_as_a_valid_outcome(tmp_path):
    """Cero candidatas es un resultado honesto, no un fallo (decisión D2).

    El fichero se escribe igual, con la lista vacía y las notas de cobertura: el agente
    no tiene que ramificar entre 'no hay fichero' y 'no hay candidatas'.
    """
    empty = HuntResult(
        query="agritech en Teruel",
        generated_at="2026-07-20T10:00:00+00:00",
        source_notes={"bdns": "bdns: 0 candidata(s) de 0 resultado(s)."},
    )
    json_path = write_output(empty, tmp_path)
    restored = HuntResult.model_validate(
        json.loads(json_path.read_text(encoding="utf-8"))
    )

    assert restored.candidates == []
    assert restored.source_notes["bdns"].endswith("0 resultado(s).")


def test_utf8_is_explicit(tmp_path):
    """Defensa contra el cp1252 de Windows: los acentos tienen que sobrevivir."""
    json_path = write_output(_result(), tmp_path)

    assert "Subvención" in json_path.read_text(encoding="utf-8")

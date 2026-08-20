from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_decision_maker_has_no_field_for_contact_data():
    """Defensa GDPR estructural: aunque la fuente devuelva email o telefono, no hay
    donde guardarlos. Ver knowledge/lecciones-scraping.md §6."""
    from startup_hunter.schema import DecisionMaker

    dm = DecisionMaker(
        name="Alex Rivera",
        role="CEO",
        linkedin_url="https://www.linkedin.com/in/example-alex-rivera/",
        source_url="https://hoteltech.example/equipo",
    )

    assert set(dm.model_dump()) == {"name", "role", "linkedin_url", "source_url"}


def test_hunt_result_carries_no_commercial_judgement():
    """El pipeline entrega hechos; quien decide si encaja es el agente (decision A2)."""
    from startup_hunter.schema import HuntResult

    fields = set(HuntResult.model_fields)

    assert fields == {
        "query",
        "generated_at",
        "candidates",
        "source_notes",
        "stats",
    }
    assert not {"score", "is_client_opportunity", "reason"} & fields


def test_raw_hit_requires_real_evidence_url():
    import pytest
    from pydantic import ValidationError

    from startup_hunter.schema import RawHit

    with pytest.raises(ValidationError):
        RawHit(name="Sin evidencia", source_url="not-a-url", source="igape")


def test_startup_candidate_requires_real_evidence_url():
    from startup_hunter.schema import Source, StartupCandidate

    with pytest.raises(ValidationError):
        StartupCandidate(
            name="No Evidence",
            description="d",
            sector="s",
            location="Galicia",
            signal="sig",
            source_url="not-a-url",
            found_via="web",
        )

    with pytest.raises(ValidationError):
        Source(found_via="web", source_url="")

    with pytest.raises(ValidationError):
        Source(found_via="web", source_url="https://example.com/bad path")


def test_candidate_key_does_not_collide_on_media_source_domain():
    from startup_hunter.candidate_identity import candidate_key
    from startup_hunter.schema import MergedCandidate, Source

    def candidate(name: str) -> MergedCandidate:
        return MergedCandidate(
            name=name,
            description="d",
            sector="s",
            location="Galicia",
            sources=[
                Source(
                    found_via="web",
                    source_url=f"https://capital-riesgo.es/article/{name.lower().replace(' ', '-')}",
                )
            ],
        )

    assert candidate_key(candidate("Alpha Robotics")) != candidate_key(candidate("Beta Drones"))


def test_candidate_key_prefers_the_cif_over_name_and_domain():
    """El CIF es la identidad canonica: la misma empresa con otro nombre da la misma clave."""
    from startup_hunter.candidate_identity import candidate_key
    from startup_hunter.schema import MergedCandidate, Source

    def candidate(name: str, tax_id: str) -> MergedCandidate:
        return MergedCandidate(
            name=name, description="d", sector="s", location="Galicia", tax_id=tax_id,
            sources=[Source(found_via="bdns", source_url="https://x.es/a")],
        )

    assert candidate_key(candidate("Acme Cristaleria SL", "B36000001")) == "tax:B36000001"
    # Mismo CIF, nombre distinto -> misma clave. Distinto CIF -> distinta.
    assert candidate_key(candidate("Acme Cristaleria SLU", "B36000001")) == "tax:B36000001"
    assert candidate_key(candidate("Otra SL", "B36000002")) != "tax:B36000001"

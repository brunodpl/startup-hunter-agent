"""Tests del registro de candidatas ya reportadas.

Lo que se protege aquí es la propiedad que hace útil un hunt diario: que el operador vea
hablar de una empresa la primera vez. Y la separación anotar/registrar, que es lo que
evita perder candidatas cuando el mensaje no llega a salir.
"""

from __future__ import annotations

import json

import pytest

from startup_hunter.ledger import Ledger, default_ledger_path
from startup_hunter.schema import MergedCandidate, Source


def _candidate(name="AgriTech", website=None, location="Vigo", tax_id=None) -> MergedCandidate:
    return MergedCandidate(
        name=name,
        description="d",
        sector="agritech",
        location=location,
        sources=[Source(found_via="bdns", source_url="https://x.es/a")],
        website=website,
        tax_id=tax_id,
    )


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "seen.json"


def test_everything_is_new_against_an_empty_ledger(ledger_path):
    ledger = Ledger(ledger_path)
    candidates = [_candidate("A"), _candidate("B")]

    assert ledger.annotate(candidates) == 2
    assert [c.is_new for c in candidates] == [True, True]


def test_annotate_does_not_write_anything(ledger_path):
    """hunt no debe registrar: si lo hiciera, un fallo antes del envío perdería candidatas."""
    Ledger(ledger_path).annotate([_candidate("A")])

    assert not ledger_path.exists()


def test_marked_candidates_are_no_longer_new_on_the_next_run(ledger_path):
    Ledger(ledger_path).mark([_candidate("A")])

    fresh = [_candidate("A"), _candidate("B")]
    new = Ledger(ledger_path).annotate(fresh)

    assert new == 1
    assert [c.is_new for c in fresh] == [False, True]


def test_mark_reported_is_idempotent_so_retrying_is_safe(ledger_path):
    """Reintentar mark-seen tras un fallo no debe alterar el registro."""
    ledger = Ledger(ledger_path)
    assert ledger.mark_reported([_candidate("A")], today="2026-07-20") == 1
    assert ledger.mark_reported([_candidate("A")], today="2026-07-25") == 0

    stored = json.loads(ledger_path.read_text(encoding="utf-8"))
    entry = next(iter(stored.values()))
    assert entry["first_seen"] == "2026-07-20"  # conserva el primer avistamiento
    assert entry["status"] == "reported"


def test_identity_is_the_domain_when_there_is_one(ledger_path):
    """La misma empresa con otro nombre pero el mismo dominio ya no es nueva."""
    Ledger(ledger_path).mark([_candidate("AgriTech S.L.", website="https://agritech.es")])

    otro_nombre = _candidate("AgriTech", website="https://www.agritech.es/inicio")

    assert Ledger(ledger_path).is_new(otro_nombre) is False


def test_identity_is_the_cif_when_there_is_one(ledger_path):
    """El caso que hace robusto el registro: la BDNS escribe el nombre distinto entre runs,
    pero el CIF es el mismo, así que la empresa no se entrega dos veces."""
    Ledger(ledger_path).mark([_candidate("ACME CRISTALERIA SL", tax_id="B36000001")])

    otro_nombre = _candidate("Cristalería Tea, S.L.U.", tax_id="B36000001")

    assert Ledger(ledger_path).is_new(otro_nombre) is False


def test_cif_beats_name_and_location_in_the_ledger(ledger_path):
    """Mismo CIF, otra ubicacion reportada -> sigue siendo la misma empresa ya vista."""
    Ledger(ledger_path).mark([_candidate("Innova", location="Vigo", tax_id="B11111111")])

    assert Ledger(ledger_path).is_new(
        _candidate("Innova", location="Sevilla", tax_id="B11111111")
    ) is False


def test_same_name_in_another_location_is_a_different_company(ledger_path):
    Ledger(ledger_path).mark([_candidate("Innova", location="Vigo")])

    assert Ledger(ledger_path).is_new(_candidate("Innova", location="Sevilla")) is True


def test_a_corrupt_ledger_does_not_break_the_run(ledger_path):
    """Perder el registro cuesta repetir candidatas. Abortar el hunt cuesta más."""
    ledger_path.write_text("{ esto no es json", encoding="utf-8")

    ledger = Ledger(ledger_path)

    assert len(ledger) == 0
    assert ledger.is_new(_candidate("A")) is True


def test_a_missing_ledger_is_not_an_error(tmp_path):
    ledger = Ledger(tmp_path / "no" / "existe" / "seen.json")

    assert len(ledger) == 0


def test_mark_creates_the_parent_directory(tmp_path):
    path = tmp_path / "nuevo" / "seen.json"

    Ledger(path).mark([_candidate("A")])

    assert path.exists()


def test_ledger_path_is_configurable_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("STARTUP_HUNTER_LEDGER", str(tmp_path / "otro.json"))

    assert default_ledger_path() == tmp_path / "otro.json"


def test_ledger_lives_outside_the_output_dir(monkeypatch):
    """output/ es de artefactos por run y se puede borrar entero; el registro no."""
    monkeypatch.delenv("STARTUP_HUNTER_LEDGER", raising=False)

    assert "output" not in default_ledger_path().parts


# --- las dos fases: enviado (reported) -> confirmado (confirmed) ------------


def test_reported_is_not_new_but_is_not_confirmed(ledger_path):
    Ledger(ledger_path).mark_reported([_candidate("A")])

    ledger = Ledger(ledger_path)
    assert ledger.is_new(_candidate("A")) is False
    assert ledger.status(_candidate("A")) == "reported"


def test_confirm_promotes_only_the_pending(ledger_path):
    ledger = Ledger(ledger_path)
    ledger.mark_reported([_candidate("A"), _candidate("B")])

    assert ledger.confirm([_candidate("A")]) == 1
    assert ledger.status(_candidate("A")) == "confirmed"
    assert ledger.status(_candidate("B")) == "reported"


def test_confirm_without_args_confirms_all_pending(ledger_path):
    ledger = Ledger(ledger_path)
    ledger.mark_reported([_candidate("A"), _candidate("B")])

    assert ledger.confirm() == 2
    assert Ledger(ledger_path).status(_candidate("A")) == "confirmed"


def test_confirm_is_idempotent(ledger_path):
    ledger = Ledger(ledger_path)
    ledger.mark_reported([_candidate("A")])

    assert ledger.confirm([_candidate("A")]) == 1
    assert ledger.confirm([_candidate("A")]) == 0  # ya confirmada, no re-promociona


def test_reported_never_downgrades_a_confirmed(ledger_path):
    """Volver a mark_reported una ya confirmada no debe devolverla a pendiente."""
    ledger = Ledger(ledger_path)
    ledger.mark([_candidate("A")])  # queda confirmed

    ledger.mark_reported([_candidate("A")])
    assert ledger.status(_candidate("A")) == "confirmed"


def test_stale_unconfirmed_surfaces_lost_deliveries(ledger_path):
    """Enviado hace mucho y nunca confirmado = sospechoso de entrega perdida."""
    ledger = Ledger(ledger_path)
    ledger.mark_reported([_candidate("A")], today="2026-07-01")

    stale = Ledger(ledger_path).stale_unconfirmed(days=4, today="2026-07-20")
    assert len(stale) == 1


def test_confirmed_entries_are_never_stale(ledger_path):
    ledger = Ledger(ledger_path)
    ledger.mark_reported([_candidate("A")], today="2026-07-01")
    ledger.confirm(today="2026-07-01")

    assert Ledger(ledger_path).stale_unconfirmed(days=4, today="2026-07-20") == []


def test_annotate_resurfaces_a_stale_unconfirmed_candidate(ledger_path):
    """La recuperación at-least-once: si una entrega no se confirmó, la empresa vuelve a
    contar como nueva pasado el plazo, para reenviarla si reaparece en un hunt."""
    Ledger(ledger_path).mark_reported([_candidate("A")], today="2026-07-01")

    # Reconstruimos el ledger 'hoy' seria 2026-07-20; usamos annotate con resend=4.
    ledger = Ledger(ledger_path)
    cand = _candidate("A")
    # is_new estricto: ya esta en el registro -> False.
    assert ledger.is_new(cand) is False
    # Con annotate y ventana de reenvio, una pendiente vieja resurge. Como annotate usa la
    # fecha de hoy real, comprobamos el mecanismo con un plazo de 0 dias (siempre stale).
    n = ledger.annotate([cand], resend_after_days=0)
    assert n == 1 and cand.is_new is True


def test_legacy_flat_format_is_read_as_confirmed(ledger_path):
    """Un registro del formato viejo {clave: fecha} se lee como ya confirmado."""
    import json as _json
    from startup_hunter.candidate_identity import candidate_key

    key = candidate_key(_candidate("A"))
    ledger_path.write_text(_json.dumps({key: "2026-07-10"}), encoding="utf-8")

    ledger = Ledger(ledger_path)
    assert ledger.is_new(_candidate("A")) is False
    assert ledger.status(_candidate("A")) == "confirmed"

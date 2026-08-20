"""Tests de la CLI - la superficie que invoca el agente.

Lo que se protege aquí es el CONTRATO con el agente: códigos de salida, ficheros
escritos y qué se imprime. Si esto cambia sin querer, el agente deja de funcionar
aunque el pipeline esté perfecto.
"""

from __future__ import annotations

import json

import pytest

from startup_hunter import cli
from startup_hunter.schema import HuntResult, MergedCandidate, QuerySpec, Source


def _result(candidates=None, query="agritech en Galicia") -> HuntResult:
    return HuntResult(
        query=query,
        generated_at="2026-07-20T10:00:00+00:00",
        candidates=candidates or [],
        source_notes={"bdns": "bdns: 1 candidata(s) de 1 resultado(s)."},
        stats={"candidates_total": len(candidates or [])},
    )


def _candidate(name="Acme Viticultura") -> MergedCandidate:
    return MergedCandidate(
        name=name,
        description="Viticultura de precisión.",
        sector="agritech",
        location="Galicia",
        signals=["Subvención pública concedida por 3024.0 € el 2026-06-04"],
        sources=[
            Source(
                found_via="bdns",
                source_url="https://www.infosubvenciones.es/bdnstrans/GE/es/concesiones",
            )
        ],
    )


@pytest.fixture(autouse=True)
def isolated_ledger(monkeypatch, tmp_path):
    """El registro vive fuera del repo durante los tests.

    Sin esto, un test escribiría en el `.startup-hunter/seen.json` de quien lo ejecuta y
    los resultados dependerían de su historial de hunts.
    """
    monkeypatch.setenv("STARTUP_HUNTER_LEDGER", str(tmp_path / "ledger" / "seen.json"))


@pytest.fixture
def spy_hunt(monkeypatch):
    """Sustituye run_hunt para no tocar la red, y captura con qué se llamó."""
    calls: dict = {}

    def _fake_run_hunt(spec, sources, enricher=None, budget=None, ledger=None,
                       resend_after_days=None):
        calls["spec"] = spec
        calls["sources"] = sources
        calls["enricher"] = enricher
        calls["ledger"] = ledger
        calls["resend_after_days"] = resend_after_days
        return calls.get("result", _result([_candidate()]))

    monkeypatch.setattr(cli, "run_hunt", _fake_run_hunt)
    return calls


def test_hunt_writes_candidates_json_and_exits_zero(tmp_path, spy_hunt, capsys):
    code = cli.main(["hunt", "agritech", "--location", "Galicia", "--out", str(tmp_path)])

    assert code == 0
    assert (tmp_path / "candidates.json").exists()
    out = capsys.readouterr().out
    assert "Acme Viticultura" in out
    assert "candidates.json" in out


def test_query_and_flags_reach_the_pipeline(tmp_path, spy_hunt):
    cli.main(
        [
            "hunt",
            "agritech viñedos",
            "--location",
            "Galicia",
            "--sector",
            "agritech",
            "--max-per-source",
            "7",
            "--out",
            str(tmp_path),
        ]
    )

    spec: QuerySpec = spy_hunt["spec"]
    assert spec.query == "agritech viñedos"
    assert spec.location == "Galicia"
    assert spec.sector == "agritech"
    assert spec.max_per_source == 7


def test_sources_flag_selects_the_connectors(tmp_path, spy_hunt):
    cli.main(["hunt", "agritech", "--sources", "tavily,bdns", "--out", str(tmp_path)])

    assert [c.name for c in spy_hunt["sources"]] == ["tavily", "bdns"]


def test_enrichment_is_off_unless_asked(tmp_path, spy_hunt):
    cli.main(["hunt", "agritech", "--out", str(tmp_path)])
    assert spy_hunt["enricher"] is None

    cli.main(["hunt", "agritech", "--enrich", "--out", str(tmp_path)])
    assert spy_hunt["enricher"] is not None


def test_zero_candidates_is_success_not_an_error(tmp_path, spy_hunt, capsys):
    """El agente distingue 'no hay nada' de 'se rompió' por el código de salida."""
    spy_hunt["result"] = _result([])

    code = cli.main(["hunt", "agritech en Teruel", "--out", str(tmp_path)])

    assert code == 0
    assert "Cero candidatas" in capsys.readouterr().out


def test_unknown_source_fails_with_a_usable_message(tmp_path, capsys):
    code = cli.main(["hunt", "agritech", "--sources", "crunchbase", "--out", str(tmp_path)])

    assert code == 2
    err = capsys.readouterr().err
    assert "crunchbase" in err
    assert "bdns" in err  # sugiere los disponibles


def test_empty_query_is_rejected(tmp_path, capsys):
    code = cli.main(["hunt", "   ", "--out", str(tmp_path)])

    assert code == 2
    assert "vacía" in capsys.readouterr().err


def test_out_dir_falls_back_to_the_env_var(tmp_path, spy_hunt, monkeypatch):
    monkeypatch.setenv("STARTUP_HUNTER_OUTPUT_DIR", str(tmp_path / "desde-env"))

    cli.main(["hunt", "agritech"])

    assert (tmp_path / "desde-env" / "candidates.json").exists()


def test_json_written_by_the_cli_round_trips(tmp_path, spy_hunt):
    cli.main(["hunt", "agritech", "--out", str(tmp_path)])

    payload = json.loads((tmp_path / "candidates.json").read_text(encoding="utf-8"))
    assert HuntResult.model_validate(payload).candidates[0].name == "Acme Viticultura"


def test_hunt_consults_the_ledger(tmp_path, spy_hunt):
    """Sin registro, un hunt diario repite las mismas empresas."""
    cli.main(["hunt", "agritech", "--out", str(tmp_path)])

    assert spy_hunt["ledger"] is not None


def test_mark_seen_reports_the_candidates_as_pending(tmp_path, spy_hunt, capsys):
    cli.main(["hunt", "agritech", "--out", str(tmp_path)])

    code = cli.main(["mark-seen", "--file", str(tmp_path / "candidates.json")])

    assert code == 0
    assert "1 candidata(s) marcada(s) como enviada" in capsys.readouterr().out


def test_mark_seen_leaves_the_entry_reversible_until_confirmed(tmp_path, spy_hunt):
    """El nucleo de la robustez: enviado != confirmado. Una entrega perdida es recuperable."""
    from startup_hunter.ledger import Ledger

    cli.main(["hunt", "agritech", "--out", str(tmp_path)])
    cli.main(["mark-seen", "--file", str(tmp_path / "candidates.json")])

    ledger = Ledger()
    assert len(ledger) == 1
    key = next(iter(ledger._entries))
    assert ledger._entries[key]["status"] == "reported"   # pendiente, no confirmada


def test_confirm_seen_promotes_pending_entries(tmp_path, spy_hunt, capsys):
    from startup_hunter.ledger import Ledger

    cli.main(["hunt", "agritech", "--out", str(tmp_path)])
    cli.main(["mark-seen", "--file", str(tmp_path / "candidates.json")])

    code = cli.main(["confirm-seen"])

    assert code == 0
    assert "1 candidata(s) confirmada(s)" in capsys.readouterr().out
    key = next(iter(Ledger()._entries))
    assert Ledger()._entries[key]["status"] == "confirmed"


def test_hunt_does_not_write_the_ledger(tmp_path, spy_hunt):
    """hunt NO registra: si lo hiciera, un fallo antes del envío perdería candidatas."""
    from startup_hunter.ledger import Ledger

    cli.main(["hunt", "agritech", "--out", str(tmp_path)])
    assert len(Ledger()) == 0


def test_mark_seen_without_a_hunt_fails_with_a_usable_message(tmp_path, capsys):
    code = cli.main(["mark-seen", "--file", str(tmp_path / "no-existe.json")])

    assert code == 2
    assert "Lanza un hunt antes" in capsys.readouterr().err


def test_mark_seen_rejects_a_file_that_is_not_a_hunt(tmp_path, capsys):
    bad = tmp_path / "candidates.json"
    bad.write_text("{}", encoding="utf-8")

    code = cli.main(["mark-seen", "--file", str(bad)])

    assert code == 2
    assert "no es un candidates.json válido" in capsys.readouterr().err


def test_sources_subcommand_lists_connectors(capsys):
    code = cli.main(["sources"])

    assert code == 0
    out = capsys.readouterr().out
    assert "tavily" in out and "bdns" in out

"""I/O de salida del pipeline: escribe ``candidates.json``.

Un solo artefacto, a propósito. Es el contrato canónico que consume el runtime agéntico.
El pipeline no redacta mensajes ni acopla un canal de entrega concreto.

Escritura en UTF-8 explícita: es la defensa contra el cp1252 de Windows. No la quites.
"""

from __future__ import annotations

from pathlib import Path

from startup_hunter.schema import HuntResult


def render_json(result: HuntResult) -> str:
    """Serializa el HuntResult. Es el contrato que lee el agente."""
    return result.model_dump_json(indent=2)


def write_output(result: HuntResult, out_dir: str | Path) -> Path:
    """Escribe ``candidates.json`` en out_dir (UTF-8). Devuelve su ruta."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "candidates.json"
    json_path.write_text(render_json(result), encoding="utf-8")
    return json_path

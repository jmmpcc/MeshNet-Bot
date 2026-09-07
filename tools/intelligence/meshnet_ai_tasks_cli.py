#!/usr/bin/env python3
"""CLI de prueba controlada para MeshNet Intelligence Fase IA-1.

Este programa permite validar resumen y clasificación manualmente sin conectar IA
a ningún flujo operativo. No transmite mensajes, no modifica configuración y no
escribe datos persistentes.

Ejemplos:
    python3 tools/intelligence/meshnet_ai_tasks_cli.py summarize --max-chars 140 "texto"
    python3 tools/intelligence/meshnet_ai_tasks_cli.py classify --labels incendio,inundacion,otro "texto"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.meshnet_ai import MeshNetAI  # noqa: E402
from shared.meshnet_ai_tasks import MeshNetAITasks  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    """Construye el parser completo de comandos IA-1."""
    parser = argparse.ArgumentParser(description="Pruebas manuales MeshNet Intelligence IA-1")
    sub = parser.add_subparsers(dest="command", required=True)

    summarize = sub.add_parser("summarize", help="Resume texto sin usarlo en ningún flujo operativo")
    summarize.add_argument("text", help="Texto de entrada")
    summarize.add_argument("--max-chars", type=int, default=140, help="Máximo de caracteres")
    summarize.add_argument("--context", default="", help="Contexto opcional")
    summarize.add_argument(
        "--preserve",
        default="",
        help="Conceptos a conservar separados por comas",
    )

    classify = sub.add_parser("classify", help="Clasifica texto contra etiquetas cerradas")
    classify.add_argument("text", help="Texto de entrada")
    classify.add_argument("--labels", required=True, help="Etiquetas separadas por comas")
    classify.add_argument("--context", default="", help="Contexto opcional")
    return parser


def main() -> int:
    """Ejecuta una prueba IA-1 y muestra exclusivamente JSON seguro por stdout."""
    args = _build_parser().parse_args()
    tasks = MeshNetAITasks(MeshNetAI.from_env())

    if args.command == "summarize":
        preserve = [item.strip() for item in args.preserve.split(",") if item.strip()]
        result = tasks.summarize_text(
            args.text,
            max_chars=args.max_chars,
            context=args.context,
            preserve=preserve,
        )
        payload = {
            "ok": result.ok,
            "text": result.text,
            "status": result.status,
            "error": result.error,
            "duration_ms": result.duration_ms,
        }
    else:
        labels = [item.strip() for item in args.labels.split(",") if item.strip()]
        result = tasks.classify_text(args.text, labels, context=args.context)
        payload = {
            "ok": result.ok,
            "label": result.label,
            "confidence": result.confidence,
            "reasoning": result.reasoning,
            "status": result.status,
            "error": result.error,
            "duration_ms": result.duration_ms,
        }

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

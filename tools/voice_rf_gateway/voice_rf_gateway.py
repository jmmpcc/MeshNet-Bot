#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))
    from tools.voice_rf_gateway.config import VoiceRfConfig
    from tools.voice_rf_gateway.service import VoiceRfApplication, serve
    from tools.voice_rf_gateway.text_normalizer import compose_emergency_voice_text
else:
    from .config import VoiceRfConfig
    from .service import VoiceRfApplication, serve
    from .text_normalizer import compose_emergency_voice_text


def build_parser() -> argparse.ArgumentParser:
    """Construye el CLI operativo del gateway de voz."""
    parser = argparse.ArgumentParser(description="MeshNet-Bot Voice RF Gateway v7.0.34")
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Comprueba configuración, motores TTS y bloqueo RF")
    subparsers.add_parser("serve", help="Inicia la API HTTP local")

    synthesize = subparsers.add_parser("synthesize", help="Genera un WAV local sin transmitir")
    synthesize.add_argument("--text", required=True)
    synthesize.add_argument("--test", action="store_true", help="Marca la locución como prueba")
    synthesize.add_argument("--keep", action="store_true", help="Conserva el WAV generado")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada del servicio y herramientas de diagnóstico."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = VoiceRfConfig.from_env()
    application = VoiceRfApplication(config)

    if args.command == "doctor":
        print(json.dumps(application.health(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "serve":
        serve(config)
        return 0
    if args.command == "synthesize":
        speech = compose_emergency_voice_text(
            args.text,
            callsign="EB2EAS",
            is_test=args.test,
            max_chars=config.max_text_chars,
        )
        result = application.synthesizer.synthesize(speech, prefix="manual_test")
        payload = result.to_dict()
        payload["sent"] = False
        payload["transmit_reason"] = "not_implemented_safety_lock"
        if result.ok and not args.keep:
            Path(result.output_path).unlink(missing_ok=True)
            payload["output_path"] = ""
            payload["reason"] = "generated_and_discarded"
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if result.ok else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

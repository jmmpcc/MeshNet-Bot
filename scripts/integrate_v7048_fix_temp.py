from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "tools/farmacias_guardia/farmacias_guardia.py"
t = p.read_text(encoding="utf-8")
start = t.index("def send_farmacias_aprs(")
end = t.index("\ndef send_current(", start)
replacement = r'''def send_farmacias_aprs(
    *,
    pharmacies: list[Pharmacy] | None = None,
    requested: bool = False,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Solicita APRS y registra su resultado sin alterar sus autorizaciones.

    Se conserva estrictamente el orden histórico: las autorizaciones se
    comprueban antes de cargar/formatear datos locales. Así, con APRS
    desactivado, la función sigue sin necesitar ``current.json`` ni llamar al
    dispatcher.
    """
    op_id = operation_id or new_operation_id("farmacias")
    text = ""
    if not env_bool("FARMACIAS_APRS_ENABLED", "0"):
        result = {"ok": True, "skipped": True, "error": "farmacias_aprs_disabled"}
    elif not requested and not env_bool("FARMACIAS_APRS_AUTOMATIC", "0"):
        result = {
            "ok": True,
            "skipped": True,
            "error": "farmacias_aprs_automatic_disabled",
        }
    else:
        text = farmacias_aprs_summary(pharmacies)
        result = send_application_aprs(
            source="farmacias",
            text=text,
            dest=os.getenv(
                "FARMACIAS_APRS_DESTINATION",
                os.getenv("APPS_APRS_DESTINATION", "broadcast"),
            ),
            origin="app_farmacias",
        )
    _audit_farmacias_delivery(
        operation_id=op_id,
        transport="aprs",
        destination=str(result.get("dest") or os.getenv("FARMACIAS_APRS_DESTINATION", "broadcast")),
        message=text,
        response=result,
        metadata={"requested": requested},
    )
    return result
'''
p.write_text(t[:start] + replacement + t[end:], encoding="utf-8", newline="\n")
print("v7.0.48 Farmacias compatibility fix applied")

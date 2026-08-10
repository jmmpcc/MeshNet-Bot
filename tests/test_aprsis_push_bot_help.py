from pathlib import Path


BOT_FILE = Path(__file__).resolve().parents[1] / "source" / "Telegram_Bot_Broker.py"


def _bot_source() -> str:
    """Lee el bot como texto sin importar sus dependencias de Telegram/Meshtastic."""
    return BOT_FILE.read_text(encoding="utf-8")


def _aprsis_push_block() -> str:
    """Extrae únicamente la función /aprsis_push para validar su contrato textual."""
    source = _bot_source()
    start = source.index("async def aprsis_push_cmd(")
    end = source.index("\n\n# (Opcional) estado rápido", start)
    return source[start:end]


def test_aprsis_push_help_documents_meshtastic_meshcore_and_mixed_modes():
    block = _aprsis_push_block()
    expected = (
        "/aprsis_push status",
        "/aprsis_push on all",
        "/aprsis_push on meshtastic 0,1",
        "/aprsis_push on meshcore 1,2",
        "/aprsis_push on meshtastic 0,1 meshcore 2,3",
        "RADIO_PROFILE=meshcore_only",
        "0–15",
    )
    for text in expected:
        assert text in block


def test_aprsis_push_status_uses_existing_read_only_udp_contract():
    block = _aprsis_push_block()
    assert 'if sub == "status":' in block
    assert 'msg = {"mode": "aprsis_push"}' in block
    assert 'ack.get("channel_config")' in block
    assert 'ack.get("min_gap_s", "?")' in block


def test_aprsis_push_on_and_off_keep_historical_payload_fields():
    block = _aprsis_push_block()
    assert '"mode": "aprsis_push"' in block
    assert '"enabled": 0' in block
    assert '"enabled": 1' in block
    assert '"channels": channels' in block


def test_aprsis_push_menu_no_longer_contains_obsolete_description():
    source = _bot_source()
    assert "Activa/Descativa" not in source
    assert 'BotCommand("aprsis_push", "Mirror Mesh→APRS-IS: on por transporte | off | status")' in source

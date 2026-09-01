import asyncio
import json
from types import SimpleNamespace

import source.auto_reply_bot as module


class DummyMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


def run_command(args, user_id=1):
    message = DummyMessage()
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id),
    )
    context = SimpleNamespace(args=args)
    asyncio.run(module.auto_reply_cmd(update, context))
    return message.replies


def test_status_reads_existing_configuration(tmp_path, monkeypatch):
    path = tmp_path / "auto_reply.json"
    path.write_text(json.dumps({
        "enabled": True,
        "template": "Recibido: {message}",
        "meshcore": {"channels": [2]},
        "meshtastic": {"channels": [1]},
    }), encoding="utf-8")
    monkeypatch.setenv("AUTO_REPLY_CONFIG", str(path))

    replies = run_command([])
    assert "Estado: ACTIVADA" in replies[-1]
    assert "MeshCore: 2" in replies[-1]
    assert "Meshtastic: 1" in replies[-1]


def test_admin_add_preserves_unknown_fields_and_writes_atomically(tmp_path, monkeypatch):
    path = tmp_path / "auto_reply.json"
    path.write_text(json.dumps({
        "enabled": False,
        "template": "Recibido, {message}",
        "meshcore": {"channels": [2], "future": True},
        "meshtastic": {"channels": []},
        "unknown": {"keep": 1},
    }), encoding="utf-8")
    monkeypatch.setenv("AUTO_REPLY_CONFIG", str(path))
    monkeypatch.setenv("ADMIN_IDS", "7")
    monkeypatch.setattr(module, "_radio_profile_context", lambda: {
        "profile": "meshcore_only",
        "transports": ("meshcore",),
    })

    replies = run_command(["add", "mc", "4"], user_id=7)
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert stored["meshcore"]["channels"] == [2, 4]
    assert stored["meshcore"]["future"] is True
    assert stored["unknown"] == {"keep": 1}
    assert "MeshCore: 2, 4" in replies[-1]


def test_non_admin_cannot_modify(tmp_path, monkeypatch):
    path = tmp_path / "auto_reply.json"
    path.write_text('{"enabled": false}', encoding="utf-8")
    monkeypatch.setenv("AUTO_REPLY_CONFIG", str(path))
    monkeypatch.setenv("ADMIN_IDS", "7")

    replies = run_command(["on"], user_id=8)

    assert replies == ["Solo disponible para administradores."]
    assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is False


def test_template_requires_message_placeholder(tmp_path, monkeypatch):
    path = tmp_path / "auto_reply.json"
    path.write_text('{"enabled": false}', encoding="utf-8")
    monkeypatch.setenv("AUTO_REPLY_CONFIG", str(path))
    monkeypatch.setenv("ADMIN_IDS", "7")

    replies = run_command(["texto", "Confirmado"], user_id=7)

    assert replies == ["La plantilla debe contener {message}."]
    assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is False


def test_transport_must_match_radio_profile(tmp_path, monkeypatch):
    path = tmp_path / "auto_reply.json"
    path.write_text('{"enabled": false}', encoding="utf-8")
    monkeypatch.setenv("AUTO_REPLY_CONFIG", str(path))
    monkeypatch.setenv("ADMIN_IDS", "7")
    monkeypatch.setattr(module, "_radio_profile_context", lambda: {
        "profile": "meshcore_only",
        "transports": ("meshcore",),
    })

    replies = run_command(["add", "mt", "1"], user_id=7)

    assert "no está habilitado" in replies[-1]
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == {"enabled": False}


def test_launcher_registers_command_without_touching_main_bot():
    source = __import__("pathlib").Path("source/Telegram_Bot_ChannelGateway.py").read_text(encoding="utf-8")
    assert "from auto_reply_bot import auto_reply_cmd" in source
    assert 'CommandHandler("autorespuesta", auto_reply_cmd)' in source
    assert 'upsert("autorespuesta", "Administrar autorespuesta por canal")' in source

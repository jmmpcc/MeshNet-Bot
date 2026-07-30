import json

from source.auto_reply import AutoReply


def test_replies_only_on_enabled_transport_channels(tmp_path):
    path = tmp_path / "auto_reply.json"
    path.write_text(json.dumps({
        "enabled": True,
        "template": "Recibido: {message}",
        "meshcore": {"channels": [2]},
        "meshtastic": {"channels": [1]},
    }))
    responder = AutoReply(path)

    assert responder.reply_for("meshtastic", 1, "  hola   mundo ") == "Recibido: hola mundo"
    assert responder.reply_for("meshtastic", 2, "hola") is None
    assert responder.reply_for("meshcore", 2, "mensaje") == "Recibido: mensaje"


def test_prevents_response_loops_and_recent_duplicates(tmp_path):
    path = tmp_path / "auto_reply.json"
    path.write_text(json.dumps({
        "enabled": True,
        "template": "Recibido, {message}",
        "meshtastic": {"channels": [0]},
    }))
    responder = AutoReply(path)

    assert responder.reply_for("meshtastic", 0, "Recibido, prueba") is None
    assert responder.reply_for("meshtastic", 0, "prueba") == "Recibido, prueba"
    assert responder.reply_for("meshtastic", 0, "prueba") is None


def test_configuration_is_reloaded_without_restart(tmp_path):
    path = tmp_path / "auto_reply.json"
    path.write_text('{"enabled": false}')
    responder = AutoReply(path)
    assert responder.reply_for("meshcore", 4, "hola") is None

    path.write_text(json.dumps({
        "enabled": True, "template": "OK {message}",
        "meshcore": {"channels": [4]},
    }))
    assert responder.reply_for("meshcore", 4, "hola") == "OK hola"

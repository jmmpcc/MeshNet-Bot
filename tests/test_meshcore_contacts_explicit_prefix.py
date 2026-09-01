from __future__ import annotations

from pathlib import Path


def _broker_source() -> str:
    """Devuelve el código fuente actual del broker para validar la normalización MeshCore."""
    path = Path(__file__).resolve().parents[1] / "source" / "Meshtastic_Broker.py"
    return path.read_text(encoding="utf-8")


def test_meshcore_contacts_prioriza_prefix_explicito_sobre_public_key_derivada() -> None:
    """El destino DM debe conservar el prefix explícito antes de derivar public_key[:12]."""
    source = _broker_source()

    # Normalización dict: prefix explícito entra directamente en display_prefix,
    # por lo que dm_key lo utiliza antes que el fallback public_key[:12].
    assert 'display_prefix = c.get("pubkey_prefix") or c.get("key_prefix") or c.get("prefix")' in source
    assert 'contact_id = c.get("id")' in source

    # Normalización objeto: mismo orden de prioridad para compatibilidad con
    # distintas versiones de meshcore_py.
    assert 'display_prefix = getattr(c, "pubkey_prefix", None) or getattr(c, "key_prefix", None) or getattr(c, "prefix", None)' in source
    assert 'contact_id = getattr(c, "id", None)' in source

    # La derivación desde public_key se conserva como fallback; no se elimina
    # ningún comportamiento previo cuando la API no expone prefix explícito.
    assert 'dm_key = display_id or (public_key[:12] if public_key else "") or contact_id' in source

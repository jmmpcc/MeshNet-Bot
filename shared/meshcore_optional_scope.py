"""Helper común para añadir un flood scope opcional a TX MeshCore de canal.

Este módulo no transmite ni interpreta mensajes. Su única responsabilidad es
preparar una copia de los parámetros destinados a ``MESHCORE_SEND`` cuando el
llamador ha configurado un scope explícito.

Uso::

    params = add_optional_channel_scope(
        command="MESHCORE_SEND",
        params={"kind": "chan", "channel_idx": 3, "text": "Aviso"},
        scope="#Utebo",
    )

Compatibilidad:
    - Si ``scope`` está vacío, devuelve los parámetros sin cambios.
    - Si el comando no es ``MESHCORE_SEND``, no modifica nada.
    - Si el destino no es un canal, no modifica nada; los DM quedan intactos.
    - Si el llamador ya incluyó ``scope``, respeta ese valor y no lo sustituye.

La normalización y validación definitiva del scope sigue perteneciendo al broker
MeshCore, que implementa la semántica de scope por transmisión y el comportamiento
fail-closed correspondiente.
"""
from __future__ import annotations

from typing import Any


def add_optional_channel_scope(
    command: str,
    params: dict[str, Any],
    scope: str | None,
) -> dict[str, Any]:
    """Añade ``scope`` únicamente a un TX MeshCore de canal cuando procede.

    Parámetros:
        command:
            Comando que se enviará al puerto de control del broker.
        params:
            Parámetros originales del comando. Nunca se modifican in-place.
        scope:
            Scope opcional configurado por la aplicación, por ejemplo
            ``#Utebo``. Cadena vacía o ``None`` significa conservar el
            comportamiento histórico.

    Retorno:
        Un diccionario equivalente al original. Solo incorpora la clave
        ``scope`` cuando el comando es ``MESHCORE_SEND``, el destino es de tipo
        ``chan`` y existe un scope no vacío que no venía ya en ``params``.
    """
    if command != "MESHCORE_SEND":
        return params
    if str(params.get("kind") or "").strip().lower() != "chan":
        return params
    if "scope" in params:
        return params

    normalized_scope = str(scope or "").strip()
    if not normalized_scope:
        return params

    scoped_params = dict(params)
    scoped_params["scope"] = normalized_scope
    return scoped_params

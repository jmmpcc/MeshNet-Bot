from __future__ import annotations

from ..models import Event
from .firms_tracking import (
    FirmsTrackedSource,
    _event_frp_total,
    _metadata_float,
    _metadata_int,
    _phase_description,
    _refresh_event_hashes,
)


class FirmsTrackedPresentationSource(FirmsTrackedSource):
    """Capa de presentación compatible para focos FIRMS persistentes.

    Uso:
        Se registra como implementación operativa de ``SOURCE_TYPES['firms']``.
        Hereda toda la lógica de ``FirmsTrackedSource`` y sólo corrige la
        presentación de los eventos que ve el Control Panel.

    Funcionalidad:
        - Conserva sin cambios correlación espacial/temporal, deduplicación,
          crecimiento por detecciones/FRP/extensión/confianza y resolución.
        - Corrige la forma plural ``detecciones`` en títulos descriptivos nuevos.
        - Migra visualmente focos legacy ya activos a una presentación de
          seguimiento cuando todavía no tenían ``metadata['firms_phase']``.
        - En esa migración conserva exactamente el ``raw_hash`` anterior para
          que el motor NO la interprete como una actualización retransmitible.
        - Los eventos nuevos siguen mostrando ``Inicio`` y los crecimientos
          reales siguen mostrando ``Aumento`` como en v7.0.59.
    """

    def _initial_incident(self, event: Event) -> Event:
        """Crea un foco inicial y normaliza únicamente su texto visible.

        Parámetros:
            event: cluster FIRMS recién detectado por la clase base.

        Retorna:
            El mismo evento inicial, con descripción gramaticalmente correcta y
            hashes recalculados porque se trata de un evento realmente nuevo.
        """
        event = super()._initial_incident(event)
        event.description = _correct_detection_plural(event.description)
        _refresh_event_hashes(event)
        return event

    def _evolve_incident(self, previous: Event, observed: Event) -> Event:
        """Evoluciona el foco y adapta sólo la presentación necesaria.

        Parámetros:
            previous: evento persistido antes de la consulta actual.
            observed: cluster FIRMS observado en la consulta actual.

        Retorna:
            El resultado de ``FirmsTrackedSource``. Si el foco crece, únicamente
            se corrige el plural del texto. Si el evento previo es legacy y la
            pasada actual es estable, se actualizan título/descripcion para el
            Control Panel pero se conserva ``previous.raw_hash`` para impedir una
            falsa notificación por Mesh, APRS RF o APRS-IS.
        """
        legacy_previous = not str(previous.metadata.get("firms_phase") or "").strip()
        evolved = super()._evolve_incident(previous, observed)

        if evolved.metadata.get("firms_phase") == "growth":
            evolved.description = _correct_detection_plural(evolved.description)
            _refresh_event_hashes(evolved)
            return evolved

        if legacy_previous and evolved.metadata.get("firms_phase") == "stable":
            count = _metadata_int(evolved, "latest_detection_count")
            if count <= 0:
                count = _metadata_int(evolved, "detection_count")

            frp_total = _metadata_float(evolved, "latest_frp_total_mw")
            if frp_total is None:
                frp_total = _event_frp_total(evolved)

            extent = _metadata_float(evolved, "latest_extent_km")
            if extent is None:
                extent = _metadata_float(evolved, "cluster_extent_km") or 0.0

            evolved.title = "Foco de incendio satelital en seguimiento"
            evolved.description = _correct_detection_plural(
                _phase_description("Seguimiento", count, frp_total, extent)
            )
            evolved.metadata["presentation_migrated_from_legacy"] = True

            # Esta migración es exclusivamente visual. Mantener el raw_hash
            # anterior garantiza que ``_merge_source`` no produzca ``updated``
            # ni dispare salidas secundarias por un simple cambio de texto.
            evolved.raw_hash = previous.raw_hash

        return evolved


def _correct_detection_plural(value: str) -> str:
    """Corrige únicamente el plural heredado ``detecciónes``.

    Parámetros:
        value: descripción FIRMS generada por la capa de seguimiento.

    Retorna:
        El mismo texto, sustituyendo la grafía incorrecta si estuviera presente.
    """
    return str(value or "").replace("detecciónes", "detecciones")

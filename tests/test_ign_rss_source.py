from __future__ import annotations

import unittest

from tools.emergencias_guardia.emergencias.sources.rss import RssSource


class IgnRssSourceTests(unittest.TestCase):
    """Regresión específica del perfil IGN sin modificar el motor común."""

    @staticmethod
    def _source(profile: str = "ign_earthquakes", **extra: object) -> RssSource:
        """Construye un parser RSS aislado para pruebas unitarias.

        Parámetros:
            profile: perfil RSS que debe utilizar la fuente.
            extra: valores adicionales de configuración de la fuente.

        Retorna:
            Instancia de ``RssSource`` preparada para invocar ``parse`` sin
            realizar ninguna petición HTTP.
        """
        config = {
            "profile": profile,
            "verification": "official",
            "severity": "medium",
            **extra,
        }
        return RssSource("ign_earthquakes", config, {})

    def test_ign_recovers_date_province_and_municipality(self) -> None:
        """IGN recupera fecha y área administrativa desde su texto oficial."""
        body = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss xmlns:georss="http://www.georss.org/georss" version="2.0">
          <channel>
            <item>
              <title>-Info.terremoto: 02/08/2026 13:05:32</title>
              <description>Terremoto de magnitud 2.3 en NW VISTABELLA.Z en la fecha 02/08/2026 13:05:32</description>
              <guid>es2026testz</guid>
              <georss:point>41.2282 -1.1597</georss:point>
            </item>
          </channel>
        </rss>"""

        events = self._source().parse(body)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.category, "earthquake")
        self.assertEqual(event.severity, "low")
        self.assertEqual(event.metadata.get("magnitude"), 2.3)
        self.assertEqual(event.metadata.get("ign_location"), "NW VISTABELLA.Z")
        self.assertEqual(event.province, "Zaragoza")
        self.assertEqual(event.municipality, "Vistabella")
        self.assertEqual(event.started_at, "2026-08-02T13:05:32+00:00")
        self.assertEqual(event.updated_at, "2026-08-02T13:05:32+00:00")
        self.assertAlmostEqual(event.latitude or 0.0, 41.2282)
        self.assertAlmostEqual(event.longitude or 0.0, -1.1597)

    def test_ign_offshore_location_does_not_invent_province(self) -> None:
        """Una localización marítima conserva coordenadas sin provincia falsa."""
        body = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss xmlns:georss="http://www.georss.org/georss" version="2.0">
          <channel>
            <item>
              <title>-Info.terremoto: 04/08/2026 18:17:24</title>
              <description>Terremoto de magnitud 2.6 en COSTERO CATALANA en la fecha 04/08/2026 18:17:24</description>
              <guid>es2026pehob</guid>
              <georss:point>41.9115 3.7071</georss:point>
            </item>
          </channel>
        </rss>"""

        events = self._source().parse(body)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.province, "")
        self.assertEqual(event.municipality, "")
        self.assertEqual(event.metadata.get("ign_location"), "COSTERO CATALANA")
        self.assertEqual(event.started_at, "2026-08-04T18:17:24+00:00")

    def test_generic_rss_behavior_remains_unchanged(self) -> None:
        """La ampliación IGN no altera la normalización de un RSS genérico."""
        body = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Aviso genérico</title>
              <description>Contenido normal</description>
              <guid>generic-1</guid>
              <pubDate>Tue, 11 Aug 2026 08:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>"""

        events = self._source(
            profile="generic",
            category="other",
            severity="medium",
            default_province="Zaragoza",
            default_municipality="Zaragoza",
        ).parse(body)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.category, "other")
        self.assertEqual(event.severity, "medium")
        self.assertEqual(event.province, "Zaragoza")
        self.assertEqual(event.municipality, "Zaragoza")
        self.assertEqual(event.started_at, "2026-08-11T08:00:00+00:00")
        self.assertNotIn("ign_location", event.metadata)
        self.assertNotIn("magnitude", event.metadata)


if __name__ == "__main__":
    unittest.main()

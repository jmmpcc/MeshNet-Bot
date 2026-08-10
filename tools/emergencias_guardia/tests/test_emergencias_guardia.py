import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from emergencias import engine, storage
from emergencias.api import query_from_text
from emergencias.config import DEFAULT_CONFIG
from emergencias.formatters import aprs_emergency_text, byte_chunks, compact_messages, google_maps_url
from emergencias.models import Event
from emergencias import notifier
from emergencias.sources.base import FetchResult, HttpSource, SourceError
from emergencias.sources.datex2 import Datex2Source
from emergencias.sources.json_source import JsonSource
from emergencias.sources.rss import RssSource
from emergencias.sources.firms import FirmsSource


def config():
    return json.loads(json.dumps(DEFAULT_CONFIG))


class ParserTests(unittest.TestCase):
    def test_ign_georss_earthquake_is_normalized(self):
        source = RssSource("ign_earthquakes", {
            "url": "file:///fixture.xml", "profile": "ign_earthquakes",
            "verification": "official",
        }, config())
        body = b'''<rss xmlns:georss="http://www.georss.org/georss"><channel><item>
          <guid>es2026test</guid><title>Terremoto de magnitud 4.2 en ZARAGOZA</title>
          <description><![CDATA[Evento revisado por el IGN]]></description>
          <link>https://www.ign.es/evento/es2026test</link>
          <pubDate>Wed, 29 Jul 2026 08:30:00 GMT</pubDate>
          <georss:point>41.65 -0.89</georss:point>
        </item></channel></rss>'''
        event = source.parse(body)[0]
        self.assertEqual(event.category, "earthquake")
        self.assertEqual(event.severity, "medium")
        self.assertEqual(event.metadata["magnitude"], 4.2)
        self.assertEqual((event.latitude, event.longitude), (41.65, -0.89))
        self.assertEqual(event.verification, "official")

    def test_firms_hotspot_is_explicitly_satellite_detection(self):
        source = FirmsSource("nasa_firms", {}, config())
        body = ("latitude,longitude,acq_date,acq_time,satellite,confidence,frp\n"
                "41.6501,-0.8891,2026-07-29,0830,N,high,125.4\n").encode()
        event = source.parse(body)[0]
        self.assertEqual(event.category, "wildfire")
        self.assertEqual(event.verification, "satellite_detection")
        self.assertEqual(event.severity, "high")
        self.assertEqual(event.metadata["frp_mw"], 125.4)

    def test_firms_key_is_not_written_to_cache_metadata(self):
        source = FirmsSource("nasa_firms", {
            "url_template": "https://example.test/{map_key}/{source}/{bbox}/{days}",
            "dataset": "VIIRS", "bbox": [-2, 40, 0, 42], "days": 1,
        }, config())
        fetched = FetchResult(b"", False, {"url": "https://example.test/secret"})
        with mock.patch.dict("os.environ", {"FIRMS_MAP_KEY": "secret"}), \
                mock.patch.object(HttpSource, "fetch_bytes", return_value=fetched), \
                mock.patch("emergencias.sources.firms.atomic_write_json") as write:
            result = source.fetch_bytes()
        self.assertNotIn("secret", result.metadata["url"])
        self.assertIn("***", result.metadata["url"])
        self.assertNotIn("url", source.config)
        self.assertNotIn("secret", json.dumps(write.call_args.args[1]))

    def test_firms_rejects_documentation_placeholder_before_http(self):
        source = FirmsSource("nasa_firms", {
            "url_template": "https://example.test/{map_key}/{source}/{bbox}/{days}",
        }, config())
        with mock.patch.dict("os.environ", {"FIRMS_MAP_KEY": "SU_MAP_KEY"}), \
                mock.patch.object(HttpSource, "fetch_bytes") as fetch:
            with self.assertRaisesRegex(SourceError, "texto de ejemplo"):
                source.fetch_bytes()
        fetch.assert_not_called()

    def test_rss_rejects_dtd(self):
        source = RssSource("feed", {"url": "file:///fixture.xml"}, config())
        with self.assertRaises(SourceError):
            source.parse(b'<!DOCTYPE x [<!ENTITY boom "bad">]><rss>&boom;</rss>')

    def test_json_geojson_is_normalized(self):
        source = JsonSource("municipal_json", {
            "url": "file:///fixture.json", "verification": "official",
            "default_province": "Zaragoza", "mapping": {},
        }, config())
        body = json.dumps([{
            "id": "zgz-1", "title": "Corte de tráfico", "category": "road_closed",
            "municipality": "Zaragoza",
            "geometry": {"type": "Point", "coordinates": [-0.88, 41.65]},
        }]).encode()
        event = source.parse(body)[0]
        self.assertEqual(event.event_id, "municipal_json:zgz-1")
        self.assertEqual(event.category, "road_closed")
        self.assertEqual(event.province, "Zaragoza")
        self.assertEqual((event.latitude, event.longitude), (41.65, -0.88))

    def test_zaragoza_real_schema_is_classified(self):
        source = JsonSource("municipal_json", {
            "url": "https://www.zaragoza.es/sede/servicio/via-publica/incidencia.json",
            "verification": "official", "default_province": "Zaragoza",
            "default_municipality": "Zaragoza", "records_path": "result",
            "mapping": {
                "description": "motivo", "category": "tipo.title",
                "road": "calle",
                "started_at": "inicio", "updated_at": "lastUpdated",
                "expected_end": "fin", "source_url": "uri",
            },
        }, config())
        body = json.dumps({"result": [{
            "id": 25688, "title": "AVENIDA DE PRUEBA",
            "calle": "VALENCIA, AVENIDA DE, 10",
            "motivo": "Obras en la calzada",
            "tipo": {"id": 1, "title": "Cortes de Tráfico"},
            "inicio": "2026-07-26T00:00:00", "fin": "2026-07-27T00:00:00",
            "geometry": {"type": "Point", "coordinates": [-0.88, 41.65]},
            "uri": "https://www.zaragoza.es/sede/servicio/via-publica/incidencia/25688",
        }]}).encode()
        event = source.parse(body)[0]
        self.assertEqual(event.category, "road_closed")
        self.assertEqual(event.municipality, "Zaragoza")
        self.assertEqual(event.province, "Zaragoza")
        self.assertEqual(event.description, "Obras en la calzada")
        self.assertEqual(event.road, "VALENCIA, AVENIDA DE, 10")

    def test_datex_accident_is_normalized(self):
        xml = b"""<?xml version="1.0"?>
        <d2LogicalModel xmlns="http://datex2.eu/schema/3/common">
          <payloadPublication>
            <situation id="s1">
              <situationRecord id="r1">
                <situationRecordCreationTime>2026-07-26T10:30:00Z</situationRecordCreationTime>
                <accidentType>collision</accidentType>
                <comment><value>Colision con carril afectado</value></comment>
                <roadNumber>A-2</roadNumber><kilometrePoint>314.5</kilometrePoint>
                <town>La Muela</town><administrativeArea>Zaragoza</administrativeArea>
                <latitude>41.58</latitude><longitude>-1.11</longitude>
              </situationRecord>
            </situation>
          </payloadPublication>
        </d2LogicalModel>"""
        source = Datex2Source("dgt_datex", {"url": "file:///fixture.xml"}, config())
        event = source.parse(xml)[0]
        self.assertEqual(event.category, "traffic_collision")
        self.assertEqual(event.road, "A-2")
        self.assertEqual(event.kilometre, 314.5)
        self.assertEqual(event.verification, "official")

    def test_datex_rejects_dtd(self):
        source = Datex2Source("dgt_datex", {"url": "file:///fixture.xml"}, config())
        with self.assertRaises(SourceError):
            source.parse(b'<!DOCTYPE x [<!ENTITY boom "bad">]><x>&boom;</x>')

    def test_datex_v37_uses_explicit_province_and_xsi_type(self):
        xml = b"""<?xml version="1.0"?>
        <d2LogicalModel xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <situation>
            <situationRecord xsi:type="sit:Accident" id="dgt-1">
              <validityStatus>active</validityStatus>
              <accidentType>collision</accidentType>
              <roadName>A-2</roadName><kilometerPoint>314.5</kilometerPoint>
              <municipality>La Muela</municipality><province>Zaragoza</province>
              <autonomousCommunity>Aragon</autonomousCommunity>
              <latitude>41.58</latitude><longitude>-1.11</longitude>
            </situationRecord>
          </situation>
        </d2LogicalModel>"""
        source = Datex2Source(
            "dgt_datex",
            {"url": "file:///fixture.xml", "default_province": "Incorrecta"},
            config(),
        )
        event = source.parse(xml)[0]
        self.assertEqual(event.category, "traffic_collision")
        self.assertEqual(event.province, "Zaragoza")
        self.assertEqual(event.autonomous_region, "Aragon")
        self.assertEqual(event.metadata["datex_record_type"], "Accident")

    def test_datex_does_not_invent_missing_province(self):
        xml = b"""<root><situation><situationRecord id="dgt-2">
          <roadName>A-2</roadName><municipality>La Muela</municipality>
        </situationRecord></situation></root>"""
        source = Datex2Source(
            "dgt_datex",
            {"url": "file:///fixture.xml", "default_province": "Zaragoza"},
            config(),
        )
        self.assertEqual(source.parse(xml)[0].province, "")

    def test_datex_v37_classifies_lane_closure(self):
        xml = b"""<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <situation><situationRecord xsi:type="sit:RoadOrCarriagewayOrLaneManagement" id="dgt-3">
            <roadOrCarriagewayOrLaneManagementType>laneClosures</roadOrCarriagewayOrLaneManagementType>
            <roadName>A-68</roadName><province>Zaragoza</province>
          </situationRecord></situation>
        </root>"""
        source = Datex2Source("dgt_datex", {"url": "file:///fixture.xml"}, config())
        self.assertEqual(source.parse(xml)[0].category, "lane_closed")


class EngineTests(unittest.TestCase):
    def test_empty_collection_categories_collects_nothing(self):
        cfg = config()
        cfg["filters"]["categories"] = []
        self.assertFalse(engine.event_matches(Event("x:0", "x", "0", "earthquake"), cfg))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patchers = [
            mock.patch.object(storage, "CURRENT_FILE", root / "current.json"),
            mock.patch.object(storage, "STATE_FILE", root / "state.json"),
            mock.patch.object(storage, "HISTORY_FILE", root / "history.jsonl"),
            mock.patch.object(storage, "DATA_DIR", root),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def test_national_source_requires_an_enabled_area(self):
        cfg = config()
        cfg["sources"]["dgt_datex"]["enabled"] = True
        cfg["sources"]["dgt_datex"]["url"] = "file:///fixture.xml"
        report = engine.fetch_sources(cfg, "dgt_datex")
        self.assertFalse(report["sources"]["dgt_datex"]["ok"])
        self.assertIn("requiere", report["sources"]["dgt_datex"]["error"])

    def test_merge_emits_new_update_and_resolves_after_two_misses(self):
        report = {"changes": {"new": 0, "updated": 0, "resolved": 0}}
        current = {}
        original = Event(
            "dgt:1", "dgt", "1", "road_closed", title="Corte", road="A-2",
            municipality="La Muela", province="Zaragoza",
        )
        engine._merge_source(current, {"dgt:1": original}, "dgt", "t1", 2, report)
        self.assertEqual(report["changes"]["new"], 1)
        changed = Event(
            "dgt:1", "dgt", "1", "road_closed", title="Corte total", road="A-2",
            municipality="La Muela", province="Zaragoza",
        )
        engine._merge_source(current, {"dgt:1": changed}, "dgt", "t2", 2, report)
        self.assertEqual(report["changes"]["updated"], 1)
        engine._merge_source(current, {}, "dgt", "t3", 2, report)
        self.assertEqual(current["dgt:1"].status, "active")
        engine._merge_source(current, {}, "dgt", "t4", 2, report)
        self.assertEqual(current["dgt:1"].status, "resolved")
        self.assertEqual(report["changes"]["resolved"], 1)

    def test_radius_filter(self):
        cfg = config()
        cfg["areas"] = [{
            "id": "centro", "type": "radius", "name": "centro",
            "latitude": 41.65, "longitude": -0.89, "radius_km": 10, "enabled": True,
        }]
        near = Event("x:1", "x", "1", "other", latitude=41.66, longitude=-0.90)
        far = Event("x:2", "x", "2", "other", latitude=40.41, longitude=-3.70)
        self.assertTrue(engine.event_matches(near, cfg))
        self.assertFalse(engine.event_matches(far, cfg))


class ApiAndFormattingTests(unittest.TestCase):
    def test_filters_cli_saves_category_by_severity_matrix(self):
        cfg = config()
        rules = {"medium": ["civil_protection"], "high": ["earthquake", "wildfire"]}
        with mock.patch("emergencias.cli.load_config", return_value=cfg), \
                mock.patch("emergencias.cli.save_config") as save:
            from emergencias.cli import main
            result = main(["filters", "set", "--rules-json", json.dumps(rules)])
        self.assertEqual(result, 0)
        self.assertEqual(cfg["notifications"]["propagation_filter"]["rules"]["medium"],
                         ["civil_protection"])
        self.assertEqual(cfg["notifications"]["propagation_filter"]["rules"]["high"],
                         ["earthquake", "wildfire"])
        save.assert_called_once()

    def test_firms_source_can_be_enabled_with_url_template(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("emergencias.cli.load_config", return_value=cfg), \
                mock.patch("emergencias.cli.save_config") as save:
            from emergencias.cli import main
            result = main(["source", "enable", "nasa_firms"])
        self.assertEqual(result, 0)
        self.assertTrue(cfg["sources"]["nasa_firms"]["enabled"])
        save.assert_called_once()

    def test_filters_cli_updates_selected_severities_and_categories(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.json"
            with mock.patch("emergencias.cli.load_config", return_value=cfg), \
                 mock.patch("emergencias.cli.save_config") as save:
                from emergencias.cli import main
                result = main([
                    "filters", "set", "--severities", "low,high",
                    "--categories", "wildfire,road_closed",
                ])
                self.assertEqual(result, 0)
                saved = save.call_args.args[0]
                propagation = saved["notifications"]["propagation_filter"]
                self.assertEqual(propagation["severities"], ["low", "high"])
                self.assertNotIn("minimum_severity", propagation)
                self.assertEqual(propagation["categories"], ["road_closed", "wildfire"])

    def test_propagation_filter_accepts_non_contiguous_severities(self):
        cfg = config()
        cfg["notifications"]["propagation_filter"] = {
            "severities": ["low", "high"], "categories": ["road_closed"],
        }
        low = Event("x:1", "x", "1", "road_closed", severity="low")
        medium = Event("x:2", "x", "2", "road_closed", severity="medium")
        high = Event("x:3", "x", "3", "road_closed", severity="high")
        self.assertEqual(notifier.route_event(low, cfg), "servicios")
        self.assertIsNone(notifier.route_event(medium, cfg))
        self.assertEqual(notifier.route_event(high, cfg), "emergencias")

    def test_filters_cli_keeps_minimum_severity_compatibility(self):
        cfg = config()
        with mock.patch("emergencias.cli.load_config", return_value=cfg), \
             mock.patch("emergencias.cli.save_config") as save:
            from emergencias.cli import main
            result = main([
                "filters", "set", "--minimum-severity", "high",
                "--categories", "road_closed",
            ])

        self.assertEqual(result, 0)
        propagation = save.call_args.args[0]["notifications"]["propagation_filter"]
        self.assertEqual(propagation["severities"], ["high", "critical"])

    def test_propagation_filter_does_not_remove_collected_events(self):
        cfg = config()
        cfg["notifications"]["propagation_filter"] = {
            "minimum_severity": "high", "categories": ["road_closed"],
        }
        medium = Event("x:1", "x", "1", "road_closed", severity="medium")
        other = Event("x:2", "x", "2", "wildfire", severity="critical")
        high = Event("x:3", "x", "3", "road_closed", severity="high")
        self.assertIsNone(notifier.route_event(medium, cfg))
        self.assertIsNone(notifier.route_event(other, cfg))
        self.assertEqual(notifier.route_event(high, cfg), "emergencias")

    def test_systemd_uses_incremental_check_without_direct_radio_process(self):
        systemd = APP_DIR / "systemd"
        service = (systemd / "meshnet-emergencias-check.service").read_text(encoding="utf-8")
        timer = (systemd / "meshnet-emergencias-check.timer").read_text(encoding="utf-8")
        self.assertIn("check --notify-changes", service)
        self.assertIn("OnUnitActiveSec=2min", timer)

    def test_query_aliases(self):
        self.assertEqual(query_from_text("emergencias incendios"), {"category": "wildfire"})
        self.assertEqual(query_from_text("emergencias terremotos"), {"category": "earthquake"})
        self.assertEqual(query_from_text("emergencias A-2"), {"road": "A-2"})
        self.assertIsNone(query_from_text("farma"))

    def test_messages_respect_utf8_limit(self):
        event = Event(
            "dgt:1", "dgt", "1", "traffic_collision", severity="high",
            title="Colisión con vehículo y calzada afectada",
            description="Carril derecho cerrado por intervención de emergencias.",
            road="A-2", kilometre=314.5, municipality="La Muela", province="Zaragoza",
            latitude=41.5801, longitude=-1.1187,
        )
        messages = byte_chunks([event], 140)
        self.assertTrue(messages)
        self.assertTrue(all(len(message.encode("utf-8")) <= 140 for message in messages))
        self.assertIn("ALTA · COLISIÓN", messages[0])
        self.assertIn("Dgt", messages[0])
        self.assertIn("https://maps.google.com/?q=41.5801,-1.1187", messages[0])

    def test_google_maps_link_requires_valid_coordinates(self):
        valid = Event(
            "dgt:map", "dgt_datex", "map", "traffic_obstruction",
            latitude=41.64882, longitude=-0.88909,
        )
        missing = Event("dgt:none", "dgt_datex", "none", "traffic_obstruction")
        invalid = Event(
            "dgt:bad", "dgt_datex", "bad", "traffic_obstruction",
            latitude=95, longitude=-0.88,
        )
        self.assertEqual(
            google_maps_url(valid),
            "https://maps.google.com/?q=41.64882,-0.88909",
        )
        self.assertEqual(google_maps_url(missing), "")
        self.assertEqual(google_maps_url(invalid), "")

    def test_aprs_emergency_text_preserves_type_location_and_limit(self):
        event = Event(
            event_id="aprs-1", source="test", source_event_id="1",
            category="traffic_collision", severity="high", status="active",
            title="Colisión con retenciones importantes en ambos sentidos",
            road="A-2", kilometre=315, municipality="La Muela", province="Zaragoza",
        )
        text = aprs_emergency_text(event)
        self.assertTrue(text.startswith("EMERG COLISION"))
        self.assertIn("A-2 km 315", text)
        self.assertIn("La Muela", text)
        self.assertLessEqual(len(text), 67)
        self.assertTrue(text.isascii())

    def test_aprs_emergency_text_terminal_starts_with_fin_and_type(self):
        event = Event(
            event_id="aprs-2", source="test", source_event_id="2",
            category="wildfire", severity="high", status="resolved",
            title="Incendio forestal controlado", municipality="Zuera", province="Zaragoza",
        )
        text = aprs_emergency_text(event)
        self.assertTrue(text.startswith("FIN INCENDIO"))
        self.assertIn("Zuera", text)
        self.assertLessEqual(len(text), 67)

    def test_each_compact_message_is_a_complete_event(self):
        events = [
            Event(
                f"x:{index}", "municipal_json", str(index), "road_closed",
                title=f"Calle de prueba {index}", description="Obras programadas",
                road=f"Z-{index}", municipality="Zaragoza",
                expected_end="2026-08-31T00:00:00+02:00",
            )
            for index in range(1, 4)
        ]
        messages = compact_messages(events, 140, "SERV")
        self.assertEqual(len(messages), 3)
        self.assertTrue(all(message.startswith("SERV [") for message in messages))
        self.assertTrue(all("Ayto. Zaragoza" in message for message in messages))
        self.assertTrue(all(len(message.encode("utf-8")) <= 140 for message in messages))


class NotifierTests(unittest.TestCase):
    def test_propagation_matrix_matches_category_and_exact_severity(self):
        cfg = config()
        cfg["notifications"]["propagation_filter"]["rules"] = {
            "low": [], "medium": ["civil_protection"],
            "high": ["earthquake"], "critical": ["wildfire"],
        }
        medium_civil = Event("x:1", "x", "1", "civil_protection", severity="medium")
        high_civil = Event("x:2", "x", "2", "civil_protection", severity="high")
        high_quake = Event("x:3", "x", "3", "earthquake", severity="high")
        self.assertEqual(notifier.route_event(medium_civil, cfg), "emergencias")
        self.assertIsNone(notifier.route_event(high_civil, cfg))
        self.assertEqual(notifier.route_event(high_quake, cfg), "emergencias")

    @staticmethod
    def _incremental_config():
        cfg = config()
        cfg["notifications"]["enabled"] = True
        cfg["notifications"]["inter_message_delay_seconds"] = 0
        cfg["notifications"]["routes"]["servicios"]["meshcore_channel"] = 2
        cfg["notifications"]["incremental"]["batch_window_seconds"]["servicios"] = 0
        return cfg

    def test_routes_only_serious_official_events_to_emergencias(self):
        cfg = config()
        routine = Event("m:1", "municipal_json", "1", "road_closed", severity="medium")
        serious = Event(
            "d:1", "dgt_datex", "1", "traffic_collision",
            severity="high", verification="official",
        )
        unverified = Event(
            "n:1", "news", "1", "wildfire",
            severity="critical", verification="unverified",
        )
        weather = Event("a:1", "aemet", "1", "storm", severity="high")
        future = Event(
            "m:2", "municipal_json", "2", "road_closed",
            started_at="2999-01-01T00:00:00+00:00",
        )
        self.assertEqual(notifier.route_event(routine, cfg), "servicios")
        self.assertEqual(notifier.route_event(serious, cfg), "emergencias")
        self.assertIsNone(notifier.route_event(unverified, cfg))
        self.assertEqual(notifier.route_event(weather, cfg), "meteo")
        self.assertIsNone(notifier.route_event(future, cfg))

    def test_satellite_detection_is_disabled_by_default(self):
        cfg = config()
        event = Event(
            "firms:1", "nasa_firms", "1", "wildfire",
            severity="high", verification="satellite_detection",
        )
        self.assertIsNone(notifier.route_event(event, cfg))

    def test_send_is_disabled_and_deduplicated(self):
        cfg = config()
        event = Event(
            "m:1", "municipal_json", "1", "road_closed",
            title="Corte", municipality="Zaragoza",
        )
        self.assertEqual(
            notifier.send_route([event], cfg, "servicios")["reason"],
            "notifications_disabled",
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with mock.patch.object(storage, "STATE_FILE", state_path), \
                    mock.patch.object(notifier, "_send_message", return_value={"ok": True}):
                cfg["notifications"]["enabled"] = True
                cfg["notifications"]["inter_message_delay_seconds"] = 0
                cfg["notifications"]["routes"]["servicios"]["meshcore_channel"] = 2
                first = notifier.send_route([event], cfg, "servicios")
                second = notifier.send_route([event], cfg, "servicios")
        self.assertTrue(first["sent"])
        self.assertEqual(second["reason"], "unchanged")

    def test_send_can_use_meshcore_a_and_embedded_meshtastic_b(self):
        cfg = config()
        cfg["notifications"]["enabled"] = True
        cfg["notifications"]["transport"] = "both"
        cfg["notifications"]["inter_message_delay_seconds"] = 0
        cfg["notifications"]["routes"]["servicios"].update({
            "meshcore_channel": 2, "meshtastic_channel": 7,
        })
        event = Event("m:both", "municipal_json", "both", "road_closed", title="Corte")
        targets = []
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(storage, "STATE_FILE", Path(directory) / "state.json"), \
                mock.patch.object(
                    notifier, "_send_message",
                    side_effect=lambda _config, target, _message: targets.append(target) or {"ok": True},
                ):
            result = notifier.send_route([event], cfg, "servicios")
        self.assertTrue(result["sent"])
        self.assertEqual(targets, [
            {"network": "meshcore", "channel": 2},
            {"network": "meshtastic", "channel": 7},
        ])

    def test_incremental_bootstrap_is_silent_then_sends_new_event(self):
        cfg = self._incremental_config()
        existing = Event(
            "m:1", "municipal_json", "1", "road_closed",
            title="Corte existente", municipality="Zaragoza",
        )
        added = Event(
            "m:2", "municipal_json", "2", "water_outage",
            title="Nuevo corte de agua", municipality="Zaragoza",
        )
        now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(storage, "STATE_FILE", Path(directory) / "state.json"), \
                    mock.patch.object(
                        notifier,
                        "_send_message",
                        side_effect=lambda _config, _target, message: sent.append(message) or {"ok": True},
                    ):
                baseline = notifier.process_incremental([existing], cfg, now)
                changed = notifier.process_incremental(
                    [existing, added], cfg, now + timedelta(minutes=1),
                )
        self.assertEqual(baseline["baseline"], 1)
        self.assertEqual(sent[0].splitlines()[0], "NUEVA · SERV")
        self.assertEqual(changed["sent"], 1)

    def test_incremental_sends_update_and_resolution_only_after_delivery(self):
        cfg = self._incremental_config()
        original = Event(
            "m:1", "municipal_json", "1", "road_closed",
            title="Corte", description="Un carril afectado", municipality="Zaragoza",
        )
        now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(storage, "STATE_FILE", Path(directory) / "state.json"), \
                    mock.patch.object(
                        notifier,
                        "_send_message",
                        side_effect=lambda _config, _target, message: sent.append(message) or {"ok": True},
                    ):
                notifier.process_incremental([], cfg, now)
                notifier.process_incremental([original], cfg, now + timedelta(minutes=1))
                updated = Event(
                    "m:1", "municipal_json", "1", "road_closed",
                    title="Corte", description="Corte total", municipality="Zaragoza",
                )
                notifier.process_incremental([updated], cfg, now + timedelta(minutes=2))
                resolved = Event(
                    "m:1", "municipal_json", "1", "road_closed",
                    status="resolved", title="Corte", description="Circulación restablecida",
                    municipality="Zaragoza",
                )
                notifier.process_incremental([resolved], cfg, now + timedelta(minutes=3))
        self.assertEqual(
            [message.splitlines()[0] for message in sent],
            ["NUEVA · SERV", "ACTUALIZACIÓN · SERV", "FINALIZADA · SERV"],
        )

    def test_incremental_failure_stays_pending_and_retries_with_backoff(self):
        cfg = self._incremental_config()
        cfg["notifications"]["incremental"]["retry_base_seconds"] = 60
        event = Event(
            "m:1", "municipal_json", "1", "road_closed",
            title="Corte", municipality="Zaragoza",
        )
        now = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with mock.patch.object(storage, "STATE_FILE", state_path):
                notifier.process_incremental([], cfg, now)
                with mock.patch.object(notifier, "_send_message", side_effect=RuntimeError("broker caído")):
                    failed = notifier.process_incremental(
                        [event], cfg, now + timedelta(minutes=1),
                    )
                with mock.patch.object(notifier, "_send_message", return_value={"ok": True}) as sender:
                    early = notifier.process_incremental(
                        [event], cfg, now + timedelta(minutes=1, seconds=30),
                    )
                    retried = notifier.process_incremental(
                        [event], cfg, now + timedelta(minutes=2, seconds=1),
                    )
        self.assertEqual(failed["failed"], 1)
        self.assertEqual(early["pending"], 1)
        self.assertEqual(retried["sent"], 1)
        self.assertEqual(sender.call_count, 1)


if __name__ == "__main__":
    unittest.main()

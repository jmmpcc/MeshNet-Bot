import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emergencias import engine, storage
from emergencias.api import query_from_text
from emergencias.config import DEFAULT_CONFIG
from emergencias.formatters import byte_chunks
from emergencias.models import Event
from emergencias.sources.base import SourceError
from emergencias.sources.datex2 import Datex2Source
from emergencias.sources.json_source import JsonSource


def config():
    return json.loads(json.dumps(DEFAULT_CONFIG))


class ParserTests(unittest.TestCase):
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


class EngineTests(unittest.TestCase):
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
    def test_query_aliases(self):
        self.assertEqual(query_from_text("emergencias incendios"), {"category": "wildfire"})
        self.assertEqual(query_from_text("emergencias A-2"), {"road": "A-2"})
        self.assertIsNone(query_from_text("farma"))

    def test_messages_respect_utf8_limit(self):
        event = Event(
            "dgt:1", "dgt", "1", "traffic_collision", severity="high",
            title="Colisión con vehículo y calzada afectada",
            description="Carril derecho cerrado por intervención de emergencias.",
            road="A-2", kilometre=314.5, municipality="La Muela", province="Zaragoza",
        )
        messages = byte_chunks([event], 140)
        self.assertTrue(messages)
        self.assertTrue(all(len(message.encode("utf-8")) <= 140 for message in messages))


if __name__ == "__main__":
    unittest.main()

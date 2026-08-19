import json
import unittest

from source.meshcore_observability import (
    MESHCORE_EVENT_SCHEMA_VERSION,
    MeshCoreEvent,
    MeshCoreRepeaterHop,
    build_meshcore_message_event,
)


class MeshCoreObservabilityTest(unittest.TestCase):
    """Pruebas del contrato de observabilidad sin depender de hardware MeshCore."""

    def test_message_event_reuses_enriched_broker_fields(self):
        payload = {
            "id": 1234,
            "text": "Prueba MeshCore",
            "channel_idx": 2,
            "pubkey_prefix": "A1B2C3D4",
            "public_key": "A1B2C3D4EEFF00112233",
            "from_name": "EA2TEST",
            "from_lat": 41.65,
            "from_lon": -0.88,
            "meshcore_repeaters": [
                {
                    "hash": "9a",
                    "name": "RPT-ZGZ",
                    "resolved": True,
                    "ambiguous": False,
                    "snr": 7.5,
                    "lat": 41.7,
                    "lon": -0.9,
                }
            ],
        }

        event = build_meshcore_message_event(
            payload=payload,
            kind="chan",
            channel_idx=2,
            channel_tag="EMERGENCIAS",
            transport="serial",
        )

        self.assertEqual(event.schema_version, MESHCORE_EVENT_SCHEMA_VERSION)
        self.assertEqual(event.direction, "rx")
        self.assertEqual(event.transport, "serial")
        self.assertEqual(event.message_kind, "chan")
        self.assertEqual(event.packet_id, "1234")
        self.assertEqual(event.sender_prefix, "a1b2c3d4")
        self.assertEqual(event.sender_alias, "EA2TEST")
        self.assertEqual(event.channel_idx, 2)
        self.assertEqual(event.channel_tag, "EMERGENCIAS")
        self.assertEqual(event.text, "Prueba MeshCore")
        self.assertEqual(len(event.path_hops), 1)
        self.assertEqual(event.path_hops[0].name, "RPT-ZGZ")
        self.assertEqual(event.path_hops[0].snr, 7.5)

    def test_event_serialization_is_json_safe(self):
        event = MeshCoreEvent(
            payload={"raw": b"\x01\x02", "nested": {"value": bytearray(b"\xaa")}},
            metadata={"tuple": (1, 2)},
            path_hops=[MeshCoreRepeaterHop(hash="AB", resolved=True)],
        )

        serialized = event.to_dict()
        encoded = json.dumps(serialized)

        self.assertIsInstance(encoded, str)
        self.assertEqual(serialized["payload"]["raw"], "0102")
        self.assertEqual(serialized["payload"]["nested"]["value"], "aa")
        self.assertEqual(serialized["metadata"]["tuple"], [1, 2])
        self.assertEqual(serialized["path_hops"][0]["hash"], "AB")

    def test_invalid_values_degrade_to_safe_variants(self):
        event = MeshCoreEvent(
            direction="unexpected",
            transport="wifi",
            message_kind="other",
            channel_idx="not-a-number",
            source_lat="bad",
            source_lon=None,
        )

        self.assertEqual(event.direction, "internal")
        self.assertEqual(event.transport, "unknown")
        self.assertEqual(event.message_kind, "unknown")
        self.assertIsNone(event.channel_idx)
        self.assertIsNone(event.source_lat)
        self.assertIsNone(event.source_lon)

    def test_builder_does_not_mutate_input_payload(self):
        payload = {
            "text": "hola",
            "pubkey_prefix": "ABCDEF",
            "meshcore_repeaters": [{"hash": "01", "name": "R1"}],
        }
        original = {
            "text": payload["text"],
            "pubkey_prefix": payload["pubkey_prefix"],
            "meshcore_repeaters": [dict(payload["meshcore_repeaters"][0])],
        }

        build_meshcore_message_event(payload=payload, kind="contact", transport="tcp")

        self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()

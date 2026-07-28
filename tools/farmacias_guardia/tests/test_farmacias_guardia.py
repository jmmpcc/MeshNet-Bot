import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "farmacias_guardia.py"
spec = importlib.util.spec_from_file_location("farmacias_guardia_app", MODULE_PATH)
app = importlib.util.module_from_spec(spec)
import sys
sys.modules["farmacias_guardia_app"] = app
spec.loader.exec_module(app)


class FarmaciasAppTests(unittest.TestCase):
    def test_preview_without_local_data_returns_clear_panel_message(self):
        output = StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(app, "CURRENT_FILE", Path(temp_dir) / "current.json"),
                mock.patch.object(sys, "argv", ["farmacias_guardia.py", "preview"]),
                redirect_stdout(output),
            ):
                self.assertEqual(app.main(), 1)

        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertIn("Actualizar datos", result["error"])
        self.assertNotIn("Traceback", output.getvalue())

    def test_systemd_checks_for_new_pharmacies_every_three_hours(self):
        systemd_dir = MODULE_PATH.parent / "systemd"
        daily_service = (systemd_dir / "meshnet-farmacias-daily.service").read_text(encoding="utf-8")
        daily_timer = (systemd_dir / "meshnet-farmacias-daily.timer").read_text(encoding="utf-8")
        check_service = (systemd_dir / "meshnet-farmacias-check.service").read_text(encoding="utf-8")

        self.assertIn("farmacias_guardia.py send", daily_service)
        self.assertIn("OnCalendar=*-*-* 08:30:00", daily_timer)
        self.assertNotIn("Persistent=true", daily_timer)
        self.assertIn("farmacias_guardia.py check --send", check_service)
        check_timer = (systemd_dir / "meshnet-farmacias-check.timer").read_text(encoding="utf-8")
        self.assertIn("OnUnitActiveSec=3h", check_timer)

    def test_send_pharmacies_only_formats_supplied_additions(self):
        original_request = app.broker_request
        original_profile = os.environ.get("RADIO_PROFILE")
        original_channel = os.environ.get("FARMACIAS_MESHCORE_CHANNEL")
        original_transport = os.environ.get("FARMACIAS_BROADCAST_TRANSPORT")
        sent = []
        try:
            os.environ["RADIO_PROFILE"] = "meshcore_only"
            os.environ["FARMACIAS_MESHCORE_CHANNEL"] = "1"
            os.environ["FARMACIAS_BROADCAST_TRANSPORT"] = "meshcore"
            app.broker_request = lambda command, params: sent.append((command, params)) or {"ok": True}
            addition = app.Pharmacy("Nueva", "C/ Nueva 1", "976", "Zaragoza", "Centro", "G", "2026-07-25", "new-1")
            result = app.send_pharmacies([addition], "NUEVAS FARMACIAS DE GUARDIA")
            self.assertTrue(result["sent"])
            self.assertTrue(sent)
            self.assertIn("NUEVAS FARMACIAS DE GUARDIA", sent[0][1]["text"])
            self.assertIn("C/ Nueva 1", sent[0][1]["text"])
        finally:
            app.broker_request = original_request
            if original_profile is None:
                os.environ.pop("RADIO_PROFILE", None)
            else:
                os.environ["RADIO_PROFILE"] = original_profile
            if original_channel is None:
                os.environ.pop("FARMACIAS_MESHCORE_CHANNEL", None)
            else:
                os.environ["FARMACIAS_MESHCORE_CHANNEL"] = original_channel
            if original_transport is None:
                os.environ.pop("FARMACIAS_BROADCAST_TRANSPORT", None)
            else:
                os.environ["FARMACIAS_BROADCAST_TRANSPORT"] = original_transport

    def test_normalize_generic_api_record(self):
        item = app.normalize_record({
            "title": "Farmacia Ejemplo",
            "streetAddress": "Avenida de Madrid, 120",
            "telephone": "976123456",
            "addressLocality": "Zaragoza",
            "sector": "Delicias",
            "horarioGuardia": "09:15 a 09:15",
            "id": "zgz-1",
        }, "2026-07-24")
        self.assertIsNotNone(item)
        self.assertEqual(item.locality, "Zaragoza")
        self.assertEqual(item.area, "Delicias")
        self.assertEqual(item.identity, "zgz-1")


    def test_normalize_zaragoza_nested_guardia_sector(self):
        item = app.normalize_record({
            "title": "Farmacia Ejemplo",
            "streetAddress": "Avenida de Madrid, 185",
            "telephone": "976332929",
            "guardia": {
                "sector": "Sector Delicias-Esquina C/ Biarritz-Urb. La Bombarda",
                "horario": "Abiertas de 9:15 h. a 9:15 h. del día siguiente",
                "turno": "T-17",
            },
            "id": "zgz-nested-1",
        }, "2026-07-24")
        self.assertIsNotNone(item)
        self.assertEqual(item.locality, "Zaragoza")
        self.assertEqual(item.area, "Delicias")
        self.assertEqual(item.schedule, "Abiertas de 9:15 h. a 9:15 h. del día siguiente")

    def test_normalize_guardia_as_combined_text(self):
        item = app.normalize_record({
            "title": "Farmacia Texto",
            "streetAddress": "Pº Constitución, 6",
            "telephone": "976229947",
            "guardia": "Abiertas de 9:15 h. a 9:15 h. del día siguiente. Sector Centro-Junto a Plaza Paraíso. Turno: T-17",
            "id": "zgz-text-1",
        }, "2026-07-24")
        self.assertIsNotNone(item)
        self.assertEqual(item.area, "Centro")
        self.assertEqual(item.schedule, "Abiertas de 9:15 h. a 9:15 h. del día siguiente")

    def test_normalize_compound_sector_is_preserved(self):
        self.assertEqual(
            app.normalize_area("Sector Avda. Cataluña-Barrio La Jota", "Zaragoza"),
            "Avda. Cataluña-Barrio La Jota",
        )

    def test_format_query_zaragoza_lists_and_filters_areas(self):
        original_loader = app.load_pharmacies
        try:
            app.load_pharmacies = lambda: [
                app.Pharmacy("A", "Av Madrid 185", "1", "Zaragoza", "Delicias", "G", "2026-07-24", "1"),
                app.Pharmacy("B", "Pº Sagasta 8", "2", "Zaragoza", "Gran Vía", "G", "2026-07-24", "2"),
            ]
            areas = "\n".join(app.format_query("farma zaragoza", "meshcore"))
            self.assertIn("Delicias", areas)
            self.assertIn("Gran Vía", areas)

            filtered = "\n".join(app.format_query("farma zaragoza delicias", "meshcore"))
            self.assertIn("AV MADRID 185", filtered.upper())
            self.assertNotIn("SAGASTA", filtered.upper())
        finally:
            app.load_pharmacies = original_loader


    def test_format_query_help_keeps_localities_listing(self):
        original_loader = app.load_pharmacies
        try:
            app.load_pharmacies = lambda: [
                app.Pharmacy("A", "Av Madrid 185", "1", "Zaragoza", "Delicias", "G", "2026-07-24", "1"),
                app.Pharmacy("B", "C/ Mayor 1", "2", "Utebo", "Utebo", "G", "2026-07-24", "2"),
            ]
            response = "\n".join(app.format_query("farma ayuda", "meshcore"))
            self.assertIn("Localidades:", response)
            self.assertIn("Zaragoza", response)
            self.assertIn("Utebo", response)
            self.assertIn("Uso: farma <localidad>", response)
        finally:
            app.load_pharmacies = original_loader

    def test_format_query_without_arguments_returns_all_guardias(self):
        original_loader = app.load_pharmacies
        try:
            app.load_pharmacies = lambda: [
                app.Pharmacy("A", "Av Madrid 185", "1", "Zaragoza", "Delicias", "G", "2026-07-24", "1"),
                app.Pharmacy("B", "C/ Mayor 1", "2", "Utebo", "Utebo", "G", "2026-07-24", "2"),
            ]
            response = "\n".join(app.format_query("farma", "meshcore"))
            self.assertIn("DELICIAS", response)
            self.assertIn("AV MADRID 185", response.upper())
            self.assertIn("UTEBO", response)
            self.assertIn("C/ MAYOR 1", response.upper())
            self.assertNotIn("Uso: farma <localidad>", response)
        finally:
            app.load_pharmacies = original_loader

    def test_send_meshcore_dm_can_send_more_than_six_parts_for_bare_farma(self):
        original_request = app.broker_request
        original_delay = os.environ.get("FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS")
        original_limit = os.environ.get("FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE")
        sent = []
        try:
            os.environ["FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS"] = "0"
            os.environ["FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE"] = "6"
            app.broker_request = lambda command, params: sent.append((command, params)) or {"ok": True}
            messages = [f"parte-{idx}" for idx in range(1, 9)]
            count = app._send_meshcore_dm("abc123", messages, send_all=True)
            self.assertEqual(count, 8)
            self.assertEqual([params["text"] for _, params in sent], messages)
        finally:
            app.broker_request = original_request
            if original_delay is None:
                os.environ.pop("FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS", None)
            else:
                os.environ["FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS"] = original_delay
            if original_limit is None:
                os.environ.pop("FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE", None)
            else:
                os.environ["FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE"] = original_limit

    def test_byte_chunks_respect_utf8_limit(self):
        lines = ["DELICIAS", "Av Madrid 120 · 976123456", "C/ Ramón y Cajal 4 · 976000000"]
        chunks = app.byte_chunks(lines, "GUARDIA ZARAGOZA 24/07", 90)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 90 for chunk in chunks))

    def test_canonical_hash_ignores_input_order(self):
        a = app.Pharmacy("A", "C/ Uno 1", "1", "Utebo", "Utebo", "G", "2026-07-24", "1")
        b = app.Pharmacy("B", "C/ Dos 2", "2", "Zaragoza", "Centro", "G", "2026-07-24", "2")
        self.assertEqual(app.canonical_hash([a, b]), app.canonical_hash([b, a]))

    def test_added_pharmacies_ignores_updates_and_removals(self):
        existing = app.Pharmacy("A", "C/ Uno 1", "1", "Utebo", "Utebo", "G", "2026-07-24", "id-1")
        updated = app.Pharmacy("A", "C/ Uno 1", "999", "Utebo", "Utebo", "G", "2026-07-24", "id-1")
        removed = app.Pharmacy("B", "C/ Dos 2", "2", "Utebo", "Utebo", "G", "2026-07-24", "id-2")
        added = app.Pharmacy("C", "C/ Tres 3", "3", "Zaragoza", "Centro", "G", "2026-07-24", "id-3")

        self.assertEqual(app.added_pharmacies([existing, removed], [updated, added]), [added])

    def test_added_pharmacies_preserves_source_order(self):
        first = app.Pharmacy("A", "C/ Uno 1", "1", "Utebo", "Utebo", "G", "2026-07-24", "id-1")
        second = app.Pharmacy("B", "C/ Dos 2", "2", "Zaragoza", "Centro", "G", "2026-07-24", "id-2")

        self.assertEqual(app.added_pharmacies([], [second, first]), [second, first])

    def test_listener_handles_meshcore_dm_without_broker_plugin(self):
        original_loader = app.load_pharmacies
        original_request = app.broker_request
        original_dedup = dict(app._MESH_EVENT_DEDUP)
        sent = []
        try:
            app.load_pharmacies = lambda: [
                app.Pharmacy("A", "Av Madrid 185", "976", "Zaragoza", "Delicias", "G", "2026-07-24", "1"),
            ]
            app.broker_request = lambda command, params: sent.append((command, params)) or {"ok": True}
            app._MESH_EVENT_DEDUP.clear()
            app._handle_broker_event({
                "type": "packet",
                "ts": 123,
                "packet": {
                    "fromId": "meshcore:abc123",
                    "rxTime": 123,
                    "meshcore": 1,
                    "meshcore_kind": "contact",
                    "meshcore_pubkey_prefix": "abc123",
                    "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "[MC:abc123] farma"},
                },
            })
            self.assertTrue(sent)
            self.assertEqual(sent[0][0], "MESHCORE_SEND")
            self.assertEqual(sent[0][1]["kind"], "contact")
            self.assertEqual(sent[0][1]["contact_prefix"], "abc123")
        finally:
            app.load_pharmacies = original_loader
            app.broker_request = original_request
            app._MESH_EVENT_DEDUP.clear()
            app._MESH_EVENT_DEDUP.update(original_dedup)

    def test_listener_resolves_meshcore_channel_alias_to_dm(self):
        original_loader = app.load_pharmacies
        original_request = app.broker_request
        original_channel = os.environ.get("FARMACIAS_MESHCORE_CHANNEL")
        original_dedup = dict(app._MESH_EVENT_DEDUP)
        sent = []
        try:
            os.environ["FARMACIAS_MESHCORE_CHANNEL"] = "1"
            app.load_pharmacies = lambda: [
                app.Pharmacy("A", "Av Madrid 185", "976", "Zaragoza", "Delicias", "G", "2026-07-24", "1"),
            ]
            def fake_request(command, params):
                sent.append((command, params))
                if command == "MESHCORE_CONTACTS":
                    return {"ok": True, "contacts": [{"name": "EB2EAS-T1000E", "dm_key": "abc123"}]}
                return {"ok": True}
            app.broker_request = fake_request
            app._MESH_EVENT_DEDUP.clear()
            app._handle_broker_event({
                "type": "packet",
                "ts": 125,
                "packet": {
                    "fromId": "meshcore",
                    "rxTime": 125,
                    "meshcore": 1,
                    "meshcore_kind": "chan",
                    "meshcore_chan_idx": 1,
                    "meshcore_pubkey_prefix": None,
                    "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "EB2EAS-T1000E: Farma Zaragoza"},
                },
            })
            sends = [item for item in sent if item[0] == "MESHCORE_SEND"]
            self.assertTrue(sends)
            self.assertEqual(sends[0][1]["contact_prefix"], "abc123")
        finally:
            app.load_pharmacies = original_loader
            app.broker_request = original_request
            if original_channel is None:
                os.environ.pop("FARMACIAS_MESHCORE_CHANNEL", None)
            else:
                os.environ["FARMACIAS_MESHCORE_CHANNEL"] = original_channel
            app._MESH_EVENT_DEDUP.clear()
            app._MESH_EVENT_DEDUP.update(original_dedup)

    def test_listener_resolves_meshcore_channel_alias_from_env(self):
        original_loader = app.load_pharmacies
        original_request = app.broker_request
        original_channel = os.environ.get("FARMACIAS_MESHCORE_CHANNEL")
        original_aliases = os.environ.get("MESHCORE_CONTACT_ALIASES")
        original_dedup = dict(app._MESH_EVENT_DEDUP)
        sent = []
        try:
            os.environ["FARMACIAS_MESHCORE_CHANNEL"] = "1"
            os.environ["MESHCORE_CONTACT_ALIASES"] = "abc123:EB2EAS-T1000E"
            app.load_pharmacies = lambda: [
                app.Pharmacy("A", "Av Madrid 185", "976", "Zaragoza", "Delicias", "G", "2026-07-24", "1"),
            ]
            app.broker_request = lambda command, params: sent.append((command, params)) or {"ok": True}
            app._MESH_EVENT_DEDUP.clear()
            app._handle_broker_event({
                "type": "packet",
                "ts": 126,
                "packet": {
                    "fromId": "meshcore",
                    "rxTime": 126,
                    "meshcore": 1,
                    "meshcore_kind": "chan",
                    "meshcore_chan_idx": 1,
                    "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "EB2EAS-T1000E: Farma Zaragoza"},
                },
            })
            contacts_queries = [item for item in sent if item[0] == "MESHCORE_CONTACTS"]
            sends = [item for item in sent if item[0] == "MESHCORE_SEND"]
            self.assertEqual(contacts_queries, [])
            self.assertTrue(sends)
            self.assertEqual(sends[0][1]["contact_prefix"], "abc123")
        finally:
            app.load_pharmacies = original_loader
            app.broker_request = original_request
            if original_channel is None:
                os.environ.pop("FARMACIAS_MESHCORE_CHANNEL", None)
            else:
                os.environ["FARMACIAS_MESHCORE_CHANNEL"] = original_channel
            if original_aliases is None:
                os.environ.pop("MESHCORE_CONTACT_ALIASES", None)
            else:
                os.environ["MESHCORE_CONTACT_ALIASES"] = original_aliases
            app._MESH_EVENT_DEDUP.clear()
            app._MESH_EVENT_DEDUP.update(original_dedup)

    def test_listener_rejects_unconfigured_meshcore_channel(self):
        original_request = app.broker_request
        original_channel = os.environ.get("FARMACIAS_MESHCORE_CHANNEL")
        sent = []
        try:
            os.environ["FARMACIAS_MESHCORE_CHANNEL"] = "1"
            app.broker_request = lambda command, params: sent.append((command, params)) or {"ok": True}
            app._handle_broker_event({
                "type": "packet",
                "packet": {
                    "fromId": "meshcore:abc123",
                    "rxTime": 124,
                    "meshcore": 1,
                    "meshcore_kind": "chan",
                    "meshcore_chan_idx": 2,
                    "meshcore_pubkey_prefix": "abc123",
                    "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "farma"},
                },
            })
            self.assertEqual(sent, [])
        finally:
            app.broker_request = original_request
            if original_channel is None:
                os.environ.pop("FARMACIAS_MESHCORE_CHANNEL", None)
            else:
                os.environ["FARMACIAS_MESHCORE_CHANNEL"] = original_channel


if __name__ == "__main__":
    unittest.main()

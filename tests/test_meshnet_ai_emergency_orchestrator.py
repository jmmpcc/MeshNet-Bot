"""Pruebas acumulables de MeshNet Intelligence IA-2E."""

from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from shared.meshnet_ai_emergency_orchestrator import EmergencyAIShadowOrchestrator


def event(source: str, event_id: str, **overrides):
    """Crea un evento mapping mínimo compatible con IA-2A/B/C/D."""

    data = {
        "event_id": event_id,
        "source": source,
        "category": "weather",
        "severity": "medium",
        "verification": "official",
        "status": "active",
        "title": f"Evento {event_id}",
        "description": "Descripción de prueba",
        "municipality": "Zaragoza",
        "province": "Zaragoza",
        "latitude": 41.65,
        "longitude": -0.88,
        "started_at": "2026-09-09T08:00:00+00:00",
        "updated_at": "2026-09-09T08:30:00+00:00",
        "metadata": {},
    }
    data.update(overrides)
    return data


def result(ok=True, status="ok", **kwargs):
    """Resultado inmutable suficiente para simular las fases previas."""

    defaults = {
        "ok": ok,
        "status": status,
        "phase": "updated",
        "candidate": True,
        "relation": "contextual",
        "summary": "Resumen",
        "notes": "Sin observaciones",
        "explanation": "Explicación factual",
        "confidence": 0.8,
        "brief": "Brief",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class FakeAI:
    """Doble mínimo: permite probar flags sin red ni proveedor."""

    def __init__(self, *, enabled=True, emergencies=True, correlation=True):
        self.config = SimpleNamespace(enabled=enabled)
        self._features = {
            "emergencies": emergencies,
            "correlation": correlation,
        }

    def feature_enabled(self, name):
        return self.config.enabled and bool(self._features.get(name, False))


class Recorder:
    """Dependencia inyectable que registra llamadas y devuelve un resultado fijo."""

    def __init__(self, value=None, exc=None):
        self.value = value
        self.exc = exc
        self.calls = []

    def analyze_event(self, event_value, **kwargs):
        self.calls.append((event_value, kwargs))
        if self.exc:
            raise self.exc
        return self.value

    def explain(self, event_value, **kwargs):
        self.calls.append((event_value, kwargs))
        if self.exc:
            raise self.exc
        return self.value

    def correlate(self, event_a, event_b, **kwargs):
        self.calls.append((event_a, event_b, kwargs))
        if self.exc:
            raise self.exc
        return self.value

    def build(self, event_value, **kwargs):
        self.calls.append((event_value, kwargs))
        if self.exc:
            raise self.exc
        return self.value


class EmergencyAIShadowOrchestratorTests(unittest.TestCase):
    """Verifica aislamiento, límites, fallos parciales e inmutabilidad IA-2E."""

    def make_orchestrator(self, ai=None, **overrides):
        ai = ai or FakeAI()
        dependencies = {
            "observer": Recorder(result()),
            "evolution_explainer": Recorder(result()),
            "correlator": Recorder(result()),
            "brief_builder": Recorder(result()),
        }
        dependencies.update(overrides)
        orchestrator = EmergencyAIShadowOrchestrator(
            ai,
            enabled=True,
            **dependencies,
        )
        return orchestrator, dependencies

    def test_global_off_never_calls_any_component(self):
        orchestrator, deps = self.make_orchestrator(ai=FakeAI(enabled=False))
        output = orchestrator.analyze(event("aemet_cap", "a:1"), change="updated")
        self.assertFalse(output.ok)
        self.assertEqual(output.status, "disabled")
        self.assertTrue(all(not item.calls for item in deps.values()))

    def test_emergencies_off_never_calls_any_component(self):
        orchestrator, deps = self.make_orchestrator(
            ai=FakeAI(emergencies=False)
        )
        output = orchestrator.analyze(event("aemet_cap", "a:1"), change="updated")
        self.assertFalse(output.ok)
        self.assertEqual(output.status, "disabled")
        self.assertTrue(all(not item.calls for item in deps.values()))

    def test_orchestrator_off_never_calls_any_component(self):
        ai = FakeAI()
        deps = {
            "observer": Recorder(result()),
            "evolution_explainer": Recorder(result()),
            "correlator": Recorder(result()),
            "brief_builder": Recorder(result()),
        }
        orchestrator = EmergencyAIShadowOrchestrator(ai, enabled=False, **deps)
        output = orchestrator.analyze(event("aemet_cap", "a:1"), change="updated")
        self.assertFalse(output.ok)
        self.assertEqual(output.status, "disabled")
        self.assertTrue(all(not item.calls for item in deps.values()))

    def test_coordinates_all_phases_without_mutating_inputs(self):
        main = event("aemet_cap", "a:1")
        peer = event("datex2", "d:1", latitude=41.66, longitude=-0.87)
        before_main = copy.deepcopy(main)
        before_peer = copy.deepcopy(peer)
        orchestrator, deps = self.make_orchestrator()

        output = orchestrator.analyze(
            main,
            change="updated",
            peer_events=[peer],
        )

        self.assertTrue(output.ok)
        self.assertEqual(output.status, "ok")
        self.assertEqual(output.event_id, "a:1")
        self.assertEqual(output.deterministic_phase, "updated")
        self.assertEqual(len(deps["observer"].calls), 1)
        self.assertEqual(len(deps["evolution_explainer"].calls), 1)
        self.assertEqual(len(deps["correlator"].calls), 1)
        self.assertEqual(len(deps["brief_builder"].calls), 1)
        brief_kwargs = deps["brief_builder"].calls[0][1]
        self.assertIs(output.analysis, brief_kwargs["analysis"])
        self.assertIs(output.evolution, brief_kwargs["evolution"])
        self.assertEqual(tuple(output.correlations), brief_kwargs["correlations"])
        self.assertEqual(main, before_main)
        self.assertEqual(peer, before_peer)

    def test_correlation_off_skips_correlator_but_keeps_other_phases(self):
        orchestrator, deps = self.make_orchestrator(
            ai=FakeAI(correlation=False)
        )
        output = orchestrator.analyze(
            event("aemet_cap", "a:1"),
            change="updated",
            peer_events=[event("datex2", "d:1")],
        )
        self.assertTrue(output.ok)
        self.assertEqual(output.correlations, ())
        self.assertEqual(deps["correlator"].calls, [])
        self.assertEqual(len(deps["brief_builder"].calls), 1)

    def test_non_candidate_never_reaches_correlator(self):
        main = event("aemet_cap", "a:1", latitude=41.65, longitude=-0.88)
        far_peer = event("datex2", "d:1", latitude=28.10, longitude=-17.10)
        orchestrator, deps = self.make_orchestrator()
        output = orchestrator.analyze(main, peer_events=[far_peer], change="updated")
        self.assertTrue(output.ok)
        self.assertEqual(deps["correlator"].calls, [])

    def test_same_source_never_reaches_correlator(self):
        orchestrator, deps = self.make_orchestrator()
        output = orchestrator.analyze(
            event("aemet_cap", "a:1"),
            peer_events=[event("aemet_cap", "a:2")],
            change="updated",
        )
        self.assertTrue(output.ok)
        self.assertEqual(deps["correlator"].calls, [])

    def test_limit_is_capped_to_eight_before_correlator(self):
        main = event("aemet_cap", "a:main")
        peers = [
            event(f"source_{index}", f"peer:{index:02d}")
            for index in range(20)
        ]
        orchestrator, deps = self.make_orchestrator()
        output = orchestrator.analyze(
            main,
            change="updated",
            peer_events=peers,
            max_correlation_candidates=99,
        )
        self.assertTrue(output.ok)
        self.assertEqual(len(deps["correlator"].calls), 8)
        self.assertEqual(len(output.correlations), 8)

    def test_candidate_selection_order_is_stable_by_event_id(self):
        main = event("aemet_cap", "a:main")
        peers = [
            event("source_z", "peer:z"),
            event("source_a", "peer:a"),
            event("source_m", "peer:m"),
        ]
        orchestrator, deps = self.make_orchestrator()
        orchestrator.analyze(main, change="updated", peer_events=peers)
        called_ids = [call[1]["event_id"] for call in deps["correlator"].calls]
        self.assertEqual(called_ids, ["peer:a", "peer:m", "peer:z"])

    def test_one_phase_exception_produces_partial_without_aborting_rest(self):
        orchestrator, deps = self.make_orchestrator(
            observer=Recorder(exc=RuntimeError("fallo 2A"))
        )
        output = orchestrator.analyze(
            event("aemet_cap", "a:1"),
            change="updated",
            peer_events=[event("datex2", "d:1")],
        )
        self.assertTrue(output.ok)
        self.assertEqual(output.status, "partial")
        self.assertIsNone(output.analysis)
        self.assertIsNotNone(output.evolution)
        self.assertEqual(len(output.correlations), 1)
        self.assertIn("IA-2A RuntimeError", output.error)
        self.assertEqual(len(deps["brief_builder"].calls), 1)

    def test_one_correlation_exception_does_not_cancel_remaining_candidates(self):
        class SelectiveCorrelator:
            def __init__(self):
                self.calls = []

            def correlate(self, event_a, event_b, **kwargs):
                self.calls.append(event_b["event_id"])
                if event_b["event_id"] == "peer:a":
                    raise TimeoutError("timeout simulado")
                return result()

        correlator = SelectiveCorrelator()
        orchestrator, _ = self.make_orchestrator(correlator=correlator)
        output = orchestrator.analyze(
            event("aemet_cap", "main"),
            change="updated",
            peer_events=[
                event("source_b", "peer:b"),
                event("source_a", "peer:a"),
            ],
        )
        self.assertTrue(output.ok)
        self.assertEqual(output.status, "partial")
        self.assertEqual(correlator.calls, ["peer:a", "peer:b"])
        self.assertEqual(len(output.correlations), 1)
        self.assertIn("IA-2B TimeoutError", output.error)

    def test_no_valid_results_returns_no_analysis(self):
        invalid = result(ok=False, status="not_available")
        orchestrator, _ = self.make_orchestrator(
            observer=Recorder(invalid),
            evolution_explainer=Recorder(invalid),
            brief_builder=Recorder(invalid),
        )
        output = orchestrator.analyze(
            event("aemet_cap", "a:1"),
            change="updated",
        )
        self.assertFalse(output.ok)
        self.assertEqual(output.status, "no_analysis")

    def test_invalid_candidate_limit_fails_before_components(self):
        orchestrator, deps = self.make_orchestrator()
        output = orchestrator.analyze(
            event("aemet_cap", "a:1"),
            max_correlation_candidates=0,
        )
        self.assertFalse(output.ok)
        self.assertEqual(output.status, "error")
        self.assertTrue(all(not item.calls for item in deps.values()))

    def test_firms_phase_remains_deterministic(self):
        firms = event(
            "nasa_firms",
            "firms:1",
            category="wildfire",
            metadata={"firms_phase": "stable"},
        )
        orchestrator, _ = self.make_orchestrator()
        output = orchestrator.analyze(firms, change="updated")
        self.assertEqual(output.deterministic_phase, "stable")

    def test_duration_is_non_negative_integer(self):
        orchestrator, _ = self.make_orchestrator()
        output = orchestrator.analyze(event("aemet_cap", "a:1"), change="updated")
        self.assertIsInstance(output.duration_ms, int)
        self.assertGreaterEqual(output.duration_ms, 0)


if __name__ == "__main__":
    unittest.main()

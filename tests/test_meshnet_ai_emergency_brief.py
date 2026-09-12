from __future__ import annotations

import json
import unittest
from copy import deepcopy

from shared.meshnet_ai import AIConfig, AIFeatures, AIResult
from shared.meshnet_ai_emergency_brief import (
    EmergencyAISituationalBriefBuilder,
    deterministic_brief_snapshot,
)


class FakeAI:
    """Doble mínimo de MeshNetAI para probar IA-2D sin red ni credenciales."""

    def __init__(self, *, enabled=True, emergencies=True, response=None):
        self.config = AIConfig(
            enabled=enabled,
            provider="ollama",
            model="test",
            features=AIFeatures(emergencies=emergencies),
        )
        self.response = response or AIResult(ok=False, status="degraded", error="sin respuesta")
        self.calls = 0
        self.last_prompt = ""
        self.last_system = ""

    def feature_enabled(self, feature: str) -> bool:
        return self.config.enabled and bool(self.config.features.as_dict().get(feature, False))

    def generate_text(self, prompt: str, system: str = "") -> AIResult:
        self.calls += 1
        self.last_prompt = prompt
        self.last_system = system
        return self.response


def event(**overrides):
    data = {
        "event_id": "nasa_firms:test:001",
        "source": "nasa_firms",
        "category": "wildfire",
        "severity": "high",
        "verification": "satellite_detection",
        "status": "active",
        "change": "updated",
        "title": "Aumento del foco de incendio satelital",
        "description": "Aumento de posible foco detectado por NASA FIRMS",
        "municipality": "Zaragoza",
        "province": "Zaragoza",
        "latitude": 41.65,
        "longitude": -0.88,
        "started_at": "2026-09-07T10:00:00+00:00",
        "updated_at": "2026-09-07T12:00:00+00:00",
        "metadata": {"firms_phase": "growth", "private_secret": "NO ENVIAR"},
    }
    data.update(overrides)
    return data


def analysis(ok=True, phase="growth"):
    return {
        "ok": ok,
        "summary": "Posible foco FIRMS con aumento observado.",
        "notes": "Detección satelital; requiere contexto adicional.",
        "confidence": 0.9,
        "phase": phase,
    }


def evolution(ok=True, phase="growth"):
    return {
        "ok": ok,
        "phase": phase,
        "explanation": "Aumentan detecciones, FRP y extensión observada.",
        "confidence": 0.92,
    }


def correlation(ok=True, candidate=True, relation="contextual"):
    return {
        "ok": ok,
        "candidate": candidate,
        "relation": relation,
        "explanation": "Existe contexto meteorológico cercano sin confirmar identidad.",
        "confidence": 0.8,
        "distance_km": 3.2,
        "time_delta_minutes": 20.0,
    }


class EmergencyAISituationalBriefTests(unittest.TestCase):
    def test_snapshot_uses_minimal_event_and_valid_components(self):
        snapshot = deterministic_brief_snapshot(
            event(), analysis=analysis(), evolution=evolution(), correlations=[correlation()]
        )
        self.assertEqual(snapshot["phase"], "growth")
        self.assertNotIn("metadata", snapshot["event"])
        self.assertIn("ia2a_analysis", snapshot["shadow_components"])
        self.assertIn("ia2c_evolution", snapshot["shadow_components"])
        self.assertIn("ia2b_correlations", snapshot["shadow_components"])
        self.assertNotIn("private_secret", json.dumps(snapshot))

    def test_invalid_or_mismatched_components_are_excluded(self):
        snapshot = deterministic_brief_snapshot(
            event(), analysis=analysis(False), evolution=evolution(phase="stable"),
            correlations=[correlation(candidate=False), correlation(relation="invented")]
        )
        self.assertEqual(snapshot["shadow_components"], {})

    def test_global_disabled_never_calls_provider(self):
        ai = FakeAI(enabled=False)
        result = EmergencyAISituationalBriefBuilder(ai).build(event(), analysis=analysis())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "disabled")
        self.assertEqual(ai.calls, 0)

    def test_emergencies_disabled_never_calls_provider(self):
        ai = FakeAI(emergencies=False)
        result = EmergencyAISituationalBriefBuilder(ai).build(event(), analysis=analysis())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "feature_disabled")
        self.assertEqual(ai.calls, 0)

    def test_no_valid_components_never_calls_provider(self):
        ai = FakeAI()
        result = EmergencyAISituationalBriefBuilder(ai).build(event())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_available")
        self.assertEqual(ai.calls, 0)

    def test_unknown_phase_never_calls_provider(self):
        ai = FakeAI()
        unknown = event(source="aemet_cap", change="", status="active", metadata={})
        result = EmergencyAISituationalBriefBuilder(ai).build(unknown, analysis={**analysis(), "phase": ""})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "not_available")
        self.assertEqual(ai.calls, 0)

    def test_invalid_limits_never_call_provider(self):
        ai = FakeAI()
        result = EmergencyAISituationalBriefBuilder(ai).build(event(), analysis=analysis(), max_brief_chars=0)
        self.assertFalse(result.ok)
        self.assertEqual(ai.calls, 0)

    def test_valid_response_is_accepted(self):
        ai = FakeAI(response=AIResult(
            ok=True,
            status="available",
            duration_ms=12,
            text=json.dumps({
                "brief": "Posible foco FIRMS en fase determinista de crecimiento, con aumento observado y contexto meteorológico cercano.",
                "uncertainties": "La correlación es contextual y no confirma identidad ni causalidad.",
                "confidence": 0.88,
            }),
        ))
        result = EmergencyAISituationalBriefBuilder(ai).build(
            event(), analysis=analysis(), evolution=evolution(), correlations=[correlation()]
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.phase, "growth")
        self.assertEqual(result.confidence, 0.88)
        self.assertEqual(result.status, "available")
        self.assertEqual(set(result.components), {"ia2a_analysis", "ia2c_evolution", "ia2b_correlations"})
        constraints = json.loads(ai.last_prompt)["constraints"]
        self.assertIn("no_operational_decisions", constraints)
        self.assertEqual(constraints["output_language"], "es")
        self.assertIn("Responde SIEMPRE en español", ai.last_system)
        self.assertIn("No decidas prioridad", ai.last_system)
        self.assertIn("posible foco", ai.last_system)
        self.assertIn("ni describas una fase de crecimiento del incendio", ai.last_system.casefold())
        self.assertIn("stable no significa extinguido", ai.last_system.casefold())
        self.assertEqual(
            constraints["firms_evidence"],
            {"detections": False, "extent": False, "frp": False},
        )

    def test_non_string_text_fields_are_rejected(self):
        for bad in (["texto"], None, {"x": 1}):
            with self.subTest(bad=bad):
                ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
                    "brief": bad,
                    "uncertainties": "texto",
                    "confidence": 0.5,
                })))
                result = EmergencyAISituationalBriefBuilder(ai).build(event(), analysis=analysis())
                self.assertFalse(result.ok)

    def test_invalid_confidence_is_rejected(self):
        for value in ("0.8", True, False, None, -0.1, 1.1, float("inf")):
            with self.subTest(value=value):
                text = json.dumps({"brief": "Texto factual.", "uncertainties": "Ninguna adicional.", "confidence": value})
                ai = FakeAI(response=AIResult(ok=True, status="available", text=text))
                result = EmergencyAISituationalBriefBuilder(ai).build(event(), analysis=analysis())
                self.assertFalse(result.ok)
                self.assertIn("confidence", result.error)

    def test_firms_overstatement_is_rejected(self):
        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
            "brief": "Ha aumentado la superficie afectada por el incendio.",
            "uncertainties": "",
            "confidence": 0.8,
        })))
        result = EmergencyAISituationalBriefBuilder(ai).build(event(), evolution=evolution())
        self.assertFalse(result.ok)
        self.assertIn("sobreafirmación", result.error)

    def test_firms_negated_affected_surface_is_allowed(self):
        """La incertidumbre real observada no debe provocar un falso positivo."""

        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
            "brief": "Posible foco FIRMS con crecimiento de las detecciones satelitales.",
            "uncertainties": "No se puede confirmar el incendio ni establecer con certeza la superficie afectada.",
            "confidence": 0.9,
        })))
        result = EmergencyAISituationalBriefBuilder(ai).build(event(), evolution=evolution())
        self.assertTrue(result.ok)
        self.assertIn("superficie afectada", result.uncertainties)

    def test_firms_absent_frp_can_be_stated_as_missing_information(self):
        """Regresión IA-2E: negar datos FRP ausentes no es una sobreafirmación.

        Cómo se llama:
            Reproduce la frase real devuelta por el proveedor en Raspberry.

        Funcionalidad:
            Verifica que, con frp=false, una frase que diga explícitamente que no
            se dispone de información sobre potencia radiante sea aceptada.
        """
        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
            "brief": (
                "Posible foco FIRMS con aumento del número de detecciones. "
                "No se dispone de información sobre la extensión o potencia "
                "radiante del fenómeno."
            ),
            "uncertainties": (
                "No es posible determinar con los datos disponibles la superficie "
                "afectada ni la intensidad precisa del posible evento."
            ),
            "confidence": 0.9,
        })))

        result = EmergencyAISituationalBriefBuilder(ai).build(
            event(),
            analysis=analysis(),
        )

        self.assertTrue(result.ok)
        self.assertIn("No se dispone de información", result.brief)

    def test_firms_frp_mention_without_frp_evidence_is_rejected(self):
        """Regresión IA-2E: IA-2D no puede inventar potencia radiante observada.

        Cómo se llama:
            Reproduce la incertidumbre observada en Raspberry, donde el proveedor
            mencionó potencia radiante pese a que el evento no incluía ningún dato
            FRP.

        Funcionalidad:
            Comprueba que la barrera de evidencia rechace cualquier mención de FRP
            o potencia radiante cuando ``firms_evidence["frp"]`` es False.
        """
        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
            "brief": "Posible foco FIRMS en seguimiento satelital.",
            "uncertainties": (
                "No puede evaluarse la intensidad real más allá de la potencia "
                "radiante observada por FIRMS."
            ),
            "confidence": 0.8,
        })))

        result = EmergencyAISituationalBriefBuilder(ai).build(
            event(),
            analysis=analysis(),
        )

        self.assertFalse(result.ok)
        self.assertIn("sobreafirmación", result.error)
        constraints = json.loads(ai.last_prompt)["constraints"]
        self.assertEqual(constraints["firms_evidence"]["frp"], False)

    def test_firms_prompt_does_not_trust_unsafe_shadow_wording(self):
        """Regresión IA-2E: IA-2D debe reformular texto sombra potencialmente inseguro.

        La salida simulada es segura, pero la entrada IA-2A contiene la formulación
        "incendio activo". El prompt debe indicar explícitamente que los componentes
        sombra no son autoridad semántica y que esa expresión no debe copiarse.
        """
        unsafe_analysis = {
            **analysis(),
            "summary": (
                "La detección FIRMS indica un evento de incendio forestal activo "
                "no verificado."
            ),
        }
        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
            "brief": (
                "Posible foco observado por FIRMS con aumento de detecciones "
                "satelitales en la fase determinista growth."
            ),
            "uncertainties": "La detección satelital no confirma un incendio.",
            "confidence": 0.85,
        })))

        result = EmergencyAISituationalBriefBuilder(ai).build(
            event(),
            analysis=unsafe_analysis,
            evolution=evolution(),
        )

        self.assertTrue(result.ok)
        self.assertIn("NO son autoridad semántica", ai.last_system)
        self.assertIn("aunque aparezcan en un componente sombra", ai.last_system)
        self.assertIn("Evita expresamente 'incendio activo'", ai.last_system)

    def test_firms_categorical_fire_growth_is_rejected(self):
        """La fase growth de FIRMS nunca debe convertirse en crecimiento del incendio."""

        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
            "brief": "El evento se encuentra en fase de crecimiento del incendio.",
            "uncertainties": "La observación es satelital.",
            "confidence": 0.9,
        })))
        result = EmergencyAISituationalBriefBuilder(ai).build(event(), evolution=evolution())
        self.assertFalse(result.ok)
        self.assertIn("sobreafirmación", result.error)

    def test_firms_stable_cannot_become_extinguished_confirmed_fire(self):
        """Regresión Codex P2: stable no puede convertirse en incendio extinguido/confirmado."""

        stable_event = event(
            change="updated",
            title="Posible foco satelital estable",
            metadata={"firms_phase": "stable"},
        )
        stable_analysis = analysis(phase="stable")
        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
            "brief": "El incendio está extinguido y confirmado.",
            "uncertainties": "La observación procede de FIRMS.",
            "confidence": 0.95,
        })))
        result = EmergencyAISituationalBriefBuilder(ai).build(stable_event, analysis=stable_analysis)
        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "stable")
        self.assertIn("sobreafirmación", result.error)

    def test_firms_negation_does_not_cover_later_affirmative_clause(self):
        """Regresión Codex P2: cada aparición sensible requiere su propia negación."""

        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
            "brief": "Posible foco FIRMS observado por satélite.",
            "uncertainties": (
                "No se puede determinar la superficie afectada, pero la superficie "
                "afectada es de 100 km²."
            ),
            "confidence": 0.9,
        })))
        result = EmergencyAISituationalBriefBuilder(ai).build(event(), evolution=evolution())
        self.assertFalse(result.ok)
        self.assertIn("sobreafirmación", result.error)

    def test_text_limits_reuse_safe_fit(self):
        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
            "brief": "Primera frase completa. Segunda frase que no debería quedar cortada a mitad de palabra porque excede el límite solicitado.",
            "uncertainties": "Sin datos adicionales relevantes.",
            "confidence": 0.7,
        })))
        result = EmergencyAISituationalBriefBuilder(ai).build(event(), analysis=analysis(), max_brief_chars=45)
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.brief), 45)
        self.assertEqual(result.brief, "Primera frase completa. Segunda frase que")

    def test_event_and_components_are_not_mutated(self):
        ev = event()
        an = analysis()
        evo = evolution()
        corr = [correlation()]
        originals = deepcopy((ev, an, evo, corr))
        ai = FakeAI(response=AIResult(ok=True, status="available", text=json.dumps({
            "brief": "Síntesis factual del posible foco.",
            "uncertainties": "Información limitada a observaciones disponibles.",
            "confidence": 0.7,
        })))
        EmergencyAISituationalBriefBuilder(ai).build(ev, analysis=an, evolution=evo, correlations=corr)
        self.assertEqual((ev, an, evo, corr), originals)


if __name__ == "__main__":
    unittest.main()
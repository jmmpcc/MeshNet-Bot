import os
import sys
import unittest
from pathlib import Path
from unittest import mock


SOURCE_DIR = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(SOURCE_DIR))

import zaragoza_commands as commands


class _AllowLimiter:
    """Limitador de prueba que permite siempre la consulta."""

    def check_and_record(self, _ctx):
        return True, 0, False


class _DuplicateLimiter:
    """Limitador de prueba que simula un paquete duplicado."""

    def check_and_record(self, _ctx):
        return False, 0, True


class _RateLimitedLimiter:
    """Limitador de prueba que simula haber agotado la cuota."""

    def check_and_record(self, _ctx):
        return False, 120, False


class _MessagesClient:
    """Cliente simulado que devuelve los mensajes indicados por el test."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.seen_text = None

    def query(self, ctx):
        self.seen_text = commands._normalized_command(ctx.text)
        return list(self.messages)


class ZaragozaCommandsTests(unittest.TestCase):
    """
    Pruebas de regresión del manejador AGENDA/NOTICIAS.

    No necesitan radio ni servidor HTTP real. Sustituyen el cliente y el
    limitador por dobles controlados para verificar exclusivamente la lógica del
    módulo broker-side.
    """

    ENV_NAMES = (
        "ZARAGOZA_COMMAND_ENABLED",
        "ZARAGOZA_DM_INTER_MESSAGE_DELAY_SECONDS",
        "ZARAGOZA_DM_MAX_MESSAGES_PER_RESPONSE",
        "ZARAGOZA_MESHCORE_CHANNEL",
        "ZARAGOZA_MESHTASTIC_CHANNEL",
    )

    def setUp(self):
        """Guarda entorno/globales y activa Zaragoza solo durante cada test."""
        self.original_limiter = commands._LIMITER
        self.original_client = commands._CLIENT
        self.original_env = {
            name: os.environ.get(name)
            for name in self.ENV_NAMES
        }

        commands._LIMITER = _AllowLimiter()

        os.environ["ZARAGOZA_COMMAND_ENABLED"] = "true"
        os.environ[
            "ZARAGOZA_DM_INTER_MESSAGE_DELAY_SECONDS"
        ] = "0"
        os.environ[
            "ZARAGOZA_DM_MAX_MESSAGES_PER_RESPONSE"
        ] = "4"
        os.environ["ZARAGOZA_MESHCORE_CHANNEL"] = "-1"
        os.environ["ZARAGOZA_MESHTASTIC_CHANNEL"] = "-1"

    def tearDown(self):
        """Restaura exactamente el estado previo a cada test."""
        commands._LIMITER = self.original_limiter
        commands._CLIENT = self.original_client

        for name, value in self.original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def _ctx(
        text="agenda hoy",
        *,
        direct=True,
        channel=None,
        network="meshcore",
        packet_id="pkt-1",
    ):
        """Crea un contexto mínimo reutilizable en las pruebas."""
        return commands.ZaragozaCommandContext(
            network=network,
            source_id="abc123",
            text=text,
            channel=channel,
            is_direct=direct,
            packet_id=packet_id,
        )

    def test_recognizes_only_reserved_namespaces(self):
        """AGENDA/NOTICIA(S) se reconocen como palabras completas."""
        valid = (
            "AGENDA",
            "agenda hoy",
            "Agenda mañana",
            "AGENDA INFANTIL",
            "NOTICIAS",
            "noticias movilidad",
            "NOTICIA",
            "noticia tranvía",
        )
        invalid = (
            "",
            "FARMA",
            "EMERGENCIAS",
            "agendado",
            "mi agenda",
            "noticiaste",
            "sin noticias",
        )

        for text in valid:
            self.assertTrue(
                commands.is_zaragoza_command(text),
                text,
            )

        for text in invalid:
            self.assertFalse(
                commands.is_zaragoza_command(text),
                text,
            )

    def test_disabled_command_is_not_consumed(self):
        """Con el servicio deshabilitado no intercepta tráfico."""
        os.environ["ZARAGOZA_COMMAND_ENABLED"] = "false"
        sent = []

        self.assertFalse(
            commands.is_allowed_origin(self._ctx())
        )
        self.assertFalse(
            commands.handle_zaragoza_command(
                self._ctx(),
                sent.append,
            )
        )
        self.assertEqual(sent, [])

    def test_direct_agenda_uses_api_messages(self):
        """Un DM AGENDA entrega exactamente los mensajes de la API."""
        client = _MessagesClient(
            [
                "AGENDA [1/2]\nEvento uno",
                "AGENDA [2/2]\nEvento dos",
            ]
        )
        commands._CLIENT = client
        sent = []

        self.assertTrue(
            commands.handle_zaragoza_command(
                self._ctx("  AGENDA   MAÑANA  "),
                sent.append,
            )
        )

        self.assertEqual(
            client.seen_text,
            "AGENDA MAÑANA",
        )
        self.assertEqual(
            sent,
            client.messages,
        )

    def test_direct_news_uses_api_messages(self):
        """Un DM NOTICIAS conserva las partes LoRa generadas por la API."""
        client = _MessagesClient(
            [
                "NOTICIAS [1/2]\nMovilidad uno",
                "NOTICIAS [2/2]\nMovilidad dos",
            ]
        )
        commands._CLIENT = client
        sent = []

        self.assertTrue(
            commands.handle_zaragoza_command(
                self._ctx("noticias movilidad"),
                sent.append,
            )
        )
        self.assertEqual(sent, client.messages)

    def test_public_origin_requires_configured_channel(self):
        """El tráfico de canal permanece deshabilitado por defecto."""
        meshcore = self._ctx(
            direct=False,
            channel=5,
            network="meshcore",
        )
        self.assertFalse(
            commands.is_allowed_origin(meshcore)
        )

        os.environ["ZARAGOZA_MESHCORE_CHANNEL"] = "5"
        self.assertTrue(
            commands.is_allowed_origin(meshcore)
        )

        meshtastic = self._ctx(
            direct=False,
            channel=5,
            network="meshtastic",
        )
        self.assertFalse(
            commands.is_allowed_origin(meshtastic)
        )

        os.environ[
            "ZARAGOZA_MESHTASTIC_CHANNEL"
        ] = "5"
        self.assertTrue(
            commands.is_allowed_origin(meshtastic)
        )

    def test_duplicate_is_consumed_without_second_response(self):
        """Un paquete duplicado no consulta ni responde una segunda vez."""
        commands._LIMITER = _DuplicateLimiter()
        commands._CLIENT = _MessagesClient(
            ["no debe enviarse"]
        )
        sent = []

        self.assertTrue(
            commands.handle_zaragoza_command(
                self._ctx(),
                sent.append,
            )
        )
        self.assertEqual(sent, [])

    def test_rate_limit_sends_single_notice(self):
        """Al alcanzar la cuota se devuelve un único aviso breve."""
        commands._LIMITER = _RateLimitedLimiter()
        commands._CLIENT = _MessagesClient(
            ["no debe consultarse"]
        )
        sent = []

        self.assertTrue(
            commands.handle_zaragoza_command(
                self._ctx(),
                sent.append,
            )
        )

        self.assertEqual(len(sent), 1)
        self.assertIn(
            "Límite de consultas Zaragoza",
            sent[0],
        )

    def test_empty_agenda_result_has_specific_message(self):
        """Una agenda sin resultados se distingue de un fallo del servicio."""
        commands._CLIENT = _MessagesClient([])
        sent = []

        self.assertTrue(
            commands.handle_zaragoza_command(
                self._ctx("agenda infantil"),
                sent.append,
            )
        )
        self.assertEqual(
            sent,
            ["AGENDA: sin resultados para esa consulta."],
        )

    def test_empty_news_result_has_specific_message(self):
        """Una búsqueda de noticias sin resultados devuelve respuesta útil."""
        commands._CLIENT = _MessagesClient([])
        sent = []

        self.assertTrue(
            commands.handle_zaragoza_command(
                self._ctx("noticias inexistente"),
                sent.append,
            )
        )
        self.assertEqual(
            sent,
            ["NOTICIAS: sin resultados para esa consulta."],
        )

    def test_service_failure_returns_temporary_error(self):
        """Una caída de la API se consume y devuelve aviso sin propagar error."""
        class _FailingClient:
            def query(self, _ctx):
                raise RuntimeError("timeout simulado")

        commands._CLIENT = _FailingClient()
        sent = []

        self.assertTrue(
            commands.handle_zaragoza_command(
                self._ctx(),
                sent.append,
            )
        )
        self.assertEqual(
            sent,
            [
                "Servicio de agenda/noticias "
                "no disponible temporalmente."
            ],
        )

    def test_handler_limits_logical_messages(self):
        """El manejador conserva un límite defensivo independiente de la API."""
        commands._CLIENT = _MessagesClient(
            [
                "parte-1",
                "parte-2",
                "parte-3",
                "parte-4",
                "parte-5",
            ]
        )
        sent = []

        self.assertTrue(
            commands.handle_zaragoza_command(
                self._ctx(),
                sent.append,
            )
        )

        self.assertEqual(
            sent,
            [
                "parte-1",
                "parte-2",
                "parte-3",
                "Respuesta truncada a 4 mensajes.",
            ],
        )


if __name__ == "__main__":
    unittest.main()

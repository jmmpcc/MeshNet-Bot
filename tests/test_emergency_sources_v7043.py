from tools.emergencias_guardia.emergencias.sources.aemet_cap import AemetCapSource
from tools.emergencias_guardia.emergencias.sources.che_rss import CheRssSource


BASE_CONFIG = {
    "fetch": {
        "timeout_seconds": 1,
        "max_response_bytes": 100000,
        "user_agent": "test",
    }
}


def test_aemet_cap_normalizes_weather_warning():
    body = b'''<?xml version="1.0" encoding="UTF-8"?>
    <alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
      <identifier>AEMET-TEST-1</identifier><sender>AEMET</sender>
      <sent>2026-08-10T08:00:00+00:00</sent><status>Actual</status><msgType>Alert</msgType><scope>Public</scope>
      <info><language>es-ES</language><category>Met</category><event>Tormentas</event>
      <urgency>Expected</urgency><severity>Severe</severity><certainty>Likely</certainty>
      <headline>Aviso naranja por tormentas</headline><description>Tormentas fuertes.</description>
      <onset>2026-08-10T10:00:00+00:00</onset><expires>2026-08-10T18:00:00+00:00</expires>
      <area><areaDesc>Ribera del Ebro de Zaragoza</areaDesc></area></info>
    </alert>'''
    source = AemetCapSource("aemet_cap", {"verification": "official"}, BASE_CONFIG)
    events = source.parse(body)
    assert len(events) == 1
    event = events[0]
    assert event.source_event_id == "AEMET-TEST-1"
    assert event.category == "storm"
    assert event.severity == "high"
    assert event.status == "active"
    assert event.metadata["cap_area"] == "Ribera del Ebro de Zaragoza"


def test_aemet_cap_cancel_becomes_resolved():
    body = b'''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
      <identifier>AEMET-TEST-2</identifier><sent>2026-08-10T09:00:00+00:00</sent>
      <status>Actual</status><msgType>Cancel</msgType><scope>Public</scope>
      <info><language>es-ES</language><event>Viento</event><severity>Moderate</severity>
      <headline>Fin de aviso por viento</headline></info></alert>'''
    source = AemetCapSource("aemet_cap", {"verification": "official"}, BASE_CONFIG)
    event = source.parse(body)[0]
    assert event.status == "resolved"
    assert event.category == "strong_wind"


def test_che_rss_filters_non_hydrological_items():
    body = b'''<rss><channel>
      <item><guid>1</guid><title>Informe semanal de embalses</title><description>Reservas semanales.</description></item>
      <item><guid>2</guid><title>Posibilidad de crecidas s\xc3\xbabitas en barrancos de Zaragoza</title>
      <description>Comunicaci\xc3\xb3n CHE - SAIH Ebro ante lluvias intensas.</description></item>
    </channel></rss>'''
    source = CheRssSource("che_saih", {"verification": "official"}, BASE_CONFIG)
    events = source.parse(body)
    assert len(events) == 1
    assert events[0].source_event_id == "2"
    assert events[0].category == "flood"
    assert events[0].province == "Zaragoza"
    assert events[0].metadata["hydrological_warning"] is True

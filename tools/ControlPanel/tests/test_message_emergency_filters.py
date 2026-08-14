from tools.ControlPanel.message_emergency_filters import enrich_and_filter_operations


def _operation(event_id: str, severity: str, category: str, metadata=None):
    return {
        "operation_id": f"op-{event_id}",
        "event_id": event_id,
        "severity": severity,
        "category": category,
        "result": "ok",
        "deliveries": [{"metadata": metadata or {}}],
    }


def test_enrich_uses_event_id_province_without_parsing_message():
    operations = [_operation("evt-1", "high", "road_closed")]
    result = enrich_and_filter_operations(operations, {"evt-1": "Zaragoza"})
    assert result[0]["province"] == "Zaragoza"


def test_delivery_metadata_province_has_priority_for_future_records():
    operations = [_operation("evt-1", "high", "road_closed", {"province": "Huesca"})]
    result = enrich_and_filter_operations(operations, {"evt-1": "Zaragoza"})
    assert result[0]["province"] == "Huesca"


def test_filters_combine_province_severity_and_category():
    operations = [
        _operation("evt-1", "high", "road_closed"),
        _operation("evt-2", "medium", "road_closed"),
        _operation("evt-3", "high", "wildfire"),
    ]
    provinces = {"evt-1": "Zaragoza", "evt-2": "Zaragoza", "evt-3": "Huesca"}
    result = enrich_and_filter_operations(
        operations,
        provinces,
        province="Zaragoza",
        severity="high",
        category="road_closed",
    )
    assert [item["event_id"] for item in result] == ["evt-1"]


def test_missing_structured_province_does_not_guess_from_text():
    operation = _operation("unknown", "high", "road_closed")
    operation["message"] = "Incidencia en Zaragoza"
    result = enrich_and_filter_operations([operation], {}, province="Zaragoza")
    assert result == []

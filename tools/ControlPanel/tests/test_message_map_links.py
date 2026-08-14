from tools.ControlPanel.message_map_links import _extension_script


def test_message_map_links_wraps_existing_detail_renderer():
    script = _extension_script()
    assert "originalAuditDetailHtml = auditDetailHtml" in script
    assert "auditDetailHtml = function(operation, index)" in script


def test_message_map_links_only_detects_google_maps_coordinate_urls():
    script = _extension_script()
    assert "maps\\.google\\.com" in script
    assert "google\\.com\\/maps" in script
    assert "latitude < -90" in script
    assert "longitude < -180" in script


def test_message_map_links_open_google_maps_in_new_tab():
    script = _extension_script()
    assert "https://www.google.com/maps/search/?api=1&query=" in script
    assert 'target=\"_blank\"' in script
    assert 'rel=\"noopener noreferrer\"' in script


def test_message_map_links_does_not_register_new_api_endpoint():
    script = _extension_script()
    assert "/api/" not in script

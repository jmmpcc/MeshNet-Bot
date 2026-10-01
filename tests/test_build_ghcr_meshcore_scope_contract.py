from __future__ import annotations

from pathlib import Path


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-ghcr.yml"


def _workflow_text() -> str:
    """Return the GHCR workflow text used to decide Docker rebuild targets.

    The test deliberately validates the workflow as plain text instead of parsing
    YAML. GitHub Actions expressions such as ``${{ ... }}`` are not relevant to
    this contract; what matters here is that the scope modules remain explicitly
    mapped to the Docker images that import/use them.
    """
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_meshcore_channel_scope_rebuilds_broker_and_bot() -> None:
    """The shared MeshCore TX-scope runtime must rebuild broker and bot images.

    ``source/meshcore_channel_scope.py`` is used by the broker runtime to carry
    and apply the flood scope, while the Telegram side also imports helpers from
    the same module. Rebuilding only one image can leave the installation in a
    split-version state where Telegram reports a scope but the broker transmits
    without it.
    """
    text = _workflow_text()

    marker = "if changed_match '^source/meshcore_channel_scope\\.py$'; then"
    start = text.index(marker)
    end = text.index("fi", start)
    block = text[start:end]

    assert "rebuild_broker=true" in block
    assert "rebuild_bot=true" in block


def test_meshcore_scope_telegram_modules_rebuild_bot() -> None:
    """Telegram-only scope parser/wrapper changes must publish a new bot image."""
    text = _workflow_text()

    marker = "if changed_match '^source/(meshcore_scope_bot|meshcore_scope_token)\\.py$'; then"
    start = text.index(marker)
    end = text.index("fi", start)
    block = text[start:end]

    assert "rebuild_bot=true" in block
    assert "rebuild_broker=true" not in block


def test_workflow_change_still_forces_all_images() -> None:
    """Keep the existing safety net that rebuilds every image after CI edits."""
    text = _workflow_text()

    marker = "if changed_match '^\\.github/workflows/'; then"
    start = text.index(marker)
    end = text.index("fi", start)
    block = text[start:end]

    for target in (
        "rebuild_broker=true",
        "rebuild_bot=true",
        "rebuild_aprs=true",
        "rebuild_bridge=true",
        "rebuild_bridge_bc=true",
    ):
        assert target in block

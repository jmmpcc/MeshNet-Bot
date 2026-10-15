from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.rpi.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "build-ghcr.yml"
ENV_EXAMPLE = ROOT / ".env_example"
FARMACIAS_COMMANDS = ROOT / "source" / "farmacias_commands.py"
UPDATER = ROOT / "scripts" / "update_meshnet.sh"


def test_optional_apps_have_independent_dockerfiles() -> None:
    farmacias = (ROOT / "Dockerfile.farmacias").read_text(encoding="utf-8")
    emergencias = (ROOT / "Dockerfile.emergencias").read_text(encoding="utf-8")

    assert "tools/farmacias_guardia/" in farmacias
    assert "tools/emergencias_guardia/" in emergencias
    assert "COPY shared/" in farmacias
    assert "COPY shared/" in emergencias


def test_optional_apps_are_compose_profiles() -> None:
    text = COMPOSE.read_text(encoding="utf-8")

    assert 'profiles: ["farmacias"]' in text
    assert 'profiles: ["emergencias"]' in text
    assert "ghcr.io/jmmpcc/meshnet-bot-farmacias:latest" in text
    assert "ghcr.io/jmmpcc/meshnet-bot-emergencias:latest" in text
    assert "meshnet-farmacias-check" in text
    assert "meshnet-farmacias-daily" in text
    assert "meshnet-emergencias-check" in text


def test_optional_apps_persist_under_bot_data() -> None:
    text = COMPOSE.read_text(encoding="utf-8")

    assert "FARMACIAS_DATA_DIR: /app/bot_data/farmacias_guardia" in text
    assert "EMERGENCIAS_DATA_DIR: /app/bot_data/emergencias_guardia" in text
    assert "./bot_data:/app/bot_data:rw" in text


def test_farmacias_legacy_listener_is_disabled_inside_container() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert 'FARMACIAS_COMMAND_LISTENER_ENABLED: "0"' in text


def test_new_installations_keep_optional_commands_disabled() -> None:
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    command_text = FARMACIAS_COMMANDS.read_text(encoding="utf-8")

    assert "FARMACIAS_COMMAND_ENABLED=0" in env_text
    assert "EMERGENCIAS_COMMAND_ENABLED=0" in env_text
    assert '_env_bool("FARMACIAS_COMMAND_ENABLED", "0")' in command_text


def test_workflow_publishes_optional_images_independently() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert 'rebuild_farmacias=false' in text
    assert 'rebuild_emergencias=false' in text
    assert 'targets+=("farmacias")' in text
    assert 'targets+=("emergencias")' in text
    assert 'dockerfile=Dockerfile.farmacias' in text
    assert 'dockerfile=Dockerfile.emergencias' in text
    assert "farmacias_commands\\.py" in text
    assert "emergencias_commands\\.py" in text


def test_updater_avoids_parallel_legacy_restart_for_docker_profiles() -> None:
    text = UPDATER.read_text(encoding="utf-8")

    assert "farmacias_docker_profile" in text
    assert "emergencias_docker_profile" in text
    assert "restart_farmacias=false" in text
    assert "restart_emergencias=false" in text
    assert "AVISO: perfil Docker" in text

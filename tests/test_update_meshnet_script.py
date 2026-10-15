from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update_meshnet.sh"


def test_update_meshnet_shell_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_update_meshnet_covers_all_versioned_host_services() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    expected_units = {
        "meshnet-farmacias-api.service",
        "meshnet-farmacias-daily.timer",
        "meshnet-farmacias-check.timer",
        "meshnet-emergencias-api.service",
        "meshnet-emergencias-check.timer",
        "meshnet-emergencias-public-map.timer",
        "meshnet-emergencias-public-map.path",
        "meshnet-control-panel.service",
        "meshnet-mobile-api.service",
        "meshnet-voice-rf.service",
    }
    for unit in expected_units:
        assert unit in text


def test_update_meshnet_preserves_inactive_services() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'systemctl is-active --quiet "${unit}"' in text
    assert 'instalado pero inactivo; se respeta su estado' in text
    assert "systemctl enable" not in text


def test_update_meshnet_uses_safe_git_and_docker_flow() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "git pull --ff-only" in text
    assert "git reset" not in text
    assert "git checkout --" not in text
    assert 'docker compose -f "${COMPOSE_FILE}" pull "${selected_docker_services[@]}"' in text
    assert 'docker compose -f "${COMPOSE_FILE}" up -d "${selected_docker_services[@]}"' in text
    assert 'docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans' not in text
    assert 'MESHNET_DOCKER_SERVICES' in text
    assert 'ps --services --status running' in text
    assert 'config --services' in text


def test_update_meshnet_maps_host_directories_and_shared_code() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    for path in (
        "^tools/farmacias_guardia/",
        "^tools/emergencias_guardia/",
        "^tools/ControlPanel/",
        "^tools/MobileAPI/",
        "^tools/voice_rf_gateway/",
        "^shared/",
    ):
        assert path in text


def test_update_meshnet_rejects_global_sudo_execution() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'EUID} -eq 0 && -n "${SUDO_USER:-}"' in text
    assert "no ejecute el actualizador completo con sudo" in text
    assert "run sudo" in text


def test_update_meshnet_never_starts_all_compose_services_implicitly() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'run docker compose -f "${COMPOSE_FILE}" up -d' in text
    assert 'up -d --remove-orphans' not in text
    assert 'up -d "${selected_docker_services[@]}"' in text
    assert 'no hay servicios Docker activos ni MESHNET_DOCKER_SERVICES definido; Docker no arrancará ningún servicio' in text


def test_compose_profiles_do_not_imply_autostart() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'COMPOSE_PROFILES únicamente habilita la disponibilidad' in text
    assert 'por sí solo NO provoca que se arranquen' in text

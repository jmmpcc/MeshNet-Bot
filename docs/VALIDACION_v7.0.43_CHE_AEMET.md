# Validación v7.0.43 — AEMET/CHE y estado de fuentes

## 1. Pruebas unitarias

```bash
cd /home/meshnet/MeshNet-Bot
python3 -m pytest tests/test_emergency_sources_v7043.py -v
```

Debe finalizar sin `FAILED` ni `ERROR`.

## 2. AEMET histórico activo

```bash
AEMET_ALERTS_ENABLED=1 \
PYTHONPATH=/home/meshnet/MeshNet-Bot \
python3 - <<'PY'
from tools.emergencias_guardia.emergencias.config import load_config
from tools.emergencias_guardia.emergencias.engine import fetch_sources
import json
cfg = load_config()
cfg["sources"]["aemet_cap"]["enabled"] = True
print(json.dumps(fetch_sources(cfg, only="aemet_cap"), indent=2, ensure_ascii=False))
PY
```

Debe mostrar `skipped: external_owner`.

## 3. AEMET fallback sin API key

```bash
AEMET_ALERTS_ENABLED=0 \
PYTHONPATH=/home/meshnet/MeshNet-Bot \
python3 - <<'PY'
from tools.emergencias_guardia.emergencias.config import load_config
from tools.emergencias_guardia.emergencias.engine import fetch_sources
import json
cfg = load_config()
cfg["sources"]["aemet_cap"]["enabled"] = True
print(json.dumps(fetch_sources(cfg, only="aemet_cap"), indent=2, ensure_ascii=False))
PY
```

Si `AEMET_API_KEY` no está configurada debe aparecer únicamente el error actual. No deben permanecer `skipped: external_owner` ni su `reason`.

## 4. CHE/SAIH

```bash
PYTHONPATH=/home/meshnet/MeshNet-Bot \
python3 - <<'PY'
from tools.emergencias_guardia.emergencias.config import load_config
from tools.emergencias_guardia.emergencias.engine import fetch_sources
import json
cfg = load_config()
cfg["sources"]["che_saih"]["enabled"] = True
print(json.dumps(fetch_sources(cfg, only="che_saih"), indent=2, ensure_ascii=False))
PY
```

Debe mostrar `skipped: not_operational` y explicar que CHE/SAIH queda pendiente de un endpoint público estructurado. No debe intentar descargar ni parsear la página HTML de comunicaciones CHE.

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "tools/ControlPanel/web_admin.py"
text = path.read_text(encoding="utf-8")

old = '''from fastapi import FastAPI, HTTPException, Request as FastAPIRequest\nfrom fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse\nfrom pydantic import BaseModel, Field\nfrom shared.delivery_audit import export_operations_csv, query_operations\n\nBASE_DIR = Path(__file__).resolve().parent\nREPO_DIR = BASE_DIR.parent.parent\nif str(REPO_DIR) not in sys.path:\n    sys.path.insert(0, str(REPO_DIR))\n'''
new = '''from fastapi import FastAPI, HTTPException, Request as FastAPIRequest\nfrom fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse\nfrom pydantic import BaseModel, Field\n\n# La raíz del repositorio debe estar disponible antes de importar helpers\n# compartidos. Cuando systemd ejecuta este fichero directamente, Python añade\n# tools/ControlPanel a sys.path, pero no necesariamente la raíz MeshNet-Bot.\nBASE_DIR = Path(__file__).resolve().parent\nREPO_DIR = BASE_DIR.parent.parent\nif str(REPO_DIR) not in sys.path:\n    sys.path.insert(0, str(REPO_DIR))\n\nfrom shared.delivery_audit import export_operations_csv, query_operations\n'''

if old not in text:
    raise SystemExit("Bloque de importación esperado no encontrado; no se modifica el fichero")

text = text.replace(old, new, 1)
text = text.replace("UI 2 · v7.0.48", "UI 2 · v7.0.48.1", 1)
path.write_text(text, encoding="utf-8")
print("v7.0.48.1 ControlPanel import-order hotfix applied")

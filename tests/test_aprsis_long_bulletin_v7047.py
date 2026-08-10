import asyncio, importlib.util, os, sys
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]; P=ROOT/"source"/"meshtastic_to_aprs.py"
def load():
 os.environ.setdefault("APRSIS_USER","EB2EAS-11"); sys.modules.setdefault("aprslib",mock.MagicMock()); s=importlib.util.spec_from_file_location("gw7047",P); m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); return m
def test_disabled():
 m=load(); m.APRSIS_LONG_BULLETIN_TEST_ENABLED=0; x=asyncio.run(m.send_aprsis_long_bulletin_test("PRUEBA "*20,0)); assert not x["sent"] and x["reason"]=="disabled"
def test_real_long_bln(monkeypatch):
 m=load(); m.APRSIS_LONG_BULLETIN_TEST_ENABLED=1; m.APRSIS_LONG_TEST_MAX_CHARS=400; m.APRSIS_EMERGENCY_BULLETIN_GROUP=""; monkeypatch.setattr(m,"_aprsis_ready",lambda:True); cap=[]
 async def send(line): cap.append(line); return True
 monkeypatch.setattr(m,"_aprsis_send_line_safe",send); text="PRUEBA BLN LARGO CORTE DE TRAFICO CV-128 km 21.5 CATI URL https://maps.google.com/?q=40.47123,0.02234 FIN-PRUEBA-BLN-LARGO"; assert len(text)>67; x=asyncio.run(m.send_aprsis_long_bulletin_test(text,0)); assert x["sent"] and x["bulletin"]=="BLN0" and x["chars"]==len(text); assert "::BLN0     :" in x["line"] and x["line"].endswith("FIN-PRUEBA-BLN-LARGO") and text in x["line"]
def test_group(monkeypatch):
 m=load(); m.APRSIS_LONG_BULLETIN_TEST_ENABLED=1; m.APRSIS_EMERGENCY_BULLETIN_GROUP="EMERG"; monkeypatch.setattr(m,"_aprsis_ready",lambda:True)
 async def send(line): return True
 monkeypatch.setattr(m,"_aprsis_send_line_safe",send); x=asyncio.run(m.send_aprsis_long_bulletin_test("PRUEBA",3)); assert x["bulletin"]=="BLN3EMERG" and "::BLN3EMERG:" in x["line"]

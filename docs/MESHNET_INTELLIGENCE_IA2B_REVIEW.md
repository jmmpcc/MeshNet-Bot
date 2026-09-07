# IA-2B — Checklist de revisión

- [ ] `main` base confirmado en `4218b8979e9b2801551d7649f863638d5467cc95`.
- [ ] Solo se añaden ficheros IA-2B; no se modifica código operativo de Emergencias.
- [ ] `MESHNET_AI_ENABLED=0` mantiene cero llamadas externas.
- [ ] `MESHNET_AI_EMERGENCIES_ENABLED=0` mantiene cero llamadas externas.
- [ ] `MESHNET_AI_CORRELATION_ENABLED=0` mantiene cero llamadas externas.
- [ ] Pares no candidatos no consumen proveedor.
- [ ] Fuentes iguales nunca son candidato IA-2B.
- [ ] Distancia/tiempo fuera de límites impiden correlación IA.
- [ ] Payload al proveedor no contiene `metadata` completa.
- [ ] `relation` solo admite `same_incident`, `contextual`, `unrelated`, `uncertain`.
- [ ] `explanation` debe ser texto no vacío.
- [ ] `confidence` debe ser numérica y finita.
- [ ] Los dos eventos permanecen inmutables.
- [ ] IA-2B no modifica `verification` a `confirmed_multi_source`.
- [ ] IA-2B no escribe en storage ni genera mensajes/transmisiones.
- [ ] Suite IA-0 + IA-1 + IA-2A + IA-2B completa en verde.
- [ ] Revisión de Codex sin P1/P2 pendientes antes de Ready/Merge.

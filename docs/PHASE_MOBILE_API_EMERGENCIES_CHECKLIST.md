# Checklist — Mobile API Emergencias

1. Ejecutar tests de `tests/test_mobile_api_emergencies.py`.
2. Desplegar la rama fusionada en la Raspberry.
3. Reiniciar el servicio Mobile API que ejecuta `tools.MobileAPI.mobile_api_v7054:app`.
4. Comprobar `GET /api/v1/capabilities` con Bearer y verificar:
   - `emergencies_read: true`
   - `emergencies_coordinates: true`
5. Comprobar `GET /api/v1/emergencies?limit=5` con Bearer.
6. Confirmar que las coordenadas coinciden con los eventos de `emergencias_guardia`.

No es necesario reiniciar, detener o modificar el servicio de Emergencias para esta validación.

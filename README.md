# MLB Daily Screen

Pizarrón automático de MLB que cada mañana extrae:

- Calendario y abridores confirmados desde la API oficial de MLB.
- Métricas Tier A (SIERA, xFIP, FIP, K-BB%, CSW%, WHIP, BABIP, LOB%) directamente de FanGraphs vía `pybaseball`.
- Líneas (moneyline + totales) desde The Odds API.
- Clima por estadio desde Open-Meteo.

Aplica filtros de eliminación (muestra <4 aperturas, lluvia >30%, favorito ≤ -200) y flags de regresión (gap ERA vs xFIP > 0.80 en cualquier dirección), y publica un reporte en Markdown en `reports/YYYY-MM-DD.md` cada día a las 10:00 AM ET.

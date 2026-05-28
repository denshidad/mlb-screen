# MLB Daily Screen

Pizarrón automático de MLB que cada mañana extrae:

- Calendario y abridores confirmados desde la API oficial de MLB.
- Métricas Tier A (SIERA, xFIP, FIP, K-BB%, CSW%, WHIP, BABIP, LOB%) directamente de FanGraphs vía `pybaseball`.
- Líneas (moneyline + totales) desde The Odds API.
- Clima por estadio desde Open-Meteo.

Aplica filtros de eliminación (muestra <4 aperturas, lluvia >30%, favorito ≤ -200) y flags de regresión (gap ERA vs xFIP > 0.80 en cualquier dirección), y publica un reporte en Markdown en `reports/YYYY-MM-DD.md` cada día a las 10:00 AM ET.

## Setup desde iPhone

1. Crea una cuenta gratuita en [the-odds-api.com](https://the-odds-api.com/) y copia tu API key.
1. Crea este repo en GitHub (público para usar Actions gratis).
1. Sube los 4 archivos manteniendo la estructura:
- `requirements.txt`
- `screen.py`
- `.github/workflows/daily.yml`
- `README.md`
1. En Settings → Secrets and variables → Actions → New repository secret, agrega:
- Name: `ODDS_API_KEY`
- Value: tu key
1. Dispara la primera corrida manualmente: Actions tab → Daily MLB Screen → Run workflow.
1. Espera 1-2 minutos. El reporte aparecerá en `reports/YYYY-MM-DD.md`.

## Limitaciones honestas

- No incluye señales de sharp money / line movement (requieren servicios pagos).
- No calcula edge final automáticamente: necesitas pares de probabilidad modelo (numberFire) para Fase 4.
- The Odds API free: 500 requests/mes (suficiente para 1 corrida diaria).
- `pybaseball` depende de que FanGraphs no cambie su layout; si se rompe, hay que actualizar la librería.

## Lectura del reporte

El reporte se organiza en tres secciones:

- **🚫 Eliminados**: juegos descartados por filtros duros.
- **⚡ Candidatos con flags de regresión**: juegos donde un abridor sobreperforma o subperforma significativamente; el lado contrario (o el mismo, según dirección) puede tener valor.
- **📋 Resto del pizarrón**: sin flags pero con métricas para consulta.

Cruza los flags con la probabilidad de numberFire y tu plantilla de CLV antes de jugar.

## Recordatorios

- Apuestas implican varianza; ningún sistema es infalible.
- Límite duro: ≤3% del bankroll por parlay.
- Registra cada apuesta en tu plantilla de CLV para medir si le ganas a la línea de cierre a largo plazo.

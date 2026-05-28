"""
MLB Daily Screen (v2) — no FanGraphs dependency.

Data sources (all accessible from cloud / GitHub Actions IPs):
  - statsapi.mlb.com         : schedule, probable pitchers, per-pitcher season stats
  - baseballsavant.mlb.com   : expected stats (xwOBA, wOBA, xERA) for the regression engine
  - The Odds API             : moneyline + totals (needs ODDS_API_KEY secret)
  - Open-Meteo               : weather by stadium

Writes reports/YYYY-MM-DD.md
"""
from __future__ import annotations
import os
import io
import sys
import csv
import math
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
NOW = datetime.now(timezone.utc).astimezone()
TODAY = NOW.strftime("%Y-%m-%d")
YEAR = NOW.year
REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (compatible; mlb-screen/2.0)"}

VENUE_COORDS: dict[str, tuple[float, float]] = {
    "Yankee Stadium": (40.8296, -73.9262), "Fenway Park": (42.3467, -71.0972),
    "Tropicana Field": (27.7682, -82.6534), "Rogers Centre": (43.6414, -79.3894),
    "Oriole Park at Camden Yards": (39.2839, -76.6217), "Progressive Field": (41.4962, -81.6852),
    "Comerica Park": (42.3390, -83.0485), "Kauffman Stadium": (39.0517, -94.4803),
    "Guaranteed Rate Field": (41.8300, -87.6338), "Rate Field": (41.8300, -87.6338),
    "Target Field": (44.9817, -93.2776), "Minute Maid Park": (29.7572, -95.3552),
    "Daikin Park": (29.7572, -95.3552), "Globe Life Field": (32.7473, -97.0817),
    "T-Mobile Park": (47.5914, -122.3325), "Angel Stadium": (33.8003, -117.8827),
    "Oakland Coliseum": (37.7516, -122.2005), "Sutter Health Park": (38.5803, -121.5133),
    "Citi Field": (40.7571, -73.8458), "Citizens Bank Park": (39.9061, -75.1665),
    "Truist Park": (33.8908, -84.4678), "loanDepot park": (25.7780, -80.2197),
    "Nationals Park": (38.8730, -77.0074), "PNC Park": (40.4469, -80.0057),
    "Great American Ball Park": (39.0975, -84.5066), "Wrigley Field": (41.9484, -87.6553),
    "American Family Field": (43.0280, -87.9712), "Busch Stadium": (38.6226, -90.1928),
    "Coors Field": (39.7559, -104.9942), "Chase Field": (33.4453, -112.0667),
    "Dodger Stadium": (34.0739, -118.2400), "Petco Park": (32.7073, -117.1566),
    "Oracle Park": (37.7786, -122.3893),
}


def safe_float(v, default=float("nan")):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def american_to_implied(odds):
    o = safe_float(odds)
    if math.isnan(o):
        return None
    return (-o / (-o + 100)) if o < 0 else (100 / (o + 100))


# ---------------------------------------------------------------------------
# MLB Stats API
# ---------------------------------------------------------------------------
def fetch_schedule() -> list[dict]:
    url = "https://statsapi.mlb.com/api/v1/schedule"
    params = {"sportId": 1, "date": TODAY, "hydrate": "probablePitcher,venue,team"}
    r = requests.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    games = []
    for d in r.json().get("dates", []):
        for g in d.get("games", []):
            away, home = g["teams"]["away"], g["teams"]["home"]
            ap = away.get("probablePitcher") or {}
            hp = home.get("probablePitcher") or {}
            games.append({
                "gameTime": g.get("gameDate", ""),
                "away_team": away["team"]["name"],
                "home_team": home["team"]["name"],
                "away_pitcher": ap.get("fullName"),
                "away_pid": ap.get("id"),
                "home_pitcher": hp.get("fullName"),
                "home_pid": hp.get("id"),
                "venue": g["venue"]["name"],
            })
    return games


def fetch_pitcher_season(pid) -> dict | None:
    """Traditional season stats for one pitcher, by MLBAM id."""
    if not pid:
        return None
    url = f"https://statsapi.mlb.com/api/v1/people/{pid}"
    params = {"hydrate": f"stats(group=[pitching],type=[season],season={YEAR})"}
    try:
        r = requests.get(url, params=params, headers=UA, timeout=20)
        r.raise_for_status()
        person = r.json()["people"][0]
        splits = person.get("stats", [{}])[0].get("splits", [])
        if not splits:
            return None
        s = splits[0]["stat"]
        bf = safe_float(s.get("battersFaced"))
        k = safe_float(s.get("strikeOuts"))
        bb = safe_float(s.get("baseOnBalls"))
        kpct = (k / bf * 100) if bf else float("nan")
        bbpct = (bb / bf * 100) if bf else float("nan")
        return {
            "GS": int(safe_float(s.get("gamesStarted"), 0)),
            "IP": s.get("inningsPitched", "?"),
            "ERA": safe_float(s.get("era")),
            "WHIP": safe_float(s.get("whip")),
            "K_pct": kpct,
            "BB_pct": bbpct,
            "KBB_pct": (kpct - bbpct) if not math.isnan(kpct) else float("nan"),
        }
    except Exception as e:
        print(f"[stats] pid {pid} failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Baseball Savant — expected stats (the regression engine)
# ---------------------------------------------------------------------------
def fetch_savant_expected() -> dict[str, dict]:
    url = "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
    params = {"type": "pitcher", "year": YEAR, "min": "1", "csv": "true"}
    out: dict[str, dict] = {}
    try:
        r = requests.get(url, params=params, headers=UA, timeout=30)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        for row in reader:
            pid = (row.get("player_id") or "").strip()
            if not pid:
                continue
            out[pid] = {
                "woba": safe_float(row.get("woba")),
                "xwoba": safe_float(row.get("est_woba")),
                "era": safe_float(row.get("era")),
                "xera": safe_float(row.get("xera")),
                "ba": safe_float(row.get("ba")),
                "xba": safe_float(row.get("est_ba")),
            }
    except Exception as e:
        print(f"[savant] failed: {e}", file=sys.stderr)
    return out


def build_pitcher(pid, name, savant) -> dict | None:
    base = fetch_pitcher_season(pid)
    if base is None and not pid:
        return None
    base = base or {}
    sv = savant.get(str(pid), {}) if pid else {}
    base["Name"] = name or "?"
    base["xwoba"] = sv.get("xwoba", float("nan"))
    base["woba"] = sv.get("woba", float("nan"))
    base["xera"] = sv.get("xera", float("nan"))
    return base


# ---------------------------------------------------------------------------
# Odds + weather
# ---------------------------------------------------------------------------
def fetch_odds() -> dict[str, dict]:
    if not ODDS_API_KEY:
        return {}
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
    params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h,totals",
              "oddsFormat": "american", "bookmakers": "fanduel,draftkings"}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[odds] failed: {e}", file=sys.stderr)
        return {}
    result = {}
    for g in r.json():
        ml = {}
        for book in g.get("bookmakers", []):
            for m in book.get("markets", []):
                if m["key"] == "h2h":
                    for o in m["outcomes"]:
                        ml.setdefault(o["name"], []).append(o["price"])
        ml_avg = {n: sum(p) / len(p) for n, p in ml.items() if p}
        result[f"{g.get('away_team')}@{g.get('home_team')}"] = {"ml": ml_avg}
    return result


def fetch_weather(lat, lon) -> dict:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {"latitude": lat, "longitude": lon,
              "current": "temperature_2m,wind_speed_10m,precipitation_probability",
              "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto"}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("current", {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------
def screen_game(game, savant, odds, weather) -> dict:
    away = build_pitcher(game["away_pid"], game["away_pitcher"], savant)
    home = build_pitcher(game["home_pid"], game["home_pitcher"], savant)
    eliminations, flags = [], []

    for p, side in [(away, "visitante"), (home, "local")]:
        if not p:
            continue
        gs = p.get("GS", 0)
        if gs < 4:
            eliminations.append(f"{p['Name']} ({side}) muestra insuficiente: {gs} aperturas")
        elif gs < 7:
            flags.append(f"⚠️ {p['Name']} ({side}) muestra limitada: {gs} aperturas (4-6)")

    precip = safe_float(weather.get("precipitation_probability"), 0)
    wind = safe_float(weather.get("wind_speed_10m"), 0)
    if precip > 30:
        eliminations.append(f"Lluvia {precip:.0f}% (>30%)")
    if wind > 15:
        flags.append(f"⚠️ Viento {wind:.1f} mph (>15) — descartar totales/RL")

    # Regression engine: wOBA vs xwOBA gap (Statcast)
    for p, side in [(away, "visitante"), (home, "local")]:
        if not p:
            continue
        wo, xwo = p.get("woba", float("nan")), p.get("xwoba", float("nan"))
        if math.isnan(wo) or math.isnan(xwo):
            continue
        gap = xwo - wo  # positive => allowing harder contact than results show => lucky
        if gap > 0.030:
            flags.append(
                f"🔻 {p['Name']} ({side}) sobreperforma: wOBA {wo:.3f} vs xwOBA {xwo:.3f} "
                f"(gap {gap:+.3f}) → regresión negativa probable; el lado contrario gana valor"
            )
        elif gap < -0.030:
            flags.append(
                f"🔺 {p['Name']} ({side}) subperforma: wOBA {wo:.3f} vs xwOBA {xwo:.3f} "
                f"(gap {gap:+.3f}) → regresión positiva probable; su lado puede tener valor"
            )

    game_odds = odds.get(f"{game['away_team']}@{game['home_team']}", {})
    for team, price in game_odds.get("ml", {}).items():
        if price <= -200:
            eliminations.append(f"{team} ML {price:+.0f} (favorito >-200, viola regla de armado)")

    return {"game": game, "away": away, "home": home, "weather": weather,
            "odds": game_odds, "eliminations": eliminations, "flags": flags}


def pline(p, label) -> str:
    if not p:
        return f"- **{label}** abridor no encontrado"
    def f(x, n=2):
        return "n/d" if (isinstance(x, float) and math.isnan(x)) else f"{x:.{n}f}"
    return (f"- **{label}** {p.get('Name','?')} ({p.get('GS','?')} GS): "
            f"ERA {f(p.get('ERA'))} · xERA {f(p.get('xera'))} · WHIP {f(p.get('WHIP'))} · "
            f"K-BB% {f(p.get('KBB_pct'),1)} · wOBA {f(p.get('woba'),3)} · xwOBA {f(p.get('xwoba'),3)}")


def build_report(screens) -> str:
    L = [f"# Screen MLB — {TODAY}", "",
         f"**Juegos:** {len(screens)} · "
         f"**Eliminados:** {sum(1 for s in screens if s['eliminations'])} · "
         f"**Con flags:** {sum(1 for s in screens if s['flags'] and not s['eliminations'])}",
         "", "_Motor de regresión: wOBA real vs xwOBA esperado (Statcast). "
         "Sin SIERA/xFIP porque FanGraphs bloquea IPs de nube._", "", "---", ""]

    elim = [s for s in screens if s["eliminations"]]
    if elim:
        L += ["## 🚫 Eliminados", ""]
        for s in elim:
            g = s["game"]
            L.append(f"**{g['away_team']} @ {g['home_team']}** ({g['venue']}):")
            L += [f"  - {e}" for e in s["eliminations"]]
            L.append("")

    flagged = [s for s in screens if s["flags"] and not s["eliminations"]]
    if flagged:
        L += ["## ⚡ Candidatos con flags", ""]
        for s in flagged:
            g = s["game"]
            L += [f"### {g['away_team']} @ {g['home_team']}", f"_{g['venue']}_", "",
                  pline(s["away"], "Visitante:"), pline(s["home"], "Local:"), ""]
            L += [f"- {f}" for f in s["flags"]]
            ml = s["odds"].get("ml", {})
            if ml:
                L.append("- Líneas ML: " + " · ".join(f"{t} {p:+.0f}" for t, p in ml.items()))
            w = s["weather"]
            if w:
                L.append(f"- Clima: {safe_float(w.get('temperature_2m')):.0f}°F, "
                         f"viento {safe_float(w.get('wind_speed_10m')):.1f} mph, "
                         f"lluvia {safe_float(w.get('precipitation_probability')):.0f}%")
            L.append("")

    other = [s for s in screens if not s["flags"] and not s["eliminations"]]
    if other:
        L += ["## 📋 Resto del pizarrón", ""]
        for s in other:
            g = s["game"]
            L += [f"### {g['away_team']} @ {g['home_team']}",
                  pline(s["away"], "Visitante:"), pline(s["home"], "Local:"), ""]

    L += ["---", "", "_La calibración del edge sigue requiriendo numberFire o tu juicio. "
          "Varianza siempre presente; ≤3% del bankroll por parlay; registra el CLV._"]
    return "\n".join(L)


def main():
    print(f"[screen] {TODAY}")
    games = fetch_schedule()
    print(f"[screen] {len(games)} games")
    if not games:
        (REPORTS_DIR / f"{TODAY}.md").write_text(f"# Screen MLB — {TODAY}\n\nSin juegos hoy.\n")
        return
    savant = fetch_savant_expected()
    print(f"[screen] savant rows: {len(savant)}")
    odds = fetch_odds()
    print(f"[screen] odds matchups: {len(odds)}")
    screens = []
    for g in games:
        coords = VENUE_COORDS.get(g["venue"])
        weather = fetch_weather(*coords) if coords else {}
        screens.append(screen_game(g, savant, odds, weather))
    out = REPORTS_DIR / f"{TODAY}.md"
    out.write_text(build_report(screens))
    print(f"[screen] wrote {out}")


if __name__ == "__main__":
    main()

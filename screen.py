"""
MLB Daily Screen — pulls FanGraphs Tier A, MLB schedule, odds and weather,
applies elimination + regression filters, and writes a Markdown report.

Designed to run from GitHub Actions on a daily cron.
"""
from __future__ import annotations
import os
import sys
import math
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
TODAY = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
YEAR = datetime.now(timezone.utc).astimezone().year
REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

# 30 MLB venues with approximate coordinates for Open-Meteo
VENUE_COORDS: dict[str, tuple[float, float]] = {
    "Yankee Stadium": (40.8296, -73.9262),
    "Fenway Park": (42.3467, -71.0972),
    "Tropicana Field": (27.7682, -82.6534),
    "Rogers Centre": (43.6414, -79.3894),
    "Oriole Park at Camden Yards": (39.2839, -76.6217),
    "Progressive Field": (41.4962, -81.6852),
    "Comerica Park": (42.3390, -83.0485),
    "Kauffman Stadium": (39.0517, -94.4803),
    "Guaranteed Rate Field": (41.8300, -87.6338),
    "Rate Field": (41.8300, -87.6338),
    "Target Field": (44.9817, -93.2776),
    "Minute Maid Park": (29.7572, -95.3552),
    "Daikin Park": (29.7572, -95.3552),
    "Globe Life Field": (32.7473, -97.0817),
    "T-Mobile Park": (47.5914, -122.3325),
    "Angel Stadium": (33.8003, -117.8827),
    "Oakland Coliseum": (37.7516, -122.2005),
    "Sutter Health Park": (38.5803, -121.5133),
    "Citi Field": (40.7571, -73.8458),
    "Citizens Bank Park": (39.9061, -75.1665),
    "Truist Park": (33.8908, -84.4678),
    "loanDepot park": (25.7780, -80.2197),
    "Nationals Park": (38.8730, -77.0074),
    "PNC Park": (40.4469, -80.0057),
    "Great American Ball Park": (39.0975, -84.5066),
    "Wrigley Field": (41.9484, -87.6553),
    "American Family Field": (43.0280, -87.9712),
    "Busch Stadium": (38.6226, -90.1928),
    "Coors Field": (39.7559, -104.9942),
    "Chase Field": (33.4453, -112.0667),
    "Dodger Stadium": (34.0739, -118.2400),
    "Petco Park": (32.7073, -117.1566),
    "Oracle Park": (37.7786, -122.3893),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def american_to_implied(odds: float | int | None) -> float | None:
    if odds is None:
        return None
    try:
        odds = float(odds)
    except (TypeError, ValueError):
        return None
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def safe_float(value, default=float("nan")):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
def fetch_schedule() -> list[dict]:
    """Today's schedule + probable pitchers from the official MLB Stats API."""
    url = "https://statsapi.mlb.com/api/v1/schedule"
    params = {
        "sportId": 1,
        "date": TODAY,
        "hydrate": "probablePitcher,venue,team",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    games = []
    for date in r.json().get("dates", []):
        for g in date.get("games", []):
            away = g["teams"]["away"]
            home = g["teams"]["home"]
            games.append({
                "gamePk": g["gamePk"],
                "gameTime": g.get("gameDate", ""),
                "away_team": away["team"]["name"],
                "home_team": home["team"]["name"],
                "away_pitcher": (away.get("probablePitcher") or {}).get("fullName"),
                "home_pitcher": (home.get("probablePitcher") or {}).get("fullName"),
                "venue": g["venue"]["name"],
            })
    return games


def fetch_pitcher_metrics() -> pd.DataFrame:
    """Current-season starting-pitcher Advanced stats from FanGraphs via pybaseball."""
    from pybaseball import pitching_stats
    # qual=0 returns all pitchers (we filter on GS later)
    df = pitching_stats(YEAR, qual=0)
    # Keep useful columns; some may be missing depending on pybaseball/FanGraphs version
    keep = ["Name", "Team", "GS", "IP", "ERA", "FIP", "xFIP", "SIERA",
            "K/9", "BB/9", "K-BB%", "K%", "BB%", "WHIP", "BABIP", "LOB%",
            "HR/9", "CStr%", "SwStr%"]
    for col in keep:
        if col not in df.columns:
            df[col] = float("nan")
    return df[keep].copy()


def fetch_odds() -> dict[str, dict]:
    """Moneylines and totals from The Odds API. Returns {away_team@home_team: {...}}."""
    if not ODDS_API_KEY:
        return {}
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h,totals",
        "oddsFormat": "american",
        "bookmakers": "fanduel,draftkings",
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[odds] failed: {e}", file=sys.stderr)
        return {}
    result = {}
    for g in r.json():
        home = g.get("home_team")
        away = g.get("away_team")
        ml = {}
        total = {}
        for book in g.get("bookmakers", []):
            for m in book.get("markets", []):
                if m["key"] == "h2h":
                    for o in m["outcomes"]:
                        ml.setdefault(o["name"], []).append(o["price"])
                elif m["key"] == "totals":
                    for o in m["outcomes"]:
                        key = ("Over" if o["name"] == "Over" else "Under", o["point"])
                        total.setdefault(key, []).append(o["price"])
        # Average prices across books
        ml_avg = {name: sum(prices) / len(prices) for name, prices in ml.items() if prices}
        total_avg = {k: sum(v) / len(v) for k, v in total.items() if v}
        result[f"{away}@{home}"] = {"ml": ml_avg, "totals": total_avg}
    return result


def fetch_weather(lat: float, lon: float) -> dict:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,wind_speed_10m,wind_direction_10m,precipitation_probability",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "auto",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("current", {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Screen logic
# ---------------------------------------------------------------------------
def lookup_pitcher(df: pd.DataFrame, name: str | None) -> dict | None:
    if not name:
        return None
    norm = name.lower().strip()
    rows = df[df["Name"].str.lower().str.strip() == norm]
    if rows.empty:
        # Try simplified match (last name only as fallback)
        last = norm.split()[-1]
        rows = df[df["Name"].str.lower().str.contains(last, na=False)]
    if rows.empty:
        return None
    r = rows.iloc[0]
    cstr = safe_float(r["CStr%"])
    swstr = safe_float(r["SwStr%"])
    csw = (cstr + swstr) if not (math.isnan(cstr) or math.isnan(swstr)) else float("nan")
    return {
        "Name": r["Name"],
        "Team": r["Team"],
        "GS": int(safe_float(r["GS"], 0)),
        "IP": safe_float(r["IP"]),
        "ERA": safe_float(r["ERA"]),
        "FIP": safe_float(r["FIP"]),
        "xFIP": safe_float(r["xFIP"]),
        "SIERA": safe_float(r["SIERA"]),
        "K-BB%": safe_float(r["K-BB%"]),
        "WHIP": safe_float(r["WHIP"]),
        "BABIP": safe_float(r["BABIP"]),
        "LOB%": safe_float(r["LOB%"]),
        "HR/9": safe_float(r["HR/9"]),
        "CSW%": csw,
    }


def screen_game(game: dict, metrics: pd.DataFrame, odds: dict, weather: dict) -> dict:
    away = lookup_pitcher(metrics, game["away_pitcher"])
    home = lookup_pitcher(metrics, game["home_pitcher"])
    eliminations = []
    flags = []

    # Sample-size flags
    for p, side in [(away, "visitante"), (home, "local")]:
        if not p:
            continue
        if p["GS"] < 4:
            eliminations.append(f"{p['Name']} ({side}) muestra insuficiente: {p['GS']} aperturas")
        elif p["GS"] < 7:
            flags.append(f"⚠️ {p['Name']} ({side}) muestra limitada: {p['GS']} aperturas (4-6)")

    # Weather eliminations
    precip = safe_float(weather.get("precipitation_probability"), 0)
    wind = safe_float(weather.get("wind_speed_10m"), 0)
    if precip > 30:
        eliminations.append(f"Lluvia {precip:.0f}% (>30%)")
    if wind > 15:
        flags.append(f"⚠️ Viento {wind:.1f} mph (>15) — descartar totales/RL")

    # Regression flags (ERA vs xFIP gap)
    for p, side in [(away, "visitante"), (home, "local")]:
        if not p or math.isnan(p["ERA"]) or math.isnan(p["xFIP"]):
            continue
        gap = p["ERA"] - p["xFIP"]
        if gap < -0.80:
            flags.append(
                f"🔻 {p['Name']} ({side}) sobreperforma: ERA {p['ERA']:.2f} vs xFIP {p['xFIP']:.2f} "
                f"(gap {gap:+.2f}) → regresión negativa probable; el lado contrario gana valor"
            )
        elif gap > 0.80:
            flags.append(
                f"🔺 {p['Name']} ({side}) subperforma: ERA {p['ERA']:.2f} vs xFIP {p['xFIP']:.2f} "
                f"(gap {gap:+.2f}) → regresión positiva probable; su lado puede tener valor"
            )

    # Odds + eliminations on extreme favorites
    odds_key = f"{game['away_team']}@{game['home_team']}"
    game_odds = odds.get(odds_key, {})
    ml = game_odds.get("ml", {})
    for team, price in ml.items():
        if price <= -200:
            eliminations.append(f"{team} ML {price:+.0f} (favorito >-200, viola regla de armado)")

    return {
        "game": game,
        "away_metrics": away,
        "home_metrics": home,
        "weather": weather,
        "odds": game_odds,
        "eliminations": eliminations,
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def format_pitcher_line(p: dict | None, label: str) -> str:
    if not p:
        return f"- **{label}:** abridor no encontrado en FanGraphs"
    return (
        f"- **{label}** {p['Name']} ({p['Team']}, {p['GS']} GS): "
        f"SIERA {p['SIERA']:.2f} · xFIP {p['xFIP']:.2f} · FIP {p['FIP']:.2f} · ERA {p['ERA']:.2f} · "
        f"K-BB% {p['K-BB%']:.1f} · CSW% {p['CSW%']:.1f} · WHIP {p['WHIP']:.2f} · "
        f"BABIP {p['BABIP']:.3f} · LOB% {p['LOB%']:.1f}"
    )


def build_report(screens: list[dict]) -> str:
    lines = [f"# Screen MLB — {TODAY}", ""]
    n_total = len(screens)
    n_elim = sum(1 for s in screens if s["eliminations"])
    n_flagged = sum(1 for s in screens if s["flags"] and not s["eliminations"])
    lines += [
        f"**Juegos hoy:** {n_total}  ",
        f"**Eliminados:** {n_elim}  ",
        f"**Con flags de regresión:** {n_flagged}",
        "",
        "---",
        "",
    ]

    # Eliminations section
    elim = [s for s in screens if s["eliminations"]]
    if elim:
        lines += ["## 🚫 Eliminados", ""]
        for s in elim:
            g = s["game"]
            lines.append(f"**{g['away_team']} @ {g['home_team']}** ({g['venue']}):")
            for e in s["eliminations"]:
                lines.append(f"  - {e}")
            lines.append("")

    # Regression / flag candidates
    flagged = [s for s in screens if s["flags"] and not s["eliminations"]]
    if flagged:
        lines += ["## ⚡ Candidatos con flags de regresión", ""]
        for s in flagged:
            g = s["game"]
            lines.append(f"### {g['away_team']} @ {g['home_team']}")
            lines.append(f"_{g['venue']}_")
            lines.append("")
            lines.append(format_pitcher_line(s["away_metrics"], "Visitante:"))
            lines.append(format_pitcher_line(s["home_metrics"], "Local:"))
            lines.append("")
            for f in s["flags"]:
                lines.append(f"- {f}")
            # Odds, if any
            ml = s["odds"].get("ml", {})
            if ml:
                ml_str = " · ".join(f"{t} {p:+.0f}" for t, p in ml.items())
                lines.append(f"- Líneas ML promedio: {ml_str}")
            w = s["weather"]
            if w:
                lines.append(
                    f"- Clima: {safe_float(w.get('temperature_2m')):.0f}°F, "
                    f"viento {safe_float(w.get('wind_speed_10m')):.1f} mph, "
                    f"lluvia {safe_float(w.get('precipitation_probability')):.0f}%"
                )
            lines.append("")

    # Full board, neutral
    other = [s for s in screens if not s["flags"] and not s["eliminations"]]
    if other:
        lines += ["## 📋 Resto del pizarrón (sin flags ni eliminaciones)", ""]
        for s in other:
            g = s["game"]
            lines.append(f"### {g['away_team']} @ {g['home_team']}")
            lines.append(format_pitcher_line(s["away_metrics"], "Visitante:"))
            lines.append(format_pitcher_line(s["home_metrics"], "Local:"))
            ml = s["odds"].get("ml", {})
            if ml:
                ml_str = " · ".join(f"{t} {p:+.0f}" for t, p in ml.items())
                lines.append(f"- Líneas ML: {ml_str}")
            lines.append("")

    lines += [
        "---",
        "",
        "_Recordatorios: este screen entrega Tier A real y eliminaciones automáticas. ",
        "La calibración final del edge sigue requiriendo tu juicio o numberFire. ",
        "Apuestas implican varianza; respeta tu límite de bankroll (≤3% por parlay) ",
        "y registra el CLV._",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"[screen] running for {TODAY}")
    games = fetch_schedule()
    print(f"[screen] {len(games)} games on the slate")
    if not games:
        out = REPORTS_DIR / f"{TODAY}.md"
        out.write_text(f"# Screen MLB — {TODAY}\n\nSin juegos programados hoy.\n")
        print(f"[screen] no games, wrote {out}")
        return

    metrics = fetch_pitcher_metrics()
    print(f"[screen] pulled FanGraphs metrics for {len(metrics)} pitchers")

    odds = fetch_odds()
    print(f"[screen] pulled odds for {len(odds)} matchups")

    screens = []
    for g in games:
        coords = VENUE_COORDS.get(g["venue"])
        weather = fetch_weather(*coords) if coords else {}
        screens.append(screen_game(g, metrics, odds, weather))

    report = build_report(screens)
    out = REPORTS_DIR / f"{TODAY}.md"
    out.write_text(report)
    print(f"[screen] wrote {out} ({len(report)} chars)")


if __name__ == "__main__":
    main()

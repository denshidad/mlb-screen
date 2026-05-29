"""
MLB Daily Screen (v5) — adds a transparent win-probability model + auto edge.

Pipeline:
  1. Schedule + probable pitchers + per-pitcher stats : statsapi.mlb.com
  2. Expected stats (xwOBA / wOBA)                     : Baseball Savant (pybaseball)
  3. Standings (team strength)                         : statsapi.mlb.com
  4. Moneylines + line-movement snapshots              : The Odds API
  5. Weather                                           : Open-Meteo

Model: log5(team win% regressed to .500) + home-field + starter xwOBA edge.
Edge  = model prob  -  de-vigged market prob.  Flag if >= +3%.

HONEST NOTE: the MLB market is very efficient. Small edges (3-6%) are the
actionable zone; treat edges > 8% with suspicion (the model is likely missing
info the market has: injuries, bullpen, lineup). Calibrate against your CLV log.

Writes reports/YYYY-MM-DD.md ; snapshots in snapshots/YYYY-MM-DD.json
"""
from __future__ import annotations
import os
import sys
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
NOW = datetime.now(timezone.utc).astimezone()
TODAY = NOW.strftime("%Y-%m-%d")
YEAR = NOW.year
REPORTS_DIR = Path("reports"); REPORTS_DIR.mkdir(exist_ok=True)
SNAP_DIR = Path("snapshots"); SNAP_DIR.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (compatible; mlb-screen/5.0)"}

HFA = 0.035          # home-field advantage (win prob)
EDGE_MIN = 0.03      # minimum model edge to flag
PRIOR_G = 34         # games of .500 prior to regress team win% (heavier = less favorite-biased)
XWOBA_SCALE = 1.0    # starter xwOBA gap -> win prob nudge (capped +/-0.08)

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


def implied(odds):
    o = safe_float(odds)
    if math.isnan(o):
        return None
    return (-o / (-o + 100)) if o < 0 else (100 / (o + 100))


# ---------------------------------------------------------------------------
# MLB Stats API: schedule, pitchers, standings
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
                "away_team": away["team"]["name"], "home_team": home["team"]["name"],
                "away_pitcher": ap.get("fullName"), "away_pid": ap.get("id"),
                "home_pitcher": hp.get("fullName"), "home_pid": hp.get("id"),
                "venue": g["venue"]["name"],
            })
    return games


def fetch_pitcher_season(pid) -> dict | None:
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
            "ERA": safe_float(s.get("era")), "WHIP": safe_float(s.get("whip")),
            "KBB_pct": (kpct - bbpct) if not math.isnan(kpct) else float("nan"),
        }
    except Exception as e:
        print(f"[stats] pid {pid} failed: {e}", file=sys.stderr)
        return None


def fetch_standings() -> dict[str, dict]:
    url = "https://statsapi.mlb.com/api/v1/standings"
    params = {"leagueId": "103,104", "season": YEAR, "standingsTypes": "regularSeason"}
    out = {}
    try:
        r = requests.get(url, params=params, headers=UA, timeout=20)
        r.raise_for_status()
        for rec in r.json().get("records", []):
            for tr in rec.get("teamRecords", []):
                out[tr["team"]["name"]] = {"w": tr.get("wins", 0), "l": tr.get("losses", 0)}
    except Exception as e:
        print(f"[standings] failed: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Savant
# ---------------------------------------------------------------------------
def fetch_savant_expected() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        from pybaseball import statcast_pitcher_expected_stats
        df = statcast_pitcher_expected_stats(YEAR, 1)
        print(f"[savant] columns: {list(df.columns)}", file=sys.stderr)
        for _, row in df.iterrows():
            pid = str(row.get("player_id", "")).strip()
            if not pid or pid.lower() == "nan":
                continue
            out[pid] = {"woba": safe_float(row.get("woba")), "xwoba": safe_float(row.get("est_woba"))}
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
    return base


# ---------------------------------------------------------------------------
# Odds + snapshots
# ---------------------------------------------------------------------------
def fetch_odds() -> dict[str, dict]:
    if not ODDS_API_KEY:
        return {}
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
    params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h",
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
        ml_avg = {n: round(sum(p) / len(p)) for n, p in ml.items() if p}
        result[f"{g.get('away_team')}@{g.get('home_team')}"] = {"ml": ml_avg}
    return result


def update_snapshots(odds) -> tuple[dict, int]:
    path = SNAP_DIR / f"{TODAY}.json"
    history = []
    if path.exists():
        try:
            history = json.loads(path.read_text())
        except Exception:
            history = []
    history.append({"ts": NOW.isoformat(timespec="minutes"),
                    "ml": {k: v.get("ml", {}) for k, v in odds.items()}})
    path.write_text(json.dumps(history))
    return (history[0]["ml"] if history else {}), len(history)


def movement(key, current_ml, opening) -> dict:
    open_ml = opening.get(key, {})
    out = {}
    for team, cur in current_ml.items():
        op = open_ml.get(team)
        di = None
        if op is not None and implied(cur) is not None and implied(op) is not None:
            di = implied(cur) - implied(op)
        out[team] = (op, cur, di)
    return out


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
# Win-probability model
# ---------------------------------------------------------------------------
def regressed_winpct(w, l):
    g = w + l
    return 0.5 if g <= 0 else (w + PRIOR_G / 2) / (g + PRIOR_G)


def log5(pa, pb):
    num = pa * (1 - pb)
    den = pa * (1 - pb) + pb * (1 - pa)
    return 0.5 if den == 0 else num / den


def model_prob_home(game, away_p, home_p, standings):
    ha, aw = standings.get(game["home_team"]), standings.get(game["away_team"])
    if not ha or not aw:
        return None
    p = log5(regressed_winpct(ha["w"], ha["l"]), regressed_winpct(aw["w"], aw["l"]))
    p += HFA
    hx = home_p.get("xwoba") if home_p else float("nan")
    ax = away_p.get("xwoba") if away_p else float("nan")
    if not (math.isnan(hx) or math.isnan(ax)):
        p += max(-0.08, min(0.08, (ax - hx) * XWOBA_SCALE))  # better (lower) home xwOBA -> home up
    return max(0.05, min(0.95, p))


def compute_edge(game, model_p_home):
    """Return (value_team, edge, value_odds) using de-vigged market, or None."""
    ml = game.get("_ml", {})
    h, a = game["home_team"], game["away_team"]
    ih, ia = implied(ml.get(h)), implied(ml.get(a))
    if model_p_home is None or ih is None or ia is None:
        return None
    fair_h = ih / (ih + ia)
    edge_h = model_p_home - fair_h
    if edge_h >= EDGE_MIN:
        return (h, edge_h, ml.get(h))
    if -edge_h >= EDGE_MIN:
        return (a, -edge_h, ml.get(a))
    return None


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------
def screen_game(game, savant, odds, opening, standings, weather, n_snaps) -> dict:
    away = build_pitcher(game["away_pid"], game["away_pitcher"], savant)
    home = build_pitcher(game["home_pid"], game["home_pitcher"], savant)
    eliminations, flags, reg_value_teams = [], [], set()
    key = f"{game['away_team']}@{game['home_team']}"
    game_odds = odds.get(key, {})
    game["_ml"] = game_odds.get("ml", {})
    mv = movement(key, game["_ml"], opening)

    def mv_note(team):
        if team not in mv:
            return ""
        op, cur, di = mv[team]
        if op is None or di is None:
            return ""
        if di > 0.01:
            return f" · ✅ mercado a favor ({op:+d}→{cur:+d}, {di:+.1%})"
        if di < -0.01:
            return f" · ⚠️ mercado EN CONTRA ({op:+d}→{cur:+d}, {di:+.1%})"
        return f" · mercado estable ({op:+d}→{cur:+d})"

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

    for p, side in [(away, "visitante"), (home, "local")]:
        if not p:
            continue
        wo, xwo = p.get("woba", float("nan")), p.get("xwoba", float("nan"))
        if math.isnan(wo) or math.isnan(xwo):
            continue
        gap = xwo - wo
        opp = game["home_team"] if side == "visitante" else game["away_team"]
        own = game["away_team"] if side == "visitante" else game["home_team"]
        if gap > 0.030:
            reg_value_teams.add(opp)
            flags.append(f"🔻 {p['Name']} ({side}) sobreperforma: wOBA {wo:.3f} vs xwOBA {xwo:.3f} "
                         f"(gap {gap:+.3f}) → valor en {opp}")
        elif gap < -0.030:
            reg_value_teams.add(own)
            flags.append(f"🔺 {p['Name']} ({side}) subperforma: wOBA {wo:.3f} vs xwOBA {xwo:.3f} "
                         f"(gap {gap:+.3f}) → posible valor en {own}")

    for team, price in game["_ml"].items():
        if price <= -200:
            eliminations.append(f"{team} ML {price:+d} (favorito >-200)")

    model_p = model_prob_home(game, away, home, standings)
    edge = compute_edge(game, model_p) if not eliminations else None

    return {"game": game, "away": away, "home": home, "weather": weather,
            "mv": mv, "mv_note": mv_note, "eliminations": eliminations, "flags": flags,
            "model_p": model_p, "edge": edge, "reg_value_teams": reg_value_teams}


def pline(p, label) -> str:
    if not p:
        return f"- **{label}** abridor no encontrado"
    def f(x, n=2):
        return "n/d" if (isinstance(x, float) and math.isnan(x)) else f"{x:.{n}f}"
    return (f"- **{label}** {p.get('Name','?')} ({p.get('GS','?')} GS): "
            f"ERA {f(p.get('ERA'))} · WHIP {f(p.get('WHIP'))} · K-BB% {f(p.get('KBB_pct'),1)} · "
            f"wOBA {f(p.get('woba'),3)} · xwOBA {f(p.get('xwoba'),3)}")


def ml_line(mv) -> str:
    parts = []
    for team, (op, cur, di) in mv.items():
        if cur is None:
            continue
        parts.append(f"{team} {op:+d}→{cur:+d} ({di:+.1%})" if (op is not None and di is not None)
                     else f"{team} {cur:+d}")
    return " · ".join(parts)


def build_report(screens, n_snaps) -> str:
    snap_note = (f"Snapshot #{n_snaps}; movimiento desde la apertura."
                 if n_snaps > 1 else "1ª corrida del día (apertura registrada).")
    L = [f"# Screen MLB — {TODAY}", "",
         f"**Juegos:** {len(screens)} · "
         f"**Eliminados:** {sum(1 for s in screens if s['eliminations'])} · "
         f"**Con flags:** {sum(1 for s in screens if s['flags'] and not s['eliminations'])}",
         "", f"_Modelo log5+xwOBA+localía vs línea sin vig. {snap_note}_", "", "---", ""]

    # TOP: model edges
    edges = [s for s in screens if s.get("edge")]
    edges.sort(key=lambda s: s["edge"][1], reverse=True)
    L += ["## ✅ Edge del modelo (≥ +3%)", ""]
    if not edges:
        L.append("_Ningún lado supera +3% hoy — pizarrón eficiente. No-play es válido._")
    for s in edges:
        team, edge, odds = s["edge"]
        g = s["game"]
        double = " 🟢 **DOBLE SEÑAL** (modelo + regresión coinciden)" if team in s["reg_value_teams"] else ""
        warn = "  ⚠️ edge alto, sospecha (revisa lesiones/lineup)" if edge > 0.08 else ""
        L.append(f"- **{team} ML {odds:+d}** — edge **{edge:+.1%}**{s['mv_note'](team)}{double}{warn}")
        L.append(f"  _{g['away_team']} @ {g['home_team']}_")
    L += ["", "---", ""]

    elim = [s for s in screens if s["eliminations"]]
    if elim:
        L += ["## 🚫 Eliminados", ""]
        for s in elim:
            g = s["game"]
            L.append(f"**{g['away_team']} @ {g['home_team']}**: " + "; ".join(s["eliminations"]))
        L.append("")

    flagged = [s for s in screens if s["flags"] and not s["eliminations"]]
    if flagged:
        L += ["## ⚡ Candidatos con flags", ""]
        for s in flagged:
            g = s["game"]
            mp = s["model_p"]
            mp_str = f" · Modelo: {g['home_team']} {mp:.0%}" if mp is not None else ""
            L += [f"### {g['away_team']} @ {g['home_team']}", f"_{g['venue']}_{mp_str}", "",
                  pline(s["away"], "Visitante:"), pline(s["home"], "Local:"), ""]
            L += [f"- {f}" for f in s["flags"]]
            if s["mv"]:
                L.append(f"- Movimiento ML: {ml_line(s['mv'])}")
            L.append("")

    other = [s for s in screens if not s["flags"] and not s["eliminations"]]
    if other:
        L += ["## 📋 Resto del pizarrón", ""]
        for s in other:
            g = s["game"]
            L += [f"### {g['away_team']} @ {g['home_team']}",
                  pline(s["away"], "Visitante:"), pline(s["home"], "Local:")]
            if s["mv"]:
                L.append(f"- Movimiento ML: {ml_line(s['mv'])}")
            L.append("")

    L += ["---", "", "_El modelo es una brújula, no un oráculo: edges 3-6% son la zona accionable; "
          ">8% suele significar que falta info (lesión/bullpen/lineup) que el mercado ya tiene. "
          "Doble señal = mayor confianza. ≤3% del bankroll por parlay; registra el CLV._"]
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
    standings = fetch_standings()
    print(f"[screen] standings teams: {len(standings)}")
    odds = fetch_odds()
    print(f"[screen] odds matchups: {len(odds)}")
    opening, n_snaps = update_snapshots(odds)
    print(f"[screen] snapshot #{n_snaps}")
    screens = []
    for g in games:
        coords = VENUE_COORDS.get(g["venue"])
        weather = fetch_weather(*coords) if coords else {}
        screens.append(screen_game(g, savant, odds, opening, standings, weather, n_snaps))
    (REPORTS_DIR / f"{TODAY}.md").write_text(build_report(screens, n_snaps))
    print("[screen] wrote report")


if __name__ == "__main__":
    main()

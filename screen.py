"""
MLB Daily Screen (v6) — adds pitcher-strikeout props for elite-K starters.

Pipeline:
  1. Schedule + probable pitchers + per-pitcher stats : statsapi.mlb.com
  2. Expected stats (xwOBA / wOBA)                     : Baseball Savant (pybaseball)
  3. Standings (team strength)                         : statsapi.mlb.com
  4. Moneylines + line-movement snapshots              : The Odds API (/odds)
  5. Pitcher strikeouts (elite-K starters only)        : The Odds API (/events/{id}/odds)
  6. Weather                                           : Open-Meteo

Quota-safe: props are pulled ONLY on the evening run (NOW.hour >= 21) and ONLY
for starters with K-BB% >= ELITE_KBB, so the free 500/month tier suffices.

Markets evaluated with an edge number: moneyline (model) + pitcher Ks (Poisson).
"""
from __future__ import annotations
import os
import sys
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
NOW = datetime.now(timezone.utc).astimezone()
TODAY = NOW.strftime("%Y-%m-%d")
YEAR = NOW.year
REPORTS_DIR = Path("reports"); REPORTS_DIR.mkdir(exist_ok=True)
SNAP_DIR = Path("snapshots"); SNAP_DIR.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (compatible; mlb-screen/6.0)"}

HFA = 0.035
EDGE_MIN = 0.03
PRIOR_G = 34
XWOBA_SCALE = 1.0
ELITE_KBB = 16.0          # only pull Ks props for starters with K-BB% >= this
PROPS_WINDOW_H = 4.0      # pull props within this many hours before the FIRST game (any day)
ODDS_BASE = "https://api.the-odds-api.com/v4/sports/baseball_mlb"

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
    if math.isnan(o) or -100 < o < 100:  # invalid American odds (e.g. 0, -10) -> reject
        return None
    return (-o / (-o + 100)) if o < 0 else (100 / (o + 100))


def poisson_cdf(k, lam):
    """P(X <= k) for Poisson(lam)."""
    if lam <= 0 or k < 0:
        return 1.0 if k >= 0 else 0.0
    s, term = 0.0, math.exp(-lam)
    for i in range(0, k + 1):
        if i > 0:
            term *= lam / i
        s += term
    return min(1.0, s)


def over_prob(line, lam):
    """P(strikeouts > line) for a .5 line."""
    thr = math.floor(line) + 1
    return max(0.0, 1.0 - poisson_cdf(thr - 1, lam))


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
                "away_team": away["team"]["name"], "home_team": home["team"]["name"],
                "away_pitcher": ap.get("fullName"), "away_pid": ap.get("id"),
                "home_pitcher": hp.get("fullName"), "home_pid": hp.get("id"),
                "venue": g["venue"]["name"], "gameTime": g.get("gameDate"),
            })
    return games


def earliest_start(games):
    """Earliest first-pitch (aware UTC datetime) among today's games, or None."""
    times = []
    for g in games:
        t = g.get("gameTime")
        if t:
            try:
                times.append(datetime.fromisoformat(t.replace("Z", "+00:00")))
            except Exception:
                pass
    return min(times) if times else None


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
        gs = int(safe_float(s.get("gamesStarted"), 0))
        ip = safe_float(s.get("inningsPitched"))
        kpct = (k / bf * 100) if bf else float("nan")
        bbpct = (bb / bf * 100) if bf else float("nan")
        return {
            "GS": gs, "IP": ip, "ERA": safe_float(s.get("era")), "WHIP": safe_float(s.get("whip")),
            "K_pct": kpct, "KBB_pct": (kpct - bbpct) if not math.isnan(kpct) else float("nan"),
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
    base["pid"] = pid
    base["xwoba"] = sv.get("xwoba", float("nan"))
    base["woba"] = sv.get("woba", float("nan"))
    return base


def expected_ks(p) -> float:
    """Project a starter's strikeouts: K% * expected batters faced."""
    kpct = p.get("K_pct", float("nan"))
    if math.isnan(kpct):
        return float("nan")
    gs, ip = p.get("GS", 0), p.get("IP", float("nan"))
    avg_ip = (ip / gs) if (gs and not math.isnan(ip)) else 5.5
    exp_bf = max(18.0, min(28.0, avg_ip * 4.3))
    return (kpct / 100.0) * exp_bf


# ---------------------------------------------------------------------------
# Odds + snapshots + props
# ---------------------------------------------------------------------------
def fetch_odds() -> dict[str, dict]:
    if not ODDS_API_KEY:
        return {}
    params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h",
              "oddsFormat": "american", "bookmakers": "fanduel,draftkings"}
    try:
        r = requests.get(f"{ODDS_BASE}/odds", params=params, timeout=20)
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
        result[f"{g.get('away_team')}@{g.get('home_team')}"] = {"ml": ml_avg, "event_id": g.get("id")}
    return result


def fetch_strikeout_props(event_id) -> dict[str, dict]:
    """Return {player_name: {line, over, under}} for pitcher_strikeouts in one event."""
    if not ODDS_API_KEY or not event_id:
        return {}
    params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "pitcher_strikeouts",
              "oddsFormat": "american", "bookmakers": "fanduel,draftkings"}
    try:
        r = requests.get(f"{ODDS_BASE}/events/{event_id}/odds", params=params, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[props] event {event_id} failed: {e}", file=sys.stderr)
        return {}
    agg: dict = {}  # (name, point) -> {"over": [...], "under": [...]}
    for book in r.json().get("bookmakers", []):
        for m in book.get("markets", []):
            if m.get("key") != "pitcher_strikeouts":
                continue
            for o in m.get("outcomes", []):
                name = o.get("description") or o.get("name")
                point = o.get("point")
                side = o.get("name")
                price = o.get("price")
                # reject impossible American odds (the -10 bug)
                if price is None or -100 < price < 100:
                    continue
                d = agg.setdefault((name, point), {"over": [], "under": []})
                if side == "Over":
                    d["over"].append(price)
                elif side == "Under":
                    d["under"].append(price)
    # For each pitcher keep ONLY the best-covered single line (never blend across lines)
    best: dict = {}  # name -> (coverage, {line, over, under})
    for (name, point), d in agg.items():
        cov = len(d["over"]) + len(d["under"])
        entry = {
            "line": point,
            "over": round(sum(d["over"]) / len(d["over"])) if d["over"] else None,
            "under": round(sum(d["under"]) / len(d["under"])) if d["under"] else None,
        }
        if name not in best or cov > best[name][0]:
            best[name] = (cov, entry)
    return {name: v[1] for name, v in best.items()}


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
    # Clean opening: per game, the EARLIEST snapshot where both sides have valid odds
    opening = {}
    for snap in history:
        for key, ml in snap.get("ml", {}).items():
            if key in opening:
                continue
            if len(ml) >= 2 and all(implied(v) is not None for v in ml.values()):
                opening[key] = ml
    return opening, len(history)


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
# Model
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
    p = log5(regressed_winpct(ha["w"], ha["l"]), regressed_winpct(aw["w"], aw["l"])) + HFA
    hx = home_p.get("xwoba") if home_p else float("nan")
    ax = away_p.get("xwoba") if away_p else float("nan")
    if not (math.isnan(hx) or math.isnan(ax)):
        p += max(-0.08, min(0.08, (ax - hx) * XWOBA_SCALE))
    return max(0.05, min(0.95, p))


def compute_edge(game, model_p_home):
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


def name_match(target, candidates):
    if not target:
        return None
    t = target.lower().strip()
    for c in candidates:
        if c.lower().strip() == t:
            return c
    last = t.split()[-1]
    for c in candidates:
        if last in c.lower():
            return c
    return None


def ks_edge(pitcher, props):
    """Return dict with line, proj, over_price, model_p, edge for a starter, or None."""
    if not pitcher:
        return None
    match = name_match(pitcher.get("Name"), list(props.keys()))
    if not match:
        return None
    pr = props[match]
    line, over = pr.get("line"), pr.get("over")
    if line is None or over is None:
        return None
    proj = expected_ks(pitcher)
    if math.isnan(proj):
        return None
    model_p = over_prob(line, proj)
    io, iu = implied(over), implied(pr.get("under"))
    fair_over = io / (io + iu) if (io and iu) else io
    edge = model_p - fair_over if fair_over else None
    return {"name": pitcher["Name"], "line": line, "over": over, "proj": proj,
            "model_p": model_p, "edge": edge}


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------
def screen_game(game, savant, odds, opening, standings, weather, props_by_key, n_snaps) -> dict:
    away = build_pitcher(game["away_pid"], game["away_pitcher"], savant)
    home = build_pitcher(game["home_pid"], game["home_pitcher"], savant)
    eliminations, flags, reg_value_teams = [], [], set()
    key = f"{game['away_team']}@{game['home_team']}"
    game_odds = odds.get(key, {})
    game["_ml"] = game_odds.get("ml", {})
    mv = movement(key, game["_ml"], opening)

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

    # Strikeout props for elite-K starters
    ks = []
    if not eliminations:
        props = props_by_key.get(key, {})
        for p in (away, home):
            if p and not math.isnan(p.get("KBB_pct", float("nan"))) and p["KBB_pct"] >= ELITE_KBB:
                ke = ks_edge(p, props)
                if ke:
                    ks.append(ke)

    return {"game": game, "away": away, "home": home, "weather": weather, "mv": mv,
            "eliminations": eliminations, "flags": flags, "model_p": model_p, "edge": edge,
            "reg_value_teams": reg_value_teams, "ks": ks}


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


def suggested_units(edge):
    """Conservative unit sizing by edge tier; big edges capped (suspicion rule)."""
    if edge > 0.08:
        return 1.0   # suspect -> cap
    if edge >= 0.06:
        return 1.5
    return 1.0       # 3-6%


def daily_card(screens) -> list[str]:
    """Decision-ready list of ONLY qualifying plays across all markets."""
    plays = []
    for s in screens:
        if s.get("edge"):
            team, edge, odds = s["edge"]
            dbl = " 🟢DOBLE" if team in s["reg_value_teams"] else ""
            plays.append((edge, f"{team} ML {odds:+d}", dbl))
        for k in s.get("ks", []):
            if k["edge"] is not None and k["edge"] >= EDGE_MIN:
                plays.append((k["edge"], f"{k['name']} Over {k['line']} K ({k['over']:+d})", ""))
    L = ["## 🎯 JUGADAS DEL DÍA", ""]
    if not plays:
        L += ["**HOY: NO-PLAY** — ningún mercado supera el umbral. Día de descanso (es válido y disciplinado).", ""]
        return L
    total = 0.0
    for edge, txt, dbl in sorted(plays, key=lambda x: x[0], reverse=True):
        u = suggested_units(edge)
        total += u
        sus = " ⚠️sospecha (revisa rival/innings)" if edge > 0.08 else ""
        L.append(f"- **{txt}** — edge {edge:+.1%} → **{u:g}u** (${u*10:.0f}){dbl}{sus}")
    L += ["", f"_Exposición total sugerida: {total:g}u (~${total*10:.0f}). "
          f"Revisa matices (rival, límite de innings) antes de cerrar. Registra el CLV._", ""]
    return L


def build_report(screens, n_snaps, props_pulled) -> str:
    snap_note = (f"Snapshot #{n_snaps}; movimiento desde apertura."
                 if n_snaps > 1 else "1ª corrida del día.")
    props_note = ("Props de ponches: evaluados (corrida pre-juego)." if props_pulled
                  else "Props de ponches: se evalúan en la corrida pre-juego (tarde).")
    L = [f"# Screen MLB — {TODAY}", ""]
    L += daily_card(screens)
    L += ["---", "",
          f"**Juegos:** {len(screens)} · "
          f"**Eliminados:** {sum(1 for s in screens if s['eliminations'])} · "
          f"**Con flags:** {sum(1 for s in screens if s['flags'] and not s['eliminations'])}",
          "", f"_Detalle abajo. Modelo log5+xwOBA+localía vs línea sin vig. {snap_note} {props_note}_", "", "---", ""]

    # Model ML edges
    edges = sorted([s for s in screens if s.get("edge")], key=lambda s: s["edge"][1], reverse=True)
    L += ["## ✅ Edge del modelo — Moneyline (≥ +3%)", ""]
    if not edges:
        L.append("_Ningún ML supera +3% — pizarrón eficiente._")
    for s in edges:
        team, edge, odds = s["edge"]
        double = " 🟢 **DOBLE SEÑAL**" if team in s["reg_value_teams"] else ""
        warn = "  ⚠️ edge alto, sospecha" if edge > 0.08 else ""
        g = s["game"]
        L.append(f"- **{team} ML {odds:+d}** — edge **{edge:+.1%}**{double}{warn}  _({g['away_team']} @ {g['home_team']})_")
    L.append("")

    # Strikeout prop edges
    ks_all = [(s, k) for s in screens for k in s.get("ks", [])]
    L += ["## 🎯 Ponches — candidatos élite", ""]
    if not props_pulled:
        L.append("_Se evalúan en la corrida pre-juego (tarde)._")
    elif not ks_all:
        L.append("_Sin candidatos élite con línea hoy._")
    else:
        ks_all.sort(key=lambda x: (x[1]["edge"] if x[1]["edge"] is not None else -1), reverse=True)
        for s, k in ks_all:
            e = k["edge"]
            tag = " ✅" if (e is not None and e >= EDGE_MIN) else ""
            estr = f"{e:+.1%}" if e is not None else "n/d"
            L.append(f"- **{k['name']} Over {k['line']} K ({k['over']:+d})** — proyección {k['proj']:.1f} K · "
                     f"edge **{estr}**{tag}")
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

    L += ["---", "", "_Modelo y proyección son brújulas, no oráculos. Edges 3-6% accionables; "
          ">8% sospecha. Doble señal = más confianza. ≤3% del bankroll por parlay; registra el CLV._"]
    return "\n".join(L)


def main():
    print(f"[screen] {TODAY} hour={NOW.hour}")
    games = fetch_schedule()
    print(f"[screen] {len(games)} games")
    if not games:
        (REPORTS_DIR / f"{TODAY}.md").write_text(f"# Screen MLB — {TODAY}\n\nSin juegos hoy.\n")
        return
    savant = fetch_savant_expected()
    standings = fetch_standings()
    odds = fetch_odds()
    opening, n_snaps = update_snapshots(odds)
    print(f"[screen] savant={len(savant)} standings={len(standings)} odds={len(odds)} snap#{n_snaps}")

    # --- Props: pull once/day within PROPS_WINDOW_H hours before the FIRST game ---
    # Persist to a file so EVERY later run's report still shows them (and we don't re-pull).
    props_path = SNAP_DIR / f"props_{TODAY}.json"
    props_by_key = {}
    props_pulled = False
    if props_path.exists():
        try:
            props_by_key = json.loads(props_path.read_text())
            props_pulled = True
        except Exception:
            props_by_key = {}
    else:
        es = earliest_start(games)
        in_window = es is not None and NOW < es and (es - NOW) <= timedelta(hours=PROPS_WINDOW_H)
        if in_window:
            for g in games:
                ap = build_pitcher(g["away_pid"], g["away_pitcher"], savant)
                hp = build_pitcher(g["home_pid"], g["home_pitcher"], savant)
                elite = any(p and not math.isnan(p.get("KBB_pct", float("nan"))) and p["KBB_pct"] >= ELITE_KBB
                            for p in (ap, hp))
                key = f"{g['away_team']}@{g['home_team']}"
                eid = odds.get(key, {}).get("event_id")
                if elite and eid:
                    props_by_key[key] = fetch_strikeout_props(eid)
            props_path.write_text(json.dumps(props_by_key))
            props_pulled = True
            print(f"[screen] props pulled for {len(props_by_key)} elite-K games (window before first pitch)")
        else:
            nxt = (es.isoformat() if es else "?")
            print(f"[screen] props not yet (first pitch {nxt}); will pull within {PROPS_WINDOW_H}h before")

    screens = []
    for g in games:
        coords = VENUE_COORDS.get(g["venue"])
        weather = fetch_weather(*coords) if coords else {}
        screens.append(screen_game(g, savant, odds, opening, standings, weather, props_by_key, n_snaps))
    (REPORTS_DIR / f"{TODAY}.md").write_text(build_report(screens, n_snaps, props_pulled))
    print("[screen] wrote report")


if __name__ == "__main__":
    main()

# SharpEdge Backend — Railway
# Endpoints: /health, /odds, /plays, /scores, /injuries, /summary, /chat, /polymarket
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import os, json, datetime, requests, re, math
from zoneinfo import ZoneInfo

app = Flask(__name__)
CORS(app, origins="*", methods=["GET","POST","OPTIONS"], allow_headers=["Content-Type"])

ODDS_KEY   = os.getenv("ODDS_API_KEY", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# ─────────────────────────── ESPN HELPERS ───────────────────────────

ESPN_SPORT_MAP = {
    "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "mls": "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard",
}
SPORT_LABELS = {"nhl":"NHL","mlb":"MLB","nba":"NBA","mls":"MLS"}

def get_today_str():
    return datetime.datetime.now(ET).strftime("%Y%m%d")

def fetch_espn_scoreboard(sport_key: str, date_str: str = None) -> list:
    if date_str is None:
        date_str = get_today_str()
    url = ESPN_SPORT_MAP.get(sport_key)
    if not url:
        return []
    try:
        r = requests.get(url, params={"dates": date_str}, timeout=8)
        return r.json().get("events", [])
    except Exception as e:
        print(f"ESPN fetch error {sport_key}: {e}")
        return []

def parse_int_odds(s):
    try:
        return int(str(s).replace("+","").strip())
    except:
        return None

def american_to_prob(o):
    if o is None: return 0.5
    o = int(str(o).replace("+",""))
    return 100/(o+100) if o>0 else abs(o)/(abs(o)+100)

def vig_free(p1, p2):
    t = p1 + p2
    if t == 0: return 0.5, 0.5
    return p1/t, p2/t

def fmt_american(o):
    if o is None: return "N/A"
    return f"+{o}" if o > 0 else str(o)

# ─────────────────────────── PLAYS ENGINE ───────────────────────────

def analyze_espn_event(ev: dict, sport_label: str) -> list:
    """Analyze a single ESPN event and return candidate plays."""
    comps = ev.get("competitions", [{}])[0]
    competitors = comps.get("competitors", [])
    odds_list = comps.get("odds", [])
    if not odds_list:
        return []
    odds = odds_list[0]

    away = next((c["team"]["displayName"] for c in competitors if c.get("homeAway")=="away"), "Away")
    home = next((c["team"]["displayName"] for c in competitors if c.get("homeAway")=="home"), "Home")
    away_abbr = next((c["team"]["abbreviation"] for c in competitors if c.get("homeAway")=="away"), "")
    home_abbr = next((c["team"]["abbreviation"] for c in competitors if c.get("homeAway")=="home"), "")

    try:
        ev_dt = datetime.datetime.fromisoformat(ev["date"].replace("Z","+00:00")).astimezone(ET)
        game_time = ev_dt.strftime("%-I:%M %p ET")
    except:
        game_time = "--"

    ml = odds.get("moneyline", {})
    away_close = parse_int_odds(ml.get("away",{}).get("close",{}).get("odds"))
    home_close = parse_int_odds(ml.get("home",{}).get("close",{}).get("odds"))
    away_open  = parse_int_odds(ml.get("away",{}).get("open",{}).get("odds"))
    home_open  = parse_int_odds(ml.get("home",{}).get("open",{}).get("odds"))

    total_obj   = odds.get("total", {})
    ou_line     = odds.get("overUnder")
    under_close = parse_int_odds(total_obj.get("under",{}).get("close",{}).get("odds"))
    over_close  = parse_int_odds(total_obj.get("over", {}).get("close",{}).get("odds"))
    under_open  = parse_int_odds(total_obj.get("under",{}).get("open",{}).get("odds"))

    if not (away_close and home_close):
        return []

    ao = away_open  if away_open  else away_close
    ho = home_open  if home_open  else home_close
    away_move = away_close - ao
    home_move = home_close - ho

    nvp_a, nvp_h = vig_free(american_to_prob(away_close), american_to_prob(home_close))
    candidates = []

    # ── ML PLAYS ──
    for team, team_abbr, odds_val, nvp, move in [
        (away, away_abbr, away_close, nvp_a, away_move),
        (home, home_abbr, home_close, nvp_h, home_move),
    ]:
        inds = []
        score = 0
        is_dog = odds_val > 0

        # RLM: underdog line shortened = sharp action
        if is_dog and move < -6:
            inds.append(f"&#x26A1; RLM → {team.split()[-1]}")
            score += 12

        # Steam: favorite moved by sharps
        if not is_dog and move < -10:
            inds.append(f"Steam {team.split()[-1]} ({move:+d}¢)")
            score += 8

        # NVP value on underdog
        if is_dog and nvp > 0.40:
            inds.append(f"NVP {round(nvp*100,1)}%")
            score += 6

        # Sweet spot underdog range
        if is_dog and 110 <= odds_val <= 200:
            inds.append(f"Rango +EV ({fmt_american(odds_val)})")
            score += 4

        # Stable line = no sharp movement but still value
        if is_dog and abs(move) <= 3 and nvp > 0.38:
            inds.append("Línea estable · valor intacto")
            score += 3

        if score >= 10 and len(inds) >= 2:
            conf = min(74, 50 + score)
            units = 1.5 if conf>=70 else (1.0 if conf>=65 else (1.0 if conf>=58 else 0.5))
            stake = 7.50 if units==1.5 else (5.0 if units==1.0 else 2.50)
            label = "TOP PLAY" if conf>=70 else ("SHARP" if conf>=65 else ("VALUE" if conf>=58 else "WATCH"))
            book_impl = american_to_prob(odds_val)
            edge = max(round(abs(nvp - book_impl)*100 + abs(move)*0.3, 1), 3.5)
            candidates.append({
                "sport": sport_label,
                "matchup": f"{away} @ {home}",
                "away_team": away, "home_team": home,
                "away_abbr": away_abbr, "home_abbr": home_abbr,
                "game_time": game_time,
                "bet": f"{team} ML",
                "odds": fmt_american(odds_val),
                "odds_raw": odds_val,
                "confidence": conf, "edge": edge,
                "label": label, "units": units, "stake": stake,
                "indicators": inds,
                "bookmaker": "DraftKings",
                "data_sport": sport_label.lower(),
                "_score": score,
            })

    # ── TOTALS ──
    if under_close and ou_line:
        uo = under_open if under_open else under_close
        under_move = under_close - uo
        t_inds = []
        t_score = 0

        if under_move < -8:
            t_inds.append(f"&#x26A1; Sharp Under ({under_move:+d}¢)")
            t_score += 12

        if under_close > -115:
            t_inds.append(f"Under valor ({fmt_american(under_close)})")
            t_score += 6

        t_inds.append(f"O/U {ou_line}")
        t_score += 2

        if t_score >= 8:
            conf = min(72, 50 + t_score)
            units = 1.5 if conf>=70 else (1.0 if conf>=65 else (1.0 if conf>=58 else 0.5))
            stake = 7.50 if units==1.5 else (5.0 if units==1.0 else 2.50)
            label = "TOP PLAY" if conf>=70 else ("SHARP" if conf>=65 else ("VALUE" if conf>=58 else "WATCH"))
            edge = max(round(abs(under_move)*0.4, 1), 3.5)
            candidates.append({
                "sport": sport_label,
                "matchup": f"{away} @ {home}",
                "away_team": away, "home_team": home,
                "away_abbr": away_abbr, "home_abbr": home_abbr,
                "game_time": game_time,
                "bet": f"Under {ou_line}",
                "odds": fmt_american(under_close),
                "odds_raw": under_close,
                "confidence": conf, "edge": edge,
                "label": label, "units": units, "stake": stake,
                "indicators": t_inds,
                "bookmaker": "DraftKings",
                "data_sport": sport_label.lower(),
                "_score": t_score,
            })

    return candidates


def generate_plays(date_str: str = None) -> dict:
    """Fetch ESPN data for all sports and return sorted plays."""
    if date_str is None:
        date_str = get_today_str()

    all_cands = []
    total_games = 0

    for sport_key, sport_label in SPORT_LABELS.items():
        events = fetch_espn_scoreboard(sport_key, date_str)
        total_games += len(events)
        for ev in events:
            cands = analyze_espn_event(ev, sport_label)
            all_cands.extend(cands)

    # Sort by score * edge descending
    all_cands.sort(key=lambda p: p["_score"] * p["edge"], reverse=True)

    # Deduplicate: max 1 play per matchup
    seen = set()
    top_plays = []
    for p in all_cands:
        key = p["matchup"]
        if key not in seen and len(top_plays) < 8:
            seen.add(key)
            p["id"] = len(top_plays) + 1
            # Clean internal field
            del p["_score"]
            top_plays.append(p)

    now_et = datetime.datetime.now(ET)
    return {
        "date": date_str,
        "generated_at": now_et.isoformat(),
        "total_games_scanned": total_games,
        "plays_count": len(top_plays),
        "plays": top_plays,
        "avg_confidence": round(sum(p["confidence"] for p in top_plays)/max(len(top_plays),1), 1),
        "top_edge": max((p["edge"] for p in top_plays), default=0),
        "data_source": "ESPN/DraftKings",
    }


# ─────────────────────────── ESPN SCOREBOARD/SCORES ─────────────────

def build_summary_from_espn():
    date_str = get_today_str()
    total = 0
    sports_active = []
    rlm_detected = []
    injuries = []

    for sk, sl in SPORT_LABELS.items():
        events = fetch_espn_scoreboard(sk, date_str)
        if events:
            total += len(events)
            sports_active.append(sl)
        # Check for any RLM signals
        for ev in events:
            comps = ev.get("competitions", [{}])[0]
            competitors = comps.get("competitors", [])
            odds_list = comps.get("odds", [])
            if not odds_list: continue
            odds = odds_list[0]
            ml = odds.get("moneyline", {})
            away_close = parse_int_odds(ml.get("away",{}).get("close",{}).get("odds"))
            away_open  = parse_int_odds(ml.get("away",{}).get("open",{}).get("odds"))
            home_close = parse_i

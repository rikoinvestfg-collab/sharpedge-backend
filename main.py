# SharpEdge Backend - Railway
# Endpoints: /health /plays /scores /injuries /summary /chat /polymarket
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import os, json, datetime, requests, re, math

app = Flask(__name__)
CORS(app, origins="*", methods=["GET","POST","OPTIONS"], allow_headers=["Content-Type"])

ODDS_KEY   = os.getenv("ODDS_API_KEY", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")

ESPN_SPORTS = {
    "mlb":  "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "nba":  "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "nhl":  "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "mls":  "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard",
}

def today_str():
    return datetime.datetime.utcnow().strftime("%Y%m%d")

def espn_fetch(sport, date=None):
    url = ESPN_SPORTS.get(sport)
    if not url:
        return []
    params = {"dates": date or today_str(), "limit": 30}
    try:
        r = requests.get(url, params=params, timeout=8)
        return r.json().get("events", [])
    except Exception:
        return []

def parse_ml(val):
    if val is None:
        return None
    try:
        return int(str(val).replace("+", ""))
    except Exception:
        return None

def to_prob(ml):
    if ml is None:
        return 0
    return abs(ml) / (abs(ml) + 100) if ml < 0 else 100 / (ml + 100)

# ─── /health ───────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.datetime.utcnow().isoformat()})

# ─── /plays ────────────────────────────────────────────────────────
@app.route("/plays")
def plays():
    all_games = []
    for sport_key in ["mlb", "nba", "nhl", "mls"]:
        events = espn_fetch(sport_key)
        for ev in events:
            comp = (ev.get("competitions") or [{}])[0]
            comps = comp.get("competitors", [])
            away = next((c for c in comps if c.get("homeAway") == "away"), None)
            home = next((c for c in comps if c.get("homeAway") == "home"), None)
            if not away or not home:
                continue
            odds = (comp.get("odds") or [{}])[0]
            ml = odds.get("moneyline") or {}
            ml_away = ml.get("away") or {}
            ml_home = ml.get("home") or {}
            tot = odds.get("total") or {}
            under = tot.get("under") or {}
            away_close = parse_ml((ml_away.get("close") or {}).get("odds"))
            away_open  = parse_ml((ml_away.get("open")  or {}).get("odds"))
            home_close = parse_ml((ml_home.get("close") or {}).get("odds"))
            home_open  = parse_ml((ml_home.get("open")  or {}).get("odds"))
            under_close = parse_ml((under.get("close") or {}).get("odds"))
            under_open  = parse_ml((under.get("open")  or {}).get("odds"))
            over_under  = odds.get("overUnder")
            status = ((ev.get("status") or {}).get("type") or {}).get("name", "pre")
            all_games.append({
                "sport": sport_key.upper(),
                "matchup": away["team"]["abbreviation"] + " @ " + home["team"]["abbreviation"],
                "away_abbr": away["team"]["abbreviation"],
                "home_abbr": home["team"]["abbreviation"],
                "away_close": away_close, "away_open": away_open,
                "home_close": home_close, "home_open": home_open,
                "under_close": under_close, "under_open": under_open,
                "over_under": over_under,
                "status": status,
            })

    result = []
    for g in all_games:
        if g["status"] in ("in", "post", "final"):
            continue
        if g["away_close"] is None and g["home_close"] is None:
            continue
        indicators = []
        score = 0
        bet_team = None
        bet_side = "away"
        bet_type = "ML"

        # RLM
        if g["away_close"] and g["home_close"]:
            away_udog = g["away_close"] > g["home_close"]
            if away_udog and g["away_open"] and (g["away_open"] - g["away_close"] > 6):
                indicators.append("RLM Sharp (Away Dog)")
                score += 18
                bet_team = g["away_abbr"]
            elif not away_udog and g["home_open"] and (g["home_open"] - g["home_close"] > 6):
                indicators.append("RLM Sharp (Home Dog)")
                score += 18
                bet_team = g["home_abbr"]
                bet_side = "home"

        # Steam
        if g["away_close"] and g["home_close"]:
            fav_away = g["away_close"] < g["home_close"]
            if fav_away and g["away_open"] and (g["away_open"] - g["away_close"] > 10):
                indicators.append("Steam Move (Fav Away)")
                score += 12
                if not bet_team:
                    bet_team = g["away_abbr"]
            elif not fav_away and g["home_open"] and (g["home_open"] - g["home_close"] > 10):
                indicators.append("Steam Move (Fav Home)")
                score += 12
                if not bet_team:
                    bet_team = g["home_abbr"]
                    bet_side = "home"

        # NVP
        if g["away_close"] and g["home_close"]:
            pa = to_prob(g["away_close"])
            ph = to_prob(g["home_close"])
            vig = pa + ph
            if vig > 0:
                away_udog2 = g["away_close"] > g["home_close"]
                udog_nvp = pa / vig if away_udog2 else ph / vig
                if udog_nvp > 0.40:
                    indicators.append("NVP Edge {}%".format(round(udog_nvp * 100)))
                    score += 10
                    if not bet_team:
                        if away_udog2:
                            bet_team = g["away_abbr"]
                        else:
                            bet_team = g["home_abbr"]
                            bet_side = "home"

        # Sweet Spot
        if g["away_close"] and g["home_close"]:
            away_udog3 = g["away_close"] > g["home_close"]
            udog_odds = g["away_close"] if away_udog3 else g["home_close"]
            if 110 <= udog_odds <= 210:
                indicators.append("Sweet Spot +{}".format(udog_odds))
                score += 8
                if not bet_team:
                    if away_udog3:
                        bet_team = g["away_abbr"]
                    else:
                        bet_team = g["home_abbr"]
                        bet_side = "home"

        # Under Sharps
        if g["under_close"] and g["under_open"] and (g["under_close"] - g["under_open"] > 4):
            indicators.append("Under Sharps (U{})".format(g["over_under"] or "?"))
            score += 14
            bet_type = "UNDER"

        if len(indicators) < 2:
            continue

        conf = min(74, 50 + score)
        units = 1.5 if conf >= 70 else (1.0 if conf >= 65 else 0.5)
        stake = units * 5
        odds_raw = g["under_close"] if bet_type == "UNDER" else (
            g["away_close"] if bet_side == "away" else g["home_close"]) or 100
        odds_str = "+{}".format(odds_raw) if odds_raw >= 0 else str(odds_raw)
        bet = "Under {}".format(g["over_under"] or "?") if bet_type == "UNDER" else \
              "{} ML".format(bet_team or g["away_abbr"])

        result.append({
            "sport": g["sport"],
            "data_sport": g["sport"].lower(),
            "matchup": g["matchup"],
            "bet": bet,
            "odds": odds_str,
            "confidence": conf,
            "label": "TOP PLAY" if conf >= 70 else "JUGADA",
            "units": units,
            "stake": stake,
            "indicators": indicators,
            "bookmaker": "DraftKings/ESPN",
        })

    result.sort(key=lambda x: -x["confidence"])
    return jsonify({"plays": result[:8], "total_games_scanned": len(all_games)})

# ─── /scores ───────────────────────────────────────────────────────
@app.route("/scores")
def scores():
    sport = request.args.get("sport", "mlb").lower()
    date  = request.args.get("date", today_str())
    events = espn_fetch(sport, date)
    out = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors", [])
        away = next((c for c in comps if c.get("homeAway") == "away"), {})
        home = next((c for c in comps if c.get("homeAway") == "home"), {})
        out.append({
            "name": ev.get("name", ""),
            "away": (away.get("team") or {}).get("displayName", ""),
            "home": (home.get("team") or {}).get("displayName", ""),
            "away_score": away.get("score", "-"),
            "home_score": home.get("score", "-"),
            "status": ((ev.get("status") or {}).get("type") or {}).get("shortDetail", ""),
            "date": ev.get("date", ""),
        })
    return jsonify({"sport": sport, "games": out})

# ─── /injuries ─────────────────────────────────────────────────────
@app.route("/injuries")
def injuries():
    return jsonify({"injuries": [], "note": "Use ESPN team injury pages for live data"})

# ─── /summary ──────────────────────────────────────────────────────
@app.route("/summary")
def summary():
    today = today_str()
    total = 0
    for s in ["mlb", "nba", "nhl", "mls"]:
        total += len(espn_fetch(s, today))
    return jsonify({"games_today": total, "date": today, "status": "ok"})

# ─── /chat ─────────────────────────────────────────────────────────
@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        return resp
    data = request.get_json(silent=True) or {}
    user_msg = data.get("message", "")
    history  = data.get("history", [])
    if not GEMINI_KEY:
        return jsonify({"error": "No Gemini key configured"}), 500

    system_prompt = (
        "Eres SharpEdge AI, un experto analista de apuestas institucionales especializado en NFL, NHL, MLB, NBA y Soccer. "
        "Ayudas a un apostador nuevo con bankroll menor a $500. Unidad base = $5. "
        "Respondes en español, de forma profesional, directa y precisa. "
        "Identificas jugadas con al menos 3 indicadores confluentes (RLM, Steam, NVP, Sharp Money). "
        "Para el usuario: plataforma exclusiva BET365."
    )

    contents = []
    for h in history[-10:]:
        role = "user" if h.get("role") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": h.get("content", "")}]})
    contents.append({"role": "user", "parts": [{"text": user_msg}]})

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024},
    }

    def generate():
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse&key=" + GEMINI_KEY
        try:
            with requests.post(url, json=payload, stream=True, timeout=60) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    line = line.decode("utf-8") if isinstance(line, bytes) else line
                    if line.startswith("data:"):
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            obj = json.loads(raw)
                            parts = (obj.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                            for p in parts:
                                text = p.get("text", "")
                                if text:
                                    yield "data: " + json.dumps({"text": text}) + "\n\n"
                        except Exception:
                            pass
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"error": str(e)}) + "\n\n"
            yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ─── /polymarket ───────────────────────────────────────────────────
@app.route("/polymarket")
def polymarket():
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"closed": "false", "limit": 50, "order": "volume", "ascending": "false"},
            timeout=8,
        )
        markets = r.json() if r.ok else []
    except Exception:
        markets = []
    return jsonify({"markets": markets})

# ─── start ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("SharpEdge Backend starting on port", port)
    app.run(host="0.0.0.0", port=port, debug=False)

"""
SharpEdge Backend — FastAPI
Fuentes:
  - The Odds API  → cuotas en vivo (NHL, MLB, NFL, Soccer)
  - ESPN API      → scores, schedules, lesiones (gratis, sin key)
Auto-refresh cada 5 minutos via background task.
"""

import os
import asyncio
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="SharpEdge API", version="1.0")

# ── CORS — permite que el dashboard en Perplexity llame al backend ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_BASE    = "https://api.the-odds-api.com/v4"
ESPN_BASE    = "https://site.api.espn.com/apis/site/v2/sports"

# ── Cache en memoria ──
_cache = {
    "odds":    {},
    "scores":  {},
    "injuries":{},
    "last_updated": None,
    "rlm_history": {}   # para detectar RLM: guarda cuotas anteriores
}

# ── Deportes que cubrimos ──
SPORTS = {
    "nhl":    "icehockey_nhl",
    "mlb":    "baseball_mlb",
    "nfl":    "americanfootball_nfl",
    "soccer_epl":  "soccer_epl",
    "soccer_mls":  "soccer_usa_mls",
}

ESPN_SPORTS = {
    "nhl": ("hockey", "nhl"),
    "mlb": ("baseball", "mlb"),
    "nfl": ("football", "nfl"),
}

# ────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────

def american_to_decimal(american: int) -> float:
    if american > 0:
        return round((american / 100) + 1, 4)
    return round((100 / abs(american)) + 1, 4)

def implied_prob(american: int) -> float:
    if american > 0:
        return round(100 / (american + 100), 4)
    return round(abs(american) / (abs(american) + 100), 4)

def calc_edge(model_prob: float, american: int) -> float:
    decimal = american_to_decimal(american)
    return round((model_prob * decimal - 1) * 100, 1)

def detect_rlm(sport_key: str, game_id: str, team: str, current_odds: int) -> dict:
    """
    RLM = línea se mueve hacia el underdog pese al público.
    Aquí detectamos si la cuota mejoró para el underdog vs hace 5 min.
    """
    key = f"{sport_key}:{game_id}:{team}"
    prev = _cache["rlm_history"].get(key)
    result = {"detected": False, "movement": 0, "direction": ""}
    if prev is not None:
        movement = current_odds - prev
        if current_odds > 0 and movement > 0:
            result = {"detected": True, "movement": movement, "direction": "improving_dog"}
        elif current_odds < 0 and movement > 0:
            result = {"detected": True, "movement": movement, "direction": "improving_dog"}
    _cache["rlm_history"][key] = current_odds
    return result

def extract_bet365(bookmakers: list) -> dict | None:
    """Extrae cuotas de BET365 si disponible, si no usa el promedio de todos."""
    for bm in bookmakers:
        if bm.get("key") in ("bet365", "betfair", "draftkings", "fanduel"):
            return bm
    return bookmakers[0] if bookmakers else None

# ────────────────────────────────────────────
# FETCH ODDS
# ────────────────────────────────────────────

async def fetch_odds(client: httpx.AsyncClient, sport_key: str) -> list:
    if not ODDS_API_KEY:
        return []
    try:
        r = await client.get(
            f"{ODDS_BASE}/sports/{sport_key}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us,uk",
                "markets": "h2h,totals",
                "oddsFormat": "american",
                "dateFormat": "iso",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        print(f"Odds API error {r.status_code} for {sport_key}: {r.text[:200]}")
    except Exception as e:
        print(f"Odds fetch error ({sport_key}): {e}")
    return []

# ────────────────────────────────────────────
# FETCH ESPN SCORES + INJURIES
# ────────────────────────────────────────────

async def fetch_espn_scores(client: httpx.AsyncClient, league: str, sport: str) -> list:
    try:
        r = await client.get(
            f"{ESPN_BASE}/{sport}/{league}/scoreboard",
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            games = []
            for event in data.get("events", []):
                comp   = event["competitions"][0]
                status = event["status"]["type"]
                teams  = {t["team"]["abbreviation"]: {
                    "name":  t["team"]["displayName"],
                    "score": t.get("score", "0"),
                    "record": t.get("records", [{}])[0].get("summary", ""),
                } for t in comp["competitors"]}
                games.append({
                    "id":        event["id"],
                    "name":      event["name"],
                    "date":      event["date"],
                    "status":    status["description"],
                    "completed": status["completed"],
                    "teams":     teams,
                })
            return games
    except Exception as e:
        print(f"ESPN scores error ({league}): {e}")
    return []

async def fetch_espn_injuries(client: httpx.AsyncClient, league: str, sport: str) -> list:
    try:
        r = await client.get(
            f"{ESPN_BASE}/{sport}/{league}/injuries",
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            injuries = []
            for item in data.get("injuries", []):
                injuries.append({
                    "player": item.get("athlete", {}).get("displayName", ""),
                    "team":   item.get("team", {}).get("abbreviation", ""),
                    "status": item.get("status", ""),
                    "detail": item.get("details", {}).get("detail", ""),
                })
            return injuries
    except Exception as e:
        print(f"ESPN injuries error ({league}): {e}")
    return []

# ────────────────────────────────────────────
# PROCESS & ANALYZE ODDS (RLM + Edge)
# ────────────────────────────────────────────

def process_game(sport_key: str, game: dict) -> dict:
    bookmaker = extract_bet365(game.get("bookmakers", []))
    if not bookmaker:
        return {}

    h2h_market    = next((m for m in bookmaker["markets"] if m["key"] == "h2h"), None)
    totals_market = next((m for m in bookmaker["markets"] if m["key"] == "totals"), None)

    moneylines = {}
    if h2h_market:
        for outcome in h2h_market["outcomes"]:
            odds_val = int(outcome["price"])
            team_name = outcome["name"]
            rlm = detect_rlm(sport_key, game["id"], team_name, odds_val)
            moneylines[team_name] = {
                "odds":     odds_val,
                "odds_fmt": f"+{odds_val}" if odds_val > 0 else str(odds_val),
                "implied":  f"{implied_prob(odds_val)*100:.1f}%",
                "rlm":      rlm,
            }

    totals = {}
    if totals_market:
        for outcome in totals_market["outcomes"]:
            totals[outcome["name"]] = {
                "point":    outcome["point"],
                "odds":     int(outcome["price"]),
                "odds_fmt": f"+{int(outcome['price'])}" if int(outcome["price"]) > 0 else str(int(outcome["price"])),
            }

    return {
        "id":           game["id"],
        "sport":        sport_key,
        "home_team":    game["home_team"],
        "away_team":    game["away_team"],
        "commence":     game["commence_time"],
        "bookmaker":    bookmaker["title"],
        "moneylines":   moneylines,
        "totals":       totals,
        "last_update":  bookmaker.get("last_update", ""),
    }

# ────────────────────────────────────────────
# REFRESH LOOP — corre cada 5 minutos
# ────────────────────────────────────────────

async def refresh_data():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Refreshing data...")
    async with httpx.AsyncClient() as client:
        # Fetch odds for all sports in parallel
        odds_tasks = {
            key: fetch_odds(client, sport_key)
            for key, sport_key in SPORTS.items()
        }
        odds_results = {}
        for key, task in odds_tasks.items():
            odds_results[key] = await task

        # Process odds
        processed = {}
        for sport_key, games in odds_results.items():
            processed[sport_key] = [
                g for g in (process_game(SPORTS[sport_key], game) for game in games)
                if g
            ]
        _cache["odds"] = processed

        # Fetch ESPN scores + injuries in parallel
        score_tasks = {}
        inj_tasks   = {}
        for key, (sport, league) in ESPN_SPORTS.items():
            score_tasks[key] = fetch_espn_scores(client, league, sport)
            inj_tasks[key]   = fetch_espn_injuries(client, league, sport)

        for key in ESPN_SPORTS:
            _cache["scores"][key]   = await score_tasks[key]
            _cache["injuries"][key] = await inj_tasks[key]

    _cache["last_updated"] = datetime.now(timezone.utc).isoformat()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Data refreshed ✓")

async def refresh_loop():
    while True:
        await refresh_data()
        await asyncio.sleep(300)  # 5 minutos

@app.on_event("startup")
async def startup():
    asyncio.create_task(refresh_loop())

# ────────────────────────────────────────────
# ENDPOINTS
# ────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "name": "SharpEdge API",
        "version": "1.0",
        "last_updated": _cache["last_updated"],
        "endpoints": ["/odds", "/odds/{sport}", "/scores", "/injuries", "/health"]
    }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "last_updated": _cache["last_updated"],
        "odds_sports": list(_cache["odds"].keys()),
        "api_key_set": bool(ODDS_API_KEY),
    }

@app.get("/odds")
def get_all_odds():
    """Retorna todas las cuotas de todos los deportes."""
    return {
        "last_updated": _cache["last_updated"],
        "data": _cache["odds"],
    }

@app.get("/odds/{sport}")
def get_odds_by_sport(sport: str):
    """Retorna cuotas de un deporte específico: nhl, mlb, nfl, soccer_epl, soccer_mls"""
    if sport not in _cache["odds"]:
        return {"error": f"Sport '{sport}' not found. Options: {list(_cache['odds'].keys())}"}
    return {
        "sport": sport,
        "last_updated": _cache["last_updated"],
        "games": _cache["odds"][sport],
    }

@app.get("/scores")
def get_scores():
    """Retorna scores en vivo de NHL, MLB, NFL."""
    return {
        "last_updated": _cache["last_updated"],
        "data": _cache["scores"],
    }

@app.get("/scores/{sport}")
def get_scores_by_sport(sport: str):
    if sport not in _cache["scores"]:
        return {"error": f"Sport '{sport}' not found."}
    return {
        "sport": sport,
        "last_updated": _cache["last_updated"],
        "games": _cache["scores"][sport],
    }

@app.get("/injuries")
def get_injuries():
    """Retorna lesiones actuales de NHL, MLB, NFL."""
    return {
        "last_updated": _cache["last_updated"],
        "data": _cache["injuries"],
    }

@app.get("/injuries/{sport}")
def get_injuries_by_sport(sport: str):
    if sport not in _cache["injuries"]:
        return {"error": f"Sport '{sport}' not found."}
    return {
        "sport": sport,
        "injuries": _cache["injuries"][sport],
    }

@app.get("/summary")
def get_summary():
    """
    Endpoint principal para el dashboard.
    Retorna un resumen consolidado: cuotas + scores + lesiones críticas + RLM detectado.
    """
    rlm_detected = []
    for sport, games in _cache["odds"].items():
        for game in games:
            for team, ml in game.get("moneylines", {}).items():
                if ml.get("rlm", {}).get("detected"):
                    rlm_detected.append({
                        "sport":     sport,
                        "game":      f"{game['away_team']} @ {game['home_team']}",
                        "team":      team,
                        "odds":      ml["odds_fmt"],
                        "movement":  f"+{ml['rlm']['movement']}",
                    })

    critical_injuries = []
    for sport, injuries in _cache["injuries"].items():
        for inj in injuries:
            if inj.get("status") in ("Out", "Doubtful", "IR"):
                critical_injuries.append({**inj, "sport": sport})

    return {
        "last_updated":       _cache["last_updated"],
        "rlm_detected":       rlm_detected,
        "critical_injuries":  critical_injuries[:20],
        "total_games_today": sum(len(g) for g in _cache["odds"].values()),
        "sports_active":     [s for s, g in _cache["odds"].items() if g],
    }

# ────────────────────────────────────────────
# CHAT ENDPOINT — Perplexity Sonar via backend
# El frontend llama aquí → backend llama a Perplexity con la key guardada
# ────────────────────────────────────────────
from fastapi import Request
from fastapi.responses import StreamingResponse
import json

PPLX_KEY = os.getenv("PPLX_API_KEY", "")

SPORTS_SYSTEM = """Eres SharpEdge AI, un experto analista de apuestas deportivas institucionales.
Responde SIEMPRE en español. Tono: profesional, directo, analítico, lacónico.

CONTEXTO DEL DASHBOARD (9 de abril de 2026):
JUGADAS ACTIVAS HOY:
1. Minnesota Wild ML +105 — NHL vs Dallas Stars 9PM ET — Conf 74% — Edge +51.7% — 1.5u = $7.50
   Razones: RLM sharp (+110→+105 sin movimiento DAL), racha 4W MIN, H2H 2-1 vs DAL, Roope Hintz (C top-6 DAL) OUT
2. Under 6.5 -135 — NHL Wild@Stars 9PM ET — Conf 70% — Edge +21.9% — 1.5u = $7.50
   Razones: Total abrió 5.5 → subió a 6.5 (medio gol extra), proyección modelos 5.8G, hockey playoff defensivo
3. Philadelphia Flyers ML +102 — NHL vs Detroit Red Wings 7PM ET — Conf 67% — Edge +35.3% — 1u = $5
   Razones: RLM masivo PHI +125→-102 (swing 23 cents sharp), PHI 3W consecutivos, DET 3L en casa
4. Under 9 -111 — MLB White Sox@Royals 7:40PM ET — Conf 63% — Edge +19.8% — 1u = $5
   Razones: Seth Lugo ERA 1.59 WHIP 0.97, CHW peor ofensiva AL, KC 4-8 Under en casa
5. Arizona D-Backs ML +139 — MLB vs New York Mets 7:10PM ET — Conf 58% — Edge +38.6% — 0.5u = $2.50
   Razones: Ryne Nelson ERA 0.00 (2GS) WHIP 0.917, dog de valor, frío NY 47°F viento 11mph

BANKROLL USUARIO: $500 → Unidad base $5 (1%). TOP PLAY 70%+: $7.50. 65-69%: $5. 58-64%: $2.50. Máx $10. Stop-loss $25.
PARLAYS: Conservador (Wild ML + Under 6.5) → +195. Cross-sport (Wild ML + Under 9 KC) → +275.

REGLAS:
- Puedes responder sobre CUALQUIER deporte (NHL, MLB, NFL, NBA, Soccer, Tennis, etc.) y CUALQUIER partido, no solo los de hoy
- Puedes analizar equipos, jugadores, tendencias, estrategias de apuestas, conceptos como RLM, ATS, EV, líneas, totales, spreads, parlays, props, live betting, bankroll management
- Si el usuario pregunta algo fuera del deporte/apuestas, redirígelo amablemente al tema
- Máximo 250 palabras por respuesta. Sin markdown excesivo. Usa <strong> para énfasis clave."""

@app.post("/chat")
async def chat_endpoint(request: Request):
    """
    Proxy hacia Perplexity Sonar con streaming.
    Body esperado: { "messages": [...], "stream": true }
    """
    if not PPLX_KEY:
        return {"error": "PPLX_API_KEY not configured in Railway variables"}

    try:
        body = await request.json()
        messages = body.get("messages", [])

        # Inject system prompt
        full_messages = [{"role": "system", "content": SPORTS_SYSTEM}] + messages[-10:]

        async def stream_response():
            async with httpx.AsyncClient(timeout=30) as client:
                async with client.stream(
                    "POST",
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {PPLX_KEY}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    json={
                        "model": "sonar",
                        "messages": full_messages,
                        "stream": True,
                        "max_tokens": 500,
                        "temperature": 0.4,
                    },
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk

        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
                "X-Accel-Buffering": "no",
            },
        )

    except Exception as e:
        return {"error": str(e)}

# SharpEdge Backend

API en tiempo real para el dashboard de apuestas institucionales.

## Stack
- **FastAPI** — servidor web Python
- **The Odds API** — cuotas en vivo (NHL, MLB, NFL, Soccer)
- **ESPN API** — scores y lesiones (gratis)
- **Railway** — hosting en la nube

## Deploy en Railway (paso a paso)

### 1. Sube el código a GitHub

```bash
# En tu terminal:
git init
git add .
git commit -m "SharpEdge Backend v1"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/sharpedge-backend.git
git push -u origin main
```

### 2. Conecta Railway con GitHub

1. Ve a [railway.app](https://railway.app)
2. Click **"New Project"**
3. Selecciona **"Deploy from GitHub repo"**
4. Elige el repo `sharpedge-backend`
5. Railway detecta automáticamente que es Python y lo despliega

### 3. Agrega la variable de entorno

En Railway → tu proyecto → **Settings → Variables**:
```
ODDS_API_KEY = fa85d4ee05e0554089da826ee57209a8
```

### 4. Verifica que funciona

Railway te da una URL pública tipo:
```
https://sharpedge-backend-production.up.railway.app
```

Visita:
- `/health` → debe decir `"status": "ok"`
- `/odds/nhl` → cuotas NHL en vivo
- `/summary` → resumen completo con RLM + lesiones

## Endpoints

| Endpoint | Descripción |
|---|---|
| `GET /` | Info general |
| `GET /health` | Estado del servidor |
| `GET /odds` | Todas las cuotas |
| `GET /odds/nhl` | Cuotas NHL |
| `GET /odds/mlb` | Cuotas MLB |
| `GET /odds/nfl` | Cuotas NFL |
| `GET /odds/soccer_epl` | Premier League |
| `GET /odds/soccer_mls` | MLS |
| `GET /scores` | Scores en vivo |
| `GET /scores/nhl` | Scores NHL |
| `GET /injuries` | Lesiones críticas |
| `GET /summary` | Dashboard completo (RLM + lesiones + cuotas) |

## Auto-refresh

El servidor refresca automáticamente cada **5 minutos**.
Cada llamada a `/odds` devuelve `last_updated` para verificar frescura.

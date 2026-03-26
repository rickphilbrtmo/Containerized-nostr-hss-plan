# Containerized Architecture for Nostr-HSS

## Context

The current stack runs all three components (HSS, AS, REST API) in a single process via `run.py`. The goal is to containerize them so that:

- **HSS** is a singleton — all AS peers connect to it on port 3868
- **AS** can scale out — each replica maintains its own persistent Diameter connection to HSS
- **API** can scale out — decoupled from AS via HTTP instead of an in-process call

`py-diameter` lives at `/home/rayray/supershi/py-diameter/` (sibling to `nostr-hss/`) and is a proper installable package (`pyproject.toml`, package name `python-diameter`).

---

## Container Layout

| Component | Replicas | Role | Ports | Volumes |
|-----------|----------|------|-------|---------|
| **hss** | 1 | `hss.py` + pallet_watcher thread | 3868 (Diameter inbound) | `/data/hss.db` (rw) |
| **as** | N | `client.py` + embedded FastAPI | 3869 (Diameter out), 8090 (HTTP in) | `/data/hss.db` (ro) |
| **api** | N | `api.py` | 8080 (HTTP) | `/data/hss.db` (ro) |

Docker internal DNS load-balances `http://as:8090` across AS replicas. Each AS replica uses its container hostname as the Diameter `ORIGIN_HOST`, ensuring unique Diameter identities without pre-configuration.

---

## Files to Change

### 0. Fix pre-existing bug — `pallet_watcher.py` line 227

`trigger_pnr` takes 2 args but caller passes 3.

```python
# Before:
hss_app_ref.trigger_pnr(None, pallet_id, result)

# After:
hss_app_ref.trigger_pnr(pallet_id, result)
```

### 1. `db.py` — env var for DB_PATH (line 17)

```python
DB_PATH = os.environ.get("DB_PATH", os.path.join(SCRIPT_DIR, "hss.db"))
```

### 2. `hss.py` — configurable bind, peers, and file paths

**Changes:**
- Lines 38–39: add env var fallbacks for `PALLETS_FILE` and `SUBSCRIBERS_FILE`
- `main()`: read `HSS_BIND = os.environ.get("HSS_BIND", "127.0.0.1")`
- `main()`: read `AS_PEERS = os.environ.get("AS_PEERS", "")` — comma-separated `host:port` list
- Replace hardcoded `node = Node(..., ip_addresses=["127.0.0.1"], ...)` with `HSS_BIND`
- Replace the single `add_peer("aaa://as1.nostr.realm:3869")` call with a loop over `AS_PEERS` entries. Pass resulting list (may be empty) to `add_application(hss_app, peers)`.
  - Empty peer list = accept all inbound connections (standard Diameter behavior); start with this
  - If the library rejects unregistered peers, fall back to setting `AS_PEERS=as:3869` in compose
- Fix syntax error line ~237: `name=rest-api` → `name="rest-api"` (prevents parse failure)

### 3. `client.py` — add `--serve` mode with embedded HTTP SNR gateway

**New additions:**

```python
import socket

# Module-level env var constants
HSS_HOST = os.environ.get("HSS_HOST", "127.0.0.1")
HSS_PORT = int(os.environ.get("HSS_PORT", "3868"))
AS_PORT = int(os.environ.get("AS_PORT", "3869"))
AS_HTTP_PORT = int(os.environ.get("AS_HTTP_PORT", "8090"))
ORIGIN_HOST = os.environ.get("ORIGIN_HOST", socket.gethostname() + ".nostr.realm")
ORIGIN_REALM = os.environ.get("ORIGIN_REALM", "nostr.realm")

_shared_as_app = None
_http_app = FastAPI()

@_http_app.post("/snr")
async def http_snr(request: dict):
    """Receives {npub, pallet_ids, subscribe}, calls send_snr(), returns 503 if not ready."""
    if _shared_as_app is None:
        raise HTTPException(status_code=503, detail="Diameter not ready")
    return await send_snr(request)

@_http_app.get("/health")
async def health():
    """Returns Diameter readiness status."""
    return {"status": "ok", "diameter_ready": _shared_as_app is not None}

def start_as_server(http_port):
    """Start AS in server mode with embedded HTTP gateway."""
    global _shared_as_app
    
    # Create Node with env-based identity
    node = Node(
        ip_addresses=[AS_BIND],
        realm=ORIGIN_REALM,
        host_identity=ORIGIN_HOST
    )
    
    # Connect to HSS
    node.add_peer(f"aaa://{HSS_HOST}:{HSS_PORT}", is_persistent=True)
    
    # Wait up to 30s for peer ready
    _shared_as_app = node
    for _ in range(30):
        if node.is_peer_ready():
            break
        time.sleep(1)
    else:
        logger.warning("Diameter peer not ready after 30s; continuing anyway")
    
    # Start HTTP server
    uvicorn.run(_http_app, host="0.0.0.0", port=http_port)
```

**In `main()`:** add `--serve` flag; if set, call `start_as_server()` and return early

### 4. `api.py` — replace in-process `send_snr` with HTTP POST

```python
import os
import httpx

AS_URL = os.environ.get("AS_URL", "")
PALLETS_FILE = os.environ.get("PALLETS_FILE", os.path.join(SCRIPT_DIR, "pallets.json"))

# In /subscribe endpoint (lines ~448-456):
if AS_URL:
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{AS_URL}/snr",
                json={"npub": npub, "pallet_ids": pallet_ids, "subscribe": True},
                timeout=15
            )
    except httpx.HTTPError as e:
        logger.error(f"AS request failed: {e}")
        raise HTTPException(status_code=503, detail="AS unavailable")
else:
    # Fall through to existing in-process path (preserves run.py compatibility)
    await send_snr({"npub": npub, "pallet_ids": pallet_ids, "subscribe": True})

# Same change in /unsubscribe endpoint (lines ~474-481)
```

### 5. New: `Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libssl-dev && rm -rf /var/lib/apt/lists/*

# Install py-diameter (local package — build context is supershi/)
COPY py-diameter/ /py-diameter/
RUN pip install --no-cache-dir /py-diameter/

# Install runtime dependencies
RUN pip install --no-cache-dir \
    fastapi "uvicorn[standard]" httpx pydantic \
    coincurve websocket-client requests cryptography

# Copy application
COPY nostr-hss/ /app/

# Setup data volume and env defaults
VOLUME ["/data"]
ENV DB_PATH=/data/hss.db \
    PALLETS_FILE=/app/pallets.json \
    SUBSCRIBERS_FILE=/app/subscribers.json

# CMD is set per-service in docker-compose
```

**Build context must be `supershi/`** (parent dir) so `COPY py-diameter/` resolves correctly.

### 6. New: `docker-compose.yml`

```yaml
version: "3.9"

services:
  hss:
    build:
      context: ..
      dockerfile: nostr-hss/Dockerfile
    command: python /app/hss.py --port 3868
    ports:
      - "3868:3868"
    volumes:
      - hss-data:/data
    environment:
      HSS_BIND: "0.0.0.0"
      DB_PATH: /data/hss.db
    healthcheck:
      test: ["CMD", "python", "-c", "import socket;s=socket.socket();s.settimeout(2);s.connect(('localhost',3868));s.close()"]
      interval: 10s
      retries: 5
      start_period: 15s

  as:
    build:
      context: ..
      dockerfile: nostr-hss/Dockerfile
    command: python /app/client.py --serve
    environment:
      HSS_HOST: hss
      HSS_PORT: "3868"
      AS_PORT: "3869"
      AS_HTTP_PORT: "8090"
      ORIGIN_REALM: nostr.realm
      DB_PATH: /data/hss.db
    volumes:
      - hss-data:/data
    depends_on:
      hss:
        condition: service_healthy

  api:
    build:
      context: ..
      dockerfile: nostr-hss/Dockerfile
    command: python /app/api.py --port 8080
    ports:
      - "8080:8080"
    volumes:
      - hss-data:/data
    environment:
      AS_URL: http://as:8090
      DB_PATH: /data/hss.db
    depends_on:
      - as

volumes:
  hss-data:
```

**Scale AS with:** `docker compose up --scale as=3`

### 7. `run.py` — minor: use env var defaults for bind IPs (lines 49–50)

```python
parser.add_argument("--hss-bind",  default=os.environ.get("HSS_BIND",  "100.69.131.41"))
parser.add_argument("--as1-bind",  default=os.environ.get("AS1_BIND",  "100.69.131.41"))
```

Local dev continues to work unchanged; Tailscale IPs stay as defaults.

---

## Key Gotcha: Diameter Peer Pre-Registration

The `add_application(hss_app, peers)` call binds which peers can exchange messages with the HSS app.

- **With an empty `peers` list:** Standard Diameter (RFC 3588) accepts inbound CER from any realm peer
- **Start with empty list** — if SNRs fail with routing errors, set `AS_PEERS=as:3869` in the HSS env
- Docker DNS round-robin resolves `as` to a valid replica at startup

**PNR Routing Note:** Uses the `origin_host` captured at SNR time (hss.py line 168). If the originating AS replica dies, that PNR will time out. Acceptable for POC; production fix would be sticky routing or a shared Diameter identity across AS replicas.

---

## Implementation Order

1. ✅ Fix `pallet_watcher.py` bug (line 227)
2. ✅ `db.py` — DB_PATH env var
3. ✅ `hss.py` — HSS_BIND + AS_PEERS env vars + syntax fix
4. ✅ `client.py` — add `--serve` + HTTP SNR gateway
5. ✅ `api.py` — HTTP SNR call via AS_URL
6. ✅ `Dockerfile` — build and verify
7. ✅ `docker-compose.yml` — bring up hss → as → api sequentially
8. ✅ Smoke test: `docker compose up --scale as=2`; POST a subscribe; verify PNR log

---

## Verification

```bash
# Build
cd /home/rayray/supershi/nostr-hss
docker compose build

# Start stack
docker compose up

# Health check
curl http://localhost:8090/health  # AS: {"status":"ok","diameter_ready":true}
curl http://localhost:8080/pallets # API: pallet list

# Subscribe (with valid NIP-42 auth)
curl -X POST http://localhost:8080/subscribe -d '...'

# Scale AS to 3 replicas
docker compose up --scale as=3

# Verify all 3 AS instances appear in HSS logs as connected peers
```

**Local dev (no Docker) still works as before:**

```bash
PYTHONPATH=/home/rayray/supershi/py-diameter/src python run.py
```

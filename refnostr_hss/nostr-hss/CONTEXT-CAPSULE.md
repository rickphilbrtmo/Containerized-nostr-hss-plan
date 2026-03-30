# Nostr-HSS POC — Context Capsule
Updated: 2026-03-19

## Start Everything
cd ~/nostr-hss
PYTHONPATH=~/python-diameter/src ~/python-diameter/Python-3.11.14/python run.py

## Stack
hss.py       — Diameter HSS (port 3868)
client.py    — Diameter AS client (port 3869), DM sender
api.py       — FastAPI REST (port 8080) → Caddy → https://hss.$YURDOMN.xyz
db.py        — SQLite (hss.db), write-through cache
pallet_watcher.py — Nostr relay poller (4h window, not yet live..? isn't it tho?)
client.html  — Browser UI (NIP-07 extension + NIP-46 bunker)
run.py       — Single launcher (HSS + AS + API in one process)
config.json  — service_nsec (null = DM stub mode)

## Live Endpoints
https://hss.$YURDOMN.xyz/           subscriber UI
https://hss.$YURDOMN.xyz/pallets    pallet list
https://hss.$YURDOMN.xyz/challenge  auth nonce
https://hss.$YURDOMN.xyz/subscribe  authenticated subscribe (POST)
https://hss.$YURDOMN.xyz/subscribers admin list
https://hss.$YURDOMN.xyz/history    PNR audit log
https://hss.$YURDOMN.xyz/status/{npub}

## Auth (proven E2E)
NIP-42 challenge-response → BIP-340 Schnorr → verified server-side
Browser: NIP-07 extension (Alby, nos2x) or NIP-46 bunker URI

## DM Sender Identity — PENDING DECISION
- Option A: fresh keypair (recommended)
- Option B: Raycee's personal npub
- NOT option C (Bewauya's AlbyHub npub)
- When decided: put nsec1... in config.json "service_nsec"
- DMs go live immediately, no restart needed

## Pending
1. Set service_nsec in config.json
2. Add real npubs to pallets.json
3. bech32 decode in pallet_watcher for relay filters
4. Full E2E pallet watcher test
5. systemd service for auto-start

## Security
- SYN cookies enabled (sysctl)
- NIP-42 auth: challenge one-time use, 5min TTL, Schnorr sig verified

## Python
~/python-diameter/Python-3.11.14/python
PYTHONPATH=~/python-diameter/src
Deps: fastapi uvicornwebsocket-client requests coincurve cryptography python-diameter

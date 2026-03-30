#!/usr/bin/env python3
"""
Nostr-HSS REST API — authenticated subscription gateway.

Anons prove ownership of their npub via NIP-42 style challenge-response
(Schnorr signature over a server-issued nonce) before the HSS registers
their subscription.

Endpoints:
  GET  /challenge              → {challenge_id, nonce, expires_in}
  POST /subscribe              → register npub against pallet(s)
  POST /unsubscribe            → remove npub from pallet(s)
  GET  /pallets                → list available pallets
  GET  /status/{npub}          → subscriber's current pallet subscriptions

Usage:
  python api.py [--port 8080] [--hss-port 3868]

Auth flow:
  1. GET /challenge  → get nonce
  2. Sign: create Nostr event kind=22242, tags=[["challenge", nonce]],
           content="nostr-hss-auth", sign with your nsec
  3. POST /subscribe with {npub, pallet_ids[], signed_event{...}}
"""
import argparse
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import time
import threading
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# Signature verification
from coincurve import PublicKey
from coincurve.utils import get_valid_secret

logging.basicConfig(
    format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    level=logging.INFO
)
log = logging.getLogger("nostr-hss-api")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PALLETS_FILE = os.path.join(SCRIPT_DIR, "pallets.json")

CHALLENGE_TTL = 300        # seconds (5 min)
CHALLENGE_CLEANUP = 60     # cleanup interval

# In-memory challenge store: {challenge_id: {nonce, expires_at}}
challenges: dict = {}
challenges_lock = threading.Lock()

app = FastAPI(title="Nostr-HSS API", version="0.1.0")


from fastapi.responses import FileResponse as _FileResponse

@app.get("/")
def serve_client():
    return _FileResponse(os.path.join(SCRIPT_DIR, "client.html"))
# Will be set when api.py is started alongside hss_app
_hss_app = None
_as_app = None


# ─────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────

class SignedEvent(BaseModel):
    """A NIP-42-style Nostr event used for auth."""
    id: str           # 32-byte hex event id
    pubkey: str       # 32-byte hex pubkey (x-coord of the npub)
    created_at: int   # unix timestamp
    kind: int         # must be 22242
    tags: list        # must include ["challenge", <nonce>]
    content: str
    sig: str          # 64-byte hex Schnorr signature


class SubscribeRequest(BaseModel):
    npub: str                  # bech32 npub of the subscriber
    pallet_ids: List[str]      # pallets to subscribe to
    signed_event: SignedEvent  # proof of key ownership

    @field_validator("npub")
    @classmethod
    def npub_must_start_with_npub1(cls, v):
        if not v.startswith("npub1"):
            raise ValueError("npub must start with npub1")
        return v


class UnsubscribeRequest(BaseModel):
    npub: str
    pallet_ids: List[str]
    signed_event: SignedEvent


# ─────────────────────────────────────────────
# Bech32 / npub utilities
# ─────────────────────────────────────────────

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

def _bech32_polymod(values):
    GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk

def _bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]

def _bech32_verify_checksum(hrp, data):
    return _bech32_polymod(_bech32_hrp_expand(hrp) + list(data)) == 1

def _convertbits(data, frombits, tobits, pad=True):
    acc = 0; bits = 0; ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = ((acc << frombits) | value) & 0xFFFFFFFF
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret

def npub_to_hex(npub: str) -> str:
    """Convert bech32 npub to 32-byte hex pubkey."""
    npub = npub.lower()
    if not npub.startswith("npub1"):
        raise ValueError("Not an npub")
    data = []
    for c in npub[5:]:
        d = BECH32_CHARSET.find(c)
        if d < 0:
            raise ValueError(f"Invalid bech32 char: {c!r}")
        data.append(d)
    if not _bech32_verify_checksum("npub", data):
        raise ValueError("Invalid npub checksum")
    decoded = _convertbits(data[:-6], 5, 8, False)
    if decoded is None or len(decoded) != 32:
        raise ValueError(f"Invalid npub length: {len(decoded) if decoded else 'None'}")
    return bytes(decoded).hex()


# ─────────────────────────────────────────────
# Nostr event verification (NIP-42 / NIP-01)
# ─────────────────────────────────────────────

def compute_event_id(event: dict) -> str:
    """Compute the Nostr event ID (SHA256 of canonical serialization)."""
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"],
         event["kind"], event["tags"], event["content"]],
        separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def verify_schnorr(pubkey_hex: str, msg_hex: str, sig_hex: str) -> bool:
    """
    Verify a BIP-340 Schnorr signature.
    pubkey_hex: 32-byte x-only pubkey hex
    msg_hex: 32-byte message hex
    sig_hex: 64-byte signature hex
    """
    try:
        # BIP-340: x-only pubkey → compressed 33-byte with 0x02 prefix
        pubkey_bytes = bytes.fromhex(pubkey_hex)
        if len(pubkey_bytes) != 32:
            return False
        compressed = b"\x02" + pubkey_bytes
        pub = PublicKey(compressed)
        msg = bytes.fromhex(msg_hex)
        sig = bytes.fromhex(sig_hex)
        if len(sig) != 64:
            return False
        # coincurve uses DER/compact; for Schnorr we need to use the raw verify
        # coincurve doesn't natively support BIP-340 Schnorr — use hashlib approach
        return _schnorr_verify(pubkey_bytes, msg, sig)
    except Exception as e:
        log.debug(f"Sig verify error: {e}")
        return False


def _schnorr_verify(pubkey_bytes: bytes, msg: bytes, sig: bytes) -> bool:
    """
    Pure-Python BIP-340 Schnorr verification.
    Based on the BIP-340 reference implementation.
    """
    import hashlib

    def tagged_hash(tag: str, data: bytes) -> bytes:
        th = hashlib.sha256(tag.encode()).digest()
        return hashlib.sha256(th + th + data).digest()

    P_FIELD = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
    P_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
    GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

    def point_add(P, Q):
        if P is None: return Q
        if Q is None: return P
        if P[0] == Q[0]:
            if P[1] != Q[1]: return None
            lam = (3 * P[0] * P[0] * pow(2 * P[1], P_FIELD - 2, P_FIELD)) % P_FIELD
        else:
            lam = ((Q[1] - P[1]) * pow(Q[0] - P[0], P_FIELD - 2, P_FIELD)) % P_FIELD
        x = (lam * lam - P[0] - Q[0]) % P_FIELD
        y = (lam * (P[0] - x) - P[1]) % P_FIELD
        return (x, y)

    def point_mul(P, n):
        R = None
        Q = P
        while n:
            if n & 1: R = point_add(R, Q)
            Q = point_add(Q, Q)
            n >>= 1
        return R

    try:
        px = int.from_bytes(pubkey_bytes, "big")
        if px >= P_FIELD: return False
        y_sq = (pow(px, 3, P_FIELD) + 7) % P_FIELD
        y = pow(y_sq, (P_FIELD + 1) // 4, P_FIELD)
        if pow(y, 2, P_FIELD) != y_sq: return False
        if y % 2 != 0: y = P_FIELD - y
        P = (px, y)

        r = int.from_bytes(sig[:32], "big")
        s = int.from_bytes(sig[32:], "big")
        if r >= P_FIELD or s >= P_ORDER: return False

        e_bytes = tagged_hash("BIP0340/challenge",
                              sig[:32] + pubkey_bytes + msg)
        e = int.from_bytes(e_bytes, "big") % P_ORDER

        G = (GX, GY)
        R = point_add(point_mul(G, s), point_mul(P, P_ORDER - e))
        if R is None or R[1] % 2 != 0 or R[0] != r:
            return False
        return True
    except Exception as e:
        log.debug(f"Schnorr math error: {e}")
        return False


def verify_auth_event(event: SignedEvent, expected_nonce: str) -> tuple[bool, str]:
    """
    Verify a NIP-42 auth event.
    Returns (valid: bool, reason: str)
    """
    # Kind must be 22242
    if event.kind != 22242:
        return False, f"wrong kind {event.kind}, expected 22242"

    # Timestamp within ±10 minutes
    now = int(time.time())
    if abs(event.created_at - now) > 600:
        return False, "event timestamp too far from now"

    # Challenge tag must match
    challenge_tag = next((t for t in event.tags if t and t[0] == "challenge"), None)
    if not challenge_tag or len(challenge_tag) < 2:
        return False, "missing challenge tag"
    if challenge_tag[1] != expected_nonce:
        return False, "challenge nonce mismatch"

    # Verify event ID
    ev_dict = {
        "pubkey": event.pubkey,
        "created_at": event.created_at,
        "kind": event.kind,
        "tags": event.tags,
        "content": event.content,
    }
    computed_id = compute_event_id(ev_dict)
    if computed_id != event.id:
        return False, f"event id mismatch (got {event.id[:8]}... expected {computed_id[:8]}...)"

    # Verify Schnorr signature
    if not verify_schnorr(event.pubkey, event.id, event.sig):
        return False, "invalid signature"

    return True, "ok"


# ─────────────────────────────────────────────
# Challenge cleanup
# ─────────────────────────────────────────────

def _cleanup_challenges():
    while True:
        time.sleep(CHALLENGE_CLEANUP)
        now = time.time()
        with challenges_lock:
            expired = [k for k, v in challenges.items() if v["expires_at"] < now]
            for k in expired:
                del challenges[k]
        if expired:
            log.debug(f"Cleaned up {len(expired)} expired challenges")

threading.Thread(target=_cleanup_challenges, daemon=True, name="challenge-cleanup").start()


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.get("/pallets")
def list_pallets():
    """List available pallets."""
    with open(PALLETS_FILE) as f:
        pallets = json.load(f)
    return {
        pid: {
            "name": p["name"],
            "description": p["description"],
            "hashtags": p["hashtags"],
            "hype_threshold": p["hype_threshold"],
        }
        for pid, p in pallets.items()
    }


@app.get("/challenge")
def get_challenge():
    """Issue a one-time challenge nonce for auth."""
    challenge_id = secrets.token_hex(16)
    nonce = secrets.token_hex(32)
    expires_at = time.time() + CHALLENGE_TTL

    with challenges_lock:
        challenges[challenge_id] = {"nonce": nonce, "expires_at": expires_at}

    return {
        "challenge_id": challenge_id,
        "nonce": nonce,
        "expires_in": CHALLENGE_TTL,
        "instructions": (
            "Create a Nostr event: kind=22242, content='nostr-hss-auth', "
            "tags=[['challenge', '<nonce>']], sign with your nsec. "
            "Include the full signed event in POST /subscribe."
        )
    }


def _resolve_challenge(challenge_id: str) -> Optional[str]:
    """Look up and consume a challenge. Returns nonce or None."""
    with challenges_lock:
        ch = challenges.get(challenge_id)
        if not ch:
            return None
        if ch["expires_at"] < time.time():
            del challenges[challenge_id]
            return None
        # Consume it (one-time use)
        del challenges[challenge_id]
        return ch["nonce"]


def _verify_subscription_request(npub: str, signed_event: SignedEvent) -> tuple[bool, str]:
    """
    Verify that:
    1. The signed event's pubkey matches the npub
    2. The challenge in the event is valid (we look it up by nonce directly)
    3. The Schnorr sig is valid
    """
    # Decode npub → hex pubkey
    try:
        pubkey_hex = npub_to_hex(npub)
    except ValueError as e:
        return False, f"invalid npub: {e}"

    # Pubkey in event must match npub
    if signed_event.pubkey.lower() != pubkey_hex.lower():
        return False, f"event pubkey {signed_event.pubkey[:8]}... doesn't match npub"

    # Find challenge by nonce (scan — challenges are short-lived and small)
    challenge_tag = next((t for t in signed_event.tags if t and t[0] == "challenge"), None)
    if not challenge_tag:
        return False, "missing challenge tag in event"
    submitted_nonce = challenge_tag[1] if len(challenge_tag) > 1 else ""

    # Find + consume matching challenge
    matched_id = None
    with challenges_lock:
        for cid, cv in list(challenges.items()):
            if cv["nonce"] == submitted_nonce:
                if cv["expires_at"] < time.time():
                    del challenges[cid]
                    return False, "challenge expired"
                matched_id = cid
                del challenges[cid]  # consume
                break

    if not matched_id:
        return False, "challenge not found or already used"

    # Verify event id + signature
    valid, reason = verify_auth_event(signed_event, submitted_nonce)
    return valid, reason


@app.post("/subscribe")
def subscribe(req: SubscribeRequest):
    """Authenticated subscription: registers npub against one or more pallets."""
    log.info(f"Subscribe request: npub={req.npub[:20]}... pallets={req.pallet_ids}")

    # Validate pallets exist
    with open(PALLETS_FILE) as f:
        pallets = json.load(f)
    unknown = [p for p in req.pallet_ids if p not in pallets]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown pallets: {unknown}")

    # Verify auth
    valid, reason = _verify_subscription_request(req.npub, req.signed_event)
    if not valid:
        log.warning(f"Auth failed for {req.npub[:20]}: {reason}")
        raise HTTPException(status_code=401, detail=f"Auth failed: {reason}")

    log.info(f"Auth OK for {req.npub[:20]}...")

    # Send SNR to HSS
    if _as_app is None:
        raise HTTPException(status_code=503, detail="Diameter AS not connected")

    import sys as _sys; send_snr = _sys.modules["client"].send_snr if "client" in _sys.modules else __import__("client").send_snr
    try:
        send_snr(_as_app, req.npub, req.pallet_ids, subscribe=True)
    except Exception as e:
        log.error(f"SNR failed: {e}")
        raise HTTPException(status_code=500, detail=f"HSS registration failed: {e}")

    return {
        "status": "subscribed",
        "npub": req.npub,
        "pallets": req.pallet_ids,
    }


@app.post("/unsubscribe")
def unsubscribe(req: UnsubscribeRequest):
    """Authenticated unsubscribe."""
    log.info(f"Unsubscribe request: npub={req.npub[:20]}... pallets={req.pallet_ids}")

    valid, reason = _verify_subscription_request(req.npub, req.signed_event)
    if not valid:
        raise HTTPException(status_code=401, detail=f"Auth failed: {reason}")

    if _as_app is None:
        raise HTTPException(status_code=503, detail="Diameter AS not connected")

    import sys as _sys; send_snr = _sys.modules["client"].send_snr if "client" in _sys.modules else __import__("client").send_snr
    try:
        send_snr(_as_app, req.npub, req.pallet_ids, subscribe=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"HSS deregistration failed: {e}")

    return {"status": "unsubscribed", "npub": req.npub, "pallets": req.pallet_ids}


@app.get("/health")
def health():
    return {"status": "ok", "ts": int(time.time())}


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def start_api(as_app_ref, hss_app_ref=None, port: int = 8080):
    """Start the API server with references to the live Diameter apps."""
    global _as_app, _hss_app
    _as_app = as_app_ref
    _hss_app = hss_app_ref
    log.info(f"Starting Nostr-HSS REST API on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


# ─────────────────────────────────────────────
# DB-backed admin/status endpoints
# ─────────────────────────────────────────────

@app.get("/subscribers")
def list_subscribers():
    """List all active subscribers and their pallets (admin view)."""
    import sys, os
    sys.path.insert(0, SCRIPT_DIR)
    import db as _db
    return {"subscribers": _db.get_all_subscribers()}


@app.get("/history")
def pnr_history(npub: str = None, limit: int = 50):
    """Recent PNR (hype alert) history. Optional ?npub= filter."""
    import sys
    sys.path.insert(0, SCRIPT_DIR)
    import db as _db
    return {"events": _db.get_pnr_history(npub=npub, limit=limit)}


@app.get("/status/{npub}")
def subscriber_status(npub: str):
    """Check which pallets an npub is subscribed to (DB-backed)."""
    import sys
    sys.path.insert(0, SCRIPT_DIR)
    import db as _db
    pallets = _db.get_subscriber_pallets(npub)
    return {
        "npub": npub,
        "subscribed": len(pallets) > 0,
        "pallets": pallets,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    # Standalone mode (no live Diameter — useful for testing auth flow only)
    log.warning("Running in standalone mode — Diameter not connected, /subscribe will return 503")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")



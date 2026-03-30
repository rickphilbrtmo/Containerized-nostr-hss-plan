#!/usr/bin/env python3
"""
Pallet Watcher — polls Nostr relays every 4 hours for hype activity
on curated npubs/hashtags per pallet.

Hype score = weighted sum of reactions + zaps on posts by curated npubs,
             plus posts using pallet hashtags, within the last 4 hours.

When score > threshold → calls hss_app.trigger_pnr() for each pallet.

Usage:
  Typically imported and run as a thread from hss.py,
  or: python pallet_watcher.py --once  (single scan, prints JSON result)
"""
import json
import logging
import time
import argparse
import os
import sys
import threading
from datetime import datetime, timezone

import websocket

log = logging.getLogger("pallet-watcher")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PALLETS_FILE = os.path.join(SCRIPT_DIR, "pallets.json")

RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
]

HYPE_WINDOW_SECONDS = 4 * 60 * 60  # 4 hours
POLL_INTERVAL_SECONDS = 1 * 60 * 60

# Hype score weights
W_REACTION = 1      # Kind 7 (reaction/like)
W_ZAP = 5           # Kind 9735 (zap receipt)
W_REPLY = 2         # Kind 1 reply
W_REPOST = 3        # Kind 6 (repost/boost)

# ── bech32 / npub → hex (no external deps) ────────────────────────────────
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

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
        d = _BECH32_CHARSET.find(c)
        if d < 0:
            raise ValueError(f"Invalid bech32 char: {c!r}")
        data.append(d)
    if not _bech32_verify_checksum("npub", data):
        raise ValueError("Invalid npub checksum")
    decoded = _convertbits(data[:-6], 5, 8, False)
    if decoded is None or len(decoded) != 32:
        raise ValueError(f"Invalid npub length: {len(decoded) if decoded else 'None'}")
    return bytes(decoded).hex()
# ──────────────────────────────────────────────────────────────────────────


def nostr_req(relay_url: str, filters: list, timeout: float = 10.0) -> list:
    """
    Send a REQ to a Nostr relay and collect events until EOSE or timeout.
    Returns list of event dicts.
    """
    events = []
    done = threading.Event()

    def on_message(ws, message):
        try:
            msg = json.loads(message)
            if msg[0] == "EVENT":
                events.append(msg[2])
            elif msg[0] == "EOSE":
                done.set()
                ws.close()
        except Exception:
            pass

    def on_error(ws, error):
        done.set()

    def on_close(ws, *args):
        done.set()

    def on_open(ws):
        req = json.dumps(["REQ", "sub1"] + filters)
        ws.send(req)

    ws = websocket.WebSocketApp(
        relay_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    done.wait(timeout=timeout)
    ws.close()
    return events


def fetch_events_for_pallet(pallet: dict, since: int) -> list:
    """Fetch recent events related to a pallet from multiple relays."""
    npubs_hex = []
    for npub in pallet.get("npubs", []):
        try:
            npubs_hex.append(npub_to_hex(npub))
        except ValueError as e:
            log.warning(f"Skipping invalid npub {npub[:20]}: {e}")

    hashtags = pallet.get("hashtags", [])
    all_events = []

    filters = []
    if npubs_hex:
        # Posts by curated authors
        filters.append({"authors": npubs_hex, "kinds": [1], "since": since, "limit": 200})
        # Reactions/zaps targeting those authors' posts
        filters.append({"kinds": [7, 9735, 6], "since": since, "limit": 500})

    if hashtags:
        # Posts with pallet hashtags
        filters.append({"kinds": [1], "#t": hashtags, "since": since, "limit": 200})

    for relay in RELAYS:
        try:
            evs = nostr_req(relay, filters, timeout=8.0)
            all_events.extend(evs)
            log.debug(f"  {relay}: {len(evs)} events")
        except Exception as e:
            log.warning(f"Relay {relay} error: {e}")

    return all_events


def score_events(events: list, curated_npubs: set, pallet_hashtags: set) -> tuple:
    """
    Score the event list for hype.
    Returns (score, top_event_id, top_author_npub)
    """
    # Count engagements per note
    note_scores = {}   # note_id → score
    note_authors = {}  # note_id → author pubkey

    # First pass: index original posts
    for ev in events:
        kind = ev.get("kind")
        author = ev.get("pubkey", "")
        ev_id = ev.get("id", "")
        if kind == 1:
            tags = {t[0]: t[1] for t in ev.get("tags", []) if len(t) >= 2}
            ev_hashtags = {t[1].lower() for t in ev.get("tags", []) if t[0] == "t"}
            # Only count posts by curated authors or with pallet hashtags
            if author in curated_npubs or bool(ev_hashtags & pallet_hashtags):
                if ev_id not in note_scores:
                    note_scores[ev_id] = 0
                    note_authors[ev_id] = author

    # Second pass: score reactions/zaps/reposts
    for ev in events:
        kind = ev.get("kind")
        e_tags = [t[1] for t in ev.get("tags", []) if t[0] == "e" and len(t) >= 2]

        if kind == 7:  # Reaction
            for ref_id in e_tags:
                if ref_id in note_scores:
                    note_scores[ref_id] += W_REACTION
        elif kind == 9735:  # Zap receipt
            for ref_id in e_tags:
                if ref_id in note_scores:
                    note_scores[ref_id] += W_ZAP
        elif kind == 6:  # Repost
            for ref_id in e_tags:
                if ref_id in note_scores:
                    note_scores[ref_id] += W_REPOST
        elif kind == 1:  # Reply
            ev_id = ev.get("id", "")
            author = ev.get("pubkey", "")
            # Check if this is a reply to a tracked note
            for ref_id in e_tags:
                if ref_id in note_scores:
                    note_scores[ref_id] += W_REPLY

    total_score = sum(note_scores.values())

    # Top note
    top_note_id = ""
    top_note_author = ""
    if note_scores:
        top_note_id = max(note_scores, key=note_scores.get)
        top_note_author = note_authors.get(top_note_id, "")

    return total_score, top_note_id, top_note_author


def run_scan(hss_app_ref=None) -> dict:
    """
    Run a full hype scan across all pallets.
    If hss_app_ref is provided, calls trigger_pnr for breached pallets.
    Returns scan results dict.
    """
    with open(PALLETS_FILE) as f:
        pallets = json.load(f)

    since = int(time.time()) - HYPE_WINDOW_SECONDS
    results = {}

    log.info(f"Starting pallet scan (since {datetime.fromtimestamp(since, tz=timezone.utc).isoformat()})")

    for pallet_id, pallet in pallets.items():
        log.info(f"Scanning pallet: {pallet['name']}")
        events = fetch_events_for_pallet(pallet, since)

        curated_npubs = set(pallet.get("npubs", []))
        hashtags = set(h.lower() for h in pallet.get("hashtags", []))

        score, top_note_id, top_note_author = score_events(events, curated_npubs, hashtags)
        threshold = pallet.get("hype_threshold", 20)

        result = {
            "pallet_id": pallet_id,
            "pallet_name": pallet["name"],
            "hype_score": score,
            "threshold": threshold,
            "top_note_id": top_note_id,
            "top_author_name": top_note_author[:16] if top_note_author else "",
            "events_fetched": len(events),
            "hype_triggered": score >= threshold,
            "scanned_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        results[pallet_id] = result

        log.info(
            f"  {pallet['name']}: score={score} threshold={threshold} "
            f"triggered={'YES 🔥' if result['hype_triggered'] else 'no'}"
        )

        if result["hype_triggered"] and hss_app_ref is not None:
            hss_app_ref.trigger_pnr(pallet_id, result)

    return results


def run_loop(hss_app_ref):
    """Continuous polling loop — runs forever, scanning every 4h."""
    log.info(f"Pallet watcher loop started (interval={POLL_INTERVAL_SECONDS}s)")
    while True:
        try:
            run_scan(hss_app_ref)
        except Exception as e:
            log.error(f"Scan error: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
        level=logging.INFO
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one scan and print JSON results")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS, help="Poll interval seconds")
    args = parser.parse_args()

    POLL_INTERVAL_SECONDS = args.interval

    if args.once:
        results = run_scan()
        print(json.dumps(results, indent=2))
    else:
        run_loop(None)


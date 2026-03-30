#!/usr/bin/env python3
"""
run.py — Nostr-HSS launcher.

Starts all three components in one process:
  1. HSS Diameter node (hss.py)        — port 3868
  2. AS1 client Diameter node (client.py) — port 3869
  3. REST API (api.py)                  — port 8080

The AS app reference is injected into the API so /subscribe
can call send_snr() directly without any IPC.
"""
import argparse
import logging
import os
import sys
import threading
import time

logging.basicConfig(
    format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
    level=logging.INFO
)
log = logging.getLogger("nostr-hss-runner")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from diameter.message.constants import VENDOR_TGPP
from diameter.node import Node
from diameter.node.peer import PEER_READY, PEER_READY_WAITING_DWA

import db as _db
import hss as hss_mod
import client as client_mod
import api as api_mod


def peer_is_ready(peer):
    return (peer.connection is not None and
            peer.connection.state in (PEER_READY, PEER_READY_WAITING_DWA))


def main():
    parser = argparse.ArgumentParser(description="Nostr-HSS all-in-one launcher")
    parser.add_argument("--hss-port",   type=int, default=3868)
    parser.add_argument("--as-port",    type=int, default=3869)
    parser.add_argument("--api-port",   type=int, default=8080)
    parser.add_argument("--hss-bind",   default="100.69.131.41", help="HSS bind address (Tailscale)")
    parser.add_argument("--as1-bind",   default="100.69.131.41", help="AS1 bind address (Tailscale)")
    parser.add_argument("--no-watcher", action="store_true", help="Skip pallet watcher")
    args = parser.parse_args()

    # ── 1. Init DB ──────────────────────────────────────────────────────────
    _db.init_db()
    _db.load_into_memory(hss_mod.subscriptions)

    # ── 2. Start HSS node ───────────────────────────────────────────────────
    hss_node = Node(
        "hss.nostr.realm", "nostr.realm",
        ip_addresses=[args.hss_bind],
        tcp_port=args.hss_port,
        vendor_ids=[VENDOR_TGPP]
    )
    hss_node.idle_timeout = 30

    # Register AS1 (local, Tailscale) and ALTAS (devbuntu2504) as known peers
    as1_peer_on_hss   = hss_node.add_peer(f"aaa://as1.nostr.realm:{args.as_port}")
    altas_peer_on_hss = hss_node.add_peer(
        "aaa://altas.nostr.realm:3869",
        ip_addresses=["100.126.145.112"]
    )
    hss_app = hss_mod.HssApplication()
    hss_node.add_application(hss_app, [as1_peer_on_hss, altas_peer_on_hss])

    log.info(f"Starting HSS on {args.hss_bind}:{args.hss_port}")
    hss_node.start()

    # ── 3. Start AS1 (client) node ──────────────────────────────────────────
    as_node = Node(
        "as1.nostr.realm", "nostr.realm",
        ip_addresses=[args.as1_bind],
        tcp_port=args.as_port,
        vendor_ids=[VENDOR_TGPP]
    )
    as_node.idle_timeout = 30

    as_app = client_mod.AsApplication(origin_host="as1.nostr.realm")
    hss_peer = as_node.add_peer(
        f"aaa://hss.nostr.realm:{args.hss_port}",
        ip_addresses=[args.hss_bind],
        is_persistent=True
    )
    hss_peer.reconnect_wait = 5
    as_node.add_application(as_app, [hss_peer])

    log.info(f"Starting AS1 client on {args.as1_bind}:{args.as_port}")
    as_node.start()

    # ── 4. Wait for Diameter peer handshake ─────────────────────────────────
    log.info("Waiting for HSS ↔ AS1 peer connection...")
    for _ in range(30):
        if peer_is_ready(hss_peer):
            break
        time.sleep(0.3)
    else:
        log.error("Diameter peer did not connect — check ports")
        hss_node.stop()
        as_node.stop()
        sys.exit(1)

    log.info("✓ Diameter peer connected (CER/CEA complete)")

    # ── 4b. Re-subscribe all DB subscribers so origin_host/realm are live ───
    restored = 0
    for npub, sub_data in list(hss_mod.subscriptions.items()):
        pallets = list(sub_data.get("pallets", set()))
        if pallets:
            try:
                client_mod.send_snr(as_app, npub, pallets, subscribe=True)
                restored += 1
            except Exception as e:
                log.warning(f"Re-subscribe failed for {npub[:20]}: {e}")
    if restored:
        log.info(f"Re-subscribed {restored} subscriber(s) from DB via SNR")

    # ── 5. Inject as_app into REST API ──────────────────────────────────────
    api_mod._as_app  = as_app
    api_mod._hss_app = hss_app

    # Patch send_snr import inside api.py's subscribe handler
    sys.modules["client"] = client_mod

    # ── 6. Start pallet watcher ─────────────────────────────────────────────
    if not args.no_watcher:
        hss_mod.start_pallet_watcher(hss_app)

    # ── 7. Start REST API (blocks — runs uvicorn in main thread) ─────────────
    log.info(f"Starting REST API on 0.0.0.0:{args.api_port}")
    try:
        import uvicorn
        uvicorn.run(api_mod.app, host="0.0.0.0", port=args.api_port, log_level="info")
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        log.info("Shutting down...")
        as_node.stop()
        hss_node.stop()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Nostr-HSS Client / AS (Application Server)
- Connects to HSS via Diameter Sh
- Sends SNR to subscribe npubs to pallets
- Receives PNR → sends Nostr DM to subscriber npub

Usage (standalone):
  python client.py --npub <npub> --pallets health_wellness,bitcoin_tech
  python client.py --from-file   # register all from subscribers.json

  Optional overrides:
    --origin-host   (default: as1.nostr.realm)
    --origin-realm  (default: nostr.realm)
    --bind          (default: 127.0.0.1)
    --hss-host      (default: 127.0.0.1)
    --hss-port      (default: 3868)
    --port          (default: 3869)
"""
import json
import logging
import sys
import time
import os
import argparse

from diameter.message.constants import *
from diameter.message.commands.subscribe_notifications import SubscribeNotificationsRequest
from diameter.message.commands.push_notification import (
    PushNotificationRequest, PushNotificationAnswer
)
from diameter.message.avp.grouped import UserIdentity, VendorSpecificApplicationId
from diameter.node import Node
from diameter.node.application import ThreadingApplication
from diameter.node.peer import PEER_READY, PEER_READY_WAITING_DWA

logging.basicConfig(
    format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
    level=logging.INFO
)
log = logging.getLogger("nostr-hss-client")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBSCRIBERS_FILE = os.path.join(SCRIPT_DIR, "subscribers.json")


def send_nostr_dm(recipient_npub: str, message: str):
    """Send a Nostr DM. POC: logs only. Replace with real NIP-04 DM."""
    log.info(f"[NOSTR DM] → {recipient_npub[:20]}...\n  {message[:200]}")


def _make_vsai():
    vsai = VendorSpecificApplicationId()
    vsai.vendor_id = VENDOR_TGPP
    vsai.auth_application_id = 16777217
    return vsai


class AsApplication(ThreadingApplication):
    """Application Server — receives PNR, dispatches DMs, sends PNA."""

    def __init__(self, origin_host: str = "as1.nostr.realm", origin_realm: str = "nostr.realm"):
        super().__init__(16777217, is_auth_application=True)
        self.origin_host_str  = origin_host
        self.origin_realm_str = origin_realm
        self.origin_host_b    = origin_host.encode()
        self.origin_realm_b   = origin_realm.encode()
        log.info(f"AS identity: {origin_host} / {origin_realm}")

    def handle_request(self, msg):
        if msg.header.command_code == 309:
            return self._handle_pnr(msg)
        return None

    def _handle_pnr(self, req):
        """Handle Push-Notification-Request from HSS."""
        try:
            npub = req.user_identity.public_identity if req.user_identity else None
            raw  = req.user_data

            log.info(f"PNR received for npub={npub} (via {self.origin_host_str})")

            try:
                hype = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except Exception:
                hype = {"raw": str(raw)}

            pallet_name = hype.get("pallet_name", hype.get("pallet_id", "your pallet"))
            score       = hype.get("hype_score", "?")
            top_note    = hype.get("top_note_id", "")
            top_author  = hype.get("top_author_name", "")

            dm = (f"🔥 Hype alert on \"{pallet_name}\"!\n"
                  f"Buzz score: {score} in the last 4 hours.\n")
            if top_note:
                dm += f"Top note: https://primal.net/e/{top_note}\n"
            if top_author:
                dm += f"Buzzing from: @{top_author}"

            if npub:
                send_nostr_dm(npub, dm)

            pna = self.generate_answer(req)
            pna.vendor_specific_application_id  = _make_vsai()
            pna.auth_session_state              = 1
            pna.origin_host                     = self.origin_host_b
            pna.origin_realm                    = self.origin_realm_b
            pna.result_code                     = E_RESULT_CODE_DIAMETER_SUCCESS
            return pna

        except Exception as e:
            log.error(f"PNR handling error: {e}", exc_info=True)
            pna = self.generate_answer(req)
            pna.origin_host  = self.origin_host_b
            pna.origin_realm = self.origin_realm_b
            pna.result_code  = E_RESULT_CODE_DIAMETER_UNABLE_TO_COMPLY
            return pna


def peer_is_ready(peer):
    return (peer.connection is not None and
            peer.connection.state in (PEER_READY, PEER_READY_WAITING_DWA))


def send_snr(app, npub: str, pallet_ids: list, subscribe: bool = True):
    """Send an SNR from the AS application to the HSS."""
    snr = SubscribeNotificationsRequest()
    snr.session_id                     = f"{app.origin_host_str};{int(time.time())};{abs(hash(npub)) & 0xFFFF}"
    snr.vendor_specific_application_id = _make_vsai()
    snr.auth_session_state             = 1
    snr.origin_host                    = app.origin_host_b
    snr.origin_realm                   = app.origin_realm_b
    snr.destination_realm              = b"nostr.realm"
    snr.destination_host               = b"hss.nostr.realm"

    ui = UserIdentity()
    ui.public_identity = npub
    snr.user_identity  = ui
    snr.service_indication = [p.encode() if isinstance(p, str) else p for p in pallet_ids]
    snr.subs_req_type  = 0 if subscribe else 1

    log.info(f"Sending SNR: {npub[:20]}... pallets={pallet_ids} subscribe={subscribe}")
    try:
        ans = app.send_request(snr, timeout=10)
        if ans:
            log.info(f"SNA result: {getattr(ans, 'result_code', '?')}")
        else:
            log.warning("SNR timed out — no SNA received")
    except Exception as e:
        log.error(f"SNR send error: {e}", exc_info=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--npub")
    parser.add_argument("--pallets")
    parser.add_argument("--from-file",    action="store_true")
    parser.add_argument("--origin-host",  default="as1.nostr.realm")
    parser.add_argument("--origin-realm", default="nostr.realm")
    parser.add_argument("--bind",         default="127.0.0.1")
    parser.add_argument("--hss-host",     default="127.0.0.1")
    parser.add_argument("--hss-port",     type=int, default=3868)
    parser.add_argument("--port",         type=int, default=3869)
    args = parser.parse_args()

    node = Node(
        args.origin_host, args.origin_realm,
        ip_addresses=[args.bind],
        tcp_port=args.port,
        vendor_ids=[VENDOR_TGPP]
    )
    node.idle_timeout = 30

    app = AsApplication(origin_host=args.origin_host, origin_realm=args.origin_realm)
    hss_peer = node.add_peer(
        f"aaa://hss.nostr.realm:{args.hss_port}",
        ip_addresses=[args.hss_host],
        is_persistent=True
    )
    hss_peer.reconnect_wait = 5
    node.add_application(app, [hss_peer])

    log.info(f"Starting {args.origin_host} on {args.bind}:{args.port} → HSS {args.hss_host}:{args.hss_port}")
    node.start()

    log.info("Waiting for Diameter peer (HSS)...")
    for _ in range(30):
        if peer_is_ready(hss_peer):
            break
        time.sleep(0.5)
    else:
        log.error("Could not reach HSS — is hss.py running?")
        node.stop()
        sys.exit(1)

    log.info("Connected to HSS. Registering subscriptions...")

    subs = []
    if args.from_file:
        with open(SUBSCRIBERS_FILE) as f:
            data = json.load(f)
        for sid, sdata in data.items():
            if sdata.get("active"):
                subs.append((sdata["npub"], sdata["pallets"]))
    elif args.npub and args.pallets:
        subs.append((args.npub, args.pallets.split(",")))
    else:
        log.error("Use --npub + --pallets or --from-file")
        node.stop()
        sys.exit(1)

    for npub, pallets in subs:
        send_snr(app, npub, pallets, subscribe=True)
        time.sleep(0.2)

    log.info(f"Registered {len(subs)} subscriber(s). Listening for PNRs on {args.origin_host}...")

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        log.info(f"Shutting down {args.origin_host}")
        node.stop()


if __name__ == "__main__":
    main()

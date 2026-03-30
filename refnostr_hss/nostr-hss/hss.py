#!/usr/bin/env python3
"""
Nostr-HSS: Home Subscriber Server (Diameter Sh interface)
Stores subscriber records, handles SNR (Subscribe-Notifications-Request),
fires PNR (Push-Notification-Request) when hype threshold is exceeded.

Usage:
  python hss.py [--port 3868]
"""
import json
import logging
import threading
import time
import sys
import os
import argparse

from diameter.message.constants import *
from diameter.message.commands.subscribe_notifications import (
    SubscribeNotificationsRequest, SubscribeNotificationsAnswer
)
from diameter.message.commands.push_notification import (
    PushNotificationRequest, PushNotificationAnswer
)
from diameter.message.avp.grouped import UserIdentity, VendorSpecificApplicationId
from diameter.node import Node
from diameter.node.application import ThreadingApplication

import db as _db

logging.basicConfig(
    format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
    level=logging.INFO
)
log = logging.getLogger("nostr-hss")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PALLETS_FILE = os.path.join(SCRIPT_DIR, "pallets.json")
SUBSCRIBERS_FILE = os.path.join(SCRIPT_DIR, "subscribers.json")

# In-memory subscription table keyed by npub
# {npub: {pallets: set, threshold: int|None, session_id: str, origin_host, origin_realm}}
subscriptions = {}
subscriptions_lock = threading.Lock()


def load_data():
    with open(PALLETS_FILE) as f:
        pallets = json.load(f)
    with open(SUBSCRIBERS_FILE) as f:
        subscribers = json.load(f)
    return pallets, subscribers


def _make_vsai():
    vsai = VendorSpecificApplicationId()
    vsai.vendor_id = VENDOR_TGPP
    vsai.auth_application_id = 16777217
    return vsai


class HssApplication(ThreadingApplication):
    """HSS Sh application — handles SNR, sends PNR."""

    def __init__(self):
        super().__init__(16777217, is_auth_application=True)
        self.pallets, self.initial_subscribers = load_data()

    def handle_request(self, msg):
        """Dispatch incoming requests by command code."""
        cmd = msg.header.command_code
        if cmd == 308:
            return self._handle_snr(msg)
        else:
            log.warning(f"Unhandled command code: {cmd}")
            return None

    def _handle_snr(self, req):
        """Handle Subscribe-Notifications-Request."""
        try:
            npub = req.user_identity.public_identity if req.user_identity else None
            pallet_ids = []
            for si in (req.service_indication or []):
                pallet_ids.append(si.decode() if isinstance(si, bytes) else si)
            subs_req_type = req.subs_req_type  # 0=Subscribe 1=Unsubscribe

            log.info(f"SNR: npub={npub} pallets={pallet_ids} type={subs_req_type}")

            ans = self.generate_answer(req)
            ans.vendor_specific_application_id = _make_vsai()

            if not npub or not pallet_ids:
                ans.result_code = E_RESULT_CODE_DIAMETER_MISSING_AVP
                return ans

            with subscriptions_lock:
                if subs_req_type == 0:  # Subscribe
                    threshold = None
                    for sid, sdata in self.initial_subscribers.items():
                        if sdata["npub"] == npub:
                            threshold = sdata.get("threshold_override")
                            break
                    if npub not in subscriptions:
                        subscriptions[npub] = {
                            "npub": npub,
                            "pallets": set(),
                            "threshold": threshold,
                            "session_id": req.session_id,
                            "origin_host": req.origin_host,
                            "origin_realm": req.origin_realm,
                        }
                    else:
                        # Refresh session routing info on reconnect
                        subscriptions[npub]["session_id"] = req.session_id
                        subscriptions[npub]["origin_host"] = req.origin_host
                        subscriptions[npub]["origin_realm"] = req.origin_realm
                    subscriptions[npub]["pallets"].update(pallet_ids)
                    log.info(f"Subscribed: {npub} → {subscriptions[npub]['pallets']}")
                    for pid in pallet_ids:
                        _db.add_subscription(npub, pid)
                    _db.upsert_subscriber(npub, req.origin_host, req.origin_realm)

                elif subs_req_type == 1:  # Unsubscribe
                    if npub in subscriptions:
                        for pid in pallet_ids:
                            subscriptions[npub]["pallets"].discard(pid)
                            _db.remove_subscription(npub, pid)
                        if not subscriptions[npub]["pallets"]:
                            del subscriptions[npub]
                    log.info(f"Unsubscribed: {npub} from {pallet_ids}")

            ans.result_code = E_RESULT_CODE_DIAMETER_SUCCESS
            return ans

        except Exception as e:
            log.error(f"SNR handling error: {e}", exc_info=True)
            ans = self.generate_answer(req)
            ans.vendor_specific_application_id = _make_vsai()
            ans.result_code = E_RESULT_CODE_DIAMETER_UNABLE_TO_COMPLY
            return ans

    def trigger_pnr(self, pallet_id: str, hype_data: dict):
        """
        Called by pallet_watcher when hype threshold exceeded.
        Sends PNR to all subscribers of the given pallet.
        """
        with subscriptions_lock:
            targets = {k: dict(v) for k, v in subscriptions.items()
                       if pallet_id in v.get("pallets", set())}

        if not targets:
            log.info(f"No active subscribers for pallet {pallet_id}")
            return

        payload = json.dumps(hype_data).encode()

        for sub_npub, sub_data in targets.items():
            threshold = sub_data.get("threshold")
            if threshold is None:
                threshold = self.pallets.get(pallet_id, {}).get("hype_threshold", 20)

            if hype_data.get("hype_score", 0) < threshold:
                log.info(f"Score {hype_data['hype_score']} < threshold {threshold} for {sub_npub[:20]}, skip")
                continue

            if not sub_data.get("origin_host") or not sub_data.get("origin_realm"):
                log.warning(f"Skipping PNR for {sub_npub[:20]}: no active Diameter session")
                continue

            log.info(f"Firing PNR → {sub_npub[:20]}... (pallet={pallet_id})")
            try:
                pnr = PushNotificationRequest()
                pnr.session_id = f"hss.nostr.realm;{int(time.time())};{abs(hash(sub_npub)) & 0xFFFF}"
                pnr.vendor_specific_application_id = _make_vsai()
                pnr.auth_session_state = 1
                pnr.origin_host = b"hss.nostr.realm"
                pnr.origin_realm = b"nostr.realm"
                pnr.destination_host = sub_data["origin_host"]
                pnr.destination_realm = sub_data["origin_realm"]
                ui = UserIdentity()
                ui.public_identity = sub_npub
                pnr.user_identity = ui
                pnr.user_data = payload

                ans = self.send_request(pnr, timeout=10)
                if ans:
                    log.info(f"PNA received from {sub_npub[:20]}: result={getattr(ans, 'result_code', '?')}")
                else:
                    log.warning(f"PNR to {sub_npub[:20]} timed out")
            except Exception as e:
                log.error(f"PNR error for {sub_npub[:20]}: {e}", exc_info=True)


def start_pallet_watcher(app):
    """Launch pallet watcher as a background thread."""
    import importlib.util
    watcher_path = os.path.join(SCRIPT_DIR, "pallet_watcher.py")
    spec = importlib.util.spec_from_file_location("pallet_watcher", watcher_path)
    pw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pw)
    t = threading.Thread(target=pw.run_loop, args=(app,), daemon=True, name="pallet-watcher")
    t.start()
    log.info("Pallet watcher thread started")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3868)
    parser.add_argument("--api-port", type=int, default=8080)
    parser.add_argument("--no-watcher", action="store_true", help="Skip pallet watcher thread")
    parser.add_argument("--no-api", action="store_true", help="Skip REST API")
    args = parser.parse_args()

    node = Node(
        "hss.nostr.realm", "nostr.realm",
        ip_addresses=["127.0.0.1"],
        tcp_port=args.port,
        vendor_ids=[VENDOR_TGPP]
    )
    node.idle_timeout = 30

    # Pre-register the AS client as a known peer (no IP = accept inbound only)
    as_peer = node.add_peer("aaa://as.nostr.realm:3869")

    hss_app = HssApplication()
    node.add_application(hss_app, [as_peer])

    _db.init_db()
    _db.load_into_memory(subscriptions)

    log.info(f"Starting Nostr-HSS on 127.0.0.1:{args.port}")
    node.start()

    if not args.no_watcher:
        threading.Timer(2.0, start_pallet_watcher, args=(hss_app,)).start()

    # Start REST API in background (imports client's AS app when available)
    if not args.no_api:
        def _start_api():
            import importlib.util, sys
            api_path = os.path.join(SCRIPT_DIR, "api.py")
            spec = importlib.util.spec_from_file_location("api", api_path)
            api_mod = importlib.util.module_from_spec(spec)
            sys.modules["api"] = api_mod
            spec.loader.exec_module(api_mod)
            api_mod.start_api(as_app_ref=None, hss_app_ref=hss_app, port=args.api_port)
        threading.Thread(target=_start_api, daemon=True, name=rest-api).start()
        log.info(f"REST API will start on port {args.api_port}")

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down HSS")
        node.stop()


if __name__ == "__main__":
    main()

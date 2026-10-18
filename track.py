#!/usr/bin/env python3
"""Poll Apple Store in-store pickup availability and push a phone alert when it opens up.

Modes:
  poll         check every interval_seconds (default; waits for config "start")
  once         one check, print a summary, exit
  diagnose     one check with raw details + what the product page advertises
  test-notify  send a test notification, then one check
"""
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
PT = ZoneInfo("America/Los_Angeles")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
FULFILLMENT_URL = "https://www.apple.com/shop/fulfillment-messages"
WORKFLOW_FILE = "track.yml"
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(msg):
    print(f"[{datetime.now(PT):%m-%d %H:%M:%S %Z}] {msg}", flush=True)


def http(url, data=None, headers=None, method=None, timeout=20):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


# --- Apple -------------------------------------------------------------------

def fetch_stores(cfg, location):
    q = [("pl", "true"), ("mts.0", "regular"), ("mts.1", "compact"),
         ("searchNearby", "true"), ("location", location)]
    q += [(f"parts.{i}", p) for i, p in enumerate(cfg["parts"])]
    q += list(cfg.get("extra_params", {}).items())
    body = http(f"{FULFILLMENT_URL}?{urllib.parse.urlencode(q)}",
                headers={"Accept": "application/json", "Referer": cfg["product_url"]})
    return json.loads(body)["body"]["content"]["pickupMessage"].get("stores", [])


def check(cfg):
    """Return {wanted store: {part: partsAvailability entry} or None if not found}."""
    seen = {}
    for loc in cfg["search_locations"]:
        for s in fetch_stores(cfg, loc):
            seen.setdefault(s.get("storeName", ""), s)
    results = {}
    for want in cfg["stores"]:
        store = next((s for name, s in seen.items() if want.lower() in name.lower()), None)
        if store is None:
            results[want] = None
        else:
            parts = store.get("partsAvailability", {})
            results[want] = {p: parts.get(p, {}) for p in cfg["parts"]}
    return results, seen


def is_available(info):
    return info.get("pickupDisplay") == "available"


def summarize(results):
    out = []
    for store, parts in results.items():
        if parts is None:
            out.append(f"{store}: not in search results")
            continue
        for part, info in parts.items():
            status = info.get("pickupDisplay", "no data")
            quote = strip_html(info.get("pickupSearchQuote", ""))
            out.append(f"{store}: {status}" + (f" ({quote})" if quote else ""))
    return " | ".join(out)


# --- Notifications -------------------------------------------------------------

def notify(cfg, title, message, priority="default", tags=""):
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    if not topic:
        log(f"(no ntfy topic) {title}: {message}")
        return
    try:
        http(f"https://ntfy.sh/{topic}", data=message.encode(), method="POST",
             headers={"Title": title, "Priority": priority, "Tags": tags,
                      "Click": cfg["product_url"]})
        log(f"notified: {title}")
    except Exception as e:
        log(f"ntfy failed: {e}")


def github_api(path, payload=None, method="GET"):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return None
    data = json.dumps(payload).encode() if payload is not None else None
    body = http(f"https://api.github.com/repos/{repo}/{path}", data=data, method=method,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"})
    return json.loads(body) if body else {}


def open_issue(cfg, stores):
    """Backup alert: an issue that @mentions the repo owner reaches GitHub email/app."""
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    try:
        github_api("issues", method="POST", payload={
            "title": f"Pickup available: {', '.join(sorted(stores))}",
            "body": (f"@{owner} {cfg['product_name']} shows in-store pickup at "
                     f"{', '.join(sorted(stores))}.\n\n{cfg['product_url']}\n\n"
                     f"Select: {cfg['checkout_reminder']}"),
        })
    except Exception as e:
        log(f"issue creation failed: {e}")


# --- GitHub Actions run coordination ----------------------------------------------
# Scheduled runs act as a watchdog; one long-running run polls and hands off to the next.

def older_tracker_running(handoff_from):
    me = int(os.environ["GITHUB_RUN_NUMBER"])
    runs = github_api(f"actions/workflows/{WORKFLOW_FILE}/runs?status=in_progress&per_page=50")
    return any(r["run_number"] < me and str(r["id"]) != handoff_from
               for r in runs.get("workflow_runs", []))


def should_exit(handoff_from, retries=0):
    """True if an older tracker is still active (re-checked so a finishing run doesn't count)."""
    for attempt in range(retries + 1):
        try:
            if not older_tracker_running(handoff_from):
                return False
        except Exception as e:
            log(f"run check failed: {e}")
            return False
        if attempt < retries:
            time.sleep(30)
    return True


def dispatch_next_run():
    try:
        github_api(f"actions/workflows/{WORKFLOW_FILE}/dispatches", method="POST", payload={
            "ref": os.environ.get("GITHUB_REF_NAME", "main"),
            "inputs": {"mode": "poll", "handoff_from": os.environ["GITHUB_RUN_ID"]},
        })
        log("dispatched next run")
    except Exception as e:
        log(f"dispatch failed (scheduled watchdog will restart): {e}")


# --- Modes ---------------------------------------------------------------------

def diagnose(cfg):
    try:
        page = http(cfg["product_url"])
        title = re.search(r"<title>(.*?)</title>", page, re.S)
        title = strip_html(title.group(1)) if title else "?"
        skus = sorted(set(re.findall(r'"sku"\s*:\s*"([^"]+)"', page)))
        parts = sorted(set(re.findall(r"\b[A-Z0-9]{5}LL/A\b", page)))
        refs = sorted(set(re.findall(r"fulfillment-messages[^\"'\s<]{0,200}", page)))
        log(f"product page: {len(page)} bytes, title={title}")
        log(f"skus on page: {skus[:30]}")
        log(f"part numbers on page: {parts[:60]}")
        log(f"fulfillment refs: {refs[:10]}")
        for m in re.finditer(r"[Cc]heck back[^<\"]{0,80}", page):
            log(f"page text: {m.group(0)}")
    except Exception as e:
        log(f"product page fetch failed: {e}")
    results, seen = check(cfg)
    log(f"stores returned: {sorted(seen)}")
    for store, parts in results.items():
        if parts is None:
            log(f"{store}: NOT FOUND")
            continue
        for part, info in parts.items():
            keep = {k: info.get(k) for k in ("pickupDisplay", "pickupSearchQuote", "storePickEligible",
                                             "purchaseOption", "buyability")}
            msg = info.get("messageTypes", {}).get("regular", {})
            keep["product"] = msg.get("storePickupProductTitle")
            keep["quote"] = msg.get("storePickupQuote")
            log(f"{store} [{seen_number(seen, store)}] {part}: {json.dumps(keep)}")
    log(summarize(results))


def seen_number(seen, want):
    return next((s.get("storeNumber") for name, s in seen.items() if want.lower() in name.lower()), "?")


def poll(cfg, handoff_from, max_minutes):
    start = datetime.fromisoformat(cfg["start"])
    if datetime.now(timezone.utc) < start:
        log(f"not polling until {start.astimezone(PT):%a %b %d %H:%M %Z}")
        return
    if IN_ACTIONS and should_exit(handoff_from, retries=2):
        log("another tracker run is active; exiting")
        return
    if not handoff_from:
        notify(cfg, "iPhone Duo tracker running",
               f"Checking pickup every {cfg['interval_seconds']}s at: {', '.join(cfg['stores'])}",
               priority="low", tags="eyes")

    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    renotify = cfg["renotify_minutes"] * 60
    last_alert = {}   # store -> monotonic time of last "available" alert
    failures = 0
    last_run_check = time.monotonic()
    while True:
        t0 = time.monotonic()
        try:
            results, _ = check(cfg)
        except Exception as e:
            results = None
            failures += 1
            detail = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else repr(e)
            log(f"check failed ({failures} in a row): {detail}")
            if failures == cfg["error_alert_after"]:
                notify(cfg, "iPhone Duo tracker having trouble",
                       f"{failures} failed checks in a row ({detail}). Check the Apple Store app manually.",
                       priority="default", tags="warning")
        if results is not None:
            failures = 0
            log(summarize(results))
            available = {s for s, parts in results.items()
                         if parts and any(is_available(i) for i in parts.values())}
            new = available - set(last_alert)
            due = {s for s in available & set(last_alert) if t0 - last_alert[s] >= renotify}
            gone = set(last_alert) - available
            if new or due:
                notify(cfg, "iPhone Duo PICKUP AVAILABLE",
                       f"{', '.join(sorted(available))}. Order now: {cfg['checkout_reminder']}",
                       priority="urgent", tags="rotating_light,iphone")
                last_alert.update({s: t0 for s in new | due})
            if new and IN_ACTIONS:
                open_issue(cfg, new)
            if gone:
                notify(cfg, "iPhone Duo pickup gone",
                       f"No longer showing at: {', '.join(sorted(gone))}", priority="low")
                for s in gone:
                    del last_alert[s]

        if deadline and time.monotonic() >= deadline:
            if IN_ACTIONS:
                dispatch_next_run()
            return
        if IN_ACTIONS and time.monotonic() - last_run_check >= 600:
            last_run_check = time.monotonic()
            if should_exit(handoff_from):
                log("an older tracker run is active; exiting")
                return
        time.sleep(max(1, cfg["interval_seconds"] - (time.monotonic() - t0)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="poll", choices=["poll", "once", "diagnose", "test-notify"])
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--max-minutes", type=float, default=0, help="stop polling after this long (0 = forever)")
    ap.add_argument("--handoff-from", default="", help="run id of the run that started this one")
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)

    if args.mode == "poll":
        poll(cfg, args.handoff_from, args.max_minutes)
    elif args.mode == "diagnose":
        diagnose(cfg)
    else:
        if args.mode == "test-notify":
            notify(cfg, "iPhone Duo tracker test",
                   "Notifications work. You'll get an urgent alert when pickup opens.", tags="white_check_mark")
        log(summarize(check(cfg)[0]))


if __name__ == "__main__":
    main()

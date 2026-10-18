#!/usr/bin/env python3
"""Poll Apple Store in-store pickup availability, publish live status for the GitHub Pages
dashboard (docs/index.html), and alert by GitHub issue (-> email) when pickup opens up.

Modes:
  poll         check every interval_seconds (default; waits for config "start")
  once         one check, print a summary, exit
  diagnose     one check with per-store details
  test-alert   open a test alert issue, then one check
"""
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
PT = ZoneInfo("America/Los_Angeles")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
# /shop/fulfillment-messages (what the browser uses) answers 541 to GitHub Actions IPs;
# this older endpoint returns the same per-store pickup data.
PICKUP_URL = "https://www.apple.com/shop/retail/pickup-message"
WORKFLOW_FILE = "track.yml"
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(msg):
    now = datetime.now(timezone.utc)
    print(f"[{now.astimezone(PT):%m-%d %H:%M:%S %Z} | {now:%H:%M:%S} UTC] {msg}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def both_times(iso=None):
    """'Oct 17, 11:56:42 PM PDT (Oct 18, 06:56:42 UTC)'."""
    t = datetime.fromisoformat(iso) if iso else datetime.now(timezone.utc)
    pt = t.astimezone(PT)
    return (f"{pt:%b %-d, %-I:%M:%S %p %Z} ({t.astimezone(timezone.utc):%b %-d, %H:%M:%S} UTC)")


def http(url, data=None, headers=None, method=None, timeout=20):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def describe_error(e):
    return f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else repr(e)


# --- Apple -------------------------------------------------------------------

def fetch_stores(cfg, location):
    q = [("pl", "true"), ("location", location), ("parts.0", cfg["part"])]
    q += list(cfg.get("extra_params", {}).items())
    body = json.loads(http(f"{PICKUP_URL}?{urllib.parse.urlencode(q)}",
                           headers={"Accept": "application/json", "Referer": cfg["product_url"]}))["body"]
    if "content" in body:  # fulfillment-messages shape
        body = body["content"]["pickupMessage"]
    if "stores" not in body:
        raise ValueError(body.get("errorMessage") or "no stores in response")
    return body["stores"]


def is_available(info):
    quote = strip_html(info.get("pickupSearchQuote", ""))
    return info.get("pickupDisplay") == "available" or quote.lower().startswith("available")


def check(cfg):
    """One lookup; returns [{name, number, display, quote, available}] for each configured store."""
    seen = {}
    for loc in cfg["search_locations"]:
        for s in fetch_stores(cfg, loc):
            seen.setdefault(s.get("storeName", ""), s)
    results = []
    for want in cfg["stores"]:
        store = next((s for name, s in seen.items() if want.lower() in name.lower()), None)
        info = (store or {}).get("partsAvailability", {}).get(cfg["part"], {})
        results.append({
            "name": want,
            "number": (store or {}).get("storeNumber"),
            "display": info.get("pickupDisplay", "no data") if store else "not found",
            "quote": strip_html(info.get("pickupSearchQuote", "")),
            "title": info.get("messageTypes", {}).get("regular", {}).get("storePickupProductTitle"),
            "available": is_available(info),
        })
    return results


def summarize(results):
    return " | ".join(f"{r['name']}: {r['display']}" + (f" ({r['quote']})" if r["quote"] else "")
                      for r in results)


# --- GitHub ----------------------------------------------------------------------

def github_api(path, payload=None, method="GET"):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return None
    data = json.dumps(payload).encode() if payload is not None else None
    body = http(f"https://api.github.com/repos/{repo}/{path}", data=data, method=method,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"})
    return json.loads(body) if body else {}


def alert(cfg, title, text):
    """Open an issue that @mentions the repo owner, which GitHub delivers by email / GitHub app."""
    log(f"ALERT: {title}")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    try:
        github_api("issues", method="POST", payload={
            "title": title,
            "labels": [cfg.get("alert_label", "pickup-alert")],
            "body": (f"@{owner} {text}\n\nDetected {both_times()}.\n\n**[Order now]({cfg['product_url']})**. "
                     f"Select: {cfg['checkout_reminder']}"),
        })
    except Exception as e:
        log(f"alert issue failed: {describe_error(e)}")


class StatusBoard:
    """Live status JSON kept in the body of one open issue labeled status_label (default tracker-status).

    docs/index.html (GitHub Pages) reads it through the public GitHub API.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.label = cfg.get("status_label", "tracker-status")
        self.number = None
        self.previous = {}
        self.events = []
        if not IN_ACTIONS:
            return
        try:
            found = github_api(f"issues?labels={self.label}&state=open&per_page=1")
            if found:
                self.number = found[0]["number"]
                self.previous = parse_status(found[0].get("body") or "")
        except Exception as e:
            log(f"status issue lookup failed: {describe_error(e)}")
        self.events = self.previous.get("events", [])

    def recently_available(self):
        """Stores the last run already alerted on, so a handoff doesn't alert twice."""
        checked = self.previous.get("checked_at")
        if not checked or datetime.now(timezone.utc) - datetime.fromisoformat(checked) > timedelta(minutes=30):
            return set()
        return {s["name"] for s in self.previous.get("stores", []) if s.get("available")}

    def event(self, text):
        log(f"event: {text}")
        self.events = ([{"at": now_iso(), "text": text}] + self.events)[:40]

    def publish(self, results, error, checked_at):
        status = {
            "eyebrow": self.cfg.get("eyebrow", "Apple Store pickup tracker"),
            "title": self.cfg.get("title", self.cfg["product_name"]),
            "subtitle": self.cfg.get("subtitle", ""),
            "product": self.cfg["product_name"],
            "product_url": self.cfg["product_url"],
            "checkout_reminder": self.cfg["checkout_reminder"],
            "interval_seconds": self.cfg["interval_seconds"],
            "checked_at": checked_at,
            "updated_at": now_iso(),
            "error": error,
            "stores": results,
            "events": self.events,
            "run_url": (f"{os.environ.get('GITHUB_SERVER_URL')}/{os.environ.get('GITHUB_REPOSITORY')}"
                        f"/actions/runs/{os.environ.get('GITHUB_RUN_ID')}"),
        }
        body = ("Live data for the duo-track status page, rewritten every minute by the tracker "
                "(edits here are overwritten).\n\n```json\n" + json.dumps(status, indent=1) + "\n```\n")
        if not IN_ACTIONS:
            return
        try:
            if self.number:
                github_api(f"issues/{self.number}", method="PATCH", payload={"body": body})
            else:
                self.number = github_api("issues", method="POST", payload={
                    "title": self.cfg.get("status_title", "Tracker status (live)"), "body": body,
                    "labels": [self.label]})["number"]
        except urllib.error.HTTPError as e:
            log(f"status publish failed: {describe_error(e)}")
            if e.code in (404, 410):
                self.number = None
        except Exception as e:
            log(f"status publish failed: {describe_error(e)}")


def parse_status(body):
    m = re.search(r"```json\n(.*?)\n```", body, re.S)
    try:
        return json.loads(m.group(1)) if m else {}
    except ValueError:
        return {}


# --- GitHub Actions run coordination ----------------------------------------------
# Scheduled runs act as a watchdog; one long-running run polls and hands off to the next.

def older_tracker_running(config, handoff_from):
    me = int(os.environ["GITHUB_RUN_NUMBER"])
    runs = github_api(f"actions/workflows/{WORKFLOW_FILE}/runs?status=in_progress&per_page=50")
    return any(r["run_number"] < me and str(r["id"]) != handoff_from and r.get("display_title") == f"poll {config}"
               for r in runs.get("workflow_runs", []))


def should_exit(config, handoff_from, retries=0):
    """True if an older tracker is still active (re-checked so a finishing run doesn't count)."""
    for attempt in range(retries + 1):
        try:
            if not older_tracker_running(config, handoff_from):
                return False
        except Exception as e:
            log(f"run check failed: {e}")
            return False
        if attempt < retries:
            time.sleep(30)
    return True


def dispatch_next_run(config):
    try:
        github_api(f"actions/workflows/{WORKFLOW_FILE}/dispatches", method="POST", payload={
            "ref": os.environ.get("GITHUB_REF_NAME", "main"),
            "inputs": {"mode": "poll", "config": config, "handoff_from": os.environ["GITHUB_RUN_ID"]},
        })
        log("dispatched next run")
    except Exception as e:
        log(f"dispatch failed (scheduled watchdog will restart): {e}")


# --- Modes ---------------------------------------------------------------------

def diagnose(cfg):
    page = http(cfg["product_url"])
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    skus = sorted(set(re.findall(r'"sku"\s*:\s*"([^"]+)"', page)))
    selection = re.search(r"uniqueProductSelection\s*=\s*(\{.*?\});", page)
    log(f"product page: {strip_html(title.group(1)) if title else '?'} | skus={skus} | "
        f"selection={selection.group(1) if selection else None}")
    if not cfg.get("part") and skus:
        cfg["part"] = skus[0].split("+")[0]
    if selection and "cppart" not in cfg.get("extra_params", {}):
        carrier = json.loads(selection.group(1)).get("carrierModel")
        if carrier:
            cfg.setdefault("extra_params", {})["cppart"] = carrier
    log(f"checking part={cfg['part']} params={cfg.get('extra_params')}")
    for r in check(cfg):
        log(f"{r['name']} [{r['number']}] {cfg['part']} '{r['title']}': pickupDisplay={r['display']} "
            f"quote={r['quote']!r} available={r['available']}")


def poll(cfg, config, handoff_from, max_minutes):
    start = datetime.fromisoformat(cfg["start"])
    if datetime.now(timezone.utc) < start:
        log(f"not polling until {start.astimezone(PT):%a %b %d %H:%M %Z}")
        return
    if IN_ACTIONS and should_exit(config, handoff_from, retries=2):
        log("another tracker run is active; exiting")
        return

    board = StatusBoard(cfg)
    if not handoff_from:
        board.event("Tracker started")
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    alerted = board.recently_available()
    results, checked_at = board.previous.get("stores", []), board.previous.get("checked_at")
    failures = 0
    last_run_check = time.monotonic()
    while True:
        t0 = time.monotonic()
        error = None
        try:
            results, checked_at = check(cfg), now_iso()
            log(summarize(results))
            if failures >= cfg["error_alert_after"]:
                board.event("Apple checks working again")
            failures = 0
        except Exception as e:
            error = describe_error(e)
            failures += 1
            log(f"check failed ({failures} in a row): {error}")
            if failures == cfg["error_alert_after"]:
                board.event(f"Apple checks failing ({error})")
                alert(cfg, "iPhone Duo tracker can't reach Apple",
                      f"{failures} checks in a row failed ({error}). Check the Apple Store app manually "
                      "until this recovers.")
        if error is None:
            available = {r["name"] for r in results if r["available"]}
            new, gone = available - alerted, alerted - available
            if new:
                where = ", ".join(sorted(new))
                board.event(f"Pickup available: {where}")
                alert(cfg, f"iPhone Duo pickup available: {where}",
                      f"{cfg['product_name']} shows in-store pickup at **{where}**.")
            if gone:
                board.event(f"Pickup no longer showing: {', '.join(sorted(gone))}")
            alerted = available
        board.publish(results, error, checked_at)

        if deadline and time.monotonic() >= deadline:
            if IN_ACTIONS and cfg.get("handoff", True):
                dispatch_next_run(config)
            else:
                board.event("Tracker stopped (end of run)")
                board.publish(results, None, checked_at)
            return
        if IN_ACTIONS and time.monotonic() - last_run_check >= 600:
            last_run_check = time.monotonic()
            if should_exit(config, handoff_from):
                log("an older tracker run is active; exiting")
                return
        time.sleep(max(1, cfg["interval_seconds"] - (time.monotonic() - t0)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="poll", choices=["poll", "once", "diagnose", "test-alert"])
    ap.add_argument("--config", default="config.json", help="path relative to the repo root")
    ap.add_argument("--max-minutes", type=float, default=0, help="stop polling after this long (0 = forever)")
    ap.add_argument("--handoff-from", default="", help="run id of the run that started this one")
    args = ap.parse_args()
    with open(os.path.join(ROOT, args.config)) as f:
        cfg = json.load(f)

    if args.mode == "poll":
        poll(cfg, args.config, args.handoff_from, args.max_minutes)
    elif args.mode == "diagnose":
        diagnose(cfg)
    else:
        if args.mode == "test-alert":
            alert(cfg, "Test alert from duo-track",
                  "This is a test. When pickup opens you'll get an email like this one. You can close this issue.")
        log(summarize(check(cfg)))


if __name__ == "__main__":
    main()

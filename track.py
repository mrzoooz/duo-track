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

def variants(cfg):
    """Models to watch (e.g. two colors), each {name, part, product_url}; one request covers all of them.

    A config without "variants" tracks its single "part"/"product_url". Variants whose part isn't known yet are skipped.
    """
    vs = cfg.get("variants") or [{"name": "", "part": cfg.get("part", ""), "product_url": cfg["product_url"]}]
    return [v for v in vs if v.get("part")] or vs[:1]


def fetch_stores(cfg, location):
    vs = variants(cfg)
    q = [("pl", "true"), ("location", location)] + [(f"parts.{i}", v["part"]) for i, v in enumerate(vs)]
    q += list(cfg.get("extra_params", {}).items())
    body = json.loads(http(f"{PICKUP_URL}?{urllib.parse.urlencode(q)}",
                           headers={"Accept": "application/json", "Referer": vs[0]["product_url"]}))["body"]
    if "content" in body:  # fulfillment-messages shape
        body = body["content"]["pickupMessage"]
    if "stores" not in body:
        raise ValueError(body.get("errorMessage") or "no stores in response")
    return body["stores"]


def is_available(info):
    quote = strip_html(info.get("pickupSearchQuote", ""))
    return info.get("pickupDisplay") == "available" or quote.lower().startswith("available")


def store_status(name, store, vs):
    per = []
    for v in vs:
        info = (store or {}).get("partsAvailability", {}).get(v["part"], {})
        per.append({
            "variant": v["name"],
            "display": info.get("pickupDisplay", "no data") if store else "not found",
            "quote": strip_html(info.get("pickupSearchQuote", "")),
            "title": info.get("messageTypes", {}).get("regular", {}).get("storePickupProductTitle"),
            "available": is_available(info),
        })
    open_ = [p for p in per if p["available"]]
    return {
        "name": name,
        "number": (store or {}).get("storeNumber"),
        "city": (store or {}).get("city"),
        "display": (open_ or per)[0]["display"],
        "quote": per[0]["quote"] if len(per) == 1 else " · ".join(f"{p['variant']}: {p['quote'] or p['display']}"
                                                                   for p in per),
        "available": bool(open_),
        "variants": per,
    }


def available_keys(results):
    """Store (and, when several variants are watched, color) combinations showing pickup."""
    return {r["name"] if len(r.get("variants", [])) <= 1 else f"{r['name']} ({p['variant']})"
            for r in results for p in r.get("variants", [r]) if p["available"]}


def check(cfg):
    """One lookup. Returns (configured stores' statuses, statuses of the other nearby stores Apple returned)."""
    seen = {}
    for loc in cfg["search_locations"]:
        for s in fetch_stores(cfg, loc):
            seen.setdefault(s.get("storeName", ""), s)
    results, tracked = [], set()
    for want in cfg["stores"]:
        name, store = next(((n, s) for n, s in seen.items() if want.lower() in n.lower()), (None, None))
        tracked.add(name)
        results.append(store_status(want, store, variants(cfg)))
    others = [store_status(n, s, variants(cfg)) for n, s in seen.items() if n not in tracked]
    return results, others


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


def alert(cfg, title, text, vs=None):
    """Open an issue that @mentions the repo owner, which GitHub delivers by email / GitHub app."""
    log(f"ALERT: {title}")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    vs = vs or variants(cfg)[:1]
    links = " · ".join(f"**[Order {v['name'] or 'now'}]({v['product_url']})**" for v in vs)
    try:
        github_api("issues", method="POST", payload={
            "title": title,
            "labels": [cfg.get("alert_label", "pickup-alert")],
            "body": f"@{owner} {text}\n\nDetected {both_times()}.\n\n{links}. Select: {cfg['checkout_reminder']}",
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
        self.extra = {}
        if not IN_ACTIONS:
            return
        try:
            found = github_api(f"issues?labels={self.label}&state=open&per_page=1")
            if found:
                self.number = found[0]["number"]
                self.previous = parse_status(found[0].get("body") or "")
                if self.previous.get("history_id", self.previous.get("product")) != history_id(cfg):
                    self.previous = {}  # config now tracks a different product: start its history fresh
        except Exception as e:
            log(f"status issue lookup failed: {describe_error(e)}")
        self.events = self.previous.get("events", [])

    def recently_available(self):
        """Store/color combinations the last run already alerted on, so a handoff doesn't alert twice."""
        checked = self.previous.get("checked_at")
        if not checked or datetime.now(timezone.utc) - datetime.fromisoformat(checked) > timedelta(minutes=30):
            return set()
        if "alerted" in self.previous:
            return set(self.previous["alerted"])
        return {s["name"] for s in self.previous.get("stores", []) if s.get("available")}

    def event(self, text):
        log(f"event: {text}")
        self.events = ([{"at": now_iso(), "text": text}] + self.events)[:40]

    def failure_streak(self):
        """Consecutive failed checks carried over from the previous run, if it handed off just now."""
        updated = self.previous.get("updated_at")
        if not updated or datetime.now(timezone.utc) - datetime.fromisoformat(updated) > timedelta(minutes=10):
            return 0
        return self.previous.get("failure_streak", 0)

    def publish(self, results, error, checked_at, failure_streak=0, stopped=False, paused_until=None):
        status = {
            "eyebrow": self.cfg.get("eyebrow", "Apple Store pickup tracker"),
            "title": self.cfg["title"],
            "subtitle": self.cfg.get("subtitle", ""),
            "product": self.cfg["product_name"],
            "history_id": history_id(self.cfg),
            "product_url": variants(self.cfg)[0]["product_url"],
            "variants": [{"name": v["name"], "url": v["product_url"]} for v in variants(self.cfg)],
            "checkout_reminder": self.cfg["checkout_reminder"],
            "interval_seconds": self.cfg["interval_seconds"],
            "checked_at": checked_at,
            "updated_at": now_iso(),
            "error": error,
            "failure_streak": failure_streak,
            "stopped_at": now_iso() if stopped else None,
            "paused_until": paused_until,
            "stores": results,
            "events": self.events,
            **self.extra,
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


def history_id(cfg):
    """Identifies a status history; the board starts fresh when it changes (defaults to the product name)."""
    return cfg.get("history_id", cfg["product_name"])


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


def dispatch_next_run(config, until):
    try:
        github_api(f"actions/workflows/{WORKFLOW_FILE}/dispatches", method="POST", payload={
            "ref": os.environ.get("GITHUB_REF_NAME", "main"),
            "inputs": {"mode": "poll", "config": config, "until": until or "",
                       "handoff_from": os.environ["GITHUB_RUN_ID"]},
        })
        log("dispatched next run")
    except Exception as e:
        log(f"dispatch failed (scheduled watchdog will restart): {e}")


# --- Modes ---------------------------------------------------------------------

def probe_cache(cfg):
    """Show whether Apple's pickup endpoint is served from a cache (and how stale), with and without a cache-buster."""
    v = variants(cfg)[0]
    q = [("pl", "true"), ("location", cfg["search_locations"][0]), ("parts.0", v["part"])]
    q += list(cfg.get("extra_params", {}).items())
    base = f"{PICKUP_URL}?{urllib.parse.urlencode(q)}"
    for label, url in (("plain", base), ("plain again", base),
                       ("busted", f"{base}&_={time.time_ns()}"), ("busted again", f"{base}&_={time.time_ns() + 1}")):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                                   "Referer": v["product_url"]})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                h = resp.headers
                body = json.loads(resp.read())["body"]
                quote = strip_html(body["stores"][0]["partsAvailability"][v["part"]].get("pickupSearchQuote"))
                keep = {k: v for k, v in h.items() if k.lower() in (
                    "cache-control", "expires", "age", "date", "last-modified", "etag", "vary", "x-cache",
                    "x-cache-remote", "x-check-cacheable", "akamai-cache-status", "x-akamai-transformed",
                    "server-timing", "x-served-by", "cdn-cache", "x-apple-request-uuid")}
                log(f"cache probe [{label}] HTTP {resp.status} quote={quote!r} headers={keep}")
        except urllib.error.HTTPError as e:
            log(f"cache probe [{label}] HTTP {e.code} headers={dict(e.headers)}")
    try:
        http(f"https://www.apple.com/shop/fulfillment-messages?fae=true&pl=true&mts.0=regular&parts.0={v['part']}"
             f"&location={cfg['search_locations'][0]}", headers={"Accept": "application/json"})
        log("fulfillment-messages: reachable")
    except urllib.error.HTTPError as e:
        log(f"fulfillment-messages: HTTP {e.code}")


def diagnose(cfg):
    probe_cache(cfg)
    for v in cfg.get("variants") or [cfg]:
        page = http(v["product_url"])
        title = re.search(r"<title>(.*?)</title>", page, re.S)
        skus = sorted(set(re.findall(r'"sku"\s*:\s*"([^"]+)"', page)))
        selection = re.search(r"uniqueProductSelection\s*=\s*(\{.*?\});", page)
        log(f"product page: {strip_html(title.group(1)) if title else '?'} | skus={skus} | "
            f"selection={selection.group(1) if selection else None}")
        if not v.get("part") and skus:
            v["part"] = skus[0].split("+")[0]
            log(f"discovered part {v['part']} for {v.get('name') or v['product_url']}")
        if selection and "cppart" not in cfg.get("extra_params", {}):
            carrier = json.loads(selection.group(1)).get("carrierModel")
            if carrier:
                cfg.setdefault("extra_params", {})["cppart"] = carrier
    log(f"checking parts={[v['part'] for v in variants(cfg)]} params={cfg.get('extra_params')}")
    results, others = check(cfg)
    log("untracked nearby: " + ", ".join(f"{o['name']} [{o['number']}] ({o['quote']})" for o in others))
    for r in results:
        for p in r["variants"]:
            log(f"{r['name']} [{r['number']}] {p['variant']} '{p['title']}': pickupDisplay={p['display']} "
                f"quote={p['quote']!r} available={p['available']}")


def poll(cfg, config, handoff_from, max_minutes, until=""):
    start = datetime.fromisoformat(cfg["start"])
    if datetime.now(timezone.utc) < start:
        log(f"not polling until {start.astimezone(PT):%a %b %d %H:%M %Z}")
        if IN_ACTIONS:  # tell the status page once; scheduled runs start polling at "start"
            board = StatusBoard(cfg)
            if board.previous.get("paused_until") != cfg["start"]:
                board.event(f"Paused until {both_times(cfg['start'])}")
                board.publish(board.previous.get("stores", []), None, board.previous.get("checked_at"),
                              paused_until=cfg["start"])
        return
    if IN_ACTIONS and should_exit(config, handoff_from, retries=2):
        log("another tracker run is active; exiting")
        return

    # Optional overall end time (test configs set max_total_minutes); carried across handoffs.
    if not until and cfg.get("max_total_minutes"):
        until = (datetime.now(timezone.utc) + timedelta(minutes=cfg["max_total_minutes"])).isoformat(timespec="seconds")
    end = datetime.fromisoformat(until) if until else None

    def can_continue():
        return (IN_ACTIONS and cfg.get("handoff", True)
                and (end is None or datetime.now(timezone.utc) < end - timedelta(minutes=1)))

    # Keep each runner under ~max_requests_per_runner Apple requests (one per search location per check).
    per_runner = max(1, cfg.get("max_requests_per_runner", 40) * cfg["interval_seconds"] / 60
                     / len(cfg["search_locations"]))
    max_minutes = min(max_minutes, per_runner) if max_minutes else per_runner

    board = StatusBoard(cfg)
    if not handoff_from:
        board.event("Tracker started" + (f", runs until {both_times(until)}" if end else ""))
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    if end:
        to_end = time.monotonic() + (end - datetime.now(timezone.utc)).total_seconds()
        deadline = min(deadline, to_end) if deadline else to_end
    alerted = board.recently_available()
    board.extra["alerted"] = sorted(alerted)
    results, checked_at = board.previous.get("stores", []), board.previous.get("checked_at")
    # Apple starts answering 541 to a runner IP after roughly an hour of checks, so runs are short
    # and a run that keeps failing hands off early to a fresh runner (usually a new IP).
    failures = 0                      # this run
    streak = board.failure_streak()   # across handoffs
    # History only (no email): changes in Apple's message at tracked stores, and untracked nearby stores with pickup.
    last_quotes = {r["name"]: r.get("quote") or r.get("display") for r in results}
    untracked_open = set(board.previous.get("untracked_open", []))
    board.extra["untracked_open"] = sorted(untracked_open)
    last_run_check = time.monotonic()
    while True:
        t0 = time.monotonic()
        error = None
        try:
            (results, others), checked_at = check(cfg), now_iso()
            log(summarize(results))
            if streak >= cfg["error_alert_after"]:
                board.event("Apple checks working again")
            failures = streak = 0
        except Exception as e:
            error = describe_error(e)
            failures += 1
            streak += 1
            log(f"check failed ({failures} in a row this run, {streak} overall): {error}")
            if streak == cfg["error_alert_after"]:
                board.event(f"Apple checks failing ({error})")
                alert(cfg, f"{cfg.get('alert_prefix', '')}{cfg['title']} tracker can't reach Apple",
                      f"{streak} checks in a row failed ({error}). Check the Apple Store app manually "
                      "until this recovers.")
            if failures >= cfg.get("rotate_after_failures", 3) and can_continue():
                board.event(f"Switched to a fresh runner after {error}")
                board.publish(results, error, checked_at, streak)
                dispatch_next_run(config, until)
                return
        if error is None:
            available = available_keys(results)
            new, gone = available - alerted, alerted - available
            if new:
                where = ", ".join(sorted(new))
                board.event(f"Pickup available: {where}")
                opened = [v for v in variants(cfg) if any(k == v["name"] or k.endswith(f"({v['name']})") for k in new)
                          or len(variants(cfg)) == 1]
                alert(cfg, f"{cfg.get('alert_prefix', '')}{cfg['title']} pickup available: {where}",
                      f"{cfg['product_name']} shows in-store pickup at **{where}**.", opened)
            if gone:
                board.event(f"Pickup no longer showing: {', '.join(sorted(gone))}")
            alerted = available
            board.extra["alerted"] = sorted(alerted)

            changes = {}
            touched = {k.split(" (")[0] for k in new | gone}
            for r in results:
                old_q, new_q = last_quotes.get(r["name"]), r["quote"] or r["display"]
                if old_q and new_q != old_q and r["name"] not in touched:
                    changes.setdefault((old_q, new_q), []).append(r["name"])
            for (old_q, new_q), names in changes.items():
                who = f"All {len(names)} stores" if len(names) == len(results) > 1 else ", ".join(names)
                board.event(f"{who}: “{old_q}” → “{new_q}”")
            last_quotes = {r["name"]: r["quote"] or r["display"] for r in results}

            open_elsewhere = {o["name"]: o["quote"] or o["display"] for o in others if o["available"]}
            if open_elsewhere:
                log("untracked stores showing pickup: " + ", ".join(f"{n} ({q})" for n, q in open_elsewhere.items()))
            if set(open_elsewhere) != untracked_open:
                if open_elsewhere:
                    board.event("Pickup at stores you don't track: "
                                + ", ".join(f"{n} ({q})" for n, q in sorted(open_elsewhere.items())))
                else:
                    board.event("Pickup no longer showing at any untracked nearby store")
                untracked_open = set(open_elsewhere)
                board.extra["untracked_open"] = sorted(untracked_open)
        board.publish(results, error, checked_at, streak)

        if deadline and time.monotonic() >= deadline:
            if can_continue():
                dispatch_next_run(config, until)
            else:
                board.event("Tracker stopped (scheduled end)")
                board.publish(results, error, checked_at, streak, stopped=True)
            return
        if IN_ACTIONS and time.monotonic() - last_run_check >= 600:
            last_run_check = time.monotonic()
            if should_exit(config, handoff_from):
                log("an older tracker run is active; exiting")
                return
        interval = cfg["interval_seconds"] if streak < cfg["error_alert_after"] else max(cfg["interval_seconds"], 300)
        time.sleep(max(1, interval - (time.monotonic() - t0)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="poll", choices=["poll", "once", "diagnose", "test-alert"])
    ap.add_argument("--config", default="config.json", help="path relative to the repo root")
    ap.add_argument("--max-minutes", type=float, default=0, help="stop polling after this long (0 = forever)")
    ap.add_argument("--handoff-from", default="", help="run id of the run that started this one")
    ap.add_argument("--until", default="", help="ISO time after which the tracker stops instead of handing off")
    args = ap.parse_args()
    with open(os.path.join(ROOT, args.config)) as f:
        cfg = json.load(f)

    if args.mode == "poll":
        poll(cfg, args.config, args.handoff_from, args.max_minutes, args.until)
    elif args.mode == "diagnose":
        diagnose(cfg)
    else:
        if args.mode == "test-alert":
            alert(cfg, "Test alert from duo-track",
                  "This is a test. When pickup opens you'll get an email like this one. You can close this issue.")
        log(summarize(check(cfg)[0]))


if __name__ == "__main__":
    main()

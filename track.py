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
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
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
    q = [("fae", "true"), ("pl", "true"), ("mts.0", "regular"), ("mts.1", "compact"),
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
        for key in ("fulfillment-messages", "cppart", "purchaseOption", "option.0", "carrierModel", "pickup"):
            for m in list(re.finditer(re.escape(key), page))[:4]:
                log(f"context {key!r}: {page[max(0, m.start() - 250):m.end() + 250]!r}")
    except Exception as e:
        log(f"product page fetch failed: {e}")
    part = cfg["parts"][0]
    session = make_session(cfg)
    xhr = {"X-Requested-With": "XMLHttpRequest"}
    aos = {"x-aos-ui-fetch-call-1": "a1b2c3d4e5", "x-skip-redirect": "true", **xhr}
    for opener, cp, where, hdrs, label in (
            (None, None, ("location", "92626"), {}, "plain"),
            (session, None, ("location", "92626"), {}, "cookies"),
            (session, "TMOBILE_IPHONEDUO", ("location", "92626"), {}, "cookies"),
            (session, "TMOBILE_IPHONEDUO", ("store", "R004"), {}, "cookies"),
            (session, "UNLOCKED/US", ("store", "R004"), {}, "cookies"),
            (session, "TMOBILE_IPHONEDUO", ("store", "R004"), xhr, "cookies+xhr"),
            (session, "TMOBILE_IPHONEDUO", ("store", "R004"), aos, "cookies+aos"),
            (None, "TMOBILE_IPHONEDUO", ("store", "R004"), aos, "plain+aos")):
        params = [("parts.0", part)] + ([("cppart", cp)] if cp else []) + [where]
        probe(cfg, params, opener=opener, headers=hdrs, label=label)
    for control in ("MX6X3AM/A", "MX532AM/A"):  # MagSafe charger, AirTag
        probe(cfg, [("parts.0", control), ("store", "R004")], opener=session, label="control")
    chrome = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/140.0.0.0 Safari/537.36",
              "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
              "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"', "sec-fetch-dest": "empty",
              "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin", "Accept-Language": "en-US,en;q=0.9"}
    for url in (
            "https://www.apple.com/shop/fulfillment-messages?pl=true&mts.0=regular&parts.0=MX532AM/A&location=92626",
            "https://www.apple.com/shop/fulfillment-messages?fae=true&little=false&parts.0=MX532AM/A&mts.0=regular&mts.1=sticky&fts=true",
            "https://www.apple.com/shop/retail/pickup-message?pl=true&parts.0=MX532AM/A&location=92626",
            "https://www.apple.com/shop/pickup-message-recommendations?mts.0=regular&location=92626&product=MX532AM/A",
            "https://www.apple.com/shop/delivery-message?parts.0=MX532AM/A&mt=regular",
            "https://www.apple.com/us-edu/shop/fulfillment-messages?fae=true&pl=true&mts.0=regular&parts.0=MX532AM/A&location=92626",
            "https://www.apple.com/shop/address/geo-lookup?fae=true",
            "https://www.apple.com/retail/southcoastplaza/"):
        for name, hdrs in (("safari", {}), ("chrome", chrome)):
            try:
                body = http(url, headers={"Referer": cfg["product_url"], **hdrs})
                log(f"url[{name}] {url}: 200 {len(body)}b {body[:160]!r}")
            except urllib.error.HTTPError as e:
                log(f"url[{name}] {url}: HTTP {e.code}")
            except Exception as e:
                log(f"url[{name}] {url}: {e!r}")
    try:
        results, seen = check(cfg)
    except Exception as e:
        log(f"configured check failed: {e}")
        return
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


def make_session(cfg):
    """Opener that carries the cookies a browser gets from loading the product page."""
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA), ("Accept-Language", "en-US,en;q=0.9")]
    try:
        opener.open(cfg["product_url"], timeout=20).read()
        log(f"session cookies: {sorted(c.name for c in jar)}")
    except Exception as e:
        log(f"session warmup failed: {e}")
    return opener


def probe_url(params):
    q = [("fae", "true"), ("pl", "true"), ("mts.0", "regular"), ("mts.1", "compact"),
         ("searchNearby", "true")] + params
    return f"{FULFILLMENT_URL}?{urllib.parse.urlencode(q)}"


def describe(body):
    pm = json.loads(body)["body"]["content"]["pickupMessage"]
    stores = [(s.get("storeNumber"), s.get("storeName"),
               {p: (i.get("pickupDisplay"), strip_html(i.get("pickupSearchQuote")))
                for p, i in s.get("partsAvailability", {}).items()})
              for s in pm.get("stores", [])]
    extra = {k: v for k, v in pm.items() if k != "stores" and isinstance(v, (str, bool, int))}
    return f"OK {len(stores)} stores {stores} meta={extra}"


def probe(cfg, params, opener=None, headers=None, label=""):
    req = urllib.request.Request(probe_url(params), headers={
        "User-Agent": UA, "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": cfg["product_url"], **(headers or {})})
    try:
        with (opener.open if opener else urllib.request.urlopen)(req, timeout=20) as resp:
            log(f"probe[{label}] {params}: {describe(resp.read().decode())}")
    except urllib.error.HTTPError as e:
        log(f"probe[{label}] {params}: HTTP {e.code} server={e.headers.get('Server')} {e.read()[:80]!r}")
    except Exception as e:
        log(f"probe[{label}] {params}: {e!r}")


def probe_curl(cfg, params):
    """Same request via curl (different TLS fingerprint), with a cookie jar warmed on the product page."""
    jar = "/tmp/duo-track-cookies.txt"
    base = ["curl", "-sS", "--compressed", "-c", jar, "-b", jar, "-A", UA, "-H", "Accept-Language: en-US,en;q=0.9"]
    try:
        subprocess.run(base + ["-o", "/dev/null", cfg["product_url"]], check=True, timeout=30)
        out = subprocess.run(base + ["-w", "\n%{http_code}", "-H", "Accept: application/json",
                                     "-e", cfg["product_url"], probe_url(params)],
                             capture_output=True, text=True, timeout=30).stdout
        body, code = out.rsplit("\n", 1)
        log(f"probe[curl] {params}: HTTP {code} " + (describe(body) if code == "200" else repr(body[:80])))
    except Exception as e:
        log(f"probe[curl] {params}: {e!r}")


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

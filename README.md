# duo-track

Checks Apple Store in-store pickup for **iPhone Duo 256GB in Night Sky or Star White (T-Mobile, Apple Upgrade)** every
10 seconds from 9:00 to 11:30 AM Pacific (when most restocks have landed) and every 20 seconds otherwise, at
South Coast Plaza, Fashion Island, Irvine Spectrum Center, Brea Mall, Los Cerritos and Mission Viejo.

- **Status page:** https://mrzoooz.github.io/duo-track/ (GitHub Pages, `docs/index.html`). Live per-store status,
  last check time, recent activity, and an optional sound alarm while the page is open.
- **Alerts:** when a store shows pickup, the tracker first sends an instant push through ntfy.sh (subscribe to the
  config's `ntfy_topic` in the ntfy app; a `NTFY_TOPIC` repo secret overrides it), then opens an issue that @mentions
  you so GitHub emails you as a backup. GitHub's notification emails have arrived 2–3 minutes after the issue, while
  pickup slots have lasted 30–90 seconds.

## How it works

`.github/workflows/track.yml` runs `track.py` on GitHub Actions (free for public repos).

- It reads Apple's `/shop/retail/pickup-message` endpoint for each configured variant's part (`variants` in the config;
  Night Sky is `MK1F4LL/A`), carrier `TMOBILE_IPHONEDUO`, near 92626. One request covers every variant. That returns the same "Pickup:" line the product page shows. The browser's `/shop/fulfillment-messages`
  endpoint answers HTTP 541 to GitHub's servers, so the tracker doesn't use it.
- It rewrites the JSON in the open issue labeled `tracker-status` ("Tracker status (live)") at most every 30 seconds
  (`publish_every_seconds`), and right away when something changes. The status page reads that issue through the public
  GitHub API, which allows browser requests. Edits to an issue body don't send notifications.
- The check interval is `interval_seconds`, or the `interval_seconds` of a `fast_windows` entry (Pacific `from`–`to`)
  covering the current time.
- Apple starts answering HTTP 541 to a runner's IP after roughly 60 requests, so each run makes at most ~40
  (`max_requests_per_runner`; ~6.5 minutes at 10-second checks). About 20 seconds before its last check
  (`handoff_lead_seconds`) it starts its successor on a fresh runner (usually a new IP) and keeps checking until the
  successor has published its first check (at most `handoff_wait_seconds`, 90), so there's no gap while the new runner
  boots. A run that gets 2 errors in a row (`rotate_after_failures`) hands off right away. A schedule every 15 minutes
  acts as a watchdog that starts a run if none is active.

Alert issues (label `pickup-alert`) are opened when a store first shows pickup, and once if Apple requests fail 15 times
in a row across runs (checks then slow to every 5 minutes until Apple answers again). AppleCare+, the payment schedule and the lease term are checkout choices and don't affect stock. The alert
lists them so you can select them when you order. Its link opens the product page with color, storage, carrier and
Apple Upgrade preselected (a `?term=12` query isn't honored by Apple's server). Apple's Your Saves only keeps color and storage, so it isn't used.

## Manual runs

Actions → **track** → **Run workflow**, then pick a mode:
- `test-alert`: opens a test alert issue (you should get an email), then does one check
- `diagnose`: one check with per-store details
- `once`: one check

The **config** input picks what to track: `config.json` (the Duo) or `tests/test.json` (currently iPhone 18 Pro 256GB Glacier), an
accuracy test that writes to its own status issue (view it at https://mrzoooz.github.io/duo-track/?label=tracker-status-test),
labels its alerts `[TEST]`, and switches runners like the main tracker but stops for good after 2 hours
(`max_total_minutes`).

Locally: `python3 track.py --mode once` (no dependencies).

## Pause it

Set `start` in the config to when polling should resume (e.g. `2026-10-23T00:00:00-07:00`) and cancel the running run.
Runs started before then post a "paused until" notice to the status page and exit without contacting Apple; the
scheduled watchdog starts polling again once `start` passes.

## Stop it

Actions → **track** → `···` → **Disable workflow** (then cancel any running run). Close the "Tracker status (live)" issue
if you like; a new one is created if the tracker starts again.

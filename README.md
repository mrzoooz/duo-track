# duo-track

Checks Apple Store in-store pickup for **iPhone Duo 256GB Night Sky (T-Mobile, Apple Upgrade)** every 30 seconds at
South Coast Plaza, Fashion Island, Irvine Spectrum Center, Brea Mall, Los Cerritos and Walnut Street (Philadelphia).

- **Status page:** https://mrzoooz.github.io/duo-track/ (GitHub Pages, `docs/index.html`). Live per-store status,
  last check time, recent activity, and an optional sound alarm while the page is open.
- **Alerts:** when a store shows pickup, the tracker opens an issue that @mentions you, so GitHub emails you (and
  notifies you in the GitHub app if you have it). Nothing to install.

## How it works

`.github/workflows/track.yml` runs `track.py` on GitHub Actions (free for public repos).

- It reads Apple's `/shop/retail/pickup-message` endpoint for part `MK1F4LL/A`, carrier `TMOBILE_IPHONEDUO`, near
  92626 and 19103 (one request each per check). That returns the same "Pickup:" line the product page shows. The browser's `/shop/fulfillment-messages`
  endpoint answers HTTP 541 to GitHub's servers, so the tracker doesn't use it.
- After each check it rewrites the JSON in the open issue labeled `tracker-status` ("Tracker status (live)"). The status
  page reads that issue through the public GitHub API, which allows browser requests. Edits to an issue body don't send
  notifications.
- Apple starts answering HTTP 541 to a runner's IP after roughly an hour of once-a-minute checks, so each run makes at
  most ~40 Apple requests (~10 minutes at 30-second checks with two search locations) and then starts its successor on a fresh runner
  (usually a new IP). A run that gets 3 errors in a
  row hands off right away. A schedule every 15 minutes acts as a watchdog that starts a run if none is active.

Alert issues (label `pickup-alert`) are opened when a store first shows pickup, and once if Apple requests fail 15 times
in a row across runs (checks then slow to every 5 minutes until Apple answers again). AppleCare+, the payment schedule and the lease term are checkout choices and don't affect stock. The alert
lists them so you can select them when you order, and links to Apple's Your Saves list
(https://www.apple.com/shop/yoursaves): a setup saved there keeps every selection, which the product URL can't.

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

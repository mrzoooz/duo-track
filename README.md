# duo-track

Checks Apple Store in-store pickup for **iPhone Duo 256GB Night Sky (T-Mobile, Apple Upgrade)** every minute at
South Coast Plaza, Fashion Island, Irvine Spectrum Center and Brea Mall.

- **Status page:** https://mrzoooz.github.io/duo-track/ (GitHub Pages, `docs/index.html`). Live per-store status,
  last check time, recent activity, and an optional sound alarm while the page is open.
- **Alerts:** when a store shows pickup, the tracker opens an issue that @mentions you, so GitHub emails you (and
  notifies you in the GitHub app if you have it). Nothing to install.

## How it works

`.github/workflows/track.yml` runs `track.py` on GitHub Actions (free for public repos).

- It reads Apple's `/shop/retail/pickup-message` endpoint for part `MK1F4LL/A`, carrier `TMOBILE_IPHONEDUO`, near
  92626. That returns the same "Pickup:" line the product page shows. The browser's `/shop/fulfillment-messages`
  endpoint answers HTTP 541 to GitHub's servers, so the tracker doesn't use it.
- After each check it rewrites the JSON in the open issue labeled `tracker-status` ("Tracker status (live)"). The status
  page reads that issue through the public GitHub API, which allows browser requests. Edits to an issue body don't send
  notifications.
- One run polls for ~5h40m, then starts its successor before GitHub's 6-hour job limit. A schedule every 15 minutes
  acts as a watchdog that starts a run if none is active.

Alert issues (label `pickup-alert`) are opened when a store first shows pickup, and once if Apple requests fail 15 times
in a row. AppleCare+, the payment schedule and the lease term are checkout choices and don't affect stock. The alert
lists them so you can select them when you order.

## Manual runs

Actions → **track** → **Run workflow**, then pick a mode:
- `test-alert`: opens a test alert issue (you should get an email), then does one check
- `diagnose`: one check with per-store details
- `once`: one check

The **config** input picks what to track: `config.json` (the Duo) or `tests/iphone-18-pro-max.json`, an accuracy
test that writes to its own status issue (view it at https://mrzoooz.github.io/duo-track/?label=tracker-status-test),
labels its alerts `[TEST]`, and stops after **minutes** instead of handing off.

Locally: `python3 track.py --mode once` (no dependencies).

## Stop it

Actions → **track** → `···` → **Disable workflow** (then cancel any running run). Close the "Tracker status (live)" issue
if you like; a new one is created if the tracker starts again.

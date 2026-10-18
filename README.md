# duo-track

Checks Apple Store in-store pickup for **iPhone Duo 256GB Night Sky (T-Mobile, Apple Upgrade)** every minute at
South Coast Plaza, Fashion Island, Irvine Spectrum Center and Brea Mall, starting **Oct 24, 2026 12:00 AM PT**.
When any of them shows pickup, it sends an urgent push notification that opens the product page when tapped.

## Get the alerts on your iPhone

1. Install **ntfy** from the App Store.
2. Tap **+** and subscribe to the topic `duo-track-9a5emz0zmam2` (server: ntfy.sh).
3. Allow notifications. Alerts are sent as *urgent*, so they show as Time Sensitive.

As a backup, each new availability also opens a GitHub issue that @mentions you, so GitHub email/app notifications fire too.

The repo is public, so anyone who reads `config.json` can see the topic name. To keep it private, add a repository
secret named `NTFY_TOPIC` with a new topic name and subscribe to that instead.

## How it runs

`.github/workflows/track.yml` runs `track.py` on GitHub Actions (free for public repos). It reads Apple's
`/shop/retail/pickup-message` endpoint for part `MK1F4LL/A` with carrier `TMOBILE_IPHONEDUO` near 92626, which returns
the same per-store "Pickup:" line the product page shows. The browser's `/shop/fulfillment-messages` endpoint answers
HTTP 541 to GitHub's servers, so the tracker doesn't use it.

- One run polls every 60s for ~5h40m, then starts its successor before GitHub's 6-hour job limit.
- A schedule every 15 minutes acts as a watchdog. If no tracker is running, it starts one; otherwise it exits.
- Before `start` in `config.json`, scheduled runs exit immediately.

You get:
- **urgent** alert when a store first shows pickup, repeated every 10 min while it stays available
- **low** alert when it disappears again, and when a fresh tracker starts (e.g. at midnight on Oct 24)
- one alert if Apple requests fail 15 times in a row

AppleCare+, the payment schedule and the lease term are checkout choices and don't affect stock. The alert lists them
so you can select them when you order.

## Manual runs

Actions → **track** → **Run workflow**, then pick a mode:
- `test-notify`: sends a test push, then does one check
- `diagnose`: one check with raw details (store numbers, part titles, what the product page lists)
- `once`: one check

Locally: `python3 track.py --mode once` (no dependencies).

## Stop it

Actions → **track** → `···` → **Disable workflow** (then cancel any running run).

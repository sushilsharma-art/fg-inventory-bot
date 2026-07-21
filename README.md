# FG Inventory Bot — GitHub Pages PWA

WhatsApp-style chat over the daily FG inventory file. Data is AES-256-GCM
encrypted (`data.enc.json`); the app asks for a passcode on first open.

## Files

| File | What it is |
|---|---|
| index.html | The app (passcode gate + chat) |
| data.enc.json | Encrypted inventory data — replace this daily |
| manifest.webmanifest, icon-192/512.png | PWA install metadata |
| sw.js | Service worker (offline cache, fresh data when online) |

## First deployment

1. Sign in at github.com → **New repository** → name it `fg-inventory-bot` → **Public** → Create.
2. **uploading an existing file** → drag ALL files from this `deploy` folder → Commit.
3. Repo **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`, folder `/ (root)` → Save.
4. Wait ~2 min. Your app is at: `https://<your-username>.github.io/fg-inventory-bot/`

## Daily data update

After the 11 AM task runs, `deploy/data.enc.json` is regenerated. In the repo:
open `data.enc.json` → pencil/… menu → *Delete*, then upload the new one
(or just drag it into *Upload files* — it overwrites). Nothing else changes.

## Install on phone (PWA)

- **iPhone**: open the URL in Safari → Share → **Add to Home Screen**.
- **Android**: open in Chrome → menu → **Install app**.

Voice ("FG summary") works on the hosted version, including iPhone Safari.

## Security notes

- NEVER upload `passcode.txt` (it stays in the local folder only).
- Anyone with the URL sees only encrypted bytes; the passcode unlocks it
  on-device. Share the passcode privately, not in the repo.
- To change the passcode: edit `passcode.txt`, re-run `build_chat_data.py`,
  re-upload `data.enc.json`, and tell users the new code (old cached
  passcodes will simply fail and re-prompt).

## Chat history persistence — do not break this

Each user's conversation is saved in their browser's `localStorage` under
the key `fg_chat_log_v1` (see `LOG_KEY` in index.html). This key must
**never be renamed** in any future update — doing so silently wipes every
user's saved chat on their next visit. If a log entry's data shape ever
needs to change, migrate existing entries on load rather than bumping the
key. Uploading a new `index.html` or `data.enc.json` to the repo does NOT
by itself clear anyone's history — only a key rename or the user tapping
🗑 does.

## Known data-quality notes (source file, not this app)

- **Mumbai DOI** (top card): the source file's `Mumbai DOI` column is
  computed hub-wide (Mumbai + RTV Mumbai + B2B Mumbai combined), which can
  disagree with the plain "Mumbai" row in the location breakdown below (RTV/
  B2B rows are intentionally hidden there). The app recomputes the top-card
  Mumbai DOI on the same depot-only basis as the breakdown table so the two
  numbers shown together always agree.
- **Dark Store DRR/DOI**: the source file never populates per-depot
  `Location DRR`/`Location DOI` for Dark Store rows (always 0), even when
  SOH is large. The app derives a real figure as
  `Overall DRR − DRR without DS` (verified non-negative across all 367 SKUs
  with Dark Store stock in the 21-07-2026 file).
- **Many other secondary-channel locations** (B2B offline/ecomm, RTV,
  marketplace "MP …" depots, Aramex, "Not Consider", "Unknown") also have
  `Location DRR` = 0 for ~100% of rows in the source file — this is an
  upstream data gap, not something derivable from another column, so these
  show DRR 0 / DOI "—" truthfully rather than a guessed number. Several of
  these (Not Consider, RTV*, plain "Offline") are hidden entirely from the
  location breakdown by request; the rest still appear with zero DRR/DOI.
- **Low DOI report**: filtered to SKUs with DRR > 100 by default (hides
  low-velocity noise) — both the "low doi" list and the brand summary's
  "<21 DOI" counts. Ask "doi < 21 all" for the unfiltered list.

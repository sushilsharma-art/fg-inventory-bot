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

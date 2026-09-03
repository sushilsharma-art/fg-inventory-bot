# FG Inventory Assistant

A secure, WhatsApp-style inventory bot built from the original Claude implementation. It answers FG inventory questions conversationally from any phone or browser; it is intentionally not a dashboard or control tower.

## What users can ask

- `summary`, `low doi`, `near expiry`, `out of stock`, `excess stock`
- Any SKU code or product name, including natural questions such as `how much stock do we have for nutrimix vanilla`
- Follow-ups such as `this sku in Mumbai` or `this sku trend`
- Brand, location, day-wise trend, date comparison, SIT and top-DRR questions
- Secondary-sales questions such as `total sale`, `channel level sale`, `MTD sale`, `last month sale`, `channel DRR`, `Blinkit sales`, and `<SKU> channel DRR`. Channel-level sales show current MTD, the previous complete month, and the latest three complete months in both units and value.

Every answer can be copied, saved as an image, forwarded to WhatsApp, or used as reply context. The stable chat-history key remains `fg_chat_log_v1`.

## Daily cloud refresh

The morning workflow in `.github/workflows/daily-refresh.yml` runs at 10:45 AM IST, with three 30-minute retries. It downloads the current FG Inventory, Shelfwise Inventory, and Sale Orders files, authenticates to Tableau Cloud with a Personal Access Token, downloads the approved `EComm Overall` quantity and `EComm Overall Sales` value crosstabs, reconciles them, refreshes secondary-sales history, rebuilds the encrypted WhatsApp-style bot, and publishes the verified snapshot. The evening workflow independently checks Anshul Bhatkar's dated `Channel Sales Tracker Dump` attachment at 5:40 PM IST, with 6:00 PM and 6:30 PM retries. Once the repository and secrets are configured, no computer needs to remain switched on.

Required repository secret: `FG_BOT_PASSCODE`.

Required Tableau repository secrets: `TABLEAU_PAT_NAME` and `TABLEAU_PAT_SECRET`.

Optional Gmail source secrets: `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, and `GMAIL_REFRESH_TOKEN`. Without them, the workflow uses the validated UniCommerce timestamp scan.

Optional repository variable: `FG_BOT_DATA_URL`, pointing to the deployed `data.enc.json`, preserves rolling history across runs.

Optional Tableau variables are `TABLEAU_SERVER_URL`, `TABLEAU_SITE_CONTENT_URL`, `TABLEAU_WORKBOOK_CONTENT_URL`, `TABLEAU_QUANTITY_VIEW`, and `TABLEAU_VALUE_VIEW`; the Man Matters production defaults are used when they are blank.

The workflow preserves the secondary-sales SQLite history in a private GitHub Actions cache. A passcode-encrypted seed is included only for first deployment or cache recovery. The facility mapping is also stored only as a passcode-encrypted configuration bundle. Raw Tableau, inventory, mapping, and Gmail files are excluded from the public repository.

## Local test

Build `site/data.enc.json` with `cloud_runner.py`, serve the `site` folder over HTTP, and open the local URL. The passcode is never included in the published site.

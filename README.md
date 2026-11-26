# Local-first Finance Dashboard

A self-hosted 2026 trading dashboard with a **local SQLite source of truth** and a **read-only offline-capable phone copy**. No paid synchronization provider and no cloud database.

## Requirements
- Mac with Python 3.9+ (standard library only for the server).
- Safari or Chrome on Mac and iPhone.
- For secure remote phone access, Tailscale on Mac and phone. Tailscale HTTPS serving is recommended so the phone can install the offline service worker.
- Internet connection for PDF importing: the existing Fidelity PDF importer dynamically loads PDF.js from a CDN. Previously imported data can be viewed offline.

## 1. Start on Mac

Open Terminal in this folder and run:

```bash
python3 server.py
```

On your Mac open **http://127.0.0.1:8765**. You can import Fidelity PDFs or CSV files and export backups. All saved transactions are persisted to `data/finance.sqlite3`. A public clone starts with an empty database. If a private, ignored `baseline.json` is present before the first run, it is used to seed the local database.

### CSV import format

CSV files remain local and are ignored by Git. Include a header row using these columns:

```csv
date,account,action,kind,symbol,expiry,strike,amount
2026-01-15,brokerage,buy,call,SPY,JAN 16 26,590,-125.50
2026-01-15,roth,sell,call,SPY,JAN 16 26,590,175.25
```

`account` accepts `brokerage`, `individual`, `i`, `roth`, `roth ira`, or `r`; `action` accepts buy/sell; and `kind` accepts call/put. Common aliases such as `ticker`, `side`, `type`, and `cash flow` are also accepted.

The dashboard also includes a fixed Webull historical balance of **-$1,037.00** dated August 18, 2026. It is server-enforced, included in combined totals and exports, and cannot be changed by imports or by clearing local transaction data.

## 2. Access from iPhone

The default server binds only to the Mac, not your network. For private access without opening your Mac to the public internet, use **Tailscale**:

1. Install Tailscale on both devices and sign into the same private tailnet.
2. Leave the server running on the Mac. It serves the **editable Mac dashboard on port 8765** and a **separate read-only phone dashboard on port 8766**.
3. In a separate Mac Terminal, configure **Tailscale Serve HTTPS** to proxy your local service. Current Tailscale CLI typically supports:

   ```bash
   tailscale serve --bg http://127.0.0.1:8766
   tailscale serve status
   ```

   Check `tailscale serve --help` if your installed version uses different flags. Use the private `https://...ts.net` address printed by Tailscale Serve on the iPhone. Do **not** enable Tailscale Funnel, public port forwarding, or public hosting for financial data.
4. Open the HTTPS dashboard on iPhone while online, wait until the status says **Phone · read-only**, and choose **Share → Add to Home Screen**.
5. Open it once while connected, then try Airplane Mode. The service worker caches the website and localStorage caches the latest downloaded transactions. The phone continues displaying that last snapshot when offline.

**Important:** iPhone offline support requires a secure HTTPS origin and successful initial load/service-worker installation. Safari may evict cached site data; offline storage is best-effort, not a guaranteed permanent backup. Keep the Mac SQLite database and encrypted backups as the authoritative copies.

## Data flow and permissions

```
Fidelity PDFs → Mac browser import → Mac HTTP API → Mac SQLite
                                                 ↓
                                      Tailscale HTTPS (private)
                                                 ↓
                                      iPhone browser (read-only)
                                                 ↓
                                      local cached snapshot (offline)
```

- The Mac administration service on port 8765 allows writes only from loopback. A separate phone service on port 8766 is **read-only even when reached through a local reverse proxy**. All phone POST/PUT requests are rejected with HTTP 403. Never proxy port 8765 to Tailscale.
- Phone refreshes the latest saved data when opened, brought back into focus, or periodically while visible and connected.
- Offline phone view shows **last synchronized data**, not Mac changes made after disconnect.
- Primary data remains on Mac. A copy of transaction data is deliberately stored on your phone for offline viewing. Tailscale relays encrypted traffic when needed; it is not a finance cloud database.
- The public dashboard contains no financial baseline or account identifiers. It calculates account and combined totals from data imported into the local database.
- The PDF parser recognizes Brokerage and Roth IRA statements. It imports filled option orders from both the `Pending` and `History` sections, deduplicating rows that appear in both. Validate imported records against the source PDFs; the dashboard is not tax software.

## Backup

Stop the server and copy `data/finance.sqlite3` to an encrypted location, or use the dashboard's **Export backup** button to export transactions as JSON. For a consistent live SQLite backup, use:

```bash
python3 -c "import sqlite3; s=sqlite3.connect('data/finance.sqlite3'); d=sqlite3.connect('finance-backup.sqlite3'); s.backup(d); d.close(); s.close()"
```

Keep backups outside the project folder. Do not delete `data/finance.sqlite3` unless you intend to reset the database.

## Export Firebase Firestore data for migration

The `scripts/export-firestore.mjs` utility makes a **read-only** JSON copy of every document and subcollection in a Firestore database. It uses only Node.js built-ins; no Firebase package is installed and it does not write to Firestore.

1. In Firebase Project settings → **Service accounts**, create a private service-account key. Store it outside this repository (for example, in your Downloads folder). Never commit or share it.
2. From this project folder, run:

   ```bash
   node scripts/export-firestore.mjs --credentials /absolute/path/to/service-account.json
   ```

3. The result is saved at `data/firestore-export.json`, which is ignored by Git. Its final line reports the exported document count.

The exporter preserves collection names, document IDs, nested subcollections, and Firestore values. It does not yet import into SQLite: inspect the exported collection names and record fields first so the migration can map them safely to the dashboard's trading, income, spending, and subscription tables.

### Import the reviewed export

`scripts/import_firestore.py` merges the legacy `incomeData`, `spentHistory`, and `Subscriptions` collections into the income tracker. It first previews what it will add:

```bash
python3 scripts/import_firestore.py
```

To write the import, run:

```bash
python3 scripts/import_firestore.py --apply
```

The import preserves existing dashboard records, skips records it previously imported, converts legacy dates to `YYYY-MM-DD`, and creates a timestamped SQLite backup before writing. Legacy income labels not supported by the current UI (such as `Tutoring` and `CodeNinjas`) are saved as `Other` with their original label retained in the description.

## Limitations

- The Mac must be powered on and connected for the phone to receive **new** updates; offline phone viewing works from the most recent successful synchronization.
- Data on the phone does not sync back and the phone cannot import PDFs.
- Offline cache can be cleared by the browser/OS; open online periodically to refresh.
- The local backend uses Python's standard library and SQLite, no external server packages. PDF.js is loaded from CDN on import only.
- The UI retains the original 2026-specific trading calendar and baseline data, not a multi-year trading ledger.

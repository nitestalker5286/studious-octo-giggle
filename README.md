# Star Citizen Salvage Tracker — Railway Flask Edition

A responsive Flask + SQLite web app for tracking salvaged ships and their recovered components. Public visitors have read-only access; an administrator can add, edit, and delete records.

## Included features

- Ships: name, manufacturer, and zero or more **Ship Sources** (no ship quantity column)
- Components: name, category, required whole-number size from 0 through 10, optional grade, optional spec, and quantity
- Grade options: blank, A, B, C, D
- Spec options: blank, MIL, CIV, CMP, IND, STH
- Categories include Torpedoes, Bombs, Weapons, Missiles, Radar, Salvage Module, and the original ship-component categories. Guns and Missile Rack are retired from new selections; existing records in either retired category remain unchanged and editable without forced reclassification.
- Clean public **Component Finder** that shows all components when every filter is set to All, with live Component Name search
- Duplicate public component configurations are grouped into one result by Component Name, Category, Size, Grade, and Spec
- Each grouped result has an expandable Sources section with every matching ship, ship source, and per-source quantity
- Main public Component Finder results omit the aggregate Total Qty column; per-ship quantities remain available inside each grouped Sources section and grouped totals remain available to Watchlist data
- Quick Category buttons and clickable Size, Grade, and Spec filter chips, including unspecified values
- Mobile component result cards with ship links and compact component details
- Browser-local personal Component Watchlist with grouped-component pin/unpin controls, a live pinned count, and clear-all action
- Browser-local personal notes and High/Medium/Low priorities for every pinned component
- Watchlist sorting by priority, recently pinned, component name, or category; the selected sort is saved in the browser
- Responsive right-side pinned panel beside the search controls; grouped results render below at full page width, and the panel moves below the finder on smaller screens
- Industrial Salvage-styled live cSCU-to-SCU calculator directly beneath the Personal Watchlist, using the `100 cSCU = 1 SCU` conversion and remaining beneath the Watchlist on responsive/mobile layouts
- Dedicated Pinned Components view; pins are stored only in the user's browser with no account or database migration
- Public ship directory with live ship/manufacturer search and A–Z navigation; selecting a ship shows every associated component
- Public **Submit Update** workflow for proposed corrections, additions, removals, and other inventory feedback, with existing ship/component context and optional notes
- Admin change-request review queue with Pending, Approved, Rejected, and Resolved states; reviewing or approving a request never modifies inventory automatically
- Admin multi-source editor for adding and removing multiple sources on each ship
- Public home view shows all component items by default and narrows the results through the finder, without exposing admin management controls
- Admin inventory and ship-management views with full add, edit, and delete functionality
- Admin **Save + Add Another** workflow that carries the selected ship and component defaults forward
- Admin-only CSV export; exported rows include all Ship Sources and honor all active filters, including Size
- Viewer/admin separation with password-protected changes
- Automatic migration for the expected desktop SQLite schema and common legacy column names
- Safe, idempotent `ship_sources` migration: every existing `ships.source` value is copied once without duplication on Railway restarts or redeploys
- Safe, idempotent `change_requests` migration stored in the same persistent Railway SQLite database
- Optional automatic CSV snapshot after every database change
- Railway health check and Gunicorn production server configuration
- Responsive desktop/mobile interface

## Run locally

Python 3.11+ is recommended.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set environment variables before starting:

```bash
# macOS/Linux
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export ADMIN_PASSWORD="choose-a-strong-password"

# Windows PowerShell
$env:SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
$env:ADMIN_PASSWORD = "choose-a-strong-password"
```

Then run:

```bash
flask --app app run
```

Open `http://127.0.0.1:5000`. The local database defaults to `instance/salvage_tracker.db`.

## Deploy to Railway

### 1. Put the project on GitHub

Create a new GitHub repository, copy all files in this folder to it, commit, and push.

### 2. Create the Railway service

1. In Railway, choose **New Project → Deploy from GitHub repo**.
2. Select the repository.
3. Railway will detect `requirements.txt` and use the start command in `railway.toml`.

### 3. Add a persistent volume

Add a Railway volume to the web service and mount it at:

```text
/data
```

SQLite must live on this volume. The normal deployment filesystem is temporary.

### 4. Add required variables

In the Railway service's **Variables** tab, set:

```text
DATABASE_PATH=/data/salvage_tracker.db
SECRET_KEY=<a long random value>
ADMIN_PASSWORD=<your strong admin password>
```

Generate `SECRET_KEY` locally with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Optional variable:

```text
AUTO_EXPORT_PATH=/data/star_citizen_salvage_inventory.csv
```

This maintains a server-side CSV snapshot after every change. It does **not** sync to a Google Drive for desktop folder because Railway runs on a remote server. Viewers can use **Export CSV** from the website instead.

### 5. Generate the public URL

In Railway, open **Settings → Networking → Generate Domain**. Railway will route HTTPS traffic to the app's `$PORT`. The `/health` endpoint is used for deployment checks.

### 6. Sign in

- Anyone with the URL can use the Component Finder, browse the ship directory, and submit a proposed update without editing production inventory.
- Select **Admin** and use `ADMIN_PASSWORD` to view the complete inventories, review submitted requests, export CSV, and add, edit, or delete records.

## Move an existing desktop database

1. Stop the desktop tracker so SQLite finishes writing.
2. Make a backup copy of its `.db` file.
3. Rename the copy to `salvage_tracker.db` if needed.
4. Upload/copy it to the Railway volume at `/data/salvage_tracker.db` **before** normal use.
5. Redeploy or restart the service. Startup migration preserves the legacy Ship Source, copies it into the multi-source relationship without duplication, adds other expected fields, and changes legacy `COMP` spec values to `CMP`.

The converter recognizes common legacy columns such as `ship_name`, `ship_source`, `component_name`, `component_type`, and `qty`. If a migrated table retains those legacy columns or their `NOT NULL` constraints, component creates and edits keep both the legacy and current columns synchronized. The compatibility migration is idempotent and is safe to run on every Railway restart. If the old desktop app used a substantially different schema, keep the backup and compare it before replacing the production database.

## Operational notes

- The entire public and Admin interface uses the Industrial Salvage theme: `#0B1014` background, `#141C22` panels, amber `#F59E0B` actions, `#FBBF24` hover/highlight states, `#E5E7EB` primary text, and steel-gray secondary text and borders. The same palette applies to desktop and mobile layouts.
- The Gunicorn command intentionally uses **one worker** with multiple threads. Multiple independent workers are a poor fit for a small SQLite write workload.
- SQLite uses WAL mode, foreign keys, a 30-second busy timeout, and one transaction per change.
- Optional Grade and Spec values are stored as SQL `NULL`; startup migration safely upgrades older web schemas that incorrectly required empty strings.
- Component add/edit forms validate Size as a required whole number from `0` through `10` before any database write. Missing legacy sizes are normalized to `0` by an idempotent startup compatibility step, matching production SQLite databases that enforce `CHECK (size BETWEEN 0 AND 10)`.
- Viewer change requests are review records only. Approved, rejected, and resolved statuses never invoke ship or component insert/update/delete operations; verified inventory changes must be made manually with the normal Admin tools.
- Component pins, personal notes, priorities, and Watchlist sort preference use browser `localStorage` and never write to SQLite. They are specific to the browser/profile and may be cleared by browser-data cleanup.
- A ship cannot be deleted while components still reference it.
- Set a stable `SECRET_KEY`; changing it signs out all admin sessions.
- Keep the Railway volume attached and back up `/data/salvage_tracker.db` periodically.
- If usage grows into many simultaneous editors, migrate the data to Railway PostgreSQL.

## Run tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

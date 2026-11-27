# Star Citizen Salvage Tracker — Railway Flask Edition

A responsive Flask + SQLite web app for tracking salvaged ships and their recovered components. Public visitors have read-only access; an administrator can add, edit, and delete records.

## Included features

- Ships: name, manufacturer, and optional **Ship Source** (no ship quantity column)
- Components: name, category, size, optional grade, optional spec, and quantity
- Grade options: blank, A, B, C, D
- Spec options: blank, MIL, CIV, CMP, IND, STH
- Categories include Guns, Missiles, Radar, and the original ship-component categories
- Clean public **Component Finder** with live Component Name search
- Quick Category buttons and clickable Size, Grade, and Spec filter chips, including unspecified values
- Mobile component result cards with ship links and compact component details
- Public ship directory with live ship/manufacturer search and A–Z navigation; selecting a ship shows every associated component
- Search-only public home view: viewers see finder controls and matching results, not the full admin inventory lists
- Admin inventory and ship-management views with full add, edit, and delete functionality
- Admin **Save + Add Another** workflow that carries the selected ship and component defaults forward
- Admin-only CSV export; exported rows include Ship Source and honor all active filters, including Size
- Viewer/admin separation with password-protected changes
- Automatic migration for the expected desktop SQLite schema and common legacy column names
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

- Anyone with the URL can use the Component Finder, see only matching search results, or browse the alphabetical public ship directory.
- Select **Admin** and use `ADMIN_PASSWORD` to view the complete inventories, export CSV, and add, edit, or delete records.

## Move an existing desktop database

1. Stop the desktop tracker so SQLite finishes writing.
2. Make a backup copy of its `.db` file.
3. Rename the copy to `salvage_tracker.db` if needed.
4. Upload/copy it to the Railway volume at `/data/salvage_tracker.db` **before** normal use.
5. Redeploy or restart the service. Startup migration adds missing Ship Source, Grade, Spec, and other expected fields without deleting records. It also changes legacy `COMP` spec values to `CMP`.

The converter recognizes common legacy columns such as `ship_name`, `ship_source`, `component_name`, `component_type`, and `qty`. If the old desktop app used a substantially different schema, keep the backup and compare it before replacing the production database.

## Operational notes

- The Gunicorn command intentionally uses **one worker** with multiple threads. Multiple independent workers are a poor fit for a small SQLite write workload.
- SQLite uses WAL mode, foreign keys, a 30-second busy timeout, and one transaction per change.
- Optional Grade and Spec values are stored as SQL `NULL`; startup migration safely upgrades older web schemas that incorrectly required empty strings.
- A ship cannot be deleted while components still reference it.
- Set a stable `SECRET_KEY`; changing it signs out all admin sessions.
- Keep the Railway volume attached and back up `/data/salvage_tracker.db` periodically.
- If usage grows into many simultaneous editors, migrate the data to Railway PostgreSQL.

## Run tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

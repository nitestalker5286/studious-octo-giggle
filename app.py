from __future__ import annotations

import csv
import hmac
import io
import os
import secrets
import sqlite3
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from database import (
    connect,
    enrich_ship_rows,
    group_inventory_rows,
    initialize_database,
    insert_component,
    inventory_rows,
    normalize_component_size,
    replace_ship_sources,
    transaction,
    update_component,
    write_inventory_csv,
)

CATEGORY_OPTIONS = (
    "Power Plant",
    "Cooler",
    "Shield Generator",
    "Quantum Drive",
    "Guns",
    "Missiles",
    "Bombs",
    "Weapons",
    "Turret",
    "Radar",
    "Mining Module",
    "Salvage Module",
    "Utility Item",
    "Other",
)
GRADE_OPTIONS = ("A", "B", "C", "D")
SPEC_OPTIONS = ("MIL", "CIV", "CMP", "IND", "STH")
CHANGE_REQUEST_TYPES = ("Correction", "Addition", "Removal", "Other")
CHANGE_TARGET_TYPES = ("Component", "Ship", "General")
CHANGE_REQUEST_STATUSES = ("pending", "approved", "rejected", "resolved")
F = TypeVar("F", bound=Callable[..., Any])


def _default_database_path(app: Flask) -> str:
    configured = os.getenv("DATABASE_PATH")
    if configured:
        return configured
    volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if volume_path:
        return str(Path(volume_path) / "salvage_tracker.db")
    return str(Path(app.instance_path) / "salvage_tracker.db")


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD", ""),
        DATABASE_PATH=None,
        AUTO_EXPORT_PATH=os.getenv("AUTO_EXPORT_PATH", ""),
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.getenv("RAILWAY_ENVIRONMENT")),
        PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    )
    if test_config:
        app.config.update(test_config)
    app.config["DATABASE_PATH"] = app.config.get("DATABASE_PATH") or _default_database_path(app)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    initialize_database(app.config["DATABASE_PATH"])
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    def csrf_token() -> str:
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["CATEGORY_OPTIONS"] = CATEGORY_OPTIONS
    app.jinja_env.globals["GRADE_OPTIONS"] = GRADE_OPTIONS
    app.jinja_env.globals["SPEC_OPTIONS"] = SPEC_OPTIONS
    app.jinja_env.globals["CHANGE_REQUEST_TYPES"] = CHANGE_REQUEST_TYPES
    app.jinja_env.globals["CHANGE_TARGET_TYPES"] = CHANGE_TARGET_TYPES
    app.jinja_env.globals["CHANGE_REQUEST_STATUSES"] = CHANGE_REQUEST_STATUSES

    @app.context_processor
    def navigation_context() -> dict[str, int]:
        if not session.get("is_admin"):
            return {"pending_change_request_count": 0}
        with connect(app.config["DATABASE_PATH"]) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM change_requests WHERE status = 'pending'"
            ).fetchone()[0]
        return {"pending_change_request_count": int(count)}

    @app.before_request
    def verify_csrf() -> None:
        if request.method == "POST":
            expected = session.get("_csrf_token", "")
            supplied = request.form.get("csrf_token", "")
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                abort(400, "Invalid or missing CSRF token.")

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        return response

    def admin_required(view: F) -> F:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not session.get("is_admin"):
                flash("Admin sign-in is required to make changes.", "warning")
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    def after_change() -> None:
        output_path = app.config.get("AUTO_EXPORT_PATH", "").strip()
        if output_path:
            try:
                write_inventory_csv(app.config["DATABASE_PATH"], output_path)
            except Exception:
                app.logger.exception("Automatic CSV snapshot failed")
                flash("The database was saved, but the automatic CSV snapshot failed.", "warning")

    def get_ship_or_404(ship_id: int):
        with connect(app.config["DATABASE_PATH"]) as connection:
            row = connection.execute("SELECT * FROM ships WHERE id = ?", (ship_id,)).fetchone()
            ships = enrich_ship_rows(connection, [row] if row is not None else [])
        if not ships:
            abort(404)
        return ships[0]

    def get_component_or_404(component_id: int):
        with connect(app.config["DATABASE_PATH"]) as connection:
            component = connection.execute(
                """
                SELECT c.*, s.name AS ship_name
                FROM components c JOIN ships s ON s.id = c.ship_id
                WHERE c.id = ?
                """,
                (component_id,),
            ).fetchone()
        if component is None:
            abort(404)
        return component

    def form_text(name: str, max_length: int, required: bool = False) -> str:
        value = request.form.get(name, "").strip()
        if required and not value:
            raise ValueError(f"{name.replace('_', ' ').title()} is required.")
        if len(value) > max_length:
            raise ValueError(f"{name.replace('_', ' ').title()} must be {max_length} characters or fewer.")
        return value

    def form_sources() -> list[str]:
        raw_sources = request.form.getlist("sources")
        if not raw_sources and "source" in request.form:
            raw_sources = [request.form.get("source", "")]
        sources: list[str] = []
        seen: set[str] = set()
        for raw in raw_sources:
            source = raw.strip()
            if not source:
                continue
            if len(source) > 200:
                raise ValueError("Each ship source must be 200 characters or fewer.")
            key = source.casefold()
            if key in seen:
                continue
            seen.add(key)
            sources.append(source)
        if len(sources) > 50:
            raise ValueError("A ship can have no more than 50 sources.")
        return sources

    @app.get("/")
    def index() -> str:
        def filter_value(name: str) -> str:
            value = request.args.get(name, "").strip()
            return "" if value.casefold() == "all" else value

        filters = {
            "q": request.args.get("q", "").strip(),
            "category": filter_value("category"),
            "size": filter_value("size"),
            "grade": filter_value("grade"),
            "spec": filter_value("spec"),
        }
        has_filters = any(filters.values())
        with connect(app.config["DATABASE_PATH"]) as connection:
            ship_rows = connection.execute(
                """
                SELECT s.id, s.name, s.manufacturer,
                       COUNT(c.id) AS component_records,
                       COALESCE(SUM(c.quantity), 0) AS component_units
                FROM ships s LEFT JOIN components c ON c.ship_id = s.id
                GROUP BY s.id, s.name, s.manufacturer
                ORDER BY s.name COLLATE NOCASE
                """
            ).fetchall()
            ships = enrich_ship_rows(connection, ship_rows)
            size_options = [
                str(row["size"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT size FROM components
                    WHERE size IS NOT NULL
                      AND TRIM(CAST(size AS TEXT)) <> ''
                      AND CAST(size AS INTEGER) BETWEEN 1 AND 10
                    ORDER BY CAST(size AS INTEGER), size COLLATE NOCASE
                    """
                ).fetchall()
            ]
        inventory = inventory_rows(app.config["DATABASE_PATH"], filters)
        is_admin = bool(session.get("is_admin"))
        components = inventory if is_admin else group_inventory_rows(inventory)
        total_units = sum(row["quantity"] for row in components)
        return render_template(
            "index.html",
            ships=ships,
            components=components,
            filters=filters,
            has_filters=has_filters,
            size_options=size_options,
            total_units=total_units,
            admin_configured=bool(app.config["ADMIN_PASSWORD"]),
        )

    @app.get("/watchlist")
    def watchlist() -> str:
        return render_template("watchlist.html")

    def change_request_context() -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        with connect(app.config["DATABASE_PATH"]) as connection:
            ships = connection.execute(
                "SELECT id, name, manufacturer FROM ships ORDER BY name COLLATE NOCASE"
            ).fetchall()
            components = connection.execute(
                """
                SELECT c.id, c.name, c.category,
                       CASE WHEN CAST(c.size AS INTEGER) = 0 THEN NULL ELSE c.size END AS size,
                       c.grade, c.spec,
                       s.id AS ship_id, s.name AS ship_name
                FROM components c JOIN ships s ON s.id = c.ship_id
                ORDER BY c.name COLLATE NOCASE, s.name COLLATE NOCASE, c.id
                """
            ).fetchall()
        return ships, components

    @app.route("/submit-change", methods=["GET", "POST"])
    def submit_change() -> Any:
        ships, components = change_request_context()
        if request.method == "POST":
            try:
                request_type = request.form.get("request_type", "").strip()
                target_type = request.form.get("target_type", "").strip()
                if request_type not in CHANGE_REQUEST_TYPES:
                    raise ValueError("Select a valid request type.")
                if target_type not in CHANGE_TARGET_TYPES:
                    raise ValueError("Select Component, Ship, or General.")
                if request.form.get("website", "").strip():
                    # Honeypot fields are ignored as a successful submission.
                    return redirect(url_for("change_request_thanks"))

                ship_id: int | None = None
                component_id: int | None = None
                subject_label = "General inventory request"
                raw_ship_id = request.form.get("ship_id", "").strip()
                raw_component_id = request.form.get("component_id", "").strip()
                with connect(app.config["DATABASE_PATH"]) as connection:
                    if target_type == "Ship" and raw_ship_id:
                        try:
                            ship_id = int(raw_ship_id)
                        except ValueError as error:
                            raise ValueError("Select a valid ship.") from error
                        ship = connection.execute(
                            "SELECT id, name, manufacturer FROM ships WHERE id = ?", (ship_id,)
                        ).fetchone()
                        if ship is None:
                            raise ValueError("Select a valid ship.")
                        subject_label = ship["name"]
                        if ship["manufacturer"]:
                            subject_label += f" · {ship['manufacturer']}"
                    elif target_type == "Component" and raw_component_id:
                        try:
                            component_id = int(raw_component_id)
                        except ValueError as error:
                            raise ValueError("Select a valid component.") from error
                        component = connection.execute(
                            """
                            SELECT c.id, c.ship_id, c.name, c.category, c.size, c.grade, c.spec,
                                   s.name AS ship_name
                            FROM components c JOIN ships s ON s.id = c.ship_id
                            WHERE c.id = ?
                            """,
                            (component_id,),
                        ).fetchone()
                        if component is None:
                            raise ValueError("Select a valid component.")
                        ship_id = int(component["ship_id"])
                        attributes = [component["category"]]
                        for value in (component["size"], component["grade"], component["spec"]):
                            if value:
                                attributes.append(str(value))
                        subject_label = (
                            f"{component['name']} · {component['ship_name']}"
                            f" · {' / '.join(attributes)}"
                        )
                    elif request_type != "Addition" and target_type != "General":
                        raise ValueError(f"Select the {target_type.lower()} this request concerns.")

                proposed_change = form_text("proposed_change", 4000, required=True)
                notes = form_text("notes", 2000) or None
                custom_subject = form_text("subject_name", 240)
                if custom_subject:
                    subject_label = custom_subject
                elif request_type == "Addition" and target_type != "General" and not (
                    ship_id or component_id
                ):
                    raise ValueError("Enter a name for the proposed addition.")

                with transaction(app.config["DATABASE_PATH"]) as connection:
                    connection.execute(
                        """
                        INSERT INTO change_requests
                            (request_type, target_type, ship_id, component_id, subject_label,
                             proposed_change, notes, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                        """,
                        (
                            request_type, target_type, ship_id, component_id, subject_label,
                            proposed_change, notes,
                        ),
                    )
                return redirect(url_for("change_request_thanks"))
            except ValueError as error:
                flash(str(error), "error")
            except sqlite3.IntegrityError as error:
                if "size" in str(error).lower() and "check constraint" in str(error).lower():
                    flash("Size must be a whole number from 0 to 10.", "error")
                else:
                    raise
        return render_template(
            "change_request_form.html",
            ships=ships,
            components=components,
            selected_ship_id=request.args.get("ship_id", ""),
            selected_component_id=request.args.get("component_id", ""),
            selected_target_type=request.args.get("target_type", ""),
        )

    @app.get("/submit-change/thanks")
    def change_request_thanks() -> str:
        return render_template("change_request_thanks.html")

    @app.get("/admin/change-requests")
    @admin_required
    def admin_change_requests() -> str:
        status_filter = request.args.get("status", "pending").strip().lower()
        if status_filter not in (*CHANGE_REQUEST_STATUSES, "all"):
            status_filter = "pending"
        where = "" if status_filter == "all" else "WHERE cr.status = ?"
        values: list[str] = [] if status_filter == "all" else [status_filter]
        with connect(app.config["DATABASE_PATH"]) as connection:
            requests = connection.execute(
                f"""
                SELECT cr.*, s.name AS current_ship_name,
                       c.name AS current_component_name, c.category AS current_component_category
                FROM change_requests cr
                LEFT JOIN ships s ON s.id = cr.ship_id
                LEFT JOIN components c ON c.id = cr.component_id
                {where}
                ORDER BY CASE cr.status WHEN 'pending' THEN 0 ELSE 1 END,
                         cr.created_at DESC, cr.id DESC
                """,
                values,
            ).fetchall()
            counts = {
                row["status"]: int(row["total"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS total FROM change_requests GROUP BY status"
                ).fetchall()
            }
        return render_template(
            "admin_change_requests.html",
            change_requests=requests,
            status_filter=status_filter,
            status_counts=counts,
        )

    @app.post("/admin/change-requests/<int:request_id>/status")
    @admin_required
    def admin_change_request_status(request_id: int) -> Any:
        status = request.form.get("status", "").strip().lower()
        if status not in CHANGE_REQUEST_STATUSES:
            abort(400, "Invalid request status.")
        try:
            admin_notes = form_text("admin_notes", 2000) or None
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("admin_change_requests", status=request.args.get("status", "pending")))
        with transaction(app.config["DATABASE_PATH"]) as connection:
            exists = connection.execute(
                "SELECT 1 FROM change_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if exists is None:
                abort(404)
            connection.execute(
                """
                UPDATE change_requests
                SET status = ?, admin_notes = ?, updated_at = CURRENT_TIMESTAMP,
                    reviewed_at = CASE WHEN ? = 'pending' THEN NULL ELSE CURRENT_TIMESTAMP END
                WHERE id = ?
                """,
                (status, admin_notes, status, request_id),
            )
        flash(
            f"Request #{request_id} marked {status}. Inventory was not changed.",
            "success",
        )
        return redirect(url_for("admin_change_requests", status=request.args.get("status", "pending")))

    @app.get("/ships")
    def public_ships() -> str:
        query = request.args.get("q", "").strip()
        letter = request.args.get("letter", "").strip().upper()
        if letter and (len(letter) != 1 or letter not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            letter = ""
        clauses: list[str] = []
        values: list[str] = []
        if query:
            clauses.append(
                "(s.name LIKE ? COLLATE NOCASE OR s.manufacturer LIKE ? COLLATE NOCASE "
                "OR EXISTS (SELECT 1 FROM ship_sources ss WHERE ss.ship_id = s.id "
                "AND ss.source LIKE ? COLLATE NOCASE))"
            )
            values.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
        if letter:
            clauses.append("UPPER(SUBSTR(TRIM(s.name), 1, 1)) = ?")
            values.append(letter)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect(app.config["DATABASE_PATH"]) as connection:
            ship_rows = connection.execute(
                f"""
                SELECT s.id, s.name, s.manufacturer,
                       COUNT(c.id) AS component_records,
                       COALESCE(SUM(c.quantity), 0) AS component_units
                FROM ships s LEFT JOIN components c ON c.ship_id = s.id
                {where}
                GROUP BY s.id, s.name, s.manufacturer
                ORDER BY s.name COLLATE NOCASE, s.id
                """,
                values,
            ).fetchall()
            ships = enrich_ship_rows(connection, ship_rows)
        return render_template(
            "public_ships.html",
            ships=ships,
            query=query,
            active_letter=letter,
            alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        )

    @app.get("/ships/<int:ship_id>")
    def public_ship_detail(ship_id: int) -> str:
        ship = get_ship_or_404(ship_id)
        components = inventory_rows(app.config["DATABASE_PATH"], {"ship_id": str(ship_id)})
        all_groups = group_inventory_rows(inventory_rows(app.config["DATABASE_PATH"]))
        pin_groups = {
            record_id: group
            for group in all_groups
            for record_id in group["record_ids"]
        }
        total_units = sum(row["quantity"] for row in components)
        return render_template(
            "public_ship_detail.html",
            ship=ship,
            components=components,
            pin_groups=pin_groups,
            total_units=total_units,
        )

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login() -> Any:
        if not app.config["ADMIN_PASSWORD"]:
            return render_template("login.html", admin_configured=False), 503
        if request.method == "POST":
            supplied = request.form.get("password", "")
            if hmac.compare_digest(supplied, app.config["ADMIN_PASSWORD"]):
                session.clear()
                session["is_admin"] = True
                session.permanent = True
                flash("Signed in as administrator.", "success")
                next_url = request.args.get("next", "")
                if next_url.startswith("/") and not next_url.startswith("//"):
                    return redirect(next_url)
                return redirect(url_for("index"))
            flash("Incorrect administrator password.", "error")
        return render_template("login.html", admin_configured=True)

    @app.post("/admin/logout")
    def admin_logout() -> Any:
        session.clear()
        flash("Signed out.", "success")
        return redirect(url_for("index"))

    @app.route("/ships/new", methods=["GET", "POST"])
    @admin_required
    def ship_new() -> Any:
        if request.method == "POST":
            try:
                name = form_text("name", 120, required=True)
                manufacturer = form_text("manufacturer", 120)
                sources = form_sources()
                with transaction(app.config["DATABASE_PATH"]) as connection:
                    duplicate = connection.execute(
                        "SELECT 1 FROM ships WHERE name = ? COLLATE NOCASE", (name,)
                    ).fetchone()
                    if duplicate:
                        raise ValueError("A ship with that name already exists.")
                    cursor = connection.execute(
                        "INSERT INTO ships (name, manufacturer, source) VALUES (?, ?, '')",
                        (name, manufacturer),
                    )
                    replace_ship_sources(connection, int(cursor.lastrowid), sources)
                after_change()
                flash(f"Ship “{name}” added.", "success")
                return redirect(url_for("index"))
            except ValueError as error:
                flash(str(error), "error")
            except Exception as error:
                if "UNIQUE constraint failed" in str(error):
                    flash("A ship with that name already exists.", "error")
                else:
                    raise
        source_values = request.form.getlist("sources") if request.method == "POST" else []
        return render_template("ship_form.html", ship=None, source_values=source_values)

    @app.route("/ships/<int:ship_id>/edit", methods=["GET", "POST"])
    @admin_required
    def ship_edit(ship_id: int) -> Any:
        ship = get_ship_or_404(ship_id)
        if request.method == "POST":
            try:
                name = form_text("name", 120, required=True)
                manufacturer = form_text("manufacturer", 120)
                sources = form_sources()
                with transaction(app.config["DATABASE_PATH"]) as connection:
                    duplicate = connection.execute(
                        "SELECT 1 FROM ships WHERE name = ? COLLATE NOCASE AND id <> ?",
                        (name, ship_id),
                    ).fetchone()
                    if duplicate:
                        raise ValueError("A ship with that name already exists.")
                    connection.execute(
                        """
                        UPDATE ships SET name = ?, manufacturer = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (name, manufacturer, ship_id),
                    )
                    replace_ship_sources(connection, ship_id, sources)
                after_change()
                flash(f"Ship “{name}” updated.", "success")
                return redirect(url_for("index"))
            except ValueError as error:
                flash(str(error), "error")
            except Exception as error:
                if "UNIQUE constraint failed" in str(error):
                    flash("A ship with that name already exists.", "error")
                else:
                    raise
        source_values = (
            request.form.getlist("sources") if request.method == "POST" else ship["sources"]
        )
        return render_template("ship_form.html", ship=ship, source_values=source_values)

    @app.post("/ships/<int:ship_id>/delete")
    @admin_required
    def ship_delete(ship_id: int) -> Any:
        ship = get_ship_or_404(ship_id)
        with transaction(app.config["DATABASE_PATH"]) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM components WHERE ship_id = ?", (ship_id,)
            ).fetchone()[0]
            if count:
                flash("Delete or reassign this ship’s components before deleting the ship.", "error")
                return redirect(url_for("index"))
            connection.execute("DELETE FROM ships WHERE id = ?", (ship_id,))
        after_change()
        flash(f"Ship “{ship['name']}” deleted.", "success")
        return redirect(url_for("index"))

    @app.route("/components/new", methods=["GET", "POST"])
    @admin_required
    def component_new() -> Any:
        with connect(app.config["DATABASE_PATH"]) as connection:
            ships = connection.execute("SELECT id, name FROM ships ORDER BY name COLLATE NOCASE").fetchall()
        if not ships:
            flash("Add a ship before adding a component.", "warning")
            return redirect(url_for("ship_new"))
        if request.method == "POST":
            try:
                values = validate_component_form(app.config["DATABASE_PATH"])
                with transaction(app.config["DATABASE_PATH"]) as connection:
                    insert_component(connection, *values)
                after_change()
                flash(f"Component “{values[1]}” added.", "success")
                if request.form.get("save_action") == "add_another":
                    return redirect(
                        url_for(
                            "component_new",
                            ship_id=values[0],
                            category=values[2],
                            size=values[3],
                            grade=values[4] or "",
                            spec=values[5] or "",
                        )
                    )
                return redirect(url_for("index"))
            except ValueError as error:
                flash(str(error), "error")
            except sqlite3.IntegrityError as error:
                if "size" in str(error).lower() and "check constraint" in str(error).lower():
                    flash("Size must be a whole number from 0 to 10.", "error")
                else:
                    raise
        return render_template(
            "component_form.html", component=None, ships=ships,
            selected_ship_id=request.args.get("ship_id", ""),
        )

    @app.route("/components/<int:component_id>/edit", methods=["GET", "POST"])
    @admin_required
    def component_edit(component_id: int) -> Any:
        component = get_component_or_404(component_id)
        with connect(app.config["DATABASE_PATH"]) as connection:
            ships = connection.execute("SELECT id, name FROM ships ORDER BY name COLLATE NOCASE").fetchall()
        if request.method == "POST":
            try:
                values = validate_component_form(
                    app.config["DATABASE_PATH"], existing_category=component["category"]
                )
                with transaction(app.config["DATABASE_PATH"]) as connection:
                    update_component(connection, component_id, *values)
                after_change()
                flash(f"Component “{values[1]}” updated.", "success")
                return redirect(url_for("index"))
            except ValueError as error:
                flash(str(error), "error")
            except sqlite3.IntegrityError as error:
                if "size" in str(error).lower() and "check constraint" in str(error).lower():
                    flash("Size must be a whole number from 0 to 10.", "error")
                else:
                    raise
        return render_template(
            "component_form.html", component=component, ships=ships, selected_ship_id=""
        )

    def validate_component_form(
        database_path: str, existing_category: str | None = None,
    ) -> tuple[int, str, str, int, str | None, str | None, int]:
        try:
            ship_id = int(request.form.get("ship_id", ""))
        except ValueError as error:
            raise ValueError("Select a valid ship.") from error
        with connect(database_path) as connection:
            if connection.execute("SELECT 1 FROM ships WHERE id = ?", (ship_id,)).fetchone() is None:
                raise ValueError("Select a valid ship.")
        name = form_text("name", 160, required=True)
        category = request.form.get("category", "")
        # Retired/legacy category values may remain unchanged on their existing
        # records, but cannot be selected for new records or assigned elsewhere.
        if category not in CATEGORY_OPTIONS and category != existing_category:
            raise ValueError("Select a valid component category.")
        size = normalize_component_size(request.form.get("size", ""))
        grade_value = request.form.get("grade", "").strip()
        spec_value = request.form.get("spec", "").strip()
        if grade_value and grade_value not in GRADE_OPTIONS:
            raise ValueError("Select a valid grade or leave it blank.")
        if spec_value and spec_value not in SPEC_OPTIONS:
            raise ValueError("Select a valid spec or leave it blank.")
        grade = grade_value or None
        spec = spec_value or None
        try:
            quantity = int(request.form.get("quantity", "1"))
        except ValueError as error:
            raise ValueError("Quantity must be a whole number.") from error
        if not 1 <= quantity <= 100000:
            raise ValueError("Quantity must be between 1 and 100,000.")
        return ship_id, name, category, size, grade, spec, quantity

    @app.post("/components/<int:component_id>/delete")
    @admin_required
    def component_delete(component_id: int) -> Any:
        component = get_component_or_404(component_id)
        with transaction(app.config["DATABASE_PATH"]) as connection:
            connection.execute("DELETE FROM components WHERE id = ?", (component_id,))
        after_change()
        flash(f"Component “{component['name']}” deleted.", "success")
        return redirect(url_for("index"))

    @app.get("/export.csv")
    @admin_required
    def export_csv() -> Response:
        filters = {
            key: request.args.get(key, "").strip()
            for key in ("q", "ship_id", "category", "size", "grade", "spec")
        }
        rows = inventory_rows(app.config["DATABASE_PATH"], filters)
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(
            ["Ship", "Manufacturer", "Ship Sources", "Component", "Category", "Size", "Grade", "Spec", "Qty"]
        )
        for row in rows:
            writer.writerow(
                [
                    row["ship_name"], row["manufacturer"], row["source"], row["name"],
                    row["category"], row["size"], row["grade"], row["spec"], row["quantity"],
                ]
            )
        payload = "\ufeff" + buffer.getvalue()
        return Response(
            payload,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=star_citizen_salvage_inventory.csv"},
        )

    @app.get("/health")
    def health() -> tuple[dict[str, str], int]:
        try:
            with connect(app.config["DATABASE_PATH"]) as connection:
                connection.execute("SELECT 1").fetchone()
            return {"status": "ok"}, 200
        except sqlite3.Error:  # type: ignore[name-defined]
            return {"status": "error"}, 503

    @app.errorhandler(404)
    def not_found(_error: Exception) -> tuple[str, int]:
        return render_template("404.html"), 404

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)

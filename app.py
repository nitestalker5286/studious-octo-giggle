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

from database import connect, initialize_database, inventory_rows, transaction, write_inventory_csv

CATEGORY_OPTIONS = (
    "Power Plant",
    "Cooler",
    "Shield Generator",
    "Quantum Drive",
    "Guns",
    "Missiles",
    "Missile Rack",
    "Turret",
    "Radar",
    "Mining Module",
    "Utility Item",
    "Other",
)
GRADE_OPTIONS = ("A", "B", "C", "D")
SPEC_OPTIONS = ("MIL", "CIV", "CMP", "IND", "STH")
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
            ship = connection.execute("SELECT * FROM ships WHERE id = ?", (ship_id,)).fetchone()
        if ship is None:
            abort(404)
        return ship

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

    @app.get("/")
    def index() -> str:
        filters = {
            "q": request.args.get("q", "").strip(),
            "category": request.args.get("category", "").strip(),
            "size": request.args.get("size", "").strip(),
            "grade": request.args.get("grade", "").strip(),
            "spec": request.args.get("spec", "").strip(),
        }
        has_filters = any(filters.values())
        with connect(app.config["DATABASE_PATH"]) as connection:
            ships = connection.execute(
                """
                SELECT s.id, s.name, s.manufacturer, s.source,
                       COUNT(c.id) AS component_records,
                       COALESCE(SUM(c.quantity), 0) AS component_units
                FROM ships s LEFT JOIN components c ON c.ship_id = s.id
                GROUP BY s.id, s.name, s.manufacturer, s.source
                ORDER BY s.name COLLATE NOCASE
                """
            ).fetchall()
            size_options = [
                row["size"]
                for row in connection.execute(
                    """
                    SELECT DISTINCT size FROM components
                    WHERE size IS NOT NULL AND TRIM(size) <> ''
                    ORDER BY CAST(size AS INTEGER), size COLLATE NOCASE
                    """
                ).fetchall()
            ]
        is_admin = bool(session.get("is_admin"))
        components = (
            inventory_rows(app.config["DATABASE_PATH"], filters)
            if is_admin or has_filters
            else []
        )
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

    @app.get("/ships")
    def public_ships() -> str:
        query = request.args.get("q", "").strip()
        letter = request.args.get("letter", "").strip().upper()
        if letter and (len(letter) != 1 or letter not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            letter = ""
        clauses: list[str] = []
        values: list[str] = []
        if query:
            clauses.append("(s.name LIKE ? COLLATE NOCASE OR s.manufacturer LIKE ? COLLATE NOCASE)")
            values.extend([f"%{query}%", f"%{query}%"])
        if letter:
            clauses.append("UPPER(SUBSTR(TRIM(s.name), 1, 1)) = ?")
            values.append(letter)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect(app.config["DATABASE_PATH"]) as connection:
            ships = connection.execute(
                f"""
                SELECT s.id, s.name, s.manufacturer, s.source,
                       COUNT(c.id) AS component_records,
                       COALESCE(SUM(c.quantity), 0) AS component_units
                FROM ships s LEFT JOIN components c ON c.ship_id = s.id
                {where}
                GROUP BY s.id, s.name, s.manufacturer, s.source
                ORDER BY s.name COLLATE NOCASE, s.id
                """,
                values,
            ).fetchall()
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
        total_units = sum(row["quantity"] for row in components)
        return render_template(
            "public_ship_detail.html",
            ship=ship,
            components=components,
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
                source = form_text("source", 200)
                with transaction(app.config["DATABASE_PATH"]) as connection:
                    duplicate = connection.execute(
                        "SELECT 1 FROM ships WHERE name = ? COLLATE NOCASE", (name,)
                    ).fetchone()
                    if duplicate:
                        raise ValueError("A ship with that name already exists.")
                    connection.execute(
                        "INSERT INTO ships (name, manufacturer, source) VALUES (?, ?, ?)",
                        (name, manufacturer, source),
                    )
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
        return render_template("ship_form.html", ship=None)

    @app.route("/ships/<int:ship_id>/edit", methods=["GET", "POST"])
    @admin_required
    def ship_edit(ship_id: int) -> Any:
        ship = get_ship_or_404(ship_id)
        if request.method == "POST":
            try:
                name = form_text("name", 120, required=True)
                manufacturer = form_text("manufacturer", 120)
                source = form_text("source", 200)
                with transaction(app.config["DATABASE_PATH"]) as connection:
                    duplicate = connection.execute(
                        "SELECT 1 FROM ships WHERE name = ? COLLATE NOCASE AND id <> ?",
                        (name, ship_id),
                    ).fetchone()
                    if duplicate:
                        raise ValueError("A ship with that name already exists.")
                    connection.execute(
                        """
                        UPDATE ships SET name = ?, manufacturer = ?, source = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (name, manufacturer, source, ship_id),
                    )
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
        return render_template("ship_form.html", ship=ship)

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
                    connection.execute(
                        """
                        INSERT INTO components (ship_id, name, category, size, grade, spec, quantity)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
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
                values = validate_component_form(app.config["DATABASE_PATH"])
                with transaction(app.config["DATABASE_PATH"]) as connection:
                    connection.execute(
                        """
                        UPDATE components
                        SET ship_id = ?, name = ?, category = ?, size = ?, grade = ?, spec = ?,
                            quantity = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (*values, component_id),
                    )
                after_change()
                flash(f"Component “{values[1]}” updated.", "success")
                return redirect(url_for("index"))
            except ValueError as error:
                flash(str(error), "error")
        return render_template(
            "component_form.html", component=component, ships=ships, selected_ship_id=""
        )

    def validate_component_form(
        database_path: str,
    ) -> tuple[int, str, str, str, str | None, str | None, int]:
        try:
            ship_id = int(request.form.get("ship_id", ""))
        except ValueError as error:
            raise ValueError("Select a valid ship.") from error
        with connect(database_path) as connection:
            if connection.execute("SELECT 1 FROM ships WHERE id = ?", (ship_id,)).fetchone() is None:
                raise ValueError("Select a valid ship.")
        name = form_text("name", 160, required=True)
        category = request.form.get("category", "")
        if category not in CATEGORY_OPTIONS:
            raise ValueError("Select a valid component category.")
        size = form_text("size", 20)
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
            ["Ship", "Manufacturer", "Ship Source", "Component", "Category", "Size", "Grade", "Spec", "Qty"]
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

import os
import secrets
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

import click
from dotenv import load_dotenv
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for, current_app
from psycopg import connect, errors
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash
from flask_wtf.csrf import CSRFError, CSRFProtect
import re

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "001_initial.sql"

load_dotenv(PROJECT_ROOT / ".env")

app = Flask(
    __name__, 
    template_folder=str(PROJECT_ROOT / "Frontend" / "template"),
    static_folder=str(PROJECT_ROOT / "static"),
    static_url_path="/static",
)

csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    current_app.logger.warning("Rejected CSRF-protected request: %s", error.description)
    flash("Your form session expired. Please refresh the page and submit the form again.", "error")
    return redirect(request.referrer or url_for("home"))

is_production = os.environ.get("FLASK_ENV") == "production"
secret_key = os.environ.get("FLASK_SECRET_KEY")

if is_production and (not secret_key or secret_key == "change-this-development-secret-key"):
    raise RuntimeError("A strong FLASK_SECRET_KEY must be set in production.")

app.config.update(
    SECRET_KEY=secret_key or "change-this-development-secret-key",
    DATABASE_URL=os.environ.get("DATABASE_URL"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    SESSION_COOKIE_SECURE=is_production,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


def get_db():
    if "db" not in g:
        if not app.config["DATABASE_URL"]:
            raise RuntimeError("DATABASE_URL is not configured. Set it to your PostgreSQL connection URL.")
        g.db = connect(app.config["DATABASE_URL"], row_factory=dict_row)
    return g.db


def current_store(db, organisation_id):
    """Return the user's assigned active store, falling back to Main Store."""
    store_id = session.get("store_id")
    if store_id:
        store = db.execute(
            "SELECT id, name FROM stores WHERE id = %s AND organisation_id = %s AND is_active = TRUE",
            (store_id, organisation_id),
        ).fetchone()
        if store:
            return store
    return db.execute(
        "SELECT id, name FROM stores WHERE organisation_id = %s AND name = 'Main Store' AND is_active = TRUE",
        (organisation_id,),
    ).fetchone()


def active_product_units(db, product_id):
    return db.execute(
        "SELECT id, label, quantity_in_base, selling_price FROM product_units WHERE product_id = %s AND is_active = TRUE ORDER BY quantity_in_base, label",
        (product_id,),
    ).fetchall()


@app.teardown_appcontext
def close_db(_error=None):
    database = g.pop("db", None)
    if database is not None:
        database.close()


def log_audit(action, user_id=None, details=None):
    ip_address = request.remote_addr if request else None
    get_db().execute(
        "INSERT INTO audit_logs (user_id, action, details, ip_address) VALUES (%s, %s, %s, %s)",
        (user_id, action, json.dumps(details) if details else None, ip_address)
    )
    get_db().commit()


def send_email(to_email, subject, body):
    # In a real app, this would use smtplib or an API like SendGrid/AWS SES.
    # We will log it to the console for this implementation.
    print(f"--- EMAIL TO: {to_email} ---")
    print(f"Subject: {subject}")
    print(body)
    print("----------------------------")


def init_db():
    database = get_db()
    migration_dir = PROJECT_ROOT / "migrations"
    for migration_file in sorted(migration_dir.glob("*.sql")):
        migration = migration_file.read_text(encoding="utf-8")
        for statement in migration.split(";"):
            if statement.strip():
                database.execute(statement)
    
    database.commit()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view

def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login"))
        if session.get("user_role") != "admin":
            flash("You do not have permission to access this page.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped_view


def owner_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if session.get("user_role") != "owner":
            flash("Only business owners can access inventory management.", "error")
            return redirect(url_for("dashboard"))
        if not session.get("organisation_id"):
            flash("Your account is not linked to a business yet. Contact an administrator.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped_view


def roles_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if "user_id" not in session:
                flash("Please sign in to continue.", "error")
                return redirect(url_for("login"))
            if session.get("user_role") not in allowed_roles:
                flash("You do not have permission to access this page.", "error")
                return redirect(url_for("dashboard"))
            if session.get("user_role") != "admin" and not session.get("organisation_id"):
                flash("Your account is not linked to a business yet. Contact an owner.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped_view
    return decorator


@app.cli.command("init-db")
def init_db_command():
    """Create the InventoryOS PostgreSQL tables."""
    init_db()
    click.echo("Database initialized.")


@app.cli.command("migrate-store-inventory")
def migrate_store_inventory_command():
    """Apply the idempotent multi-store and store-inventory migrations."""
    database = get_db()
    migration_dir = PROJECT_ROOT / "migrations"
    database.execute("CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
    for filename in (
        "012_stores_foundation.sql",
        "013_assign_staff_stores.sql",
        "014_store_inventory.sql",
        "015_organisation_receipt_details.sql",
        "016_reconcile_main_store_stock.sql",
        "017_sale_price_controls.sql",
        "018_sale_cost_snapshots.sql",
        "019_expenses.sql",
        "020_stock_closings.sql",
        "021_supplier_payables.sql",
        "022_receipt_customisation.sql",
        "023_product_unit_conversions.sql",
        "024_product_batches.sql",
        "025_store_product_batches.sql",
        "026_batch_expiry_and_returns.sql",
        "027_void_sales.sql",
        "028_issue_reports.sql",
    ):
        if filename == "016_reconcile_main_store_stock.sql" and database.execute(
            "SELECT 1 FROM schema_migrations WHERE filename = %s", (filename,)
        ).fetchone():
            continue
        migration = (migration_dir / filename).read_text(encoding="utf-8")
        for statement in migration.split(";"):
            if statement.strip():
                database.execute(statement)
        if filename == "016_reconcile_main_store_stock.sql":
            database.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (filename,))
    database.commit()
    click.echo("Store inventory migration applied.")


@app.cli.command("create-user")
@click.argument("email")
@click.password_option(confirmation_prompt=True)
@click.option("--admin", is_flag=True, help="Create this user as an administrator.")
def create_user_command(email, password, admin):
    """Create a user who can sign in."""
    email = email.strip().lower()
    if "@" not in email:
        raise click.UsageError("Enter a valid email address.")

    role = "admin" if admin else "owner"
    try:
        get_db().execute(
            "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, %s)",
            (email, generate_password_hash(password), role),
        )
        get_db().commit()
    except errors.UniqueViolation as error:
        get_db().rollback()
        raise click.UsageError("That email address already has an account.") from error

    click.echo(f"User created: {email}")


@app.cli.command("delete-user")
@click.argument("email")
@click.confirmation_option(prompt="Permanently delete this user?")
def delete_user_command(email):
    """Permanently delete a user by email address."""
    email = email.strip().lower()
    db = get_db()
    user = db.execute(
        "SELECT id, email, role FROM users WHERE LOWER(email) = %s", (email,)
    ).fetchone()
    if user is None:
        raise click.UsageError("No user exists with that email address.")

    if user["role"] == "admin":
        admin_count = db.execute(
            "SELECT COUNT(*) AS count FROM users WHERE role = 'admin'"
        ).fetchone()["count"]
        if admin_count <= 1:
            raise click.UsageError("Refusing to delete the last administrator account.")

    db.execute("DELETE FROM users WHERE id = %s", (user["id"],))
    db.commit()
    click.echo(f"Deleted user: {user['email']}")


@app.get("/")
def home():
    return render_template("shared/landing.html")

@app.route("/request-access", methods=["GET", "POST"])
def request_access():
    if request.method == "POST":
        fields = {
            "full_name": request.form.get("full_name", "").strip(),
            "business_name": request.form.get("business_name", "").strip(),
            "email": request.form.get("email", "").strip().lower(),
            "phone": request.form.get("phone", "").strip(),
        }

        if not fields["full_name"] or not fields["business_name"]:
            flash("Enter your name and business name.", "error")
            return render_template("shared/request_access.html", fields=fields), 400
            
        email_regex = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
        if not email_regex.match(fields["email"]):
            flash("Please enter a valid email address.", "error")
            return render_template("shared/request_access.html", fields=fields), 400

        db = get_db()
        existing_request = db.execute(
            "SELECT id FROM access_requests WHERE email = %s AND status = 'pending'", (fields["email"],)
        ).fetchone()
        
        if existing_request:
            flash("An access request is already pending for this email address.", "error")
            return render_template("shared/request_access.html", fields=fields), 400

        db.execute(
            """
            INSERT INTO access_requests (full_name, business_name, email, phone)
            VALUES (%s, %s, %s, %s)
            """,
            (fields["full_name"], fields["business_name"], fields["email"], fields["phone"] or None),
        )
        get_db().commit()
        
        log_audit("access_requested", details={"email": fields["email"], "business_name": fields["business_name"]})
        flash("Your access request has been received. We will contact you soon.", "success")
        return redirect(url_for("request_access"))

    return render_template("shared/request_access.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        
        db = get_db()
        user = db.execute(
            "SELECT id, email, password_hash, role, organisation_id, store_id, failed_login_attempts, locked_until FROM users WHERE LOWER(email) = %s", (email,)
        ).fetchone()

        if user is None:
            log_audit("failed_login_unknown_user", details={"email": email})
            flash("Invalid email address or password.", "error")
            return render_template("shared/login.html"), 401
            
        if user["locked_until"] and user["locked_until"] > datetime.now(timezone.utc):
            log_audit("login_attempt_locked_account", user_id=user["id"])
            flash("Account is temporarily locked due to too many failed attempts. Try again later.", "error")
            return render_template("shared/login.html"), 403

        if not check_password_hash(user["password_hash"], password):
            failed_attempts = user["failed_login_attempts"] + 1
            locked_until = datetime.now(timezone.utc) + timedelta(minutes=15) if failed_attempts >= 5 else None
            
            db.execute(
                "UPDATE users SET failed_login_attempts = %s, locked_until = %s WHERE id = %s",
                (failed_attempts, locked_until, user["id"])
            )
            db.commit()
            
            log_audit("failed_login", user_id=user["id"])
            if locked_until:
                log_audit("account_locked", user_id=user["id"])
                
            flash("Invalid email address or password.", "error")
            return render_template("shared/login.html"), 401

        # Success
        db.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, last_login_at = %s WHERE id = %s",
            (datetime.now(timezone.utc), user["id"]),
        )
        db.commit()
        
        session.clear()
        session["user_id"] = user["id"]
        session["user_email"] = user["email"]
        session["user_role"] = user["role"]
        session["organisation_id"] = user["organisation_id"]
        session["store_id"] = user["store_id"]
        session.permanent = request.form.get("remember_me") == "on"
        log_audit("successful_login", user_id=user["id"])
        return redirect(url_for("super_admin_dashboard") if user["role"] == "admin" else url_for("dashboard"))

    return render_template("shared/login.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        db = get_db()
        user = db.execute("SELECT id FROM users WHERE LOWER(email) = %s", (email,)).fetchone()
        
        if user:
            random_string = secrets.token_urlsafe(32)
            token = f"{user['id']}:{random_string}"
            token_hash = generate_password_hash(random_string)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            
            db.execute(
                "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                (token_hash, user["id"], expires_at)
            )
            db.commit()
            
            reset_url = url_for("reset_password", token=token, _external=True)
            send_email(email, "Password Reset Request", f"Click the link below to reset your password:\n\n{reset_url}")
            log_audit("password_reset_requested", user_id=user["id"])
            
        flash("If an account exists with that email, a reset link has been sent.", "success")
        return redirect(url_for("forgot_password"))

    return render_template("shared/forgot_password.html")

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    db = get_db()
    try:
        user_id_str, random_string = token.split(":", 1)
        user_id = int(user_id_str)
    except ValueError:
        flash("Invalid or expired reset link.", "error")
        return redirect(url_for("forgot_password"))

    resets = db.execute(
        "SELECT token_hash, expires_at FROM password_resets WHERE user_id = %s", (user_id,)
    ).fetchall()
    
    valid_reset = None
    for r in resets:
        if check_password_hash(r["token_hash"], random_string):
            valid_reset = r
            break
            
    if not valid_reset or valid_reset["expires_at"] < datetime.now(timezone.utc):
        flash("Invalid or expired reset link.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("shared/reset_password.html", token=token)
            
        db.execute(
            "UPDATE users SET password_hash = %s, failed_login_attempts = 0, locked_until = NULL WHERE id = %s",
            (generate_password_hash(password), user_id)
        )
        db.execute("DELETE FROM password_resets WHERE user_id = %s", (user_id,))
        db.commit()
        log_audit("password_reset_completed", user_id=user_id)
        
        flash("Your password has been reset. You can now sign in.", "success")
        return redirect(url_for("login"))

    return render_template("shared/reset_password.html", token=token)


@app.get("/super_admin_dashboard")
@admin_required
def super_admin_dashboard():
    db = get_db()
    last_login = db.execute(
        "SELECT last_login_at FROM users WHERE id = %s", (session["user_id"],)
    ).fetchone()
    return render_template(
        "super/super_admin_dashboard.html",
        email=session["user_email"],
        overview=get_inventory_overview(),
        last_login_at=format_timestamp(last_login["last_login_at"] if last_login else None),
    )


@app.route("/report-issue", methods=["GET", "POST"])
@roles_required("owner", "manager", "salesperson", "cashier")
def report_issue():
    db = get_db()
    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        description = request.form.get("description", "").strip()
        priority = request.form.get("priority", "normal")
        if not subject or not description or priority not in {"low", "normal", "high", "critical"}:
            flash("Add a subject, description, and valid priority.", "error")
        else:
            db.execute("INSERT INTO issue_reports (organisation_id, reported_by_user_id, subject, description, priority) VALUES (%s, %s, %s, %s, %s)", (session.get("organisation_id"), session["user_id"], subject, description, priority))
            db.commit()
            log_audit("issue_reported", user_id=session["user_id"], details={"subject": subject, "priority": priority})
            flash("Your issue was sent to the InventoryOS support team.", "success")
            return redirect(url_for("report_issue"))
    reports = db.execute("SELECT subject, priority, status, created_at FROM issue_reports WHERE reported_by_user_id = %s ORDER BY created_at DESC LIMIT 10", (session["user_id"],)).fetchall()
    return render_template("owner/owner_report_issue.html", reports=reports, active_page="report_issue", user_role=session.get("user_role"))


@app.route("/super-admin/issues", methods=["GET", "POST"])
@admin_required
def admin_issues():
    db = get_db()
    if request.method == "POST":
        issue_id = request.form.get("issue_id", "").strip()
        status = request.form.get("status", "")
        admin_note = request.form.get("admin_note", "").strip() or None
        if status not in {"open", "in_progress", "resolved", "closed"}:
            flash("Choose a valid issue status.", "error")
        else:
            resolved = status in {"resolved", "closed"}
            db.execute("UPDATE issue_reports SET status = %s, admin_note = %s, resolved_by_user_id = CASE WHEN %s THEN %s ELSE NULL END, resolved_at = CASE WHEN %s THEN NOW() ELSE NULL END, updated_at = NOW() WHERE id = %s", (status, admin_note, resolved, session["user_id"], resolved, issue_id))
            db.commit()
            log_audit("issue_report_updated", user_id=session["user_id"], details={"issue_id": issue_id, "status": status})
            flash("Issue updated.", "success")
        return redirect(url_for("admin_issues"))
    issues = db.execute("SELECT ir.*, u.email AS reporter_email, o.name AS organisation_name FROM issue_reports ir JOIN users u ON u.id = ir.reported_by_user_id LEFT JOIN organisations o ON o.id = ir.organisation_id ORDER BY CASE ir.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END, ir.created_at DESC").fetchall()
    return render_template("super/super_admin_issues.html", issues=issues, active_page="issues")


@app.get("/super-admin/users")
@admin_required
def super_admin_view_user():
    users = get_db().execute(
        """
        SELECT u.id, u.email, u.role, u.created_at, u.last_login_at, u.locked_until,
               o.name AS organisation_name
        FROM users u
        LEFT JOIN organisations o ON o.id = u.organisation_id
        ORDER BY CASE WHEN u.role = 'admin' THEN 0 ELSE 1 END, u.created_at DESC
        """
    ).fetchall()
    return render_template("super/super_admin_view_user.html", users=users, now=datetime.now(timezone.utc))


@app.route("/admin/requests")
@admin_required
def admin_requests():
    db = get_db()
    now = datetime.now(timezone.utc)
    stats_row = db.execute("""
        SELECT 
            (SELECT COUNT(*) FROM users WHERE role = 'owner' AND (locked_until IS NULL OR locked_until <= %s)) AS active,
            (SELECT COUNT(*) FROM users WHERE role = 'owner' AND locked_until > %s) AS inactive,
            (SELECT COUNT(*) FROM access_requests WHERE status = 'pending') AS available
    """, (now, now)).fetchone()
    
    pending = db.execute(
        "SELECT id, full_name, business_name, email, phone, created_at FROM access_requests WHERE status = 'pending' ORDER BY created_at ASC"
    ).fetchall()
    
    return render_template("admin_requests.html", stats=stats_row, requests=pending)

@app.post("/admin/request/<int:req_id>/<action>")
@admin_required
def admin_handle_request(req_id, action):
    if action not in ("approve", "reject"):
        flash("Invalid action.", "error")
        return redirect(url_for("admin_requests"))
        
    db = get_db()
    req = db.execute(
        "SELECT email, business_name FROM access_requests WHERE id = %s AND status = 'pending'",
        (req_id,),
    ).fetchone()
    if not req:
        flash("Request not found or already processed.", "error")
        return redirect(url_for("admin_requests"))
        
    if action == "approve":
        try:
            password = secrets.token_urlsafe(16)
            organisation = db.execute(
                "INSERT INTO organisations (name) VALUES (%s) RETURNING id",
                (req["business_name"],),
            ).fetchone()
            db.execute(
                "INSERT INTO users (email, password_hash, role, organisation_id) VALUES (%s, %s, 'owner', %s)",
                (req["email"], generate_password_hash(password), organisation["id"]),
            )
            user = db.execute("SELECT id FROM users WHERE email = %s", (req["email"],)).fetchone()
            
            random_string = secrets.token_urlsafe(32)
            token = f"{user['id']}:{random_string}"
            token_hash = generate_password_hash(random_string)
            expires_at = datetime.now(timezone.utc) + timedelta(days=7)
            
            db.execute(
                "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                (token_hash, user["id"], expires_at)
            )
            
            reset_url = url_for("reset_password", token=token, _external=True)
            send_email(req["email"], "Welcome to InventoryOS!", f"Your access request was approved!\nClick here to setup your password:\n\n{reset_url}")
            
            db.execute("UPDATE access_requests SET status = 'approved' WHERE id = %s", (req_id,))
            db.commit()
            log_audit("access_request_approved", user_id=session["user_id"], details={"request_id": req_id})
            flash(f"Approved {req['email']}.", "success")
        except errors.UniqueViolation:
            db.rollback()
            flash("User with this email already exists.", "error")
    else:
        db.execute("UPDATE access_requests SET status = 'rejected' WHERE id = %s", (req_id,))
        db.commit()
        log_audit("access_request_rejected", user_id=session["user_id"], details={"request_id": req_id})
        flash(f"Rejected {req['email']}.", "success")
        
    return redirect(url_for("admin_requests"))


@app.post("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    if session.get("user_role") == "admin":
        return redirect(url_for("super_admin_dashboard"))

    organisation_id = session.get("organisation_id")
    overview = None
    recent_movements = []
    low_stock_alerts = []
    available_stock = []
    organisation_name = None
    if organisation_id:
        db = get_db()
        organisation = db.execute(
            "SELECT name FROM organisations WHERE id = %s", (organisation_id,)
        ).fetchone()
        organisation_name = organisation["name"] if organisation else None
        overview = db.execute(
            """
            SELECT
                COUNT(*) AS product_count,
                COALESCE((SELECT SUM(quantity_remaining * unit_cost) FROM product_batches WHERE organisation_id = %s), 0) AS stock_value,
                (
                    SELECT COUNT(*)
                    FROM store_inventory si
                    JOIN products sp ON sp.id = si.product_id
                    JOIN stores ss ON ss.id = si.store_id
                    WHERE ss.organisation_id = %s AND ss.is_active = TRUE
                      AND sp.is_active = TRUE AND si.quantity <= sp.low_stock_threshold
                ) AS low_stock_count
            FROM products
            WHERE organisation_id = %s AND is_active = TRUE
            """,
            (organisation_id, organisation_id, organisation_id),
        ).fetchone()
        recent_movements = db.execute(
            """
            SELECT sm.movement_type, sm.quantity_change, sm.created_at, p.name AS product_name, p.unit
            FROM stock_movements sm
            JOIN products p ON p.id = sm.product_id
            WHERE sm.organisation_id = %s
            ORDER BY sm.created_at DESC
            LIMIT 5
            """,
            (organisation_id,),
        ).fetchall()
        low_stock_alerts = db.execute(
            """
            SELECT p.name AS product_name, p.sku, p.unit, p.low_stock_threshold,
                   si.quantity, s.name AS store_name
            FROM store_inventory si
            JOIN products p ON p.id = si.product_id
            JOIN stores s ON s.id = si.store_id
            WHERE s.organisation_id = %s AND s.is_active = TRUE
              AND p.is_active = TRUE AND si.quantity <= p.low_stock_threshold
            ORDER BY si.quantity ASC, p.name ASC
            LIMIT 5
            """,
            (organisation_id,),
        ).fetchall()
        available_stock = db.execute(
            """
            SELECT p.name, p.sku, p.unit,
                   COALESCE(SUM(si.quantity) FILTER (WHERE s.is_active = TRUE), 0) AS quantity
            FROM products p
            LEFT JOIN store_inventory si ON si.product_id = p.id
            LEFT JOIN stores s ON s.id = si.store_id AND s.is_active = TRUE
            WHERE p.organisation_id = %s AND p.is_active = TRUE
            GROUP BY p.id, p.name, p.sku, p.unit
            ORDER BY quantity ASC, p.name ASC
            LIMIT 6
            """,
            (organisation_id,),
        ).fetchall()
    return render_template(
        "owner/owner_dashboard.html",
        email=session.get("user_email"),
        is_owner=session.get("user_role") == "owner",
        has_organisation=bool(organisation_id),
        organisation_name=organisation_name,
        overview=overview,
        recent_movements=recent_movements,
        low_stock_alerts=low_stock_alerts,
        available_stock=available_stock,
        active_page="dashboard",
        user_role=session.get("user_role"),
    )


@app.get("/low-stock")
@roles_required("owner", "manager")
def low_stock():
    db = get_db()
    organisation_id = session["organisation_id"]
    alerts = db.execute(
        """
        SELECT p.id AS product_id, p.name AS product_name, p.sku, p.unit,
               p.low_stock_threshold, si.quantity, s.name AS store_name,
               s.id AS store_id, si.updated_at
        FROM store_inventory si
        JOIN products p ON p.id = si.product_id
        JOIN stores s ON s.id = si.store_id
        WHERE s.organisation_id = %s AND s.is_active = TRUE AND p.is_active = TRUE
          AND si.quantity <= p.low_stock_threshold
        ORDER BY si.quantity ASC, p.name ASC, s.name ASC
        """,
        (organisation_id,),
    ).fetchall()
    return render_template(
        "owner/owner_low_stock.html", alerts=alerts, active_page="low_stock",
        user_role=session.get("user_role"),
    )


@app.route("/team", methods=["GET", "POST"])
@roles_required("owner")
def team():
    db = get_db()
    organisation_id = session["organisation_id"]
    staff_roles = ("manager", "salesperson", "cashier")
    stores = db.execute(
        "SELECT id, name FROM stores WHERE organisation_id = %s AND is_active = TRUE ORDER BY created_at",
        (organisation_id,),
    ).fetchall()

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role", "")
        store_id = request.form.get("store_id", "").strip() or None
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email) or role not in staff_roles:
            flash("Enter a valid email address and choose a staff role.", "error")
            return redirect(url_for("team"))
        if store_id and not db.execute(
            "SELECT 1 FROM stores WHERE id = %s AND organisation_id = %s AND is_active = TRUE",
            (store_id, organisation_id),
        ).fetchone():
            flash("Choose a valid store.", "error")
            return redirect(url_for("team"))
        try:
            db.execute(
                "INSERT INTO users (email, password_hash, role, organisation_id, store_id) VALUES (%s, %s, %s, %s, %s)",
                (email, generate_password_hash(secrets.token_urlsafe(32)), role, organisation_id, store_id),
            )
            user = db.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
            random_string = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                (generate_password_hash(random_string), user["id"], datetime.now(timezone.utc) + timedelta(days=7)),
            )
            db.commit()
        except errors.UniqueViolation:
            db.rollback()
            flash("A user with this email address already exists.", "error")
            return redirect(url_for("team"))

        reset_url = url_for("reset_password", token=f"{user['id']}:{random_string}", _external=True)
        send_email(email, "You are invited to InventoryOS", f"Set your password here:\n\n{reset_url}")
        log_audit("staff_invited", user_id=session["user_id"], details={"email": email, "role": role})
        flash(f"Invitation sent to {email}.", "success")
        return redirect(url_for("team"))

    staff = db.execute(
        """
        SELECT u.id, u.email, u.role, u.created_at, u.last_login_at, u.locked_until,
               s.name AS store_name
        FROM users u LEFT JOIN stores s ON s.id = u.store_id
        WHERE u.organisation_id = %s
        ORDER BY CASE WHEN u.role = 'owner' THEN 0 ELSE 1 END, u.created_at DESC
        """,
        (organisation_id,),
    ).fetchall()
    return render_template("owner/owner_team.html", staff=staff, stores=stores, now=datetime.now(timezone.utc), active_page="team", user_role=session.get("user_role"))


@app.post("/team/<int:staff_id>/<action>")
@roles_required("owner")
def manage_team_member(staff_id, action):
    db = get_db()
    organisation_id = session["organisation_id"]
    member = db.execute(
        "SELECT id, email, role, locked_until FROM users WHERE id = %s AND organisation_id = %s",
        (staff_id, organisation_id),
    ).fetchone()
    if not member or member["role"] == "owner" or member["id"] == session["user_id"]:
        flash("That team member cannot be changed.", "error")
        return redirect(url_for("team"))

    if action == "role":
        role = request.form.get("role", "")
        if role not in {"manager", "salesperson", "cashier"}:
            flash("Choose a valid staff role.", "error")
            return redirect(url_for("team"))
        db.execute("UPDATE users SET role = %s WHERE id = %s", (role, staff_id))
        db.commit()
        log_audit("staff_role_changed", user_id=session["user_id"], details={"staff_id": staff_id, "role": role})
        flash(f"Updated {member['email']} to {role}.", "success")

    elif action == "store":
        store_id = request.form.get("store_id", "").strip() or None
        if store_id and not db.execute(
            "SELECT 1 FROM stores WHERE id = %s AND organisation_id = %s AND is_active = TRUE",
            (store_id, organisation_id),
        ).fetchone():
            flash("Choose a valid store.", "error")
            return redirect(url_for("team"))
        db.execute("UPDATE users SET store_id = %s WHERE id = %s", (store_id, staff_id))
        db.commit()
        log_audit("staff_store_assigned", user_id=session["user_id"], details={"staff_id": staff_id, "store_id": store_id})
        flash(f"Updated {member['email']}'s store assignment.", "success")

    elif action == "lock":
        db.execute("UPDATE users SET locked_until = %s WHERE id = %s", (datetime.now(timezone.utc) + timedelta(days=3650), staff_id))
        db.commit()
        log_audit("staff_locked", user_id=session["user_id"], details={"staff_id": staff_id})
        flash(f"Locked {member['email']}.", "success")

    elif action == "unlock":
        db.execute("UPDATE users SET locked_until = NULL, failed_login_attempts = 0 WHERE id = %s", (staff_id,))
        db.commit()
        log_audit("staff_unlocked", user_id=session["user_id"], details={"staff_id": staff_id})
        flash(f"Unlocked {member['email']}.", "success")

    elif action == "resend-invite":
        random_string = secrets.token_urlsafe(32)
        db.execute("DELETE FROM password_resets WHERE user_id = %s", (staff_id,))
        db.execute(
            "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
            (generate_password_hash(random_string), staff_id, datetime.now(timezone.utc) + timedelta(days=7)),
        )
        db.commit()
        reset_url = url_for("reset_password", token=f"{staff_id}:{random_string}", _external=True)
        send_email(member["email"], "Your InventoryOS setup link", f"Set or reset your password here:\n\n{reset_url}")
        log_audit("staff_invite_resent", user_id=session["user_id"], details={"staff_id": staff_id})
        flash(f"A new setup link was sent to {member['email']}.", "success")

    elif action == "remove":
        db.execute("DELETE FROM users WHERE id = %s", (staff_id,))
        db.commit()
        log_audit("staff_removed", user_id=session["user_id"], details={"staff_id": staff_id, "email": member["email"]})
        flash(f"Removed {member['email']} from your team.", "success")

    else:
        flash("Invalid staff action.", "error")
    return redirect(url_for("team"))


@app.get("/sales-dashboard")
@roles_required("owner", "manager")
def sales_dashboard():
    db = get_db()
    organisation_id = session["organisation_id"]
    today_summary = db.execute(
        """
        SELECT COALESCE(SUM(total_amount), 0) AS total_sales,
               COUNT(*) AS transaction_count,
               COALESCE(AVG(total_amount), 0) AS average_sale
        FROM sales
        WHERE organisation_id = %s AND voided_at IS NULL AND created_at >= CURRENT_DATE
        """,
        (organisation_id,),
    ).fetchone()
    week_summary = db.execute(
        """
        SELECT COALESCE(SUM(total_amount), 0) AS total_sales, COUNT(*) AS transaction_count
        FROM sales
        WHERE organisation_id = %s AND voided_at IS NULL AND created_at >= CURRENT_DATE - INTERVAL '6 days'
        """,
        (organisation_id,),
    ).fetchone()
    recent_sales = db.execute(
        """
        SELECT id, sale_number, customer_name, payment_method, total_amount, created_at
        FROM sales WHERE organisation_id = %s AND voided_at IS NULL
        ORDER BY created_at DESC LIMIT 8
        """,
        (organisation_id,),
    ).fetchall()
    return render_template(
        "owner/owner_sales_dashboard.html",
        today=today_summary,
        week=week_summary,
        recent_sales=recent_sales,
        active_page="sales",
        user_role=session.get("user_role"),
    )


@app.route("/purchases", methods=["GET", "POST"])
@roles_required("owner", "manager")
def purchases():
    db = get_db()
    organisation_id = session["organisation_id"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create-supplier":
            name = request.form.get("name", "").strip()
            phone = request.form.get("phone", "").strip() or None
            email = request.form.get("email", "").strip().lower() or None
            if not name:
                flash("Enter a supplier name.", "error")
                return redirect(url_for("purchases"))
            try:
                db.execute(
                    "INSERT INTO suppliers (organisation_id, name, phone, email) VALUES (%s, %s, %s, %s)",
                    (organisation_id, name, phone, email),
                )
                db.commit()
                flash("Supplier added.", "success")
            except errors.UniqueViolation:
                db.rollback()
                flash("That supplier already exists.", "error")
            return redirect(url_for("purchases"))

        if action == "create-purchase":
            product_id = request.form.get("product_id", "").strip()
            supplier_id = request.form.get("supplier_id", "").strip() or None
            notes = request.form.get("notes", "").strip() or None
            payment_method = request.form.get("payment_method", "cash")
            try:
                quantity = Decimal(request.form.get("quantity", ""))
                unit_cost = Decimal(request.form.get("unit_cost", ""))
                selling_price = Decimal(request.form.get("selling_price", ""))
            except InvalidOperation:
                flash("Enter valid purchase quantity, cost, and selling price.", "error")
                return redirect(url_for("purchases"))
            if quantity <= 0 or unit_cost < 0 or selling_price < 0 or payment_method not in {"cash", "transfer", "card", "credit", "other"}:
                flash("Quantity must be greater than zero and prices cannot be negative.", "error")
                return redirect(url_for("purchases"))
            batch_code = request.form.get("batch_code", "").strip() or None
            expiry_date = request.form.get("expiry_date", "").strip() or None
            if expiry_date:
                try:
                    datetime.strptime(expiry_date, "%Y-%m-%d").date()
                except ValueError:
                    flash("Enter a valid expiry date.", "error")
                    return redirect(url_for("purchases"))
            product = db.execute(
                "SELECT id, name FROM products WHERE id = %s AND organisation_id = %s AND is_active = TRUE FOR UPDATE",
                (product_id, organisation_id),
            ).fetchone()
            if not product:
                flash("Choose a valid product.", "error")
                return redirect(url_for("purchases"))
            store = current_store(db, organisation_id)
            if not store:
                flash("No active store is available for this purchase.", "error")
                return redirect(url_for("purchases"))
            if payment_method == "credit" and not supplier_id:
                flash("Choose a supplier before recording a credit purchase.", "error")
                return redirect(url_for("purchases"))
            if supplier_id and not db.execute(
                "SELECT 1 FROM suppliers WHERE id = %s AND organisation_id = %s", (supplier_id, organisation_id)
            ).fetchone():
                flash("Choose a valid supplier.", "error")
                return redirect(url_for("purchases"))
            total_amount = quantity * unit_cost
            purchase = db.execute(
                """
                INSERT INTO purchases (organisation_id, supplier_id, product_id, quantity, unit_cost, selling_price, total_amount, payment_method, notes, created_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (organisation_id, supplier_id, product["id"], quantity, unit_cost, selling_price, total_amount, payment_method, notes, session["user_id"]),
            ).fetchone()
            if payment_method == "credit":
                db.execute(
                    "INSERT INTO supplier_payables (organisation_id, supplier_id, purchase_id, amount_due) VALUES (%s, %s, %s, %s)",
                    (organisation_id, supplier_id, purchase["id"], total_amount),
                )
            db.execute(
                "UPDATE products SET stock_quantity = stock_quantity + %s, updated_at = NOW() WHERE id = %s",
                (quantity, product["id"]),
            )
            db.execute("INSERT INTO product_batches (organisation_id, store_id, product_id, purchase_id, quantity_remaining, unit_cost, selling_price, batch_code, expiry_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", (organisation_id, store["id"], product["id"], purchase["id"], quantity, unit_cost, selling_price, batch_code, expiry_date))
            db.execute(
                """
                INSERT INTO store_inventory (store_id, product_id, quantity, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (store_id, product_id)
                DO UPDATE SET quantity = store_inventory.quantity + EXCLUDED.quantity, updated_at = NOW()
                """,
                (store["id"], product["id"], quantity),
            )
            db.execute(
                """
                INSERT INTO stock_movements (organisation_id, product_id, movement_type, quantity_change, notes, created_by_user_id)
                VALUES (%s, %s, 'stock_in', %s, %s, %s)
                """,
                (organisation_id, product["id"], quantity, notes or "Purchase received", session["user_id"]),
            )
            db.commit()
            log_audit("purchase_recorded", user_id=session["user_id"], details={"product_id": product["id"], "quantity": str(quantity)})
            flash(f"Purchase recorded and {quantity} items added to {product['name']} at {store['name']}.", "success")
            return redirect(url_for("purchases"))

        flash("Invalid purchase action.", "error")
        return redirect(url_for("purchases"))

    suppliers = db.execute(
        "SELECT id, name, phone, email FROM suppliers WHERE organisation_id = %s ORDER BY name", (organisation_id,)
    ).fetchall()
    product_rows = db.execute(
        "SELECT id, name, sku, unit, cost_price FROM products WHERE organisation_id = %s AND is_active = TRUE ORDER BY name",
        (organisation_id,),
    ).fetchall()
    purchase_rows = db.execute(
        """
        SELECT p.quantity, p.unit_cost, p.total_amount, p.notes, p.created_at,
               pr.name AS product_name, pr.unit, s.name AS supplier_name
        FROM purchases p JOIN products pr ON pr.id = p.product_id
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        WHERE p.organisation_id = %s ORDER BY p.created_at DESC LIMIT 20
        """,
        (organisation_id,),
    ).fetchall()
    return render_template(
        "owner/owner_batch_purchases.html", suppliers=suppliers, products=product_rows,
        purchases=purchase_rows, active_page="purchases", user_role=session.get("user_role")
    )


@app.route("/supplier-balances", methods=["GET", "POST"])
@roles_required("owner", "manager")
def supplier_payables():
    db = get_db()
    organisation_id = session["organisation_id"]
    if request.method == "POST":
        payable_id = request.form.get("payable_id", "").strip()
        payment_method = request.form.get("payment_method", "cash")
        notes = request.form.get("notes", "").strip() or None
        try:
            amount = Decimal(request.form.get("amount", ""))
        except InvalidOperation:
            flash("Enter a valid repayment amount.", "error")
            return redirect(url_for("supplier_payables"))
        payable = db.execute(
            "SELECT id, amount_due, amount_paid FROM supplier_payables WHERE id = %s AND organisation_id = %s AND status = 'open' FOR UPDATE",
            (payable_id, organisation_id),
        ).fetchone()
        if not payable or amount <= 0 or payment_method not in {"cash", "transfer", "card", "other"}:
            flash("Choose an open supplier balance and enter a valid payment.", "error")
            return redirect(url_for("supplier_payables"))
        outstanding = payable["amount_due"] - payable["amount_paid"]
        if amount > outstanding:
            flash(f"Payment cannot be more than the supplier balance of ₦{outstanding:,.2f}.", "error")
            return redirect(url_for("supplier_payables"))
        new_paid = payable["amount_paid"] + amount
        status = "paid" if new_paid == payable["amount_due"] else "open"
        db.execute(
            "INSERT INTO supplier_payments (payable_id, amount, payment_method, notes, received_by_user_id) VALUES (%s, %s, %s, %s, %s)",
            (payable["id"], amount, payment_method, notes, session["user_id"]),
        )
        db.execute(
            "UPDATE supplier_payables SET amount_paid = %s, status = %s, paid_at = CASE WHEN %s = 'paid' THEN NOW() ELSE NULL END WHERE id = %s",
            (new_paid, status, status, payable["id"]),
        )
        db.commit()
        log_audit("supplier_payment_recorded", user_id=session["user_id"], details={"payable_id": payable["id"], "amount": str(amount)})
        flash("Supplier payment recorded.", "success")
        return redirect(url_for("supplier_payables"))
    payables = db.execute(
        """
        SELECT sp.id, sp.amount_due, sp.amount_paid, sp.created_at, su.name AS supplier_name,
               p.name AS product_name, pu.quantity, pu.unit_cost
        FROM supplier_payables sp JOIN suppliers su ON su.id = sp.supplier_id
        JOIN purchases pu ON pu.id = sp.purchase_id JOIN products p ON p.id = pu.product_id
        WHERE sp.organisation_id = %s AND sp.status = 'open'
        ORDER BY sp.created_at DESC
        """,
        (organisation_id,),
    ).fetchall()
    total_outstanding = sum((row["amount_due"] - row["amount_paid"] for row in payables), Decimal("0"))
    return render_template(
        "owner/owner_supplier_payables.html", payables=payables, total_outstanding=total_outstanding,
        active_page="supplier_payables", user_role=session.get("user_role"),
    )


@app.route("/customers", methods=["GET", "POST"])
@roles_required("owner", "manager", "salesperson")
def customers():
    db = get_db()
    organisation_id = session["organisation_id"]
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip() or None
        email = request.form.get("email", "").strip().lower() or None
        if not name:
            flash("Enter the customer's name.", "error")
            return redirect(url_for("customers"))
        try:
            db.execute(
                "INSERT INTO customers (organisation_id, name, phone, email) VALUES (%s, %s, %s, %s)",
                (organisation_id, name, phone, email),
            )
            db.commit()
            log_audit("customer_created", user_id=session["user_id"], details={"name": name})
            flash("Customer added.", "success")
        except errors.UniqueViolation:
            db.rollback()
            flash("A customer with that phone number already exists.", "error")
        return redirect(url_for("customers"))

    customer_rows = db.execute(
        """
        SELECT c.id, c.name, c.phone, c.email, c.created_at,
               COUNT(s.id) AS sale_count, COALESCE(SUM(s.total_amount), 0) AS total_spent
        FROM customers c
        LEFT JOIN sales s ON s.customer_id = c.id
        WHERE c.organisation_id = %s
        GROUP BY c.id
        ORDER BY c.created_at DESC
        """,
        (organisation_id,),
    ).fetchall()
    return render_template(
        "owner/owner_customers.html", customers=customer_rows,
        active_page="customers", user_role=session.get("user_role")
    )


@app.route("/checkout", methods=["GET", "POST"])
@roles_required("owner", "manager", "salesperson", "cashier")
def checkout():
    db = get_db()
    organisation_id = session["organisation_id"]
    if request.method == "POST":
        product_id = request.form.get("product_id", "").strip()
        unit_id = request.form.get("unit_id", "").strip()
        batch_id = request.form.get("batch_id", "").strip()
        customer_id = request.form.get("customer_id", "").strip() or None
        payment_method = request.form.get("payment_method", "")
        requested_unit_price = request.form.get("unit_price", "").strip()
        price_override_reason = request.form.get("price_override_reason", "").strip() or None
        try:
            quantity = Decimal(request.form.get("quantity", ""))
        except InvalidOperation:
            flash("Enter a valid quantity.", "error")
            return redirect(url_for("checkout"))
        if quantity <= 0 or payment_method not in {"cash", "transfer", "card", "credit", "other"}:
            flash("Choose a product, a positive quantity, and a payment method.", "error")
            return redirect(url_for("checkout"))
        if payment_method == "credit" and not customer_id:
            flash("Choose a customer before recording a credit sale.", "error")
            return redirect(url_for("checkout"))
        product = db.execute(
            """
            SELECT id, name, unit, cost_price, selling_price, stock_quantity
            FROM products WHERE id = %s AND organisation_id = %s AND is_active = TRUE FOR UPDATE
            """,
            (product_id, organisation_id),
        ).fetchone()
        if not product:
            flash("Choose a valid product.", "error")
            return redirect(url_for("checkout"))
        sale_unit = db.execute(
            "SELECT id, label, quantity_in_base, selling_price FROM product_units WHERE id = %s AND product_id = %s AND is_active = TRUE",
            (unit_id, product["id"]),
        ).fetchone()
        if not sale_unit:
            flash("Choose a valid selling unit.", "error")
            return redirect(url_for("checkout"))
        store = current_store(db, organisation_id)
        if not store:
            flash("No active store is available for this sale.", "error")
            return redirect(url_for("checkout"))
        batch = db.execute("SELECT id, quantity_remaining, unit_cost, selling_price FROM product_batches WHERE id = %s AND store_id = %s AND product_id = %s AND organisation_id = %s AND quantity_remaining > 0 FOR UPDATE", (batch_id, store["id"], product["id"], organisation_id)).fetchone()
        if not batch:
            flash("Choose an available stock batch.", "error")
            return redirect(url_for("checkout"))
        store_stock = db.execute(
            "SELECT quantity FROM store_inventory WHERE store_id = %s AND product_id = %s FOR UPDATE",
            (store["id"], product["id"]),
        ).fetchone()
        available = store_stock["quantity"] if store_stock else Decimal("0")
        base_quantity = quantity * sale_unit["quantity_in_base"]
        if batch["quantity_remaining"] < base_quantity:
            flash(f"Only {batch['quantity_remaining']} {product['unit']} is available in the selected batch.", "error")
            return redirect(url_for("checkout"))
        if available < base_quantity:
            flash(f"Only {available} {product['unit']} of {product['name']} is available at {store['name']}.", "error")
            return redirect(url_for("checkout"))
        customer = None
        if customer_id:
            customer = db.execute(
                "SELECT id, name FROM customers WHERE id = %s AND organisation_id = %s",
                (customer_id, organisation_id),
            ).fetchone()
            if not customer:
                flash("Choose a valid customer.", "error")
                return redirect(url_for("checkout"))
        list_unit_price = batch["selling_price"] * sale_unit["quantity_in_base"]
        unit_price = list_unit_price
        if requested_unit_price:
            try:
                requested_price = Decimal(requested_unit_price)
            except InvalidOperation:
                flash("Enter a valid unit price.", "error")
                return redirect(url_for("checkout"))
            if requested_price <= 0:
                flash("Unit price must be greater than zero.", "error")
                return redirect(url_for("checkout"))
            role = session.get("user_role")
            if role == "cashier" and requested_price != list_unit_price:
                flash("Cashiers must use the saved selling price.", "error")
                return redirect(url_for("checkout"))
            if role == "salesperson" and (requested_price > list_unit_price or requested_price < list_unit_price * Decimal("0.90")):
                flash("Salespeople can apply a discount of up to 10% only.", "error")
                return redirect(url_for("checkout"))
            if requested_price != list_unit_price and not price_override_reason:
                flash("Give a reason when changing the saved selling price.", "error")
                return redirect(url_for("checkout"))
            unit_price = requested_price
        discount_amount = (list_unit_price - unit_price) * quantity
        total_amount = quantity * unit_price
        receipt_prefix = db.execute(
            "SELECT receipt_prefix FROM organisations WHERE id = %s", (organisation_id,)
        ).fetchone()["receipt_prefix"]
        sale_number = f"{receipt_prefix}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{secrets.token_hex(3).upper()}"
        sale = db.execute(
            """
            INSERT INTO sales (organisation_id, sale_number, customer_id, customer_name, payment_method, total_amount, created_by_user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (organisation_id, sale_number, customer["id"] if customer else None, customer["name"] if customer else None, payment_method, total_amount, session["user_id"]),
        ).fetchone()
        db.execute(
            """
            INSERT INTO sale_items (sale_id, product_id, product_batch_id, product_name, quantity, base_quantity, unit_label, unit_price, list_unit_price, discount_amount, price_override_reason, cost_unit_price, line_total)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (sale["id"], product["id"], batch["id"], product["name"], quantity, base_quantity, sale_unit["label"], unit_price, list_unit_price, discount_amount, price_override_reason, batch["unit_cost"] * sale_unit["quantity_in_base"], total_amount),
        )
        db.execute("UPDATE product_batches SET quantity_remaining = quantity_remaining - %s WHERE id = %s", (base_quantity, batch["id"]))
        db.execute(
            "UPDATE products SET stock_quantity = stock_quantity - %s, updated_at = NOW() WHERE id = %s",
            (base_quantity, product["id"]),
        )
        db.execute(
            "UPDATE store_inventory SET quantity = quantity - %s, updated_at = NOW() WHERE store_id = %s AND product_id = %s",
            (base_quantity, store["id"], product["id"]),
        )
        db.execute(
            """
            INSERT INTO stock_movements (organisation_id, product_id, movement_type, quantity_change, notes, created_by_user_id)
            VALUES (%s, %s, 'sale', %s, %s, %s)
            """,
            (organisation_id, product["id"], -base_quantity, f"Sale {sale_number}", session["user_id"]),
        )
        if payment_method == "credit":
            db.execute(
                "INSERT INTO customer_credits (organisation_id, customer_id, sale_id, amount_due) VALUES (%s, %s, %s, %s)",
                (organisation_id, customer["id"], sale["id"], total_amount),
            )
        db.commit()
        log_audit("sale_completed", user_id=session["user_id"], details={"sale_id": sale["id"], "sale_number": sale_number})
        return redirect(url_for("sale_receipt", sale_id=sale["id"]))

    store = current_store(db, organisation_id)
    if not store:
        flash("No active store is available for checkout.", "error")
        products = []
    else:
        products = db.execute(
            """
            SELECT p.id, p.name, p.sku, p.unit, p.selling_price, si.quantity AS stock_quantity
            FROM products p
            JOIN store_inventory si ON si.product_id = p.id AND si.store_id = %s
            WHERE p.organisation_id = %s AND p.is_active = TRUE AND si.quantity > 0
              AND EXISTS (SELECT 1 FROM product_batches b WHERE b.store_id = %s AND b.product_id = p.id AND b.quantity_remaining > 0)
            ORDER BY p.name
            """,
            (store["id"], organisation_id, store["id"]),
        ).fetchall()
    for product_row in products:
        product_row["sale_units"] = active_product_units(db, product_row["id"])
        product_row["batches"] = db.execute("SELECT id, quantity_remaining, selling_price, batch_code, expiry_date, created_at FROM product_batches WHERE store_id = %s AND product_id = %s AND quantity_remaining > 0 ORDER BY expiry_date NULLS LAST, created_at, id", (store["id"], product_row["id"])).fetchall() if store else []
    customer_rows = db.execute(
        "SELECT id, name, phone FROM customers WHERE organisation_id = %s ORDER BY name", (organisation_id,)
    ).fetchall()
    return render_template("owner/owner_batch_checkout.html", products=products, customers=customer_rows, active_page="checkout", user_role=session.get("user_role"))


@app.get("/sales/<int:sale_id>/receipt")
@roles_required("owner", "manager", "salesperson", "cashier")
def sale_receipt(sale_id):
    db = get_db()
    sale = db.execute(
        """
        SELECT s.id, s.sale_number, s.customer_name, s.payment_method, s.total_amount, s.created_at,
               o.name AS organisation_name, o.address AS organisation_address, o.phone AS organisation_phone,
               o.email AS organisation_email, o.logo_url AS organisation_logo_url, o.tax_number,
               o.receipt_footer,
               u.email AS cashier_email
        FROM sales s
        JOIN organisations o ON o.id = s.organisation_id
        LEFT JOIN users u ON u.id = s.created_by_user_id
        WHERE s.id = %s AND s.organisation_id = %s
        """,
        (sale_id, session["organisation_id"]),
    ).fetchone()
    if not sale:
        abort(404)
    items = db.execute(
        """
        SELECT si.product_name, si.quantity, si.unit_price, si.line_total,
               COALESCE(si.list_unit_price, si.unit_price) AS list_unit_price,
               si.discount_amount, si.price_override_reason, COALESCE(si.unit_label, p.unit, 'unit') AS unit
        FROM sale_items si LEFT JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = %s ORDER BY si.id
        """,
        (sale_id,),
    ).fetchall()
    return render_template("owner/owner_receipt.html", sale=sale, items=items, user_role=session.get("user_role"))


@app.route("/sales/<int:sale_id>/return", methods=["GET", "POST"])
@roles_required("owner", "manager")
def sale_return(sale_id):
    db = get_db()
    organisation_id = session["organisation_id"]
    sale = db.execute("SELECT id, sale_number, created_at, voided_at FROM sales WHERE id = %s AND organisation_id = %s", (sale_id, organisation_id)).fetchone()
    if not sale:
        abort(404)
    if sale["voided_at"]:
        flash("This sale has been voided and cannot be returned.", "error")
        return redirect(url_for("sales_dashboard"))
    items = db.execute(
        """
        SELECT si.id, si.product_name, si.quantity, si.base_quantity, si.unit_label, si.product_batch_id,
               COALESCE(SUM(sr.quantity), 0) AS returned_quantity
        FROM sale_items si LEFT JOIN sale_returns sr ON sr.sale_item_id = si.id
        WHERE si.sale_id = %s
        GROUP BY si.id
        ORDER BY si.id
        """, (sale_id,)
    ).fetchall()
    if request.method == "POST":
        item_id = request.form.get("sale_item_id", "").strip()
        reason = request.form.get("reason", "").strip()
        try:
            quantity = Decimal(request.form.get("quantity", ""))
        except InvalidOperation:
            quantity = Decimal("0")
        item = next((row for row in items if str(row["id"]) == item_id), None)
        if not item or quantity <= 0 or not reason:
            flash("Choose an item, enter a valid quantity, and give a return reason.", "error")
            return redirect(url_for("sale_return", sale_id=sale_id))
        remaining = item["quantity"] - item["returned_quantity"]
        if quantity > remaining or not item["product_batch_id"]:
            flash("The return quantity is more than the quantity still eligible for return.", "error")
            return redirect(url_for("sale_return", sale_id=sale_id))
        base_quantity = item["base_quantity"]
        if base_quantity is None:
            product_unit = db.execute("SELECT quantity_in_base FROM product_units pu JOIN sale_items si ON si.product_id = pu.product_id WHERE si.id = %s AND pu.label = %s", (item["id"], item["unit_label"])).fetchone()
            if not product_unit:
                flash("This older sale cannot be returned automatically because its unit conversion is unavailable.", "error")
                return redirect(url_for("sale_return", sale_id=sale_id))
            base_quantity = item["quantity"] * product_unit["quantity_in_base"]
        returned_base = base_quantity * quantity / item["quantity"]
        batch = db.execute("SELECT id, store_id, product_id FROM product_batches WHERE id = %s FOR UPDATE", (item["product_batch_id"],)).fetchone()
        if not batch:
            flash("The original stock batch is unavailable.", "error")
            return redirect(url_for("sale_return", sale_id=sale_id))
        db.execute("INSERT INTO sale_returns (organisation_id, sale_item_id, product_batch_id, quantity, base_quantity, reason, created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)", (organisation_id, item["id"], batch["id"], quantity, returned_base, reason, session["user_id"]))
        db.execute("UPDATE product_batches SET quantity_remaining = quantity_remaining + %s WHERE id = %s", (returned_base, batch["id"]))
        db.execute("UPDATE products SET stock_quantity = stock_quantity + %s, updated_at = NOW() WHERE id = %s", (returned_base, batch["product_id"]))
        db.execute("UPDATE store_inventory SET quantity = quantity + %s, updated_at = NOW() WHERE store_id = %s AND product_id = %s", (returned_base, batch["store_id"], batch["product_id"]))
        db.execute("INSERT INTO stock_movements (organisation_id, product_id, movement_type, quantity_change, notes, created_by_user_id) VALUES (%s, %s, 'return', %s, %s, %s)", (organisation_id, batch["product_id"], returned_base, f"Sale return {sale['sale_number']}: {reason}", session["user_id"]))
        db.commit()
        log_audit("sale_return_recorded", user_id=session["user_id"], details={"sale_id": sale_id, "sale_item_id": item["id"], "quantity": str(quantity)})
        flash("Sale return recorded and stock restored to the original batch.", "success")
        return redirect(url_for("sale_return", sale_id=sale_id))
    return render_template("owner/owner_sale_return.html", sale=sale, items=items, active_page="sales", user_role=session.get("user_role"))


@app.post("/sales/<int:sale_id>/void")
@roles_required("owner", "manager")
def void_sale(sale_id):
    db = get_db()
    organisation_id = session["organisation_id"]
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Give a reason before voiding a sale.", "error")
        return redirect(url_for("sales_dashboard"))
    sale = db.execute("SELECT id, sale_number FROM sales WHERE id = %s AND organisation_id = %s AND voided_at IS NULL FOR UPDATE", (sale_id, organisation_id)).fetchone()
    if not sale:
        flash("This sale was not found or has already been voided.", "error")
        return redirect(url_for("sales_dashboard"))
    items = db.execute(
        """
        SELECT si.id, si.product_id, si.product_batch_id, COALESCE(si.base_quantity, 0) AS base_quantity,
               COALESCE(SUM(sr.base_quantity), 0) AS returned_base_quantity
        FROM sale_items si LEFT JOIN sale_returns sr ON sr.sale_item_id = si.id
        WHERE si.sale_id = %s GROUP BY si.id
        """, (sale_id,)
    ).fetchall()
    for item in items:
        restore_quantity = item["base_quantity"] - item["returned_base_quantity"]
        if restore_quantity <= 0:
            continue
        if not item["product_batch_id"] or not item["base_quantity"]:
            db.rollback()
            flash("This older sale cannot be voided automatically because batch details are missing.", "error")
            return redirect(url_for("sales_dashboard"))
        batch = db.execute("SELECT id, store_id, product_id FROM product_batches WHERE id = %s FOR UPDATE", (item["product_batch_id"],)).fetchone()
        if not batch:
            db.rollback()
            flash("The original batch is unavailable; this sale cannot be voided automatically.", "error")
            return redirect(url_for("sales_dashboard"))
        db.execute("UPDATE product_batches SET quantity_remaining = quantity_remaining + %s WHERE id = %s", (restore_quantity, batch["id"]))
        db.execute("UPDATE products SET stock_quantity = stock_quantity + %s, updated_at = NOW() WHERE id = %s", (restore_quantity, batch["product_id"]))
        db.execute("UPDATE store_inventory SET quantity = quantity + %s, updated_at = NOW() WHERE store_id = %s AND product_id = %s", (restore_quantity, batch["store_id"], batch["product_id"]))
        db.execute("INSERT INTO stock_movements (organisation_id, product_id, movement_type, quantity_change, notes, created_by_user_id) VALUES (%s, %s, 'return', %s, %s, %s)", (organisation_id, batch["product_id"], restore_quantity, f"Voided sale {sale['sale_number']}: {reason}", session["user_id"]))
    db.execute("UPDATE sales SET voided_at = NOW(), void_reason = %s, voided_by_user_id = %s WHERE id = %s", (reason, session["user_id"], sale_id))
    db.commit()
    log_audit("sale_voided", user_id=session["user_id"], details={"sale_id": sale_id, "sale_number": sale["sale_number"], "reason": reason})
    flash("Sale voided. Any stock not already returned was restored to its original batch.", "success")
    return redirect(url_for("sales_dashboard"))


@app.get("/expiry-alerts")
@roles_required("owner", "manager")
def expiry_alerts():
    db = get_db()
    organisation_id = session["organisation_id"]
    batches = db.execute(
        """
        SELECT b.id, b.batch_code, b.expiry_date, b.quantity_remaining, p.name AS product_name, p.unit, s.name AS store_name,
               (b.expiry_date - CURRENT_DATE) AS days_remaining
        FROM product_batches b JOIN products p ON p.id = b.product_id JOIN stores s ON s.id = b.store_id
        WHERE b.organisation_id = %s AND b.quantity_remaining > 0 AND b.expiry_date IS NOT NULL
          AND b.expiry_date <= CURRENT_DATE + INTERVAL '30 days'
        ORDER BY b.expiry_date, p.name
        """, (organisation_id,)
    ).fetchall()
    return render_template("owner/owner_expiry_alerts.html", batches=batches, active_page="expiry", user_role=session.get("user_role"))


@app.route("/business-settings", methods=["GET", "POST"])
@roles_required("owner")
def business_settings():
    db = get_db()
    organisation_id = session["organisation_id"]
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip() or None
        phone = request.form.get("phone", "").strip() or None
        email = request.form.get("email", "").strip().lower() or None
        logo_url = request.form.get("logo_url", "").strip() or None
        tax_number = request.form.get("tax_number", "").strip() or None
        receipt_footer = request.form.get("receipt_footer", "").strip() or None
        receipt_prefix = request.form.get("receipt_prefix", "S").strip().upper()
        if not receipt_prefix or len(receipt_prefix) > 12 or not re.fullmatch(r"[A-Z0-9-]+", receipt_prefix):
            flash("Receipt prefix can contain only letters, numbers, and hyphens (up to 12 characters).", "error")
            return redirect(url_for("business_settings"))
        if not name:
            flash("Business name is required.", "error")
        else:
            db.execute(
                """UPDATE organisations
                   SET name = %s, address = %s, phone = %s, email = %s, logo_url = %s,
                       tax_number = %s, receipt_footer = %s, receipt_prefix = %s
                   WHERE id = %s""",
                (name, address, phone, email, logo_url, tax_number, receipt_footer, receipt_prefix, organisation_id),
            )
            db.commit()
            log_audit("business_details_updated", user_id=session["user_id"])
            flash("Business receipt details updated.", "success")
            return redirect(url_for("business_settings"))
    organisation = db.execute(
        "SELECT name, address, phone, email, logo_url, tax_number, receipt_footer, receipt_prefix FROM organisations WHERE id = %s", (organisation_id,)
    ).fetchone()
    return render_template(
        "owner/owner_business_settings.html", organisation=organisation,
        active_page="settings", user_role=session.get("user_role"),
    )


@app.get("/help")
@roles_required("owner", "manager", "salesperson", "cashier")
def help_page():
    return render_template("owner/owner_help.html", active_page="help", user_role=session.get("user_role"))


@app.route("/customer-credit", methods=["GET", "POST"])
@roles_required("owner", "manager", "salesperson")
def customer_credit():
    db = get_db()
    organisation_id = session["organisation_id"]
    if request.method == "POST":
        credit_id = request.form.get("credit_id", "").strip()
        payment_method = request.form.get("payment_method", "cash")
        notes = request.form.get("notes", "").strip() or None
        try:
            amount = Decimal(request.form.get("amount", ""))
        except InvalidOperation:
            flash("Enter a valid repayment amount.", "error")
            return redirect(url_for("customer_credit"))
        credit = db.execute(
            """
            SELECT id, customer_id, amount_due, amount_paid
            FROM customer_credits
            WHERE id = %s AND organisation_id = %s AND status = 'open' FOR UPDATE
            """,
            (credit_id, organisation_id),
        ).fetchone()
        if not credit or amount <= 0:
            flash("Choose an open credit and enter a positive amount.", "error")
            return redirect(url_for("customer_credit"))
        outstanding = credit["amount_due"] - credit["amount_paid"]
        if amount > outstanding:
            flash(f"Payment cannot be more than the outstanding balance of {outstanding:.2f}.", "error")
            return redirect(url_for("customer_credit"))
        new_paid = credit["amount_paid"] + amount
        status = "paid" if new_paid == credit["amount_due"] else "open"
        db.execute(
            "INSERT INTO credit_payments (credit_id, amount, payment_method, notes, received_by_user_id) VALUES (%s, %s, %s, %s, %s)",
            (credit["id"], amount, payment_method, notes, session["user_id"]),
        )
        db.execute(
            "UPDATE customer_credits SET amount_paid = %s, status = %s, paid_at = CASE WHEN %s = 'paid' THEN NOW() ELSE NULL END WHERE id = %s",
            (new_paid, status, status, credit["id"]),
        )
        db.commit()
        log_audit("credit_payment_recorded", user_id=session["user_id"], details={"credit_id": credit["id"], "amount": str(amount)})
        flash("Customer repayment recorded.", "success")
        return redirect(url_for("customer_credit"))

    credits = db.execute(
        """
        SELECT cc.id, cc.amount_due, cc.amount_paid, cc.created_at, s.sale_number,
               c.name AS customer_name, c.phone,
               (cc.amount_due - cc.amount_paid) AS outstanding
        FROM customer_credits cc
        JOIN customers c ON c.id = cc.customer_id
        JOIN sales s ON s.id = cc.sale_id
        WHERE cc.organisation_id = %s AND cc.status = 'open'
        ORDER BY cc.created_at ASC
        """,
        (organisation_id,),
    ).fetchall()
    total_outstanding = sum((credit["outstanding"] for credit in credits), Decimal("0"))
    return render_template("owner/owner_customer_credit.html", credits=credits, total_outstanding=total_outstanding, active_page="credit", user_role=session.get("user_role"))


@app.route("/stores", methods=["GET", "POST"])
@roles_required("owner")
def stores():
    db = get_db()
    organisation_id = session["organisation_id"]
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        address = request.form.get("address", "").strip() or None
        if not name:
            flash("Enter a store name.", "error")
            return redirect(url_for("stores"))
        try:
            db.execute(
                "INSERT INTO stores (organisation_id, name, address) VALUES (%s, %s, %s)",
                (organisation_id, name, address),
            )
            db.commit()
            log_audit("store_created", user_id=session["user_id"], details={"name": name})
            flash(f"Created {name}.", "success")
        except errors.UniqueViolation:
            db.rollback()
            flash("A store with that name already exists.", "error")
        return redirect(url_for("stores"))

    store_rows = db.execute(
        """
        SELECT s.id, s.name, s.address, s.is_active, s.created_at,
               COUNT(DISTINCT u.id) AS assigned_staff
        FROM stores s
        LEFT JOIN users u ON u.store_id = s.id
        WHERE s.organisation_id = %s
        GROUP BY s.id
        ORDER BY s.created_at ASC
        """,
        (organisation_id,),
    ).fetchall()
    return render_template("owner/owner_stores.html", stores=store_rows, active_page="stores", user_role=session.get("user_role"))


@app.get("/store-inventory")
@roles_required("owner", "manager")
def store_inventory():
    db = get_db()
    organisation_id = session["organisation_id"]
    stores = db.execute(
        "SELECT id, name FROM stores WHERE organisation_id = %s AND is_active = TRUE ORDER BY created_at",
        (organisation_id,),
    ).fetchall()
    stock_rows = db.execute(
        """
        SELECT p.name AS product_name, p.sku, p.unit, s.name AS store_name, si.quantity
        FROM store_inventory si
        JOIN products p ON p.id = si.product_id
        JOIN stores s ON s.id = si.store_id
        WHERE s.organisation_id = %s AND p.is_active = TRUE
        ORDER BY p.name, s.name
        """,
        (organisation_id,),
    ).fetchall()
    return render_template(
        "owner/owner_store_inventory.html", stores=stores, stock_rows=stock_rows,
        active_page="store_inventory", user_role=session.get("user_role"),
    )


@app.route("/stock-transfers", methods=["GET", "POST"])
@roles_required("owner", "manager")
def stock_transfers():
    db = get_db()
    organisation_id = session["organisation_id"]
    if request.method == "POST":
        product_id = request.form.get("product_id", "").strip()
        from_store_id = request.form.get("from_store_id", "").strip()
        to_store_id = request.form.get("to_store_id", "").strip()
        notes = request.form.get("notes", "").strip() or None
        try:
            quantity = Decimal(request.form.get("quantity", ""))
        except InvalidOperation:
            flash("Enter a valid transfer quantity.", "error")
            return redirect(url_for("stock_transfers"))
        if quantity <= 0 or from_store_id == to_store_id:
            flash("Choose different stores and enter a positive quantity.", "error")
            return redirect(url_for("stock_transfers"))
        product = db.execute(
            "SELECT id, name FROM products WHERE id = %s AND organisation_id = %s AND is_active = TRUE",
            (product_id, organisation_id),
        ).fetchone()
        stores = db.execute(
            "SELECT id FROM stores WHERE id IN (%s, %s) AND organisation_id = %s AND is_active = TRUE",
            (from_store_id, to_store_id, organisation_id),
        ).fetchall()
        if not product or len(stores) != 2:
            flash("Choose a valid product and active stores.", "error")
            return redirect(url_for("stock_transfers"))
        source = db.execute(
            "SELECT quantity FROM store_inventory WHERE store_id = %s AND product_id = %s FOR UPDATE",
            (from_store_id, product_id),
        ).fetchone()
        if not source or source["quantity"] < quantity:
            flash(f"The source store does not have enough {product['name']} for this transfer.", "error")
            return redirect(url_for("stock_transfers"))
        batches = db.execute(
            "SELECT id, purchase_id, quantity_remaining, unit_cost, selling_price FROM product_batches WHERE store_id = %s AND product_id = %s AND quantity_remaining > 0 ORDER BY created_at, id FOR UPDATE",
            (from_store_id, product_id),
        ).fetchall()
        if sum((batch["quantity_remaining"] for batch in batches), Decimal("0")) < quantity:
            flash("The source store's batch records do not cover this transfer. Reconcile its stock before transferring.", "error")
            return redirect(url_for("stock_transfers"))
        remaining = quantity
        for batch in batches:
            moved = min(remaining, batch["quantity_remaining"])
            if moved <= 0:
                break
            db.execute("UPDATE product_batches SET quantity_remaining = quantity_remaining - %s WHERE id = %s", (moved, batch["id"]))
            db.execute("INSERT INTO product_batches (organisation_id, store_id, product_id, purchase_id, quantity_remaining, unit_cost, selling_price) VALUES (%s, %s, %s, %s, %s, %s, %s)", (organisation_id, to_store_id, product_id, batch["purchase_id"], moved, batch["unit_cost"], batch["selling_price"]))
            remaining -= moved
        db.execute(
            "UPDATE store_inventory SET quantity = quantity - %s, updated_at = NOW() WHERE store_id = %s AND product_id = %s",
            (quantity, from_store_id, product_id),
        )
        db.execute(
            """
            INSERT INTO store_inventory (store_id, product_id, quantity, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (store_id, product_id)
            DO UPDATE SET quantity = store_inventory.quantity + EXCLUDED.quantity, updated_at = NOW()
            """,
            (to_store_id, product_id, quantity),
        )
        db.execute(
            """
            INSERT INTO stock_transfers (organisation_id, product_id, from_store_id, to_store_id, quantity, notes, created_by_user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (organisation_id, product_id, from_store_id, to_store_id, quantity, notes, session["user_id"]),
        )
        db.commit()
        log_audit("stock_transferred", user_id=session["user_id"], details={"product_id": product_id, "from_store_id": from_store_id, "to_store_id": to_store_id, "quantity": str(quantity)})
        flash("Stock transfer recorded.", "success")
        return redirect(url_for("stock_transfers"))

    stores = db.execute(
        "SELECT id, name FROM stores WHERE organisation_id = %s AND is_active = TRUE ORDER BY created_at",
        (organisation_id,),
    ).fetchall()
    products = db.execute(
        "SELECT id, name, sku, unit FROM products WHERE organisation_id = %s AND is_active = TRUE ORDER BY name",
        (organisation_id,),
    ).fetchall()
    transfers = db.execute(
        """
        SELECT t.quantity, t.notes, t.created_at, p.name AS product_name, p.unit,
               fs.name AS from_store_name, ts.name AS to_store_name
        FROM stock_transfers t
        JOIN products p ON p.id = t.product_id
        JOIN stores fs ON fs.id = t.from_store_id
        JOIN stores ts ON ts.id = t.to_store_id
        WHERE t.organisation_id = %s ORDER BY t.created_at DESC LIMIT 20
        """,
        (organisation_id,),
    ).fetchall()
    return render_template(
        "owner/owner_stock_transfers.html", stores=stores, products=products, transfers=transfers,
        active_page="transfers", user_role=session.get("user_role"),
    )


@app.route("/expenses", methods=["GET", "POST"])
@roles_required("owner", "manager")
def expenses():
    db = get_db()
    organisation_id = session["organisation_id"]
    categories = ("Rent", "Transport", "Salaries", "Utilities", "Packaging", "Marketing", "Other")
    if request.method == "POST":
        category = request.form.get("category", "Other")
        description = request.form.get("description", "").strip()
        expense_date = request.form.get("expense_date", "").strip()
        notes = request.form.get("notes", "").strip() or None
        try:
            amount = Decimal(request.form.get("amount", ""))
        except InvalidOperation:
            flash("Enter a valid expense amount.", "error")
            return redirect(url_for("expenses"))
        if category not in categories or not description or amount <= 0 or not expense_date:
            flash("Enter a category, description, positive amount, and date.", "error")
            return redirect(url_for("expenses"))
        db.execute(
            """
            INSERT INTO expenses (organisation_id, category, description, amount, expense_date, notes, created_by_user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (organisation_id, category, description, amount, expense_date, notes, session["user_id"]),
        )
        db.commit()
        log_audit("expense_recorded", user_id=session["user_id"], details={"category": category, "amount": str(amount)})
        flash("Expense recorded.", "success")
        return redirect(url_for("expenses"))
    expense_rows = db.execute(
        "SELECT category, description, amount, expense_date, notes FROM expenses WHERE organisation_id = %s ORDER BY expense_date DESC, id DESC LIMIT 30",
        (organisation_id,),
    ).fetchall()
    total_expenses = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE organisation_id = %s", (organisation_id,)
    ).fetchone()["total"]
    return render_template(
        "owner/owner_expenses.html", categories=categories, expenses=expense_rows, total_expenses=total_expenses,
        active_page="expenses", user_role=session.get("user_role"), today=datetime.now(timezone.utc).date().isoformat(),
    )


@app.route("/closing-stock", methods=["GET", "POST"])
@roles_required("owner", "manager")
def closing_stock():
    db = get_db()
    organisation_id = session["organisation_id"]
    stores = db.execute(
        "SELECT id, name FROM stores WHERE organisation_id = %s AND is_active = TRUE ORDER BY name",
        (organisation_id,),
    ).fetchall()
    selected_store_id = request.values.get("store_id", "").strip()
    if selected_store_id:
        try:
            selected_store_id = int(selected_store_id)
        except ValueError:
            flash("Choose a valid store.", "error")
            return redirect(url_for("closing_stock"))
    else:
        default_store = current_store(db, organisation_id)
        if not default_store:
            flash("No active store is available. Create or activate a store first.", "error")
            return redirect(url_for("stores"))
        selected_store_id = default_store["id"]
    store = db.execute(
        "SELECT id, name FROM stores WHERE id = %s AND organisation_id = %s AND is_active = TRUE",
        (selected_store_id, organisation_id),
    ).fetchone()
    if not store:
        flash("Choose an active store.", "error")
        return redirect(url_for("closing_stock"))

    rows = db.execute(
        """
        SELECT p.id, p.name, p.sku, p.unit, p.cost_price, p.selling_price, COALESCE(si.quantity, 0) AS expected_quantity
        FROM products p
        LEFT JOIN store_inventory si ON si.product_id = p.id AND si.store_id = %s
        WHERE p.organisation_id = %s AND p.is_active = TRUE
        ORDER BY p.name
        """,
        (store["id"], organisation_id),
    ).fetchall()
    if request.method == "POST":
        notes = request.form.get("notes", "").strip() or None
        difference_reason = request.form.get("difference_reason", "").strip() or None
        counts = []
        try:
            for row in rows:
                counted = Decimal(request.form.get(f"count_{row['id']}", ""))
                if counted < 0:
                    raise InvalidOperation
                difference = counted - row["expected_quantity"]
                counts.append((row, counted, difference))
        except (InvalidOperation, ValueError):
            flash("Enter a valid physical count for every product.", "error")
            return redirect(url_for("closing_stock", store_id=store["id"]))
        if any(difference != 0 for _, _, difference in counts) and not difference_reason:
            flash("Give one reason for the stock differences before confirming.", "error")
            return redirect(url_for("closing_stock", store_id=store["id"]))
        closing = db.execute(
            "INSERT INTO stock_closings (organisation_id, store_id, notes, closed_by_user_id) VALUES (%s, %s, %s, %s) RETURNING id",
            (organisation_id, store["id"], notes, session["user_id"]),
        ).fetchone()
        for row, counted, difference in counts:
            db.execute(
                """
                INSERT INTO stock_closing_items (closing_id, product_id, expected_quantity, counted_quantity, difference_quantity, difference_reason)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (closing["id"], row["id"], row["expected_quantity"], counted, difference, difference_reason if difference else None),
            )
            if difference:
                if difference > 0:
                    db.execute("INSERT INTO product_batches (organisation_id, store_id, product_id, quantity_remaining, unit_cost, selling_price) VALUES (%s, %s, %s, %s, %s, %s)", (organisation_id, store["id"], row["id"], difference, row["cost_price"], row["selling_price"]))
                else:
                    batches = db.execute("SELECT id, quantity_remaining FROM product_batches WHERE store_id = %s AND product_id = %s AND quantity_remaining > 0 ORDER BY created_at, id FOR UPDATE", (store["id"], row["id"])).fetchall()
                    required = -difference
                    if sum((batch["quantity_remaining"] for batch in batches), Decimal("0")) < required:
                        db.rollback()
                        flash(f"Batch records for {row['name']} do not cover this closing adjustment.", "error")
                        return redirect(url_for("closing_stock", store_id=store["id"]))
                    for batch in batches:
                        removed = min(required, batch["quantity_remaining"])
                        if removed <= 0:
                            break
                        db.execute("UPDATE product_batches SET quantity_remaining = quantity_remaining - %s WHERE id = %s", (removed, batch["id"]))
                        required -= removed
                db.execute(
                    """
                    INSERT INTO store_inventory (store_id, product_id, quantity, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (store_id, product_id)
                    DO UPDATE SET quantity = EXCLUDED.quantity, updated_at = NOW()
                    """,
                    (store["id"], row["id"], counted),
                )
                db.execute(
                    "UPDATE products SET stock_quantity = stock_quantity + %s, updated_at = NOW() WHERE id = %s",
                    (difference, row["id"]),
                )
                db.execute(
                    """INSERT INTO stock_movements (organisation_id, product_id, movement_type, quantity_change, notes, created_by_user_id)
                       VALUES (%s, %s, 'adjustment', %s, %s, %s)""",
                    (organisation_id, row["id"], difference, f"Closing stock: {difference_reason}", session["user_id"]),
                )
        db.commit()
        log_audit("stock_closing_completed", user_id=session["user_id"], details={"closing_id": closing["id"], "store_id": store["id"]})
        flash(f"Closing stock saved for {store['name']}.", "success")
        return redirect(url_for("closing_stock", store_id=store["id"]))
    recent_closings = db.execute(
        """SELECT sc.closing_date, sc.created_at, s.name AS store_name, u.email AS closed_by
           FROM stock_closings sc JOIN stores s ON s.id = sc.store_id LEFT JOIN users u ON u.id = sc.closed_by_user_id
           WHERE sc.organisation_id = %s ORDER BY sc.created_at DESC LIMIT 8""",
        (organisation_id,),
    ).fetchall()
    return render_template(
        "owner/owner_closing_stock.html", stores=stores, store=store, rows=rows, recent_closings=recent_closings,
        active_page="closing_stock", user_role=session.get("user_role"),
    )


@app.get("/reports")
@roles_required("owner", "manager")
def reports():
    db = get_db()
    organisation_id = session["organisation_id"]
    period = request.args.get("period", "month")
    today = datetime.now(timezone.utc).date()
    if period == "today":
        start_date = end_date = today
    elif period == "week":
        start_date, end_date = today - timedelta(days=today.weekday()), today
    elif period == "year":
        start_date, end_date = today.replace(month=1, day=1), today
    elif period == "custom":
        try:
            start_date = datetime.strptime(request.args.get("start_date", ""), "%Y-%m-%d").date()
            end_date = datetime.strptime(request.args.get("end_date", ""), "%Y-%m-%d").date()
            if end_date < start_date:
                raise ValueError
        except ValueError:
            flash("Choose a valid custom date range.", "error")
            return redirect(url_for("reports"))
    else:
        period = "month"
        start_date, end_date = today.replace(day=1), today
    summary = db.execute(
        """
        SELECT
            (SELECT COALESCE(SUM(total_amount), 0) FROM sales WHERE organisation_id = %s AND voided_at IS NULL AND created_at::date BETWEEN %s AND %s) AS sales_revenue,
            (SELECT COALESCE(SUM(sr.quantity * si.unit_price), 0) FROM sale_returns sr JOIN sale_items si ON si.id = sr.sale_item_id JOIN sales s ON s.id = si.sale_id WHERE sr.organisation_id = %s AND s.voided_at IS NULL AND sr.created_at::date BETWEEN %s AND %s) AS returns_amount,
            (SELECT COALESCE(SUM(si.quantity * si.cost_unit_price), 0) FROM sale_items si JOIN sales s ON s.id = si.sale_id WHERE s.organisation_id = %s AND s.voided_at IS NULL AND s.created_at::date BETWEEN %s AND %s) AS sold_cogs,
            (SELECT COALESCE(SUM(sr.quantity * si.cost_unit_price), 0) FROM sale_returns sr JOIN sale_items si ON si.id = sr.sale_item_id JOIN sales s ON s.id = si.sale_id WHERE sr.organisation_id = %s AND s.voided_at IS NULL AND sr.created_at::date BETWEEN %s AND %s) AS returned_cogs,
            (SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE organisation_id = %s AND expense_date BETWEEN %s AND %s) AS total_expenses,
            (SELECT COALESCE(SUM(quantity_remaining * unit_cost), 0) FROM product_batches WHERE organisation_id = %s) AS stock_value
        """,
        (organisation_id, start_date, end_date, organisation_id, start_date, end_date, organisation_id, start_date, end_date, organisation_id, start_date, end_date, organisation_id, start_date, end_date, organisation_id),
    ).fetchone()
    summary["revenue"] = summary["sales_revenue"] - summary["returns_amount"]
    summary["cogs"] = summary["sold_cogs"] - summary["returned_cogs"]
    summary["gross_profit"] = summary["revenue"] - summary["cogs"]
    net_profit = summary["gross_profit"] - summary["total_expenses"]
    gross_margin = (summary["gross_profit"] / summary["revenue"] * Decimal("100")) if summary["revenue"] else Decimal("0")
    top_products = db.execute(
        """
        SELECT si.product_name, COALESCE(SUM(si.quantity), 0) AS quantity_sold,
               COALESCE(SUM(si.line_total), 0) AS revenue
        FROM sale_items si JOIN sales s ON s.id = si.sale_id
        WHERE s.organisation_id = %s AND s.voided_at IS NULL AND s.created_at::date BETWEEN %s AND %s
        GROUP BY si.product_name
        ORDER BY quantity_sold DESC, revenue DESC LIMIT 5
        """,
        (organisation_id, start_date, end_date),
    ).fetchall()
    low_stock_products = db.execute(
        """
        SELECT name, sku, unit, stock_quantity, low_stock_threshold
        FROM products
        WHERE organisation_id = %s AND is_active = TRUE AND stock_quantity <= low_stock_threshold
        ORDER BY stock_quantity ASC, name LIMIT 8
        """,
        (organisation_id,),
    ).fetchall()
    recent_sales = db.execute(
        """
        SELECT sale_number, customer_name, total_amount, created_at
        FROM sales WHERE organisation_id = %s AND voided_at IS NULL AND created_at::date BETWEEN %s AND %s ORDER BY created_at DESC LIMIT 6
        """,
        (organisation_id, start_date, end_date),
    ).fetchall()
    return render_template(
        "owner/owner_reports.html", summary=summary, net_profit=net_profit, gross_margin=gross_margin, period=period, start_date=start_date, end_date=end_date,
        top_products=top_products, low_stock_products=low_stock_products, recent_sales=recent_sales,
        active_page="reports", user_role=session.get("user_role"),
    )


@app.route("/products", methods=["GET", "POST"])
@roles_required("owner", "manager")
def products():
    db = get_db()
    organisation_id = session["organisation_id"]

    if request.method == "POST":
        action = request.form.get("action")
        if action == "create-category":
            category_name = request.form.get("category_name", "").strip()
            if not category_name:
                flash("Enter a category name.", "error")
            else:
                try:
                    db.execute(
                        "INSERT INTO product_categories (organisation_id, name) VALUES (%s, %s)",
                        (organisation_id, category_name),
                    )
                    db.commit()
                    flash("Category created.", "success")
                except errors.UniqueViolation:
                    db.rollback()
                    flash("That category already exists.", "error")
            return redirect(url_for("products"))

        if action == "create-product":
            name = request.form.get("name", "").strip()
            sku = request.form.get("sku", "").strip() or None
            unit = request.form.get("unit", "each").strip() or "each"
            category_id = request.form.get("category_id", "").strip() or None
            try:
                cost_price = Decimal(request.form.get("cost_price", "0"))
                selling_price = Decimal(request.form.get("selling_price", "0"))
                stock_quantity = Decimal(request.form.get("stock_quantity", "0"))
                low_stock_threshold = Decimal(request.form.get("low_stock_threshold", "0"))
            except InvalidOperation:
                flash("Prices and quantities must be valid numbers.", "error")
                return redirect(url_for("products"))

            if not name or min(cost_price, selling_price, stock_quantity, low_stock_threshold) < 0:
                flash("Enter a product name and non-negative prices and quantities.", "error")
                return redirect(url_for("products"))
            if category_id:
                category = db.execute(
                    "SELECT id FROM product_categories WHERE id = %s AND organisation_id = %s",
                    (category_id, organisation_id),
                ).fetchone()
                if not category:
                    flash("Choose a valid category.", "error")
                    return redirect(url_for("products"))
            try:
                product = db.execute(
                    """
                    INSERT INTO products (organisation_id, category_id, name, sku, unit, cost_price, selling_price, stock_quantity, low_stock_threshold)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (organisation_id, category_id, name, sku, unit, cost_price, selling_price, stock_quantity, low_stock_threshold),
                ).fetchone()
                main_store = db.execute(
                    "SELECT id FROM stores WHERE organisation_id = %s AND name = 'Main Store'",
                    (organisation_id,),
                ).fetchone()
                if main_store:
                    db.execute(
                        "INSERT INTO store_inventory (store_id, product_id, quantity) VALUES (%s, %s, %s)",
                        (main_store["id"], product["id"], stock_quantity),
                    )
                    if stock_quantity > 0:
                        db.execute("INSERT INTO product_batches (organisation_id, store_id, product_id, quantity_remaining, unit_cost, selling_price) VALUES (%s, %s, %s, %s, %s, %s)", (organisation_id, main_store["id"], product["id"], stock_quantity, cost_price, selling_price))
                db.execute(
                    "INSERT INTO product_units (product_id, label, quantity_in_base, selling_price) VALUES (%s, %s, 1, %s)",
                    (product["id"], unit, selling_price),
                )
                db.commit()
                flash("Product created.", "success")
            except errors.UniqueViolation:
                db.rollback()
                flash("A product with that SKU already exists.", "error")
            return redirect(url_for("products"))

        flash("Invalid product action.", "error")
        return redirect(url_for("products"))

    categories = db.execute(
        "SELECT id, name FROM product_categories WHERE organisation_id = %s ORDER BY name",
        (organisation_id,),
    ).fetchall()
    product_rows = db.execute(
        """
        SELECT p.id, p.name, p.sku, p.unit, p.selling_price, p.stock_quantity,
               p.low_stock_threshold, c.name AS category_name
        FROM products p
        LEFT JOIN product_categories c ON c.id = p.category_id
        WHERE p.organisation_id = %s AND p.is_active = TRUE
        ORDER BY p.created_at DESC
        """,
        (organisation_id,),
    ).fetchall()
    return render_template(
        "owner/owner_products.html",
        categories=categories,
        products=product_rows,
        active_page="products",
        user_role=session.get("user_role"),
    )


@app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@roles_required("owner", "manager")
def edit_product(product_id):
    db = get_db()
    organisation_id = session["organisation_id"]
    product = db.execute(
        """
        SELECT id, category_id, name, sku, unit, cost_price, selling_price,
               stock_quantity, low_stock_threshold, is_active
        FROM products WHERE id = %s AND organisation_id = %s
        """,
        (product_id, organisation_id),
    ).fetchone()
    if not product:
        flash("Product not found.", "error")
        return redirect(url_for("products"))

    categories = db.execute(
        "SELECT id, name FROM product_categories WHERE organisation_id = %s ORDER BY name",
        (organisation_id,),
    ).fetchall()

    if request.method == "POST":
        action = request.form.get("action", "update")
        if action == "archive":
            db.execute("UPDATE products SET is_active = FALSE, updated_at = NOW() WHERE id = %s", (product_id,))
            db.commit()
            log_audit("product_archived", user_id=session["user_id"], details={"product_id": product_id})
            flash("Product archived. Its stock history was kept.", "success")
            return redirect(url_for("products"))

        name = request.form.get("name", "").strip()
        sku = request.form.get("sku", "").strip() or None
        unit = request.form.get("unit", "").strip() or "each"
        category_id = request.form.get("category_id", "").strip() or None
        try:
            cost_price = Decimal(request.form.get("cost_price", ""))
            selling_price = Decimal(request.form.get("selling_price", ""))
            low_stock_threshold = Decimal(request.form.get("low_stock_threshold", ""))
        except InvalidOperation:
            flash("Prices and the low-stock threshold must be valid numbers.", "error")
            return render_template("owner/owner_edit_product.html", product=product, categories=categories, active_page="products", user_role=session.get("user_role")), 400

        if not name or min(cost_price, selling_price, low_stock_threshold) < 0:
            flash("Enter a product name and non-negative values.", "error")
            return render_template("owner/owner_edit_product.html", product=product, categories=categories, active_page="products", user_role=session.get("user_role")), 400
        if category_id and not db.execute(
            "SELECT 1 FROM product_categories WHERE id = %s AND organisation_id = %s",
            (category_id, organisation_id),
        ).fetchone():
            flash("Choose a valid category.", "error")
            return redirect(url_for("edit_product", product_id=product_id))
        try:
            db.execute(
                """
                UPDATE products
                SET category_id = %s, name = %s, sku = %s, unit = %s,
                    cost_price = %s, selling_price = %s, low_stock_threshold = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (category_id, name, sku, unit, cost_price, selling_price, low_stock_threshold, product_id),
            )
            db.commit()
        except errors.UniqueViolation:
            db.rollback()
            flash("A product with that SKU already exists.", "error")
            return redirect(url_for("edit_product", product_id=product_id))
        log_audit("product_updated", user_id=session["user_id"], details={"product_id": product_id})
        flash("Product updated.", "success")
        return redirect(url_for("products"))

    return render_template(
        "owner/owner_edit_product.html",
        product=product,
        categories=categories,
        active_page="products",
        user_role=session.get("user_role"),
    )


@app.route("/products/<int:product_id>/units", methods=["GET", "POST"])
@roles_required("owner", "manager")
def product_units(product_id):
    db = get_db()
    organisation_id = session["organisation_id"]
    product = db.execute(
        "SELECT id, name, unit, selling_price FROM products WHERE id = %s AND organisation_id = %s AND is_active = TRUE",
        (product_id, organisation_id),
    ).fetchone()
    if not product:
        flash("Product not found.", "error")
        return redirect(url_for("products"))
    if request.method == "POST":
        label = request.form.get("label", "").strip()
        try:
            quantity_in_base = Decimal(request.form.get("quantity_in_base", ""))
            selling_price = Decimal(request.form.get("selling_price", ""))
        except InvalidOperation:
            flash("Enter valid conversion and selling price values.", "error")
            return redirect(url_for("product_units", product_id=product_id))
        if not label or quantity_in_base <= 0 or selling_price < 0:
            flash("Enter a unit name, positive base quantity, and non-negative selling price.", "error")
            return redirect(url_for("product_units", product_id=product_id))
        try:
            db.execute(
                "INSERT INTO product_units (product_id, label, quantity_in_base, selling_price) VALUES (%s, %s, %s, %s)",
                (product_id, label, quantity_in_base, selling_price),
            )
            db.commit()
            flash(f"{label} selling unit added.", "success")
        except errors.UniqueViolation:
            db.rollback()
            flash("That unit already exists for this product.", "error")
        return redirect(url_for("product_units", product_id=product_id))
    units = active_product_units(db, product_id)
    return render_template("owner/owner_product_units.html", product=product, units=units, active_page="products", user_role=session.get("user_role"))


@app.route("/stock-movements", methods=["GET", "POST"])
@roles_required("owner", "manager")
def stock_movements():
    db = get_db()
    organisation_id = session["organisation_id"]

    if request.method == "POST":
        product_id = request.form.get("product_id", "").strip()
        movement_type = request.form.get("movement_type", "").strip()
        notes = request.form.get("notes", "").strip() or None
        try:
            quantity = Decimal(request.form.get("quantity", ""))
        except InvalidOperation:
            flash("Enter a valid quantity.", "error")
            return redirect(url_for("stock_movements"))

        if movement_type not in {"stock_in", "sale", "return", "adjustment"} or quantity == 0:
            flash("Choose a movement type and enter a non-zero quantity.", "error")
            return redirect(url_for("stock_movements"))
        if movement_type != "adjustment" and quantity < 0:
            flash("Use a positive quantity for stock-in, sales, and returns.", "error")
            return redirect(url_for("stock_movements"))

        quantity_change = -quantity if movement_type == "sale" else quantity
        product = db.execute(
            "SELECT id, name, stock_quantity, cost_price, selling_price FROM products WHERE id = %s AND organisation_id = %s FOR UPDATE",
            (product_id, organisation_id),
        ).fetchone()
        if not product:
            flash("Choose a valid product.", "error")
            return redirect(url_for("stock_movements"))
        store = current_store(db, organisation_id)
        if not store:
            flash("No active store is available for this movement.", "error")
            return redirect(url_for("stock_movements"))
        store_stock = db.execute(
            "SELECT quantity FROM store_inventory WHERE store_id = %s AND product_id = %s FOR UPDATE",
            (store["id"], product["id"]),
        ).fetchone()
        available = store_stock["quantity"] if store_stock else Decimal("0")
        if available + quantity_change < 0:
            flash(f"Cannot record this movement: {product['name']} would have negative stock at {store['name']}.", "error")
            return redirect(url_for("stock_movements"))
        if quantity_change > 0:
            db.execute("INSERT INTO product_batches (organisation_id, store_id, product_id, quantity_remaining, unit_cost, selling_price) VALUES (%s, %s, %s, %s, %s, %s)", (organisation_id, store["id"], product["id"], quantity_change, product["cost_price"], product["selling_price"]))
        elif quantity_change < 0:
            batches = db.execute("SELECT id, quantity_remaining FROM product_batches WHERE store_id = %s AND product_id = %s AND quantity_remaining > 0 ORDER BY created_at, id FOR UPDATE", (store["id"], product["id"])).fetchall()
            required = -quantity_change
            if sum((batch["quantity_remaining"] for batch in batches), Decimal("0")) < required:
                flash("This store's batch records do not cover the stock being removed. Reconcile its stock first.", "error")
                return redirect(url_for("stock_movements"))
            for batch in batches:
                removed = min(required, batch["quantity_remaining"])
                if removed <= 0:
                    break
                db.execute("UPDATE product_batches SET quantity_remaining = quantity_remaining - %s WHERE id = %s", (removed, batch["id"]))
                required -= removed

        db.execute(
            "UPDATE products SET stock_quantity = stock_quantity + %s, updated_at = NOW() WHERE id = %s",
            (quantity_change, product["id"]),
        )
        db.execute(
            """
            INSERT INTO store_inventory (store_id, product_id, quantity, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (store_id, product_id)
            DO UPDATE SET quantity = store_inventory.quantity + EXCLUDED.quantity, updated_at = NOW()
            """,
            (store["id"], product["id"], quantity_change),
        )
        db.execute(
            """
            INSERT INTO stock_movements (organisation_id, product_id, movement_type, quantity_change, notes, created_by_user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (organisation_id, product["id"], movement_type, quantity_change, notes, session["user_id"]),
        )
        db.commit()
        log_audit(
            "stock_movement_created",
            user_id=session["user_id"],
            details={"product_id": product["id"], "type": movement_type, "quantity_change": str(quantity_change)},
        )
        flash("Stock movement recorded.", "success")
        return redirect(url_for("stock_movements"))

    product_rows = db.execute(
        "SELECT id, name, sku, unit, stock_quantity FROM products WHERE organisation_id = %s AND is_active = TRUE ORDER BY name",
        (organisation_id,),
    ).fetchall()
    movement_rows = db.execute(
        """
        SELECT sm.movement_type, sm.quantity_change, sm.notes, sm.created_at, p.name AS product_name, p.unit
        FROM stock_movements sm
        JOIN products p ON p.id = sm.product_id
        WHERE sm.organisation_id = %s
        ORDER BY sm.created_at DESC
        LIMIT 20
        """,
        (organisation_id,),
    ).fetchall()
    return render_template(
        "owner/owner_stock_movements.html",
        products=product_rows,
        movements=movement_rows,
        active_page="stock_movements",
        user_role=session.get("user_role"),
    )

def get_inventory_overview():
    """Return metrics supported by the current users and access_requests schema."""
    db = get_db()
    now = datetime.now(timezone.utc)
    counts = db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM users WHERE role = 'owner') AS total_users,
            (SELECT COUNT(*) FROM users WHERE role = 'owner'
                AND (locked_until IS NULL OR locked_until <= %s)) AS active_users,
            (SELECT COUNT(*) FROM users WHERE role = 'owner' AND locked_until > %s) AS inactive_users,
            (SELECT COUNT(*) FROM access_requests WHERE status = 'pending') AS pending_onboarding_requests
        """,
        (now, now),
    ).fetchone()
    return {
        **counts,
        "pending_payment": 0,
        "suspended": 0,
        "billing_overdue": 0,
        "billing_due_soon": 0,
        "low_completeness": 0,
        "pending_privacy_requests": 0,
        "location_bars": [],
    }

def format_timestamp(timestamp):
    if not timestamp:
        return "Never"
    return timestamp.astimezone().strftime("%b %d, %Y - %I:%M %p")
@app.route('/super-admin/users/add', methods=["GET", "POST"])
@admin_required
def super_admin_add_user():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        business_name = request.form.get("business_name", "").strip()
        email_regex = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
        if not business_name or not email_regex.match(email):
            flash("Enter a business name and a valid email address.", "error")
            return render_template(
                "super/super_admin_add_user.html", email=email, business_name=business_name
            ), 400

        db = get_db()
        try:
            organisation = db.execute(
                "INSERT INTO organisations (name) VALUES (%s) RETURNING id",
                (business_name,),
            ).fetchone()
            db.execute(
                "INSERT INTO users (email, password_hash, role, organisation_id) VALUES (%s, %s, 'owner', %s)",
                (email, generate_password_hash(secrets.token_urlsafe(32)), organisation["id"]),
            )
            user = db.execute(
                "SELECT id FROM users WHERE email = %s", (email,)
            ).fetchone()
            random_string = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                (
                    generate_password_hash(random_string),
                    user["id"],
                    datetime.now(timezone.utc) + timedelta(days=7),
                ),
            )
            db.commit()
        except errors.UniqueViolation:
            db.rollback()
            flash("A user with this email address already exists.", "error")
            return render_template(
                "super/super_admin_add_user.html", email=email, business_name=business_name
            ), 400

        reset_url = url_for(
            "reset_password", token=f"{user['id']}:{random_string}", _external=True
        )
        send_email(
            email,
            "Welcome to InventoryOS",
            f"Your owner account has been created. Set your password here:\n\n{reset_url}",
        )
        log_audit(
            "owner_created_by_admin",
            user_id=session["user_id"],
            details={"email": email, "organisation_id": organisation["id"]},
        )
        flash(f"Owner account created for {email}. A password setup link was sent.", "success")
        return redirect(url_for("super_admin_add_user"))

    return render_template(
        "super/super_admin_add_user.html",
        email="",
        business_name="",
    )

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")

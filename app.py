"""
Databricks App boilerplate:
- Serves a Flask API
- Reads/writes to Lakebase (Databricks-managed Postgres) via lakebase.py
- Adapted for Job Hunter (Adzuna API)

Run locally:
    python app.py
Deploy as a Databricks App using app.yaml.
"""

import logging
import os
import re

import requests
from databricks.sdk import WorkspaceClient
from flask import Flask, jsonify, render_template, request

import lakebase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("job-hunter-app")

app = Flask(__name__)
_w = WorkspaceClient()

TABLE_NAME = os.environ.get("ADZUNA_TABLE_NAME", "job_postings")
SAVED_JOBS_TABLE_NAME = os.environ.get("SAVED_JOBS_TABLE_NAME", "saved_jobs")

# Basic job title shape check
_JOB_TITLE_RE = re.compile(r"^[A-Za-z0-9\s\-]{1,255}$")


def ensure_job_postings_table():
    """Create the job postings table in Lakebase if it doesn't exist yet."""
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            description TEXT,
            url TEXT,
            posted_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def ensure_saved_jobs_table():
    """Create the saved jobs table in Lakebase if it doesn't exist yet."""
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {SAVED_JOBS_TABLE_NAME} (
            job_id TEXT NOT NULL,
            email TEXT NOT NULL,
            title TEXT,
            saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (job_id, email)
        )
        """
    )


def _current_user_email() -> str:
    """
    Resolve the current user's email so the saved jobs can be personalized.

    Databricks Apps inject the logged-in user's identity via the
    X-Forwarded-Email header on every request. Fall back to the Databricks
    SDK's current_user API for local development where that header isn't set.
    """
    header_email = request.headers.get("X-Forwarded-Email")
    if header_email:
        return header_email
    return _w.current_user.me().user_name


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page)."""
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


@app.route("/")
def index():
    """Simple UI to view and manage job postings."""
    return render_template("index.html")


@app.route("/jobs")
def list_jobs():
    """Read job postings from Lakebase."""
    ensure_job_postings_table()
    limit = int(request.args.get("limit", 100))
    rows = lakebase.run_query(
        f"SELECT id, title, company, location, description, url, posted_at FROM {TABLE_NAME} ORDER BY posted_at DESC LIMIT %s",
        (limit,),
    )
    return jsonify(rows)


@app.route("/jobs", methods=["POST"])
def add_job():
    """Add a job posting to Lakebase."""
    ensure_job_postings_table()

    if request.is_json:
        data = request.json
    else:
        data = request.form.to_dict()

    title = data.get("title", "").strip()
    company = data.get("company", "").strip()
    location = data.get("location", "").strip()
    description = data.get("description", "").strip()
    url = data.get("url", "").strip()
    job_id = data.get("id", "").strip()

    if not title or not _JOB_TITLE_RE.match(title):
        return jsonify({"error": f"Invalid job title: {title!r}"}), 400

    if not job_id:
        job_id = f"job_{title.replace(' ', '_').lower()}"

    lakebase.run_write(
        f"""
        INSERT INTO {TABLE_NAME} (id, title, company, location, description, url, posted_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (id) DO UPDATE
            SET title = EXCLUDED.title,
                company = EXCLUDED.company,
                location = EXCLUDED.location,
                description = EXCLUDED.description,
                url = EXCLUDED.url,
                posted_at = EXCLUDED.posted_at
        """,
        (job_id, title, company, location, description, url),
    )

    return jsonify({"id": job_id, "title": title, "company": company, "location": location})


@app.route("/saved-jobs", methods=["GET"])
def get_saved_jobs():
    """Return the current user's saved jobs."""
    ensure_saved_jobs_table()
    email = _current_user_email()
    rows = lakebase.run_query(
        f"SELECT job_id, title, saved_at FROM {SAVED_JOBS_TABLE_NAME} WHERE email = %s ORDER BY saved_at DESC",
        (email,),
    )
    return jsonify(rows)


@app.route("/saved-jobs", methods=["POST"])
def save_job():
    """Save a job posting to the user's saved list."""
    ensure_saved_jobs_table()

    if request.is_json:
        job_id = request.json.get("job_id", "")
        title = request.json.get("title", "")
    else:
        job_id = request.form.get("job_id", "")
        title = request.form.get("title", "")

    if not job_id:
        return jsonify({"error": "Missing job_id"}), 400

    email = _current_user_email()

    lakebase.run_write(
        f"""
        INSERT INTO {SAVED_JOBS_TABLE_NAME} (job_id, email, title, saved_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (job_id, email) DO NOTHING
        """,
        (job_id, email, title),
    )

    return jsonify({"job_id": job_id, "email": email, "title": title})


@app.route("/saved-jobs/<job_id>", methods=["DELETE"])
def remove_saved_job(job_id):
    """Remove a job from the user's saved list."""
    ensure_saved_jobs_table()
    email = _current_user_email()

    lakebase.run_write(
        f"DELETE FROM {SAVED_JOBS_TABLE_NAME} WHERE job_id = %s AND email = %s",
        (job_id, email),
    )

    return jsonify({"message": "Job removed from saved list"})


if __name__ == '__main__':
    host = os.getenv('FLASK_RUN_HOST', '0.0.0.0')
    port = int(os.getenv('FLASK_RUN_PORT', 8000))
    app.run(debug=True, host=host, port=port)
    print(f"Flask app running on http://{host}:{port}")

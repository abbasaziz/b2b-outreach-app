"""Flask app — dashboard, prospects, bulk send, editing, monitoring."""
import os
import threading
import yaml
from datetime import date
import time
from flask import Flask, render_template, jsonify, request, redirect, url_for

from db import (init, connect, get_stats, update_prospect,
                fetch_due_prospects, sent_today_count, log_event)

import importer
import sender as smtplib_sender
import inbox
import templates_engine

CFG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
with open(CFG_PATH) as f:
    CFG = yaml.safe_load(f)

DAILY_LIMIT = 50  # hard cap regardless of config

app = Flask(__name__, template_folder="templates_web", static_folder="static")
init()

@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not found"}), 404
    return "Not found", 404


@app.errorhandler(500)
def _server_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "server error", "detail": str(e)}), 500
    return "Server error", 500

_sending_thread = None
_sending_active = False
_sending_progress = {"done": 0, "total": 0, "current": "", "last_ok": None}


# ── Dashboard ────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    stats = get_stats()
    sent_today = sent_today_count()
    remaining = max(0, DAILY_LIMIT - sent_today)
    queued = len(fetch_due_prospects(limit=500))

    from db import get_status_counts, get_tier_counts
    status_counts = get_status_counts()
    tier_counts = get_tier_counts()

    conn = connect()
    recent = conn.execute("""
        SELECT e.*, p.company_name
        FROM events e
        LEFT JOIN prospects p ON p.id = e.prospect_id
        ORDER BY e.id DESC
        LIMIT 10
    """).fetchall()
    conn.close()

    return render_template(
        "dashboard.html",
        stats=stats,
        sent_today=sent_today,
        daily_limit=DAILY_LIMIT,
        remaining=remaining,
        queued=queued,
        status_counts=status_counts,
        tier_counts=tier_counts,
        recent=[dict(r) for r in recent],
    )

# ── Prospects list ───────────────────────────────────────────────────
@app.route("/prospects")
def prospects():
    status_filter = request.args.get("status", "")
    tier_filter = request.args.get("tier", "")
    sort = request.args.get("sort", "priority_rank")
    direction = request.args.get("dir", "asc")

    # Whitelist sort columns to prevent SQL injection
    allowed_sort = {
        "priority_rank": "priority_rank",
        "tier": "tier",
        "company": "company_name",
        "status": "status",
        "sent": "emails_sent",
        "last_sent": "last_sent_at",
    }
    sort_col = allowed_sort.get(sort, "priority_rank")
    sort_dir = "DESC" if direction == "desc" else "ASC"

    q = "SELECT * FROM prospects WHERE 1=1"
    args = []
    if status_filter:
        q += " AND status = ?"
        args.append(status_filter)
    if tier_filter:
        q += " AND tier = ?"
        args.append(tier_filter)
    q += f" ORDER BY {sort_col} {sort_dir} LIMIT 500"

    conn = connect()
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    conn.close()

    from db import get_status_counts, get_tier_counts
    status_counts = get_status_counts()
    tier_counts = get_tier_counts()

    return render_template(
        "prospects.html",
        prospects=rows,
        status_filter=status_filter,
        tier_filter=tier_filter,
        sort=sort,
        direction=direction,
        status_counts=status_counts,
        tier_counts=tier_counts,
    )


# ── Prospect detail + edit ───────────────────────────────────────────
@app.route("/prospect/<int:pid>")
def prospect_detail(pid):
    conn = connect()
    p = conn.execute("SELECT * FROM prospects WHERE id = ?", (pid,)).fetchone()
    if not p:
        conn.close()
        return "Not found", 404
    logs = conn.execute(
        "SELECT * FROM send_log WHERE prospect_id = ? ORDER BY id DESC LIMIT 20",
        (pid,),
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM events WHERE prospect_id = ? ORDER BY id DESC LIMIT 30",
        (pid,),
    ).fetchall()
    conn.close()
    return render_template("prospect_detail.html",
                           prospect=dict(p),
                           logs=[dict(x) for x in logs],
                           events=[dict(x) for x in events],
                           saved=request.args.get("saved") == "1",
                           sent=request.args.get("sent"))


@app.route("/prospect/<int:pid>/edit", methods=["POST"])
def prospect_edit(pid):
    f = request.form
    fields = {
        "company_name": f.get("company_name", "").strip(),
        "decision_maker": f.get("decision_maker", "").strip(),
        "direct_email": f.get("direct_email", "").strip(),
        "company_email": f.get("company_email", "").strip(),
        "phone": f.get("phone", "").strip(),
        "city_region": f.get("city_region", "").strip(),
        "tier": f.get("tier", "B").strip(),
        "personalization_hook": f.get("personalization_hook", "").strip(),
        "sales_angle": f.get("sales_angle", "").strip(),
        "notes": f.get("notes", "").strip(),
        "status": f.get("status", "pending").strip(),
    }
    update_prospect(pid, **fields)
    return redirect(url_for("prospect_detail", pid=pid, saved="1"))


# ── Import ───────────────────────────────────────────────────────────
@app.route("/import", methods=["POST"])
def do_import():
    importer.import_file()
    return redirect("/")


# ── Add lead manually ────────────────────────────────────────────────
@app.route("/add-lead", methods=["GET"])
def add_lead_form():
    return render_template("add_lead.html", added=False, sent=False, error=None)


@app.route("/add-lead", methods=["POST"])
def add_lead_submit():
    f = request.form
    company = (f.get("company_name") or "").strip()
    direct_email = (f.get("direct_email") or "").strip()
    if not company or not direct_email:
        return render_template("add_lead.html", added=False, sent=False,
                               error="Company name and direct email are required.")

    conn = connect()
    max_rank = conn.execute(
        "SELECT COALESCE(MAX(priority_rank), 0) FROM prospects"
    ).fetchone()[0]

    cur = conn.execute("""
        INSERT INTO prospects
        (priority_rank, tier, sales_score, company_name, decision_maker,
         direct_email, company_email, phone, city_region,
         personalization_hook, sales_angle, status, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        max_rank + 1,
        f.get("tier", "B").strip() or "B",
        0.0,
        company,
        (f.get("decision_maker") or "").strip(),
        direct_email,
        (f.get("company_email") or "").strip(),
        (f.get("phone") or "").strip(),
        (f.get("city_region") or "").strip(),
        (f.get("personalization_hook") or "").strip(),
        (f.get("sales_angle") or "").strip(),
        "pending",
        "MANUAL — added via UI",
    ))
    new_id = cur.lastrowid
    conn.commit()
    conn.close()

    if f.get("send_now") == "yes":
        conn = connect()
        row = conn.execute("SELECT * FROM prospects WHERE id = ?", (new_id,)).fetchone()
        conn.close()
        ok = smtplib_sender.send_one(dict(row))
        return render_template("add_lead.html", added=True, sent=ok,
                               error=None, prospect_id=new_id)

    return render_template("add_lead.html", added=True, sent=False,
                           error=None, prospect_id=new_id)


# ── Send: single prospect, chosen template ───────────────────────────
@app.route("/prospect/<int:pid>/preview")
def prospect_preview(pid):
    template_type = request.args.get("template_type", "proposal")
    if template_type not in ("proposal", "followup1", "followup2", "followup3"):
        return jsonify({"error": "Invalid template"}), 400
    conn = connect()
    row = conn.execute("SELECT * FROM prospects WHERE id = ?", (pid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    name, body = templates_engine.template_for(dict(row), override=template_type)
    preview_html = templates_engine.render_html(body, logo_cid="preview").replace(
        "cid:preview", url_for("static", filename="moversync-mark.png")
    )
    return jsonify({
        "subject": templates_engine.subject_for(dict(row), name),
        "body": body,
        "html": preview_html,
    })


@app.route("/prospect/<int:pid>/send-with", methods=["POST"])
def prospect_send_with(pid):
    template_type = request.form.get("template_type", "proposal")
    if template_type not in ("proposal", "followup1", "followup2", "followup3"):
        return "Invalid template", 400
    conn = connect()
    row = conn.execute("SELECT * FROM prospects WHERE id = ?", (pid,)).fetchone()
    conn.close()
    if not row:
        return "Not found", 404
    ok = smtplib_sender.send_one(dict(row), template_type=template_type)
    return redirect(url_for("prospect_detail", pid=pid, sent="1" if ok else "0"))


# ── Send: bulk to selected IDs ───────────────────────────────────────
def _run_send_ids(ids, template_type):
    global _sending_active, _sending_progress
    _sending_active = True
    _sending_progress = {"done": 0, "total": len(ids), "current": "", "last_ok": None}

    try:
        conn = connect()
        rows = []
        for pid in ids:
            r = conn.execute("SELECT * FROM prospects WHERE id = ?", (pid,)).fetchone()
            if r:
                rows.append(dict(r))
        conn.close()

        for i, p in enumerate(rows):
            _sending_progress["current"] = p["company_name"]
            ok = smtplib_sender.send_one(p, template_type=template_type)
            _sending_progress["done"] = i + 1
            _sending_progress["last_ok"] = ok

            if i < len(rows) - 1:
                import random as _r
                time.sleep(_r.randint(
                    CFG["sending"]["min_delay_seconds"],
                    CFG["sending"]["max_delay_seconds"],
                ))
    finally:
        _sending_active = False


@app.route("/send/bulk", methods=["POST"])
def send_bulk():
    global _sending_thread
    if _sending_active:
        return jsonify({"status": "already_running"})

    ids_str = request.form.get("ids", "")
    template_type = request.form.get("template", "proposal").strip()

    if template_type not in ("proposal", "followup1", "followup2", "followup3"):
        return jsonify({"status": "error", "error": "invalid template"})

    try:
        ids = [int(x) for x in ids_str.split(",") if x.strip()]
    except ValueError:
        return jsonify({"status": "error", "error": "invalid ids"})

    if not ids:
        return jsonify({"status": "error", "error": "no leads selected"})
    if len(ids) > 50:
        return jsonify({"status": "error", "error": "max 50 at once"})

    already = sent_today_count()
    remaining = DAILY_LIMIT - already
    if remaining <= 0:
        return jsonify({
            "status": "limit",
            "sent": already,
            "limit": DAILY_LIMIT,
        })

    ids = ids[:remaining]
    _sending_thread = threading.Thread(
        target=_run_send_ids,
        args=(ids, template_type),
        daemon=True,
    )
    _sending_thread.start()
    return jsonify({"status": "started", "total": len(ids)})


# ── Send: auto-queue (existing behaviour) ────────────────────────────
def _run_send(limit):
    global _sending_active, _sending_progress
    _sending_active = True
    _sending_progress = {"done": 0, "total": 0, "current": "", "last_ok": None}

    def cb(i, total, prospect, ok):
        _sending_progress["done"] = i
        _sending_progress["total"] = total
        _sending_progress["current"] = prospect["company_name"]
        _sending_progress["last_ok"] = ok

    try:
        smtplib_sender.send_batch(limit=limit, progress_cb=cb)
    finally:
        _sending_active = False


@app.route("/send/start", methods=["POST"])
def send_start():
    global _sending_thread
    if _sending_active:
        return jsonify({"status": "already_running"})
    try:
        limit = int(request.form.get("limit", CFG["sending"]["daily_limit"]))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "error": "Invalid send limit"}), 400
    if not 1 <= limit <= DAILY_LIMIT:
        return jsonify({"status": "error", "error": "Limit must be between 1 and 50"}), 400
    remaining = DAILY_LIMIT - sent_today_count()
    if remaining <= 0:
        return jsonify({"status": "limit", "error": "Daily sending limit reached"})
    limit = min(limit, remaining)
    _sending_thread = threading.Thread(target=_run_send, args=(limit,), daemon=True)
    _sending_thread.start()
    return jsonify({"status": "started", "limit": limit})


@app.route("/send/progress")
def send_progress():
    return jsonify({
        "active": _sending_active,
        "done": _sending_progress["done"],
        "total": _sending_progress["total"],
        "current": _sending_progress["current"],
        "last_ok": _sending_progress["last_ok"],
    })


# ── Inbox check ──────────────────────────────────────────────────────
@app.route("/inbox/check", methods=["POST"])
def inbox_check():
    result = inbox.check_inbox()
    return jsonify(result)


@app.route("/mailbox/folders", methods=["POST"])
def mailbox_folders():
    return jsonify(smtplib_sender.ensure_imap_folders())


# ── API ──────────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/api/today")
def api_today():
    return jsonify({
        "sent_today": sent_today_count(),
        "daily_limit": DAILY_LIMIT,
    })

@app.route("/api/prospect/<int:pid>/status", methods=["POST"])
def api_prospect_status(pid):
    """Inline status change from the prospects list. Returns JSON."""
    new_status = request.form.get("status", "").strip()
    allowed = {"pending", "sent", "followup1", "followup2", "followup3",
               "replied", "bounced", "unsubscribed", "skipped"}
    if new_status not in allowed:
        return jsonify({"ok": False, "error": "invalid status"}), 400

    update_prospect(pid, status=new_status)
    log_event(pid, "status_changed", f"→ {new_status}")

    from db import get_status_counts
    return jsonify({"ok": True, "status": new_status, "counts": get_status_counts()})

if __name__ == "__main__":
    app.run(host=CFG["ui"]["host"], port=CFG["ui"]["port"], debug=False)

# Project Structure and Files: moversync-outreach

## Directory Tree
```text
moversync-outreach/
├── .vscode/
│   └── settings.json
├── static/
│   ├── app.js
│   ├── moversync-mark.svg
│   └── style.css
├── templates/
│   ├── breakup_day14.txt
│   ├── followup_day3.txt
│   ├── followup_day7.txt
│   ├── tier_a.txt
│   ├── tier_a_plus.txt
│   ├── tier_b.txt
│   └── tier_c.txt
├── templates_web/
│   ├── add_lead.html
│   ├── base.html
│   ├── dashboard.html
│   ├── prospect_detail.html
│   └── prospects.html
├── app.py
├── campaign_run_2026_09_17.py
├── config.yaml
├── db.py
├── importer.py
├── inbox.py
├── project_summary.md
├── project_summary.py
├── requirements.txt
├── run.bat
├── sender.py
└── templates_engine.py
```

## File Contents

### File: `.vscode\settings.json`

```json
{
    "python-envs.pythonProjects": [
        {
            "path": ".",
            "envManager": "ms-python.python:venv",
            "packageManager": "ms-python.python:pip"
        }
    ]
}
```

### File: `app.py`

```py
"""Flask app — dashboard, prospects, bulk send, editing, monitoring."""
import os
import threading
import yaml
from datetime import date
import time
from flask import Flask, render_template, jsonify, request, redirect, url_for

from db import (init, connect, get_stats, update_prospect,
                fetch_due_prospects, sent_today_count)
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

    # latest 5 events for the activity feed
    conn = connect()
    recent = conn.execute("""
        SELECT e.*, p.company_name
        FROM events e
        LEFT JOIN prospects p ON p.id = e.prospect_id
        ORDER BY e.id DESC
        LIMIT 8
    """).fetchall()
    failures = conn.execute("""
        SELECT COUNT(*) FROM send_log
        WHERE success = 0 AND date(sent_at) = date('now')
    """).fetchone()[0]
    folder_issue = conn.execute("""
        SELECT detail FROM events WHERE event_type = 'imap_save_error'
        ORDER BY id DESC LIMIT 1
    """).fetchone()
    latest_folder_save = conn.execute("""
        SELECT id FROM events WHERE event_type = 'imap_save_ok'
        ORDER BY id DESC LIMIT 1
    """).fetchone()
    latest_folder_error = conn.execute("""
        SELECT id FROM events WHERE event_type = 'imap_save_error'
        ORDER BY id DESC LIMIT 1
    """).fetchone()
    conn.close()

    return render_template(
        "dashboard.html",
        stats=stats,
        sent_today=sent_today,
        daily_limit=DAILY_LIMIT,
        remaining=remaining,
        queued=queued,
        recent=[dict(r) for r in recent],
        failures=failures,
        folder_issue=folder_issue[0] if folder_issue and
            (not latest_folder_save or latest_folder_error[0] > latest_folder_save[0]) else None,
    )


# ── Prospects list ───────────────────────────────────────────────────
@app.route("/prospects")
def prospects():
    status_filter = request.args.get("status", "")
    tier_filter = request.args.get("tier", "")
    q = "SELECT * FROM prospects WHERE 1=1"
    args = []
    if status_filter:
        q += " AND status = ?"
        args.append(status_filter)
    if tier_filter:
        q += " AND tier = ?"
        args.append(tier_filter)
    q += " ORDER BY priority_rank ASC LIMIT 500"

    conn = connect()
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    conn.close()
    return render_template("prospects.html", prospects=rows,
                           status_filter=status_filter,
                           tier_filter=tier_filter)


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


if __name__ == "__main__":
    app.run(host=CFG["ui"]["host"], port=CFG["ui"]["port"], debug=False)

```

### File: `campaign_run_2026_09_17.py`

```py
"""One-off, reviewed A/A+ outreach run. Dry-run unless --send is supplied."""
import argparse
import json
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from db import connect, sent_today_count
import sender
import templates_engine

MAX_SENDS = 25
REPORT = Path(__file__).parent / "data" / "logs" / "manual_campaign_2026-09-17.jsonl"
MESSAGES = [
    (1, "Holdem's video surveys and quote follow-up",
     "Hi Holdem team,\n\nI noticed Holdem offers instant quotes alongside video surveys. When an enquiry comes in, keeping the survey, quote and next follow-up together can make the handoff easier.\n\nMoverSync brings those steps into one view. The live demo below is free to try.",
     "https://holdemremovals.co.uk/"),
    (2, "Leader's enquiries across WhatsApp and web",
     "Hi Leader team,\n\nYour site gives customers several ways to reach you, including a quote form and WhatsApp. I thought a single view of each enquiry and its next step might be useful for Leader Removals.\n\nThat is what MoverSync is designed to show. You can try the live demo free below.",
     "https://www.leaderremovals.co.uk/"),
    (3, "James, a clearer view of managed moves",
     "Hi James,\n\nTotal Moving Solutions coordinates tailored moves with removal partners. Keeping the enquiry, quote and next action visible across that handoff seems especially valuable for your team.\n\nMoverSync is built around that workflow. You can try the live demo free below and see if it fits.",
     "https://totalmovingsolutions.co.uk/about/"),
    (4, "MD1's home visit to quote workflow",
     "Hi MD1 team,\n\nI saw that MD1 uses home visits to prepare tailored removal quotes. MoverSync can keep the enquiry, survey details, quote and follow-up together as the job moves forward.\n\nThe live demo below is free to try if you would like to see the workflow.",
     "https://md1removals.co.uk/faq/"),
    (5, "City Removals' survey and quote handoff",
     "Hi City Removals team,\n\nYour Nottingham team offers home surveys before quoting. I thought MoverSync's view of each enquiry, quote and next follow-up could be relevant to that process.\n\nYou can try the live demo free below and see whether it would suit your team.",
     "https://cityremovalseastmidlands.co.uk/moving-house-in-nottingham-realistic-costs-timelines-and-what-to-expect-from-professional-removals/"),
    (6, "Top Removals' enquiry follow-up",
     "Hi Top Removals team,\n\nI noticed Top Removals handles moves across the UK and Europe. With different types of enquiries coming in, a clear next action after each quote can help the office stay on top of them.\n\nMoverSync shows that workflow in one place. The live demo below is free to try.",
     "https://www.getamover.co.uk/movers/zn1Mm/top-removals-limited/"),
    (7, "VP Smart's removals and storage enquiries",
     "Hi VP Smart team,\n\nYour services cover house moves, office moves, packing and storage. MoverSync gives a removals team one place to see each enquiry, quote and follow-up across those services.\n\nYou can try the live demo free below and decide whether it would be useful for VP Smart.",
     "https://vpsmart.co.uk/"),
    (8, "Plaza's survey to quote process",
     "Hi Plaza team,\n\nI saw that Plaza uses in-person surveys to shape removal quotes. MoverSync keeps the enquiry, survey, quote and next follow-up visible as one workflow.\n\nThe live demo below is free to try if you would like to see whether it fits Plaza Removals.",
     "https://plazaremovals.co.uk/moving-services/house-removals/"),
    (9, "Movers For Move's next quote follow-up",
     "Hi Movers For Move team,\n\nYour London office takes enquiries through its site as well as by phone and email. MoverSync brings those leads, quotes and next steps into one view.\n\nYou can try the live demo free below and see whether it would help your team.",
     "https://moversformove.co.uk/contact-us/"),
    (10, "Santa Removals' video quote follow-up",
     "Hi Santa Removals team,\n\nI read that your customers can get a quote through a video call or clips. MoverSync keeps the enquiry, survey detail, quote and follow-up together after that first conversation.\n\nThe live demo below is free to try if you would like to see the flow.",
     "https://www.getamover.co.uk/movers/BbNK5/santa-removals-ltd/"),
    (11, "DFK's quote and survey process",
     "Hi DFK team,\n\nYour site offers an instant quotation or a pre-move survey, followed by planning the right van and crew. MoverSync keeps those early enquiry and quote steps visible in one place.\n\nYou can try the live demo free below and see whether it fits DFK's process.",
     "https://www.dfktransportservices.co.uk/"),
    (12, "Red Lion's video and self-surveys",
     "Hi Red Lion team,\n\nI noticed you offer both video surveys and online self-surveys. MoverSync can keep the enquiry, survey, quote and next follow-up together for the office team.\n\nThe live demo below is free to try if you would like to see how that could look.",
     "https://redlionremovals.com/house-removal-companies-cardiff/"),
    (13, "Intercity's survey to booking workflow",
     "Hi Intercity team,\n\nYour Cardiff operation offers both home visits and video surveys, alongside UK and international moves. MoverSync gives the sales team a clear view from enquiry through quote and follow-up.\n\nYou can try the live demo free below and see whether it suits Intercity.",
     "https://www.intercityremovals.com/2026/08/18/best-removal-companies-cardiff/"),
    (14, "Ricky, keeping move dates and quotes in view",
     "Hi Ricky,\n\nWest Country Movers describes a 12-vehicle fleet built to handle changing move dates. I thought a clear view of each survey, quote and next action could be useful when those dates shift.\n\nMoverSync shows that workflow. You can try the live demo free below.",
     "https://www.getamover.co.uk/movers/5nEG/west-country-movers/"),
    (15, "Movivan's quote to booking handoff",
     "Hi Movivan team,\n\nI noticed Movivan handles local London moves as well as longer-distance work. MoverSync keeps each enquiry, quote and next follow-up visible as the booking develops.\n\nThe live demo below is free to try if you would like to see whether it fits your team.",
     "https://www.getamover.co.uk/movers/5xly/movivan-removals-ltd/"),
    (16, "First Class Removals' next enquiry step",
     "Hi First Class team,\n\nYour profile covers house and commercial moves, packing and storage. MoverSync lets a removals team see the enquiry, quote and next follow-up together across those services.\n\nYou can try the live demo free below and see if it is useful for First Class Removals.",
     "https://www.getamover.co.uk/movers/d9Llg/first-class-removals-ltd/"),
    (17, "Moving Forward's quote follow-up",
     "Hi Moving Forward team,\n\nI saw your profile covers removals, packing and storage around Newbury. MoverSync keeps each enquiry, quote and next action in one view, so the team can see what is due.\n\nThe live demo below is free to try if you would like to take a look.",
     "https://www.getamover.co.uk/movers/Kj12M/moving-forward-removals/"),
    (19, "Fast Interior's tailored move enquiries",
     "Hi Fast Interior team,\n\nYour profile highlights tailored relocations, packing and storage. MoverSync gives the team a clear view of each enquiry, quote and follow-up as those services are discussed.\n\nYou can try the live demo free below and decide if it would help Fast Interior Removals.",
     "https://www.getamover.co.uk/movers/W2ED/fast-interior-removals-ltd/"),
    (22, "Removals Expert's survey follow-up",
     "Hi Removals Expert team,\n\nI noticed customers can reach you through a detailed quote form, free surveys and WhatsApp. MoverSync brings the enquiry, quote and next follow-up into one view for the team.\n\nThe live demo below is free to try if you would like to see it in action.",
     "https://www.removalsexpert.co.uk/contact-us/"),
    (23, "RMV's storage and removals enquiries",
     "Hi RMV team,\n\nYour Storage to Your Door service sits alongside household and office removals. MoverSync gives a team one place to see each enquiry, quote and next action across those services.\n\nYou can try the live demo free below and see whether it fits RMV.",
     "https://www.getamover.co.uk/movers/WwLYr/rmv-storage-removals/"),
    (24, "Alexander James' varied move enquiries",
     "Hi Alexander James team,\n\nYour listings cover house moves, international work and clearance. MoverSync keeps an enquiry, quote and follow-up visible in one place, whatever the job type.\n\nThe live demo below is free to try if you would like to see the workflow.",
     "https://www.comparemymove.com/directory/alexander-james-removals"),
    (25, "Great Moving's quote follow-up",
     "Hi Great Moving team,\n\nI noticed Great Moving handles packing as well as commercial moves. MoverSync lets the team see each enquiry, quote and next step together while a move is being planned.\n\nYou can try the live demo free below and decide if it is relevant.",
     "https://www.getamover.co.uk/movers/7wae5/great-moving-ltd/"),
    (26, "Douglas, from home visit to quote",
     "Hi Douglas,\n\nDS Removals describes house visits and item lists as part of preparing an accurate quote. MoverSync keeps that enquiry, survey detail, quote and follow-up together for the next step.\n\nThe live demo below is free to try if you would like to see it.",
     "https://www.getamover.co.uk/movers/77M1d/ds-removals/"),
]


def eligible(conn, prospect_id):
    row = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    if not row:
        return None
    p = dict(row)
    email = (p.get("direct_email") or "").strip()
    if (
        p["tier"] not in ("A", "A+")
        or p["status"] != "pending"
        or p["emails_sent"]
        or parseaddr(email)[1] != email
        or "@" not in email
        or conn.execute("SELECT 1 FROM send_log WHERE prospect_id = ? AND success = 1 LIMIT 1",
                        (prospect_id,)).fetchone()
    ):
        return None
    return p


def main(send):
    assert len(MESSAGES) <= MAX_SENDS
    ids = [item[0] for item in MESSAGES]
    assert len(set(ids)) == len(ids)
    conn = connect()
    try:
        prospects = [eligible(conn, pid) for pid in ids]
        assert all(prospects), "An intended lead is missing or no longer eligible"
        assert len({p["direct_email"].lower() for p in prospects}) == len(prospects)
        assert sent_today_count() + len(prospects) <= min(50, sender.CFG["sending"]["daily_limit"])
        for (pid, subject, message, source), p in zip(MESSAGES, prospects):
            assert source.startswith("https://")
            assert 10 <= len(subject) <= 78 and not re.search(r"[\r\n]", subject)
            assert not re.search(r"[—\U0001f300-\U0001faff]", subject + message)
            assert len(message) < 650
            assert p["company_name"].lower().split()[0] in (subject + message).lower()
            body = templates_engine.compose(message)
            assert templates_engine.DEMO_URL in body
            assert "Peter Wilson" in body and "Reply \"stop\"" in body
            if not send:
                print(f"DRY RUN {pid:>3} {p['company_name']} <{p['direct_email']}> | {subject}", flush=True)
        if not send:
            print(f"READY: {len(MESSAGES)} unique A/A+ messages. No email sent.", flush=True)
            return
    finally:
        conn.close()

    sent = 0
    for index, (pid, subject, message, source) in enumerate(MESSAGES):
        conn = connect()
        try:
            p = eligible(conn, pid)
        finally:
            conn.close()
        if p is None:
            print(f"SKIP {pid}: status or send history changed", flush=True)
            continue
        if sent_today_count() >= 50 or sent >= MAX_SENDS:
            print("STOP: send cap reached", flush=True)
            break
        body = templates_engine.compose(message)
        ok = sender.send_one(p, template_type="proposal",
                             subject_override=subject, body_override=body)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with REPORT.open("a", encoding="utf-8") as report:
            report.write(json.dumps({
                "at": now, "prospect_id": pid, "company": p["company_name"],
                "subject": subject, "source": source, "success": ok,
            }) + "\n")
        if ok:
            sent += 1
        print(f"{now} {'SENT' if ok else 'FAILED'} {index + 1}/{len(MESSAGES)} "
              f"id={pid} company={p['company_name']}", flush=True)
        if index < len(MESSAGES) - 1:
            delay = random.SystemRandom().randrange(240, 601) / 4
            print(f"WAIT {delay:.2f}s", flush=True)
            time.sleep(delay)
    print(f"COMPLETE: {sent} successful messages.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()
    main(args.send)

```

### File: `config.yaml`

```yaml
# ─────────────────────────────────────────────────────────────
# MoverSync Outreach — Configuration
# ─────────────────────────────────────────────────────────────

smtp:
  host: smtp.zoho.com
  port: 465
  use_ssl: true          # true for 465, false for 587 (STARTTLS)
  username: peter@moversync.co.uk
  password: Abb0tt4b4d!@#
  from_name: "Peter Wilson"
  from_email: peter@moversync.co.uk
  reply_to: peter@moversync.co.uk

imap:
  enabled: true          # set false if free plan disables IMAP
  host: imap.zoho.com
  port: 993
  username: peter@moversync.co.uk
  password: Abb0tt4b4d!@#
  folder: INBOX

# ── Folder names for saving sent copies (must match Zoho folder names) ──
imap_folders:
  proposals: "MoverSync/Initial"
  followup1: "MoverSync/Follow-up 1"
  followup2: "MoverSync/Follow-up 2"
  followup3: "MoverSync/Final note"

sending:
  min_delay_seconds: 60
  max_delay_seconds: 120
  daily_limit: 50              # NEW: hard cap 50/day
  send_window_start: 9
  send_window_end: 17

followup:
  day3_enabled: true
  day7_enabled: true
  day14_enabled: true

ui:
  host: 127.0.0.1
  port: 5000

```

### File: `db.py`

```py
"""SQLite schema and helpers for MoverSync Outreach."""
import sqlite3
import os
from datetime import datetime, date, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "prospects.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    priority_rank INTEGER,
    tier TEXT,
    sales_score REAL,
    company_name TEXT,
    decision_maker TEXT,
    direct_email TEXT,
    company_email TEXT,
    phone TEXT,
    city_region TEXT,
    personalization_hook TEXT,
    sales_angle TEXT,

    -- campaign state
    status TEXT DEFAULT 'pending',          -- pending | queued | sent | followup1 | followup2 | breakup | replied | bounced | unsubscribed | skipped
    emails_sent INTEGER DEFAULT 0,
    first_sent_at TEXT,
    last_sent_at TEXT,
    next_followup_due TEXT,
    last_template_used TEXT,

    -- engagement
    replied INTEGER DEFAULT 0,
    replied_at TEXT,
    bounced INTEGER DEFAULT 0,
    bounced_at TEXT,
    bounce_reason TEXT,
    unsubscribed INTEGER DEFAULT 0,

    -- inbox / reply mapping
    reply_subject TEXT,
    reply_snippet TEXT,

    -- bookkeeping
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
CREATE INDEX IF NOT EXISTS idx_prospects_tier ON prospects(tier);
CREATE INDEX IF NOT EXISTS idx_prospects_rank ON prospects(priority_rank);

CREATE TABLE IF NOT EXISTS send_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id INTEGER,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
    template TEXT,
    to_email TEXT,
    cc_email TEXT,
    subject TEXT,
    body TEXT,
    smtp_response TEXT,
    success INTEGER,
    FOREIGN KEY(prospect_id) REFERENCES prospects(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id INTEGER,
    at TEXT DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT,
    detail TEXT
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def log_event(prospect_id, event_type, detail=""):
    conn = connect()
    conn.execute(
        "INSERT INTO events (prospect_id, event_type, detail) VALUES (?,?,?)",
        (prospect_id, event_type, detail),
    )
    conn.commit()
    conn.close()


def fetch_due_prospects(limit=50):
    """Return prospects ready to send: pending, or followups due."""
    conn = connect()
    today = date.today().isoformat()
    rows = conn.execute(
        """
        SELECT * FROM prospects
        WHERE (status = 'pending' AND direct_email IS NOT NULL AND direct_email != '')
           OR (status IN ('sent','followup1','followup2')
               AND next_followup_due IS NOT NULL
               AND next_followup_due <= ?)
        ORDER BY priority_rank ASC
        LIMIT ?
        """,
        (today, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_prospect(prospect_id, **fields):
    if not fields:
        return
    keys = ", ".join(f"{k} = ?" for k in fields.keys())
    values = list(fields.values()) + [prospect_id]
    conn = connect()
    conn.execute(f"UPDATE prospects SET {keys} WHERE id = ?", values)
    conn.commit()
    conn.close()


def get_stats():
    conn = connect()
    s = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
            SUM(CASE WHEN status='replied' THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN status='bounced' THEN 1 ELSE 0 END) AS bounced,
            SUM(CASE WHEN status='unsubscribed' THEN 1 ELSE 0 END) AS unsubscribed,
            COALESCE(SUM(emails_sent), 0) AS emails_total
        FROM prospects
    """).fetchone()
    conn.close()
    return dict(s)

def sent_today_count():
    """Count successful sends today (UTC)."""
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = connect()
    n = conn.execute(
        "SELECT COUNT(*) FROM send_log WHERE success = 1 AND substr(sent_at, 1, 10) = ?",
        (today,),
    ).fetchone()[0]
    conn.close()
    return n
```

### File: `importer.py`

```py
"""Import the Excel file into SQLite. Run once, or whenever you update the sheet."""
import openpyxl
import os
import re
from db import connect, init

XLSX = os.path.join(os.path.dirname(__file__), "data",
                    "uk_removals_crm_prospects_expanded_480.xlsx")


def clean(v):
    if v is None:
        return ""
    return str(v).strip()


def extract_email(raw):
    """Return first valid-looking email from a cell. Handles 'a@x.co.uk; b@y.co.uk'."""
    raw = clean(raw)
    if not raw or raw.upper() == "UNKNOWN":
        return ""
    found = re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", raw)
    return found[0] if found else ""


def extract_all_emails(raw):
    raw = clean(raw)
    if not raw or raw.upper() == "UNKNOWN":
        return []
    return re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", raw)


def import_file():
    init()
    if not os.path.exists(XLSX):
        print(f"❌ File not found: {XLSX}")
        print("   Put your Excel file at data/uk_removals_crm_prospects_expanded_480.xlsx")
        return

    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    # Basic Lead List is sheet 1 (index)
    ws = wb["Basic Lead List"]

    conn = connect()
    conn.execute("DELETE FROM prospects")

    rows = list(ws.iter_rows(values_only=True))
    # skip first 3 header rows
    imported = 0
    for r in rows[3:]:
        if not r or not r[0]:
            continue
        try:
            rank = int(r[0])
        except (ValueError, TypeError):
            continue

        tier = clean(r[1])
        score = float(r[2]) if r[2] not in (None, "") else 0.0
        company = clean(r[3])
        decision_maker = clean(r[4])
        direct_or_best = clean(r[5])
        phone = clean(r[6])
        city = clean(r[7])
        hook = clean(r[8])
        angle = clean(r[9])

        # The "Direct / Best Email" column may contain owner email + company email
        emails = extract_all_emails(direct_or_best)
        owner_email = ""
        company_email = ""

        # Heuristic: if decision_maker name is present and one email matches
        # the first name pattern, treat that as owner; else use first as owner
        # and second as company if there are two.
        if len(emails) >= 2:
            owner_email, company_email = emails[0], emails[1]
        elif len(emails) == 1:
            owner_email = emails[0]

        # Fallback — if we have a company domain email and no owner email,
        # use the company email as the sending target.
        if not owner_email and company_email:
            owner_email = company_email
            company_email = ""

        if not owner_email:
            # No usable address — record it as skipped so we don't lose the row
            status = "skipped"
        else:
            status = "pending"

        conn.execute("""
            INSERT INTO prospects
            (priority_rank, tier, sales_score, company_name, decision_maker,
             direct_email, company_email, phone, city_region,
             personalization_hook, sales_angle, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (rank, tier, score, company, decision_maker,
              owner_email, company_email, phone, city,
              hook, angle, status))
        imported += 1

    conn.commit()
    conn.close()
    print(f"✅ Imported {imported} prospects")


if __name__ == "__main__":
    import_file()
```

### File: `inbox.py`

```py
"""IMAP reply and bounce detection with keyword classification."""
import imaplib
import email
import yaml
import os
import re
from email.header import decode_header
from datetime import datetime
from db import connect, update_prospect, log_event

CFG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
with open(CFG_PATH) as f:
    CFG = yaml.safe_load(f)

# ── Bounce signals ────────────────────────────────────────────────
BOUNCE_FROM_PARTS = ("mailer-daemon@", "postmaster@", "no-reply@", "mail-daemon@")
BOUNCE_SUBJECT_PARTS = (
    "undelivered", "delivery status", "mail delivery failed",
    "returned mail", "failure notice", "mail system error",
    "delivery has failed", "could not be delivered",
    "delivery failure", "message not delivered",
)
BOUNCE_BODY_PHRASES = (
    "the following address(es) failed",
    "does not exist",
    "user unknown",
    "mailbox unavailable",
    "recipient address rejected",
    "no such user",
    "address rejected",
    "permanent failure",
    "5.1.1", "5.1.0", "5.4.1", "5.7.1",
)

# ── Reply keyword classification ──────────────────────────────────
HARD_STOP_KEYWORDS = (
    "stop", "unsubscribe", "remove me", "take me off",
    "do not contact", "don't contact", "opt out", "opt-out",
    "leave me alone", "cease", "unsub",
)
SOFT_NO_KEYWORDS = (
    "not interested", "no thanks", "no thank you", "not for us",
    "not right now", "we're fine", "we are fine", "no need",
    "we already have", "we use something", "decline",
)
POSITIVE_KEYWORDS = (
    "interested", "tell me more", "send me info", "send info",
    "more details", "call me", "let's talk", "lets talk",
    "book a demo", "book demo", "happy to chat", "sounds good",
    "how much", "pricing", "what does it cost",
)


def _decode(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            try:
                out += part.decode(enc or "utf-8", errors="ignore")
            except Exception:
                out += part.decode("utf-8", errors="ignore")
        else:
            out += part
    return out


def _get_body_text(msg):
    """Return the first text/plain or text/html body as a string."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode("utf-8", errors="ignore")
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode("utf-8", errors="ignore")
        except Exception:
            pass
    return ""


def _classify_reply(text):
    """Return 'hard_stop', 'soft_no', 'positive', or 'neutral'."""
    # Ignore quoted earlier messages so our own opt-out line is not read as a new request.
    text = re.split(r"(?im)^\s*(?:on .+ wrote:|from:|-----original message-----|>)", text, maxsplit=1)[0]
    low = text.lower()
    # Check hard stop first — most important
    for kw in HARD_STOP_KEYWORDS:
        # Short keywords need word boundaries to avoid false positives
        if len(kw) <= 6:
            if re.search(r'\b' + re.escape(kw) + r'\b', low):
                return "hard_stop"
        else:
            if kw in low:
                return "hard_stop"
    for kw in POSITIVE_KEYWORDS:
        if kw in low:
            return "positive"
    for kw in SOFT_NO_KEYWORDS:
        if kw in low:
            return "soft_no"
    return "neutral"


def _is_bounce(from_addr, subject, body):
    if any(p in from_addr.lower() for p in BOUNCE_FROM_PARTS):
        return True
    subj_low = subject.lower()
    if any(p in subj_low for p in BOUNCE_SUBJECT_PARTS):
        return True
    body_low = body.lower()
    if any(p in body_low for p in BOUNCE_BODY_PHRASES):
        return True
    return False


def _find_failed_recipient(body):
    """Find the email address that failed, in a bounce body."""
    candidates = re.findall(
        r'[\w.\-+]+@[\w.\-]+\.[A-Za-z]{2,}', body
    )
    for addr in candidates:
        low = addr.lower()
        if "moversync" in low:
            continue
        if any(p.replace("@", "") in low for p in BOUNCE_FROM_PARTS):
            continue
        return addr
    return ""


def check_inbox():
    if not CFG["imap"].get("enabled", False):
        return {"skipped": True, "reason": "imap disabled"}

    try:
        m = imaplib.IMAP4_SSL(CFG["imap"]["host"], CFG["imap"]["port"])
        m.login(CFG["imap"]["username"], CFG["imap"]["password"])
        m.select(CFG["imap"]["folder"])
        status, data = m.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []

        results = {
            "checked": len(ids),
            "replies": 0, "bounces": 0,
            "hard_stops": 0, "soft_no": 0, "positive": 0,
        }

        conn = connect()
        for num in ids:
            status, msg_data = m.fetch(num, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            from_hdr = _decode(msg.get("From", ""))
            subject = _decode(msg.get("Subject", ""))
            from_addr_match = re.search(r"[\w.\-+]+@[\w.\-]+", from_hdr)
            from_addr = from_addr_match.group(0) if from_addr_match else ""
            body = _get_body_text(msg)

            # ── Bounce? ────────────────────────────────────────
            if _is_bounce(from_addr, subject, body):
                failed = _find_failed_recipient(body)
                if failed:
                    row = conn.execute(
                        "SELECT id FROM prospects WHERE "
                        "direct_email = ? OR company_email = ?",
                        (failed, failed),
                    ).fetchone()
                    if row:
                        update_prospect(
                            row["id"],
                            status="bounced", bounced=1,
                            bounced_at=datetime.utcnow().isoformat(timespec="seconds"),
                            bounce_reason=subject[:200],
                            next_followup_due=None,
                        )
                        log_event(row["id"], "bounce", f"{failed} — {subject[:120]}")
                        results["bounces"] += 1
                continue

            # ── Human reply — is it from a prospect? ───────────
            row = conn.execute(
                "SELECT id FROM prospects WHERE "
                "direct_email = ? OR company_email = ?",
                (from_addr, from_addr),
            ).fetchone()
            if not row:
                continue

            classification = _classify_reply(subject + "\n" + body)

            if classification == "hard_stop":
                update_prospect(
                    row["id"],
                    status="unsubscribed",
                    replied=1,
                    replied_at=datetime.utcnow().isoformat(timespec="seconds"),
                    reply_subject=subject[:255],
                    reply_snippet=body[:400],
                    unsubscribed=1,
                    next_followup_due=None,
                )
                log_event(row["id"], "unsubscribe", f"keyword match — {from_addr}")
                results["hard_stops"] += 1

            elif classification == "positive":
                update_prospect(
                    row["id"],
                    status="replied",
                    replied=1,
                    replied_at=datetime.utcnow().isoformat(timespec="seconds"),
                    reply_subject=subject[:255],
                    reply_snippet=body[:400],
                    next_followup_due=None,
                    notes="POSITIVE — reply yourself today",
                )
                log_event(row["id"], "reply_positive", from_addr)
                results["positive"] += 1

            elif classification == "soft_no":
                update_prospect(
                    row["id"],
                    status="replied",
                    replied=1,
                    replied_at=datetime.utcnow().isoformat(timespec="seconds"),
                    reply_subject=subject[:255],
                    reply_snippet=body[:400],
                    next_followup_due=None,
                    notes="SOFT NO — do not chase",
                )
                log_event(row["id"], "reply_soft_no", from_addr)
                results["soft_no"] += 1

            else:  # neutral — a real reply but not clearly one way or the other
                update_prospect(
                    row["id"],
                    status="replied",
                    replied=1,
                    replied_at=datetime.utcnow().isoformat(timespec="seconds"),
                    reply_subject=subject[:255],
                    reply_snippet=body[:400],
                    next_followup_due=None,
                )
                log_event(row["id"], "reply_neutral", from_addr)
                results["replies"] += 1

        conn.close()
        m.close()
        m.logout()
        return results

    except Exception as e:
        log_event(0, "imap_error", str(e))
        return {"error": str(e)}

```

### File: `project_summary.py`

```py
import os
from pathlib import Path

# --- CONFIGURATION ---
# Add or remove folders, files, and extensions to ignore
IGNORE_DIRS = {
    'venv', '.venv', 'env', '.git', '__pycache__', '.pytest_cache', 
    'log', 'logs', 'cache', 'database', 'db', 'node_modules', 'data'
}

IGNORE_FILES = {
    '.DS_Store', 'thumbs.db', 'project_summary.py, project_summary.md'
}

IGNORE_EXTENSIONS = {
    # Databases & Spreadsheets
    '.db', '.sqlite', '.sqlite3', '.xlsx', '.xls', '.csv', '.ods',
    # Logs
    '.log',
    # Binary / Executables / Images
    '.pyc', '.exe', '.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.tar', '.gz'
}

OUTPUT_FILE = "project_summary.md"
# ---------------------

def should_ignore(path: Path) -> bool:
    """Check if a file or directory should be ignored."""
    # Check directory parts
    for part in path.parts:
        if part in IGNORE_DIRS:
            return True
    
    # Check files and extensions
    if path.is_file():
        if path.name in IGNORE_FILES:
            return True
        if path.suffix.lower() in IGNORE_EXTENSIONS:
            return True
            
    return False

def generate_tree(dir_path: Path, prefix: str = "") -> str:
    """Recursively build a visual text-based directory tree."""
    tree_str = ""
    
    # Get sorted list of items that are not ignored
    try:
        items = sorted([item for item in dir_path.iterdir() if not should_ignore(item)],
                       key=lambda x: (x.is_file(), x.name.lower()))
    except PermissionError:
        return ""

    pointers = [r"├── "] * (len(items) - 1) + [r"└── "] if items else []
    
    for pointer, item in zip(pointers, items):
        if item.is_dir():
            tree_str += f"{prefix}{pointer}{item.name}/\n"
            extension = "│   " if pointer == r"├── " else "    "
            tree_str += generate_tree(item, prefix + extension)
        else:
            tree_str += f"{prefix}{pointer}{item.name}\n"
            
    return tree_str

def get_all_files(dir_path: Path) -> list:
    """Recursively get all files that are not ignored."""
    file_list = []
    try:
        for item in sorted(dir_path.rglob('*')):
            if item.is_file() and not should_ignore(item):
                file_list.append(item)
    except PermissionError:
        pass
    return file_list

def main():
    project_root = Path.cwd()
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as markdown_file:
        # 1. Write Header
        markdown_file.write(f"# Project Structure and Files: {project_root.name}\n\n")
        
        # 2. Write Directory Tree
        markdown_file.write("## Directory Tree\n")
        markdown_file.write("```text\n")
        markdown_file.write(f"{project_root.name}/\n")
        markdown_file.write(generate_tree(project_root))
        markdown_file.write("```\n\n")
        
        # 3. Write File Contents
        markdown_file.write("## File Contents\n\n")
        
        valid_files = get_all_files(project_root)
        
        for file_path in valid_files:
            # Skip the output file itself if it's in the same directory
            if file_path.name == OUTPUT_FILE:
                continue
                
            # Get relative path for clean display headers
            relative_path = file_path.relative_to(project_root)
            markdown_file.write(f"### File: `{relative_path}`\n\n")
            
            # Determine code block syntax highlighting based on file extension
            lang = file_path.suffix.lstrip('.') if file_path.suffix else ""
            markdown_file.write(f"```{lang}\n")
            
            # Read and write content safely
            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    markdown_file.write(f.read())
            except Exception as e:
                markdown_file.write(f"[Error reading file: {str(e)}]\n")
                
            markdown_file.write("\n```\n\n")
            
    print(f"Successfully generated {OUTPUT_FILE}!")

if __name__ == "__main__":
    main()

```

### File: `requirements.txt`

```txt
flask>=3.0
openpyxl>=3.1
pyyaml>=6.0

```

### File: `run.bat`

```bat
@echo off
setlocal

REM ── Always run from the folder this .bat lives in ──
cd /d "%~dp0"

REM ── Sanity check: is Python installed? ──
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  ERROR: Python is not installed or not in your PATH.
  echo  Install Python 3.11+ from python.org and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

REM ── Sanity check: does the venv exist? ──
if not exist ".venv\Scripts\activate.bat" (
  echo.
  echo  ERROR: Virtual environment not found.
  echo  Run this once from this folder:
  echo      python -m venv .venv
  echo      .venv\Scripts\activate.bat
  echo      pip install flask openpyxl pyyaml
  echo.
  pause
  exit /b 1
)

REM ── Activate the venv ──
call ".venv\Scripts\activate.bat"

REM ── Make sure the DB is initialised (safe to run every time) ──
python -c "from db import init; init()" 2>nul

REM ── Open the browser after 2 seconds, in the background ──
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:5000"

REM ── Start the server (this line keeps the window open) ──
echo.
echo  MoverSync Outreach is starting...
echo  Open http://127.0.0.1:5000 in your browser.
echo  To stop the server, close this window or press Ctrl+C.
echo.
python app.py

REM ── If Flask exits for any reason, keep the window open so you can see the error ──
echo.
echo  Server stopped. Press any key to close.
pause >nul
```

### File: `sender.py`

```py
"""SMTP sending with IMAP folder routing, delays, CC, multipart text+HTML."""
import smtplib
import ssl
import time
import random
import yaml
import os
import imaplib
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from datetime import datetime, date, timedelta

from db import connect, update_prospect, log_event, sent_today_count
import templates_engine

CFG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
with open(CFG_PATH) as f:
    CFG = yaml.safe_load(f)
_send_lock = threading.Lock()


def _smtp_connect():
    host = CFG["smtp"]["host"]
    port = CFG["smtp"]["port"]
    user = CFG["smtp"]["username"]
    pwd = CFG["smtp"]["password"]

    if CFG["smtp"]["use_ssl"]:
        ctx = ssl.create_default_context()
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls(context=ssl.create_default_context())
    server.login(user, pwd)
    return server


def _build_message(prospect, subject, plain_body):
    msg = EmailMessage()
    msg["From"] = formataddr((CFG["smtp"]["from_name"], CFG["smtp"]["from_email"]))
    msg["To"] = prospect["direct_email"]
    msg["Reply-To"] = CFG["smtp"]["reply_to"]
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=CFG["smtp"]["from_email"].split("@")[-1])
    msg.set_content(plain_body)
    logo_cid = make_msgid(domain=CFG["smtp"]["from_email"].split("@")[-1])
    msg.add_alternative(
        templates_engine.render_html(plain_body, logo_cid=logo_cid[1:-1]),
        subtype="html",
    )
    logo_path = os.path.join(os.path.dirname(__file__), "static", "moversync-mark.png")
    with open(logo_path, "rb") as logo_file:
        msg.get_payload()[-1].add_related(
            logo_file.read(), maintype="image", subtype="png",
            cid=logo_cid, filename="moversync-mark.png", disposition="inline",
        )
    return msg


def save_to_imap_folder(msg, folder_key):
    """Append the sent message to a Zoho folder via IMAP.
    Never raises — logs and returns False on failure.
    """
    if not CFG.get("imap", {}).get("enabled"):
        return False

    folder_name = CFG.get("imap_folders", {}).get(folder_key)
    if not folder_name:
        log_event(0, "imap_save_skip", f"no folder for {folder_key}")
        return False

    try:
        m = imaplib.IMAP4_SSL(CFG["imap"]["host"], CFG["imap"]["port"])
        m.login(CFG["imap"]["username"], CFG["imap"]["password"])
        try:
            for name in ("MoverSync", folder_name):
                status, data = m.list('""', f'"{name}"')
                exists = status == "OK" and data and any(data)
                if not exists:
                    status, data = m.create(f'"{name}"')
                    if status != "OK":
                        raise RuntimeError(f"Could not create {name}: {data}")
            status, data = m.append(f'"{folder_name}"', "\\Seen",
                                    imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if status != "OK":
                raise RuntimeError(f"IMAP append failed: {data}")
            log_event(0, "imap_save_ok", folder_name)
            return True
        finally:
            m.logout()
    except Exception as e:
        log_event(0, "imap_save_error", f"{folder_name}: {e}")
        return False


def ensure_imap_folders():
    """Create campaign folders without sending an email."""
    if not CFG.get("imap", {}).get("enabled"):
        return {"ok": False, "error": "IMAP is disabled"}
    try:
        m = imaplib.IMAP4_SSL(CFG["imap"]["host"], CFG["imap"]["port"])
        m.login(CFG["imap"]["username"], CFG["imap"]["password"])
        try:
            for name in ["MoverSync", *CFG["imap_folders"].values()]:
                status, data = m.list('""', f'"{name}"')
                if not (status == "OK" and data and any(data)):
                    status, data = m.create(f'"{name}"')
                    if status != "OK":
                        raise RuntimeError(f"Could not create {name}: {data}")
            return {"ok": True, "folders": list(CFG["imap_folders"].values())}
        finally:
            m.logout()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _advance_status(prospect, template_name):
    """Return DB field updates based on what was sent."""
    fields = {}
    status = prospect.get("status", "pending")

    if template_name.startswith("tier_"):
        fields["status"] = "sent"
        fields["next_followup_due"] = (date.today() + timedelta(days=3)).isoformat()
    elif template_name == "followup_day3.txt":
        fields["status"] = "followup1"
        fields["next_followup_due"] = (date.today() + timedelta(days=4)).isoformat()
    elif template_name == "followup_day7.txt":
        fields["status"] = "followup2"
        fields["next_followup_due"] = (date.today() + timedelta(days=7)).isoformat()
    elif template_name == "breakup_day14.txt":
        fields["status"] = "followup3"
        fields["next_followup_due"] = None

    return fields


def send_one(prospect, template_type=None, subject_override=None, body_override=None):
    with _send_lock:
        return _send_one_locked(prospect, template_type, subject_override, body_override)


def _send_one_locked(prospect, template_type=None, subject_override=None, body_override=None):
    """Send a single email. template_type: None|'proposal'|'followup1'|'followup2'|'followup3'"""
    if prospect.get("status") in ("replied", "bounced", "unsubscribed", "skipped"):
        log_event(prospect["id"], "send_blocked", "Lead is suppressed")
        return False
    if not prospect.get("direct_email"):
        log_event(prospect["id"], "send_blocked", "Missing direct email")
        return False
    if sent_today_count() >= min(50, CFG["sending"]["daily_limit"]):
        log_event(prospect["id"], "send_blocked", "Daily sending limit reached")
        return False
    if (subject_override is None) != (body_override is None):
        raise ValueError("Subject and body overrides must be supplied together")
    if subject_override is not None and template_type != "proposal":
        raise ValueError("Custom copy is only supported for an initial email")
    template_name, body = templates_engine.template_for(prospect, override=template_type)
    if body_override is not None:
        body = body_override
    if not body:
        log_event(prospect["id"], "error", "empty template")
        return False

    subject = subject_override if subject_override is not None else templates_engine.subject_for(prospect, template_name)
    if not subject or any(c in subject for c in "\r\n"):
        raise ValueError("Invalid subject")
    msg = _build_message(prospect, subject, body)

    ok = False
    smtp_response = ""
    try:
        server = _smtp_connect()
        server.send_message(msg)
        server.quit()
        ok = True
        smtp_response = "250 OK"
    except Exception as e:
        smtp_response = str(e)

    if ok:
        # Save to the correct Zoho folder (non-blocking failure)
        folder_key = templates_engine.folder_key_for(template_name)
        save_to_imap_folder(msg, folder_key)

    if ok:
        now = datetime.utcnow().isoformat(timespec="seconds")
        fields = {
            "emails_sent": (prospect.get("emails_sent") or 0) + 1,
            "last_sent_at": now,
            "last_template_used": template_name,
        }
        if not prospect.get("first_sent_at"):
            fields["first_sent_at"] = now
        fields.update(_advance_status(prospect, template_name))
        update_prospect(prospect["id"], **fields)

    conn = connect()
    conn.execute("""
        INSERT INTO send_log
        (prospect_id, template, to_email, cc_email, subject, body, smtp_response, success)
        VALUES (?,?,?,?,?,?,?,?)
    """, (prospect["id"], template_name, prospect["direct_email"],
          "", subject, body,
          smtp_response, 1 if ok else 0))
    conn.commit()
    conn.close()

    log_event(prospect["id"], "sent" if ok else "send_failed", smtp_response)
    return ok


def send_batch(limit=None, progress_cb=None):
    """Auto-pick queue (used by the old 'Start sending' button)."""
    from db import fetch_due_prospects
    limit = limit or CFG["sending"]["daily_limit"]
    queue = fetch_due_prospects(limit=limit)

    sent = 0
    for i, p in enumerate(queue):
        if sent >= CFG["sending"]["daily_limit"]:
            break
        ok = send_one(p)
        if ok:
            sent += 1
        if progress_cb:
            progress_cb(i + 1, len(queue), p, ok)
        if i < len(queue) - 1:
            time.sleep(random.randint(
                CFG["sending"]["min_delay_seconds"],
                CFG["sending"]["max_delay_seconds"],
            ))
    return sent

```

### File: `static\app.js`

```js
// Light polling for send progress
async function pollProgress() {
  try {
    const r = await fetch('/send/progress');
    const j = await r.json();
    const el = document.getElementById('sendStatus');
    if (!el) return;
    if (j.active) {
      el.textContent = `Sending… ${j.done}/${j.total} — current: ${j.current}`;
    } else if (j.total > 0) {
      el.textContent = `Done. Sent ${j.done} of ${j.total}.`;
    }
  } catch (e) {
    /* silent */
  }
}
setInterval(pollProgress, 3000);
```

### File: `static\moversync-mark.svg`

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="MoverSync">
  <rect width="64" height="64" rx="16" fill="#07111f"/>
  <path d="M13 23h25v20H13zM38 29h7l7 8v6H38" fill="none" stroke="#ffffff" stroke-width="4" stroke-linejoin="round"/>
  <circle cx="22" cy="46" r="4" fill="#1d5cff"/>
  <circle cx="45" cy="46" r="4" fill="#1d5cff"/>
</svg>

```

### File: `static\style.css`

```css
/* MoverSync Outreach — 3 colours: black, white, dark blue */

:root {
  --black: #0a0a0a;
  --white: #ffffff;
  --blue: #1e3a8a;      /* dark blue */
  --blue-hover: #1e40af;
  --grey: #6b7280;
  --grey-lt: #e5e7eb;
  --bg: #fafafa;

  /* Status colours (used sparingly) */
  --ok: #16a34a;
  --warn: #d97706;
  --bad: #dc2626;
}

* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--black);
  font-size: 15px;
  line-height: 1.5;
}

a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }

.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 24px;
  background: var(--white);
  border-bottom: 1px solid var(--grey-lt);
}
.brand { font-size: 15px; letter-spacing: -0.01em; }
.brand .dot {
  display: inline-block; width: 8px; height: 8px;
  background: var(--blue); border-radius: 50%; margin-right: 8px;
}
.topbar nav a { margin-left: 20px; color: var(--black); font-weight: 500; }
.topbar nav a:hover { color: var(--blue); }

.wrap { max-width: 1100px; margin: 32px auto; padding: 0 24px; }

.kpis {
  display: grid; grid-template-columns: repeat(6, 1fr);
  gap: 12px; margin-bottom: 28px;
}
.kpi {
  background: var(--white); border: 1px solid var(--grey-lt);
  border-radius: 10px; padding: 16px 18px;
}
.kpi span { display: block; font-size: 12px; color: var(--grey); text-transform: uppercase; letter-spacing: 0.06em; }
.kpi b { display: block; font-size: 26px; font-weight: 700; margin-top: 4px; }
.kpi b.ok { color: var(--ok); }
.kpi b.bad { color: var(--bad); }
.kpi b.warn { color: var(--warn); }

.panel {
  background: var(--white); border: 1px solid var(--grey-lt);
  border-radius: 10px; padding: 20px 24px; margin-bottom: 20px;
}
.panel h2 { font-size: 16px; margin: 0 0 14px; letter-spacing: -0.01em; }
.panel h3 { font-size: 14px; margin: 18px 0 8px; color: var(--grey); text-transform: uppercase; letter-spacing: 0.05em; }

.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.row.between { justify-content: space-between; }

.btn {
  display: inline-block; padding: 8px 14px;
  border-radius: 8px; font-size: 14px; font-weight: 500;
  border: 1px solid var(--grey-lt); background: var(--white);
  color: var(--black); cursor: pointer;
  transition: all 0.15s ease;
}
.btn:hover { border-color: var(--blue); color: var(--blue); text-decoration: none; }
.btn.primary { background: var(--blue); color: var(--white); border-color: var(--blue); }
.btn.primary:hover { background: var(--blue-hover); color: var(--white); }
.btn.ghost { background: transparent; }

.input-sm {
  padding: 8px 10px; border-radius: 8px;
  border: 1px solid var(--grey-lt); font-size: 14px;
  width: 80px;
}

.status { margin-top: 12px; font-size: 14px; color: var(--blue); min-height: 20px; }

.table { width: 100%; border-collapse: collapse; font-size: 14px; }
.table th {
  text-align: left; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--grey); padding: 10px 8px;
  border-bottom: 1px solid var(--grey-lt);
}
.table td { padding: 10px 8px; border-bottom: 1px solid var(--grey-lt); }
.table tr:hover { background: #f6f7fb; }

.small { font-size: 12px; color: var(--grey); }
.muted { color: var(--grey); }

.tier { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.t-Ap { background: #dbeafe; color: #1e3a8a; }
.t-A  { background: #e0e7ff; color: #3730a3; }
.t-B  { background: #f3f4f6; color: #374151; }
.t-C  { background: #f9fafb; color: #6b7280; }

.status { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; text-transform: lowercase; }
.s-pending      { background: #f3f4f6; color: #374151; }
.s-sent         { background: #dbeafe; color: #1e3a8a; }
.s-followup1    { background: #e0e7ff; color: #3730a3; }
.s-followup2    { background: #ede9fe; color: #5b21b6; }
.s-replied      { background: #dcfce7; color: #166534; }
.s-bounced      { background: #fee2e2; color: #991b1b; }
.s-unsubscribed { background: #fef3c7; color: #92400e; }
.s-skipped      { background: #f3f4f6; color: #9ca3af; }

.events { list-style: none; padding: 0; margin: 0; }
.events li { padding: 6px 0; border-bottom: 1px solid var(--grey-lt); font-size: 13px; }
.events li:last-child { border-bottom: none; }

/* ── Banners ────────────────────────────────────────────── */
.banner {
  padding: 12px 16px;
  border-radius: 8px;
  margin-bottom: 16px;
  font-size: 14px;
}
.banner.ok  { background: #dcfce7; color: #166534; border: 1px solid #86efac; }
.banner.bad { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }

/* ── Form ───────────────────────────────────────────────── */
.form { max-width: 640px; }
.form-row { margin-bottom: 14px; }
.form-row label {
  display: block;
  font-size: 13px;
  color: var(--grey);
  margin-bottom: 6px;
  font-weight: 500;
}
.form-row input[type="text"],
.form-row input[type="email"],
.form-row input:not([type]),
.form-row select,
.form-row textarea {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--grey-lt);
  border-radius: 8px;
  font-size: 14px;
  font-family: inherit;
  background: var(--white);
}
.form-row input:focus,
.form-row select:focus,
.form-row textarea:focus {
  outline: none;
  border-color: var(--blue);
  box-shadow: 0 0 0 3px rgba(30, 58, 138, 0.12);
}
.form-row.check label {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--black);
  font-size: 14px;
}
.form-row.check input[type="checkbox"] {
  width: auto;
  margin: 0;
}

/* ── Hero panel ─────────────────────────────────────────── */
.hero-panel {
  background: var(--white);
  border: 1px solid var(--grey-lt);
  border-radius: 10px;
  padding: 24px 28px;
  margin-bottom: 20px;
}
.hero-panel h2 { font-size: 20px; margin: 0 0 6px; }
.hero-panel p { margin: 0 0 14px; }
.hero-panel b { color: var(--blue); }
.progress-track {
  height: 6px; background: var(--grey-lt);
  border-radius: 999px; overflow: hidden;
}
.progress-fill {
  height: 100%; background: var(--blue);
  border-radius: 999px;
  transition: width 0.3s ease;
}

/* ── Steps ──────────────────────────────────────────────── */
.steps { display: grid; gap: 16px; }
.step {
  display: flex; gap: 14px; align-items: flex-start;
  padding: 14px 0;
  border-bottom: 1px solid var(--grey-lt);
}
.step:last-child { border-bottom: none; }
.step-num {
  display: inline-flex; align-items: center; justify-content: center;
  width: 28px; height: 28px; border-radius: 50%;
  background: var(--blue); color: var(--white);
  font-size: 13px; font-weight: 700; flex-shrink: 0;
}
.step b { font-size: 14px; }
.step p { margin: 4px 0 8px; color: var(--grey); }

/* ── Table wrap + checkboxes ───────────────────────────── */
.table-wrap {
  background: var(--white);
  border: 1px solid var(--grey-lt);
  border-radius: 10px;
  overflow: hidden;
  margin-bottom: 90px; /* space for bulk bar */
}
.table .check { width: 34px; text-align: center; }
.table .check input { cursor: pointer; }

/* ── Bulk bar ───────────────────────────────────────────── */
.bulk-bar {
  position: fixed;
  bottom: 0; left: 0; right: 0;
  background: var(--white);
  border-top: 1px solid var(--grey-lt);
  padding: 12px 24px;
  display: flex; align-items: center; justify-content: space-between;
  transform: translateY(100%);
  transition: transform 0.2s ease;
  box-shadow: 0 -6px 24px rgba(0, 0, 0, 0.06);
  z-index: 40;
}
.bulk-bar.active { transform: translateY(0); }
.bulk-info { font-size: 14px; color: var(--black); }
.bulk-info b { color: var(--blue); font-size: 16px; }

/* Campaign overview */
.page-heading { display:flex; justify-content:space-between; align-items:center; gap:20px; margin-bottom:24px; }
.page-heading h1 { font-size:28px; letter-spacing:-.035em; margin:0 0 4px; }
.page-heading p { margin:0; }
.eyebrow { color:#60758a; font-size:11px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; margin:0 0 8px; }
.overview-grid { display:grid; grid-template-columns:1.4fr 1fr; gap:16px; margin-bottom:16px; }
.overview-grid .hero-panel, .overview-grid .panel { margin-bottom:0; }
.hero-number { font-size:42px; font-weight:700; line-height:1.1; letter-spacing:-.05em; color:#173b60; }
.hero-number span { color:#8c9aaa; font-size:24px; font-weight:500; }
.hero-panel .muted { margin:8px 0 18px; }
.attention-list { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
.attention-list a, .attention-list div { display:flex; flex-direction:column; padding:12px; background:#f6f8fb; border-radius:7px; color:var(--black); }
.attention-list a:hover { background:#edf2f8; text-decoration:none; }
.attention-list b { font-size:24px; line-height:1.1; color:#173b60; }
.attention-list span { font-size:12px; color:var(--grey); margin-top:6px; }
.inline-message { min-height:20px; color:#173b60; margin:12px 0 0; }
.auto-send { margin-top:18px; padding-top:14px; border-top:1px solid var(--grey-lt); }
.auto-send summary { cursor:pointer; font-weight:600; color:#173b60; }
.auto-send form { display:flex; align-items:center; gap:10px; }
.auto-send .small { margin:8px 0; }
.preview-controls { margin:12px 0; }
.preview-controls select { border:1px solid var(--grey-lt); border-radius:8px; padding:8px; background:white; }
.email-preview { max-width:640px; margin:12px 0 20px; padding:18px; background:#f6f8fb; border:1px solid var(--grey-lt); border-radius:8px; }
.email-preview pre { white-space:pre-wrap; font:14px/1.55 inherit; margin:12px 0 0; }
.email-preview iframe { display:block; width:100%; height:640px; margin-top:12px; border:0; background:#ffffff; }
@media (max-width:760px) {
  .topbar, .page-heading { flex-direction:column; align-items:flex-start; }
  .topbar nav a { margin:0 14px 0 0; }
  .wrap { margin:22px auto; padding:0 16px; }
  .overview-grid { grid-template-columns:1fr; }
  .kpis { grid-template-columns:repeat(2,1fr); }
  .attention-list { grid-template-columns:repeat(3,1fr); }
  .panel, .hero-panel { padding:18px; }
}

```

### File: `templates\breakup_day14.txt`

```txt
Hi {{first_name}},

I will close the loop here. If {{company}} revisits its enquiry and quoting process, the MoverSync live demo is free to try whenever useful.

You can reply to me directly if any questions come up.

```

### File: `templates\followup_day3.txt`

```txt
Hi {{first_name}},

Following up on my note about {{company}}'s enquiry and quoting process. The free MoverSync demo shows how a lead moves through quote, follow-up and booking.

If it looks relevant, just reply and I can answer any questions.

```

### File: `templates\followup_day7.txt`

```txt
Hi {{first_name}},

If keeping track of quote follow-ups is on {{company}}'s list, the free MoverSync demo is the quickest way to see what I mean.

I would welcome your thoughts if you have a few minutes to try it.

```

### File: `templates\tier_a.txt`

```txt
Hi {{first_name}},

{{hook}}

I built MoverSync for removals teams that want a clearer path from first enquiry to quote, follow-up and booking. I thought it might be relevant to {{company}}.

The live demo is free to try. You can see the workflow for yourself in a few minutes.

```

### File: `templates\tier_a_plus.txt`

```txt
Hi {{first_name}},

{{hook}}

When enquiries reach {{company}} through different channels, it helps to see the survey, quote and next follow-up in one place. That is the workflow MoverSync is built around.

You can try the live demo free and see whether it fits the way your team works.

```

### File: `templates\tier_b.txt`

```txt
Hi {{first_name}},

{{hook}}

How does {{company}} keep track of quotes that need a reply? MoverSync gives removals teams one place to see the enquiry, quote and next action.

You can try the live demo free and decide if it would be useful.

```

### File: `templates\tier_c.txt`

```txt
Hi {{first_name}},

Could you point me to the person at {{company}} who manages enquiries and quotes? MoverSync is a CRM built for UK removals teams.

The live demo is free to try if you would like to take a look.

```

### File: `templates_engine.py`

```py
"""Choose a template and render it with the prospect's data.
Also generates a minimal, safe HTML version for multipart sends.
"""
import os
import re
import html as html_lib

TPL_DIR = os.path.join(os.path.dirname(__file__), "templates")
DEMO_URL = "https://moversync.co.uk/demo.html"
SIGNATURE = (
    'Kind regards,\n'
    'Peter Wilson\n'
    'MoverSync | UK removals CRM\n'
    'peter@moversync.co.uk\n'
    'https://moversync.co.uk\n\n'
    'Reply "stop" if you prefer no further emails.'
)


def load(name):
    path = os.path.join(TPL_DIR, name)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _proposal_template(prospect):
    tier = (prospect.get("tier") or "B").upper().replace(" ", "")
    if tier == "A+":
        return "tier_a_plus.txt"
    elif tier == "A":
        return "tier_a.txt"
    elif tier == "B":
        return "tier_b.txt"
    else:
        return "tier_c.txt"


def template_for(prospect, override=None):
    """Return (template_name, rendered_body).
    override: None (auto), 'proposal', 'followup1', 'followup2', 'followup3'
    """
    if override == "proposal":
        name = _proposal_template(prospect)
    elif override == "followup1":
        name = "followup_day3.txt"
    elif override == "followup2":
        name = "followup_day7.txt"
    elif override == "followup3":
        name = "breakup_day14.txt"
    elif override is None:
        # auto by status
        status = prospect.get("status", "pending")
        if status == "pending":
            name = _proposal_template(prospect)
        elif status == "sent":
            name = "followup_day3.txt"
        elif status == "followup1":
            name = "followup_day7.txt"
        elif status == "followup2":
            name = "breakup_day14.txt"
        else:
            name = "tier_b.txt"
    else:
        name = "tier_b.txt"

    return name, compose(render(load(name), prospect))


def compose(message):
    return (
        message.strip() + "\n\n"
        + f"Try the live demo free: {DEMO_URL}\n\n"
        + SIGNATURE + "\n"
    )


def render(body, prospect):
    first_name = (prospect.get("decision_maker") or "").split(" ")[0]
    if not first_name or first_name.lower() in ("owner", "unknown", "managing", "n/a"):
        first_name = "there"

    replacements = {
        "{{first_name}}": first_name,
        "{{company}}": prospect.get("company_name", ""),
        "{{city}}": (prospect.get("city_region") or "").split("/")[0].strip(),
        "{{hook}}": prospect.get("personalization_hook", ""),
        "{{angle}}": prospect.get("sales_angle", ""),
    }
    for k, v in replacements.items():
        body = body.replace(k, str(v or "").strip())
    return re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"


def render_html(plain_text, logo_cid="moversync-logo"):
    main_text = plain_text.split("\n\nTry the live demo free:", 1)[0]
    paragraphs = [
        '<p style="margin:0 0 16px;">' + html_lib.escape(part).replace("\n", "<br>") + '</p>'
        for part in main_text.strip().split("\n\n") if part.strip()
    ]
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#ffffff;color:#07111f;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;background:#ffffff;"><tr><td align="center" style="padding:26px 14px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:560px;background:#ffffff;">'
        '<tr><td style="padding:22px 28px;background:#07111f;'
        'font:700 20px Arial,Helvetica,sans-serif;color:#ffffff;">'
        'Mover<span style="color:#1d5cff;">Sync</span>'
        '<div style="font:11px Arial,Helvetica,sans-serif;letter-spacing:1.2px;'
        'margin-top:5px;color:#ffffff;">BUILT FOR UK REMOVALS TEAMS</div></td></tr>'
        '<tr><td style="padding:28px 28px 6px;font:15px/1.65 Arial,Helvetica,sans-serif;'
        'color:#07111f;">' + ''.join(paragraphs) + '</td></tr>'
        '<tr><td style="padding:0 28px 26px;">'
        '<a href="https://moversync.co.uk/demo.html" '
        'style="display:inline-block;padding:12px 18px;background:#1d5cff;'
        'color:#ffffff;text-decoration:none;font:700 14px Arial,Helvetica,sans-serif;'
        'border-radius:6px;">Try the live demo free</a></td></tr>'
        '<tr><td style="padding:20px 28px;border-top:1px solid #07111f;'
        'font:14px/1.6 Arial,Helvetica,sans-serif;color:#07111f;">'
        '<div style="margin-bottom:12px;">Kind regards,<br><strong>Peter Wilson</strong></div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="padding-right:10px;"><img src="cid:{html_lib.escape(logo_cid)}" '
        'width="32" height="32" alt="MoverSync logo" style="display:block;border:0;"></td>'
        '<td style="font:700 16px Arial,Helvetica,sans-serif;color:#07111f;">'
        'Mover<span style="color:#1d5cff;">Sync</span></td></tr></table>'
        '<div style="margin-top:8px;">UK removals CRM<br>'
        '<a href="mailto:peter@moversync.co.uk" style="color:#1d5cff;">peter@moversync.co.uk</a><br>'
        '<a href="https://moversync.co.uk" style="color:#1d5cff;">moversync.co.uk</a></div>'
        '</td></tr>'
        '<tr><td style="padding:14px 28px;background:#07111f;'
        'font:12px/1.5 Arial,Helvetica,sans-serif;color:#ffffff;">'
        'Reply &quot;stop&quot; if you prefer no further emails.</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def subject_for(prospect, template_name):
    company = re.sub(r"\s+(?:ltd\.?|limited)\s*$", "", prospect.get("company_name", ""), flags=re.I).strip()
    company = re.sub(r"[\r\n]+", " ", company)[:48]
    if template_name.startswith("tier_"):
        if template_name == "tier_a_plus.txt":
            return f"{company}: from enquiry to quote"
        if template_name == "tier_a.txt":
            return f"Quoting and follow-up at {company}"
        return f"A quick question for {company}"
    if template_name == "followup_day3.txt":
        return f"Live demo for {company}"
    if template_name == "followup_day7.txt":
        return f"{company}: worth a look?"
    if template_name == "breakup_day14.txt":
        return f"Closing the loop with {company}"
    return f"A quick question for {company}"


# ── Map template file → IMAP folder key ──────────────────────────────
def folder_key_for(template_name):
    """Which config folder key this template should be saved to."""
    if template_name.startswith("tier_"):
        return "proposals"
    if template_name == "followup_day3.txt":
        return "followup1"
    if template_name == "followup_day7.txt":
        return "followup2"
    if template_name == "breakup_day14.txt":
        return "followup3"
    return "proposals"

```

### File: `templates_web\add_lead.html`

```html
{% extends "base.html" %}
{% block content %}

<h2>Add a lead</h2>
<p class="small">Use this to test new templates, or to add a company you met at a networking event.</p>

{% if error %}
  <div class="banner bad">{{ error }}</div>
{% endif %}

{% if added %}
  <div class="banner ok">
    Lead added to the database.
    {% if sent %}
      Test email sent successfully.
    {% elif sent is sameas false and prospect_id %}
      Email send failed — check the console or logs.
    {% endif %}
    <a href="/prospect/{{ prospect_id }}">View it here</a>.
  </div>
{% endif %}

<form action="/add-lead" method="post" class="form">
  <div class="form-row">
    <label for="company_name">Company name *</label>
    <input id="company_name" name="company_name" required>
  </div>

  <div class="form-row">
    <label for="decision_maker">Decision maker name</label>
    <input id="decision_maker" name="decision_maker" placeholder="e.g. Dave Roberts">
  </div>

  <div class="form-row">
    <label for="direct_email">Direct / primary email *</label>
    <input id="direct_email" name="direct_email" type="email" required>
  </div>

  <div class="form-row">
    <label for="company_email">Company email (CC)</label>
    <input id="company_email" name="company_email" type="email">
  </div>

  <div class="form-row">
    <label for="phone">Phone</label>
    <input id="phone" name="phone">
  </div>

  <div class="form-row">
    <label for="city_region">City / region</label>
    <input id="city_region" name="city_region" placeholder="e.g. Manchester / North West">
  </div>

  <div class="form-row">
    <label for="tier">Tier</label>
    <select id="tier" name="tier">
      <option value="A+">A+</option>
      <option value="A">A</option>
      <option value="B" selected>B</option>
      <option value="C">C</option>
    </select>
  </div>

  <div class="form-row">
    <label for="personalization_hook">Personalisation hook</label>
    <textarea id="personalization_hook" name="personalization_hook" rows="2"
      placeholder="e.g. Noticed you cover Greater Manchester and do piano moves"></textarea>
  </div>

  <div class="form-row">
    <label for="sales_angle">Sales angle (internal)</label>
    <input id="sales_angle" name="sales_angle"
      placeholder="e.g. Paid-lead consolidation">
  </div>

  <div class="form-row check">
    <label>
      <input type="checkbox" name="send_now" value="yes">
      Send a test email immediately after adding
    </label>
  </div>

  <div class="row">
    <button type="submit" class="btn primary">Add lead</button>
    <a class="btn ghost" href="/">Cancel</a>
  </div>
</form>

{% endblock %}
```

### File: `templates_web\base.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MoverSync Outreach</title>
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>

<header class="topbar">
  <div class="brand">
    <span class="dot"></span>
    <strong>MoverSync</strong> Outreach
  </div>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/prospects">Prospects</a>
    <a href="/add-lead">Add Lead</a>
  </nav>
</header>

<main class="wrap">
{% block content %}{% endblock %}
</main>

<script src="{{ url_for('static', filename='app.js') }}"></script>
</body>
</html>
```

### File: `templates_web\dashboard.html`

```html
{% extends "base.html" %}
{% block content %}
<div class="page-heading">
  <div>
    <p class="eyebrow">Campaign overview</p>
    <h1>Outbound dashboard</h1>
    <p class="muted">Review replies and delivery issues before the next send.</p>
  </div>
  <a class="btn primary" href="/prospects?status=pending">Review prospects</a>
</div>

<section class="overview-grid">
  <div class="hero-panel">
    <p class="eyebrow">Today's sending</p>
    <div class="hero-number">{{ sent_today }} <span>/ {{ daily_limit }}</span></div>
    <p class="muted">{{ remaining }} available today · {{ queued }} leads due</p>
    <div class="progress-track" role="progressbar" aria-valuenow="{{ sent_today }}" aria-valuemin="0" aria-valuemax="{{ daily_limit }}"><div class="progress-fill" style="width: {{ (sent_today * 100 / daily_limit) | round(0) }}%"></div></div>
  </div>
  <div class="panel attention-panel">
    <p class="eyebrow">Needs attention</p>
    <div class="attention-list">
      <a href="/prospects?status=replied"><b>{{ stats.replied or 0 }}</b><span>Replies to review</span></a>
      <a href="/prospects?status=bounced"><b>{{ stats.bounced or 0 }}</b><span>Bounced leads</span></a>
      <div><b>{{ failures }}</b><span>Send failures today</span></div>
    </div>
  </div>
</section>

{% if folder_issue %}
<div class="banner bad"><b>Mailbox filing needs attention.</b> {{ folder_issue }}</div>
{% endif %}

<section class="kpis">
  <div class="kpi"><span>Total leads</span><b>{{ stats.total }}</b></div>
  <div class="kpi"><span>Pending</span><b>{{ stats.pending or 0 }}</b></div>
  <div class="kpi"><span>Initial sent</span><b>{{ stats.sent or 0 }}</b></div>
  <div class="kpi"><span>Unsubscribed</span><b>{{ stats.unsubscribed or 0 }}</b></div>
</section>

<section class="panel">
  <div class="row between">
    <div>
      <h2>Send controls</h2>
      <p class="muted">Check the inbox, then review the due queue before sending.</p>
    </div>
    <div class="row">
      <button class="btn ghost" type="button" onclick="checkInbox()">Check inbox</button>
      <button class="btn ghost" type="button" onclick="setupFolders()">Check campaign folders</button>
      <a class="btn primary" href="/prospects?status=pending">Open pending leads</a>
    </div>
  </div>
  <details class="auto-send">
    <summary>Auto-send due queue</summary>
    <p class="small">Sends the next due leads, with the daily limit enforced.</p>
    <form id="autoSendForm">
      <label for="sendLimit">Maximum emails</label>
      <input id="sendLimit" type="number" name="limit" value="25" min="1" max="50" class="input-sm">
      <button class="btn ghost" type="submit">Start sending</button>
    </form>
  </details>
  <p id="sendStatus" class="inline-message" role="status"></p>
</section>

<section class="panel">
  <h2>Recent activity</h2>
  {% if recent %}
  <ul class="events">
    {% for e in recent %}
    <li><span class="small">{{ e.at }}</span><b>{{ e.event_type|replace('_',' ') }}</b><span class="muted">{{ e.company_name or 'Mailbox' }}{% if e.detail %} · {{ e.detail }}{% endif %}</span></li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="muted">No campaign activity yet.</p>
  {% endif %}
</section>

<script>
const message = document.getElementById('sendStatus');
async function checkInbox() {
  message.textContent = 'Checking inbox...';
  try {
    const r = await fetch('/inbox/check', {method: 'POST'});
    const j = await r.json();
    message.textContent = j.error ? 'Inbox check failed: ' + j.error :
      j.skipped ? 'Inbox check skipped: IMAP is disabled.' :
      `Checked ${j.checked} new messages. ${j.replies + j.positive + j.soft_no} replies, ${j.bounces} bounces, ${j.hard_stops} opt-outs.`;
    if (!j.error && !j.skipped) setTimeout(() => location.reload(), 1500);
  } catch (e) { message.textContent = 'Inbox check failed: ' + e.message; }
}
async function setupFolders() {
  message.textContent = 'Checking campaign folders...';
  try {
    const r = await fetch('/mailbox/folders', {method: 'POST'});
    const j = await r.json();
    message.textContent = j.ok ? 'Campaign folders are ready in Peter’s mailbox.' : 'Folder check failed: ' + j.error;
  } catch (e) { message.textContent = 'Folder check failed: ' + e.message; }
}
document.getElementById('autoSendForm').addEventListener('submit', async e => {
  e.preventDefault();
  if (!confirm('Send the due queue now?')) return;
  const fd = new FormData(e.target);
  const r = await fetch('/send/start', {method: 'POST', body: fd});
  const j = await r.json();
  message.textContent = j.status === 'started' ? 'Sending started.' : (j.error || j.status);
  if (j.status === 'started') monitorSend();
});
async function monitorSend() {
  try {
    const r = await fetch('/send/progress');
    const j = await r.json();
    message.textContent = j.active
      ? `Sending ${j.done} of ${j.total || 'due queue'}: ${j.current || 'starting'}`
      : `Send run complete. Processed ${j.done} of ${j.total}.`;
    if (j.active) setTimeout(monitorSend, 3000);
    else setTimeout(() => location.reload(), 1200);
  } catch (e) { message.textContent = 'Progress unavailable: ' + e.message; }
}
fetch('/send/progress').then(r => r.json()).then(j => { if (j.active) monitorSend(); });
</script>
{% endblock %}

```

### File: `templates_web\prospect_detail.html`

```html
{% extends "base.html" %}
{% block content %}

{% if saved %}
  <div class="banner ok">Saved.</div>
{% endif %}
{% if sent == "1" %}
  <div class="banner ok">Email sent successfully.</div>
{% elif sent == "0" %}
  <div class="banner bad">Email failed — check the log below for the SMTP error.</div>
{% endif %}

<h2>{{ prospect.company_name }}</h2>

<section class="panel">
  <h2>Send now</h2>
  <p class="small">Review the message before sending. Suppressed leads are blocked.</p>
  <div class="row preview-controls">
    <label for="previewType">Preview</label>
    <select id="previewType">
      <option value="proposal">Initial email</option>
      <option value="followup1">Follow-up 1</option>
      <option value="followup2">Follow-up 2</option>
      <option value="followup3">Final note</option>
    </select>
    <button type="button" class="btn ghost" onclick="loadPreview()">Show preview</button>
  </div>
  <div id="emailPreview" class="email-preview" hidden><b id="previewSubject"></b><iframe id="previewFrame" title="Email preview" sandbox></iframe></div>
  <div class="row">
    <form method="post" action="/prospect/{{ prospect.id }}/send-with" onsubmit="return confirm('Send Proposal?')">
      <input type="hidden" name="template_type" value="proposal">
      <button class="btn primary">Send Proposal</button>
    </form>
    <form method="post" action="/prospect/{{ prospect.id }}/send-with" onsubmit="return confirm('Send Followup 1?')">
      <input type="hidden" name="template_type" value="followup1">
      <button class="btn ghost">Send Followup 1</button>
    </form>
    <form method="post" action="/prospect/{{ prospect.id }}/send-with" onsubmit="return confirm('Send Followup 2?')">
      <input type="hidden" name="template_type" value="followup2">
      <button class="btn ghost">Send Followup 2</button>
    </form>
    <form method="post" action="/prospect/{{ prospect.id }}/send-with" onsubmit="return confirm('Send Followup 3 (breakup)?')">
      <input type="hidden" name="template_type" value="followup3">
      <button class="btn ghost">Send Followup 3</button>
    </form>
  </div>
</section>
<script>
async function loadPreview() {
  const type = document.getElementById('previewType').value;
  const r = await fetch('/prospect/{{ prospect.id }}/preview?template_type=' + encodeURIComponent(type));
  const j = await r.json();
  const box = document.getElementById('emailPreview');
  document.getElementById('previewSubject').textContent = j.subject || j.error;
  document.getElementById('previewFrame').srcdoc = j.html || '';
  box.hidden = false;
}
</script>

<form method="post" action="/prospect/{{ prospect.id }}/edit" class="form">
  <section class="panel">
    <h2>Edit details</h2>

    <div class="form-row">
      <label>Company name</label>
      <input name="company_name" value="{{ prospect.company_name or '' }}">
    </div>

    <div class="form-row">
      <label>Decision maker</label>
      <input name="decision_maker" value="{{ prospect.decision_maker or '' }}">
    </div>

    <div class="form-row">
      <label>Direct email</label>
      <input name="direct_email" value="{{ prospect.direct_email or '' }}">
    </div>

    <div class="form-row">
      <label>Company email (CC)</label>
      <input name="company_email" value="{{ prospect.company_email or '' }}">
    </div>

    <div class="form-row">
      <label>Phone</label>
      <input name="phone" value="{{ prospect.phone or '' }}">
    </div>

    <div class="form-row">
      <label>City / region</label>
      <input name="city_region" value="{{ prospect.city_region or '' }}">
    </div>

    <div class="form-row">
      <label>Tier</label>
      <select name="tier">
        {% for t in ['A+', 'A', 'B', 'C'] %}
        <option value="{{ t }}" {% if prospect.tier == t %}selected{% endif %}>{{ t }}</option>
        {% endfor %}
      </select>
    </div>

    <div class="form-row">
      <label>Status</label>
      <select name="status">
        {% for s in ['pending', 'sent', 'followup1', 'followup2', 'followup3', 'replied', 'bounced', 'unsubscribed', 'skipped'] %}
        <option value="{{ s }}" {% if prospect.status == s %}selected{% endif %}>{{ s }}</option>
        {% endfor %}
      </select>
    </div>

    <div class="form-row">
      <label>Personalisation hook</label>
      <textarea name="personalization_hook" rows="3">{{ prospect.personalization_hook or '' }}</textarea>
    </div>

    <div class="form-row">
      <label>Sales angle (internal)</label>
      <input name="sales_angle" value="{{ prospect.sales_angle or '' }}">
    </div>

    <div class="form-row">
      <label>Internal notes</label>
      <textarea name="notes" rows="3">{{ prospect.notes or '' }}</textarea>
    </div>

    <div class="row">
      <button class="btn primary" type="submit">Save changes</button>
      <a class="btn ghost" href="/prospects">Back to list</a>
    </div>
  </section>
</form>

<section class="panel">
  <h2>Send history</h2>
  <table class="table">
    <thead><tr><th>When</th><th>Template</th><th>Subject</th><th>Result</th></tr></thead>
    <tbody>
    {% for l in logs %}
      <tr>
        <td class="small">{{ l.sent_at }}</td>
        <td>{{ l.template }}</td>
        <td class="small">{{ l.subject }}</td>
        <td>{{ 'OK' if l.success else 'FAIL' }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</section>

<section class="panel">
  <h2>Events</h2>
  <ul class="events">
    {% for e in events %}
      <li><span class="small">{{ e.at }}</span> <b>{{ e.event_type }}</b> <span class="muted">{{ e.detail }}</span></li>
    {% endfor %}
  </ul>
</section>

{% endblock %}

```

### File: `templates_web\prospects.html`

```html
{% extends "base.html" %}
{% block content %}

<div class="row between">
  <h2>Prospects ({{ prospects|length }})</h2>
  <div class="row">
    <a class="btn ghost" href="/prospects">Clear filters</a>
    <a class="btn primary" href="/add-lead">+ Add lead</a>
  </div>
</div>

<div id="bulkResult" class="status" style="margin-bottom: 12px;"></div>

<form id="bulkForm">
  <div class="table-wrap">
    <table class="table">
      <thead>
        <tr>
          <th class="check"><input type="checkbox" id="selectAll" title="Select all"></th>
          <th>#</th><th>Tier</th><th>Company</th><th>Contact</th>
          <th>Email</th><th>Status</th><th>Sent</th><th>Last sent</th>
        </tr>
      </thead>
      <tbody>
      {% for p in prospects %}
        <tr>
          <td class="check">
            <input type="checkbox" class="lead-check" value="{{ p.id }}">
          </td>
          <td>{{ p.priority_rank }}</td>
          <td><span class="tier t-{{ p.tier|replace('+','p')|replace(' ','') }}">{{ p.tier }}</span></td>
          <td><a href="/prospect/{{ p.id }}">{{ p.company_name }}</a></td>
          <td>{{ p.decision_maker or '—' }}</td>
          <td class="small">{{ p.direct_email or '—' }}</td>
          <td><span class="status s-{{ p.status }}">{{ p.status }}</span></td>
          <td>{{ p.emails_sent or 0 }}</td>
          <td class="small">{{ p.last_sent_at or '' }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="bulk-bar" id="bulkBar">
    <div class="bulk-info"><b id="selectedCount">0</b> selected</div>
    <div class="row">
      <button type="button" class="btn primary" onclick="submitBulk('proposal')">Send Proposal</button>
      <button type="button" class="btn ghost" onclick="submitBulk('followup1')">Send Followup 1</button>
      <button type="button" class="btn ghost" onclick="submitBulk('followup2')">Send Followup 2</button>
      <button type="button" class="btn ghost" onclick="submitBulk('followup3')">Send Followup 3</button>
      <button type="button" class="btn ghost" onclick="clearSelection()">Clear</button>
    </div>
  </div>
</form>

<script>
const checks = document.querySelectorAll('.lead-check');
const selectAll = document.getElementById('selectAll');
const bar = document.getElementById('bulkBar');
const countEl = document.getElementById('selectedCount');

function updateBar() {
  const n = document.querySelectorAll('.lead-check:checked').length;
  countEl.textContent = n;
  bar.classList.toggle('active', n > 0);
  if (selectAll) {
    selectAll.checked = (n === checks.length && checks.length > 0);
    selectAll.indeterminate = (n > 0 && n < checks.length);
  }
}

selectAll?.addEventListener('change', () => {
  checks.forEach(c => c.checked = selectAll.checked);
  updateBar();
});

checks.forEach(c => c.addEventListener('change', updateBar));

function clearSelection() {
  checks.forEach(c => c.checked = false);
  if (selectAll) { selectAll.checked = false; selectAll.indeterminate = false; }
  updateBar();
}

async function submitBulk(template) {
  const ids = Array.from(document.querySelectorAll('.lead-check:checked'))
    .map(c => c.value);

  const result = document.getElementById('bulkResult');

  if (!ids.length) { result.textContent = 'Select at least one lead first.'; return; }
  if (ids.length > 50) { result.textContent = 'Maximum 50 leads per send.'; return; }

  const label = {
    proposal: 'Proposal',
    followup1: 'Followup 1',
    followup2: 'Followup 2',
    followup3: 'Followup 3',
  }[template];

  if (!confirm(`Send ${label} to ${ids.length} lead(s)?`)) return;

  result.textContent = 'Starting…';

  const fd = new FormData();
  fd.append('ids', ids.join(','));
  fd.append('template', template);

  const r = await fetch('/send/bulk', { method: 'POST', body: fd });
  const j = await r.json();

  if (j.status === 'started') {
    result.textContent = `Sending ${label} to ${j.total} lead(s). This can take up to ${j.total} minutes.`;
    pollProgress(ids.length);
  } else if (j.status === 'limit') {
    result.textContent = `Daily limit reached. You've sent ${j.sent} of ${j.limit} today.`;
  } else if (j.status === 'already_running') {
    result.textContent = 'A send is already running. Wait for it to finish.';
  } else {
    result.textContent = 'Error: ' + (j.error || 'unknown');
  }
}

function pollProgress(expected) {
  const tick = setInterval(async () => {
    const r = await fetch('/send/progress');
    const j = await r.json();
    const result = document.getElementById('bulkResult');
    if (j.active) {
      result.textContent = `Sending… ${j.done}/${j.total} — currently on: ${j.current}`;
    } else {
      clearInterval(tick);
      result.textContent = `Done. Sent ${j.done} of ${j.total}. Refreshing…`;
      setTimeout(() => location.reload(), 1500);
    }
  }, 3000);
}

updateBar();
</script>

{% endblock %}
```


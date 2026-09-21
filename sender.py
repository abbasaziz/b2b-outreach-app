"""SMTP sending with IMAP folder routing, delays, CC, multipart text+HTML."""
import smtplib
import ssl
import time
import random
import yaml
import os
import imaplib
from email.message import EmailMessage
from datetime import datetime, date, timedelta

from db import connect, update_prospect, log_event
import templates_engine

CFG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
with open(CFG_PATH) as f:
    CFG = yaml.safe_load(f)


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
    """Build a multipart/alternative message.

    Order matters: plain text FIRST, HTML SECOND.
    Well-behaved mail clients will show the HTML version.
    Text-only clients will show the plain text version.
    Spam filters inspect BOTH.
    """
    msg = EmailMessage()
    msg["From"] = f'{CFG["smtp"]["from_name"]} <{CFG["smtp"]["from_email"]}>'
    msg["To"] = prospect["direct_email"]
    if prospect.get("company_email"):
        msg["Cc"] = prospect["company_email"]
    msg["Reply-To"] = CFG["smtp"]["reply_to"]
    msg["Subject"] = subject

    # 1. Plain text part
    msg.set_content(plain_body, subtype="plain", charset="utf-8")

    # 2. HTML alternative (modern, minimal, with button for /demo)
    html_body = templates_engine.render_html(plain_body)
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    return msg


def save_to_imap_folder(msg, folder_key):
    """Append the sent message to a Zoho folder via IMAP.

    Tries the configured name, then a small set of common variants.
    Never raises. Logs to console on failure.
    """
    if not CFG.get("imap", {}).get("enabled"):
        print("[imap] IMAP disabled in config, skipping folder save")
        return False

    configured = CFG.get("imap_folders", {}).get(folder_key)
    if not configured:
        print(f"[imap] No folder configured for key: {folder_key}")
        log_event(0, "imap_save_skip", f"no folder for {folder_key}")
        return False

    base = configured.strip()
    last = base.split("/")[-1] if "/" in base else base

    candidates = []
    for c in [
        base,
        f"Sent/{last}",
        last,
        f"INBOX.{last}",
    ]:
        if c and c not in candidates:
            candidates.append(c)

    raw = msg.as_bytes()
    internal_date = imaplib.Time2Internaldate(time.time())

    try:
        m = imaplib.IMAP4_SSL(CFG["imap"]["host"], CFG["imap"]["port"])
        m.login(CFG["imap"]["username"], CFG["imap"]["password"])

        for candidate in candidates:
            box = f'"{candidate}"'
            try:
                result = m.append(box, None, internal_date, raw)
                if result and result[0] == "OK":
                    print(f"[imap] Saved to '{candidate}'")
                    log_event(0, "imap_save_ok", candidate)
                    CFG.setdefault("imap_folders", {})[folder_key] = candidate
                    m.logout()
                    return True
                else:
                    print(f"[imap] APPEND '{candidate}' returned {result}")
            except imaplib.IMAP4.error as inner:
                print(f"[imap] APPEND '{candidate}' failed: {inner}")

        m.logout()
        print(f"[imap] All candidates failed for '{folder_key}': {candidates}")
        log_event(0, "imap_save_error", f"{folder_key}: tried {candidates}")
        return False

    except Exception as e:
        print(f"[imap] Connection error: {e}")
        log_event(0, "imap_save_error", f"{folder_key}: {e}")
        return False


def _advance_status(prospect, template_name):
    fields = {}
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


def send_one(prospect, template_type=None):
    template_name, body = templates_engine.template_for(prospect, override=template_type)
    if not body:
        log_event(prospect["id"], "error", "empty template")
        return False

    subject = templates_engine.subject_for(prospect, template_name)
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
        folder_key = templates_engine.folder_key_for(template_name)
        save_to_imap_folder(msg, folder_key)

    now = datetime.utcnow().isoformat(timespec="seconds")
    new_count = (prospect.get("emails_sent") or 0) + 1

    fields = {
        "emails_sent": new_count,
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
          prospect.get("company_email", ""), subject, body,
          smtp_response, 1 if ok else 0))
    conn.commit()
    conn.close()

    log_event(prospect["id"], "sent" if ok else "send_failed", smtp_response)
    return ok


def send_batch(limit=None, progress_cb=None):
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
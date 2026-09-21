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

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

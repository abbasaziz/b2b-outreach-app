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
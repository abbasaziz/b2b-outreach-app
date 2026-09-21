"""Template selection, plain-text rendering, and modern HTML rendering.

Design rules:
- Plain text version is the source of truth.
- HTML version is generated from the plain text.
- The demo URL line becomes a button in HTML, stays as a plain URL in text.
- No external CSS, no images, no tracking pixels.
- Works in Gmail, Outlook, Apple Mail, iOS Mail.
"""

import os
import re
import html as html_lib

TPL_DIR = os.path.join(os.path.dirname(__file__), "templates")

DEMO_URL = "https://moversync.co.uk/demo"

# Colours — keep in sync with your landing page palette
INK = "#0a0a0a"
MUTED = "#6b7280"
BORDER = "#e5e7eb"
BG_CARD = "#ffffff"
BG_PAGE = "#f5f5f7"
BLUE = "#1e3a8a"
BLUE_TEXT = "#ffffff"


# ─────────────────────────────────────────────────────────────
# Template loading and selection
# ─────────────────────────────────────────────────────────────

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
    if tier == "A":
        return "tier_a.txt"
    if tier == "B":
        return "tier_b.txt"
    return "tier_c.txt"


def template_for(prospect, override=None):
    """Return (template_name, rendered_plain_text).

    override: None (auto) | 'proposal' | 'followup1' | 'followup2' | 'followup3'
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

    return name, render(load(name), prospect)


def render(body, prospect):
    """Replace simple {{placeholders}} in a template body."""
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
        body = body.replace(k, v)
    return body


def subject_for(prospect, template_name):
    company = prospect.get("company_name", "")
    if template_name.startswith("tier_"):
        return f"quick one for {company}"
    if template_name in ("followup_day3.txt", "followup_day7.txt"):
        return f"re: quick one for {company}"
    if template_name == "breakup_day14.txt":
        return "closing the file"
    return f"quick one for {company}"


def folder_key_for(template_name):
    if template_name.startswith("tier_"):
        return "proposals"
    if template_name == "followup_day3.txt":
        return "followup1"
    if template_name == "followup_day7.txt":
        return "followup2"
    if template_name == "breakup_day14.txt":
        return "followup3"
    return "proposals"


# ─────────────────────────────────────────────────────────────
# HTML rendering
# ─────────────────────────────────────────────────────────────

_URL_RE = re.compile(r'(https?://[^\s<>"\']+)')


def _inline_links(escaped_text):
    """Escape-html text with URLs replaced by <a> tags."""
    return _URL_RE.sub(
        r'<a href="\1" style="color: %s; text-decoration: underline;">\1</a>' % BLUE,
        escaped_text,
    )


def _demo_button_html():
    """The primary call to action button."""
    return (
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        f'style="margin: 24px 0 8px 0;">'
        f'<tr><td align="left" style="border-radius: 8px; background: {BLUE};">'
        f'<a href="{DEMO_URL}" target="_blank" style="'
        f'display: inline-block; '
        f'padding: 12px 24px; '
        f'font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Arial, sans-serif; '
        f'font-size: 15px; font-weight: 600; '
        f'color: {BLUE_TEXT}; '
        f'text-decoration: none; '
        f'border-radius: 8px; '
        f'letter-spacing: 0.01em; '
        f'">'
        f'See the 60 second demo'
        f'</a></td></tr>'
        f'</table>'
    )


def render_html(plain_text):
    """Turn the plain-text template body into a modern, minimal HTML email.

    The demo URL line is replaced with a real button. Everything else is
    rendered as paragraphs preserving line breaks.
    """
    # Split body into "blocks" by blank lines. Each block becomes a paragraph.
    # Within a block, single newlines become <br>.
    lines = plain_text.split("\n")

    blocks = []
    current = []
    for line in lines:
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)

    body_parts = []
    button_used = False

    for block in blocks:
        block_text = "\n".join(block)

        # Is this block just the demo URL?
        if block_text.strip() == DEMO_URL and not button_used:
            body_parts.append(_demo_button_html())
            button_used = True
            continue

        # Otherwise, escape and render as a paragraph.
        escaped = html_lib.escape(block_text)
        escaped = _inline_links(escaped)
        escaped = escaped.replace("\n", "<br>")

        # Small or muted paragraphs (signature lines, legal)
        # if the block is short and mentions common signature markers, use muted style.
        is_signature = (
            block_text.startswith("Peter")
            or "MoverSync" in block_text and len(block_text) < 120
        )
        style = (
            f'font-size: 14px; color: {MUTED}; line-height: 1.55;'
            if is_signature else
            f'font-size: 15px; color: {INK}; line-height: 1.6;'
        )
        body_parts.append(
            f'<p style="margin: 0 0 16px 0; {style} '
            f'font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Arial, sans-serif;">'
            f'{escaped}</p>'
        )

    # If the template did not use the demo URL, still show the button
    if not button_used:
        body_parts.append(_demo_button_html())

    body_html = "\n".join(body_parts)

    shell = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MoverSync</title>
</head>
<body style="margin: 0; padding: 0; background: {BG_PAGE};
             font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;">

  <!-- outer wrapper -->
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
         style="background: {BG_PAGE}; padding: 32px 16px;">
    <tr>
      <td align="center">

        <!-- main card -->
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
               style="max-width: 560px; background: {BG_CARD};
                      border: 1px solid {BORDER}; border-radius: 12px;
                      overflow: hidden;">

          <!-- header bar -->
          <tr>
            <td style="padding: 20px 28px 12px 28px;
                       border-bottom: 1px solid {BORDER};">
              <div style="font-size: 15px; font-weight: 700; color: {INK};
                          letter-spacing: -0.01em;">
                MoverSync
              </div>
              <div style="font-size: 12px; color: {MUTED}; margin-top: 2px;">
                The CRM built for UK removals firms
              </div>
            </td>
          </tr>

          <!-- body -->
          <tr>
            <td style="padding: 24px 28px 8px 28px;">
              {body_html}
            </td>
          </tr>

          <!-- footer -->
          <tr>
            <td style="padding: 16px 28px 20px 28px;
                       border-top: 1px solid {BORDER};
                       background: #fafafa;">
              <div style="font-size: 12px; color: {MUTED}; line-height: 1.5;">
                Peter Wilson,<br>
                MoverSync<br>
                <a href="https://moversync.co.uk" style="color: {MUTED}; text-decoration: underline;">moversync.co.uk</a>
              </div>
              <div style="font-size: 11px; color: #9ca3af; margin-top: 10px; line-height: 1.5;">
                You received this because we work with UK removals firms.
                If it is not relevant, reply with the word stop and we will remove you from our list.
              </div>
            </td>
          </tr>

        </table>
        <!-- /main card -->

      </td>
    </tr>
  </table>

</body>
</html>"""

    return shell
"""Pure presentation of approved email text; never adds content or a signature."""

from html import escape
import re


def _text(value: str) -> str:
    # Isolate ASCII runs (including addresses and URLs) within Persian prose.
    # This is presentation only: no links or meeting fields are inferred.
    parts = re.split(r"([\x21-\x7e]+(?: [\x21-\x7e]+)*)", value)
    return "".join(
        f'<span dir="ltr" style="direction:ltr;unicode-bidi:isolate;">{escape(part)}</span>'
        if i % 2 else escape(part)
        for i, part in enumerate(parts)
    )


def render_email(subject: str, body: str) -> str:
    """Render untrusted subject/body without changing their wording or whitespace.

    No remote assets are required. Vazir is used if installed by the recipient;
    otherwise the client's Tahoma/Arial/sans-serif fallback is used.
    """
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    content = '<br>'.join(_text(line) for line in lines)
    font = "Vazir, Tahoma, Arial, sans-serif"
    return f'''<!doctype html>
<html lang="fa" dir="rtl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(subject)}</title></head>
<body dir="rtl" style="margin:0;padding:0;background-color:#f3f5f7;font-family:{font};">
<table role="presentation" dir="rtl" width="100%" cellpadding="0" cellspacing="0" style="width:100%;background-color:#f3f5f7;">
<tr><td align="center" style="padding:40px 16px;">
<!--[if mso]><table role="presentation" width="600" align="center"><tr><td><![endif]-->
<table role="presentation" dir="rtl" width="100%" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;table-layout:fixed;background-color:#ffffff;border:1px solid #e3e8ed;border-top:3px solid #26776c;border-radius:16px;">
<tr><td dir="rtl" align="right" style="padding:24px 24px 16px;font-family:{font};text-align:right;overflow-wrap:anywhere;word-wrap:break-word;">
<h1 style="margin:0;color:#192b38;font-size:22px;font-weight:700;line-height:1.7;">{_text(subject)}</h1>
</td></tr>
<tr><td dir="rtl" align="right" style="padding:0 24px 28px;font-family:{font};font-size:16px;line-height:2;color:#263343;text-align:right;white-space:pre-wrap;overflow-wrap:anywhere;word-wrap:break-word;">{content}</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->
</td></tr></table>
</body></html>'''

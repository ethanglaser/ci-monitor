"""Email output for the nightly CI monitor.

Generic SMTP (STARTTLS on 587 by default). Credentials come from env:
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
"""

import html
import os
import smtplib
import ssl
from email.message import EmailMessage


def send_email(subject, html_body, text_body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ["EMAIL_FROM"]
    recipient = os.environ["EMAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(msg)


def _run_link(run):
    url = run.get("html_url", "")
    rid = run.get("id", "?")
    sha = (run.get("source_sha") or "")[:10]
    date = (run.get("started_at") or "")[:16].replace("T", " ")
    return url, rid, sha, date


def render_text(report):
    lines = []
    c = report["counts"]
    curr = report["curr_run"]
    prev = report["prev_run"]

    _, curr_id, curr_sha, curr_date = _run_link(curr)
    _, prev_id, prev_sha, prev_date = _run_link(prev)

    lines.append(report["display_name"])
    lines.append("=" * len(report["display_name"]))
    lines.append(f"Current run : #{curr_id} ({curr['result']}) @ {curr_sha} ({curr_date})")
    lines.append(f"                {curr.get('html_url', '')}")
    lines.append(f"Previous run: #{prev_id} ({prev['result']}) @ {prev_sha} ({prev_date})")
    lines.append("")
    lines.append(
        f"Summary: {c['still_passing']} passing, {c['still_failing']} still failing, "
        f"{c['new_failures']} new failures, {c['fixed']} fixed"
    )
    lines.append("")

    if report["new_failures"]:
        lines.append("NEW FAILURES (regressions)")
        lines.append("-" * 26)
        for nf in report["new_failures"]:
            job = nf["job"]
            lines.append(f"* {job['name']}  (failed step: {job.get('failed_step') or 'Unknown'})")
            lines.append(f"  Verdict: {nf['verdict']}")
            snippet_head = "\n    ".join(nf["snippet"].splitlines()[-15:])
            lines.append(f"  Error tail:\n    {snippet_head}")
            if nf["repo_commits"]:
                lines.append(f"  Commits in {report['scope_name']} window:")
                for c_ in nf["repo_commits"][:10]:
                    lines.append(f"    {c_['sha']} ({c_['author']}): {c_['message']}")
            if nf["upstream_commits"]:
                lines.append(f"  Upstream commits in window:")
                for c_ in nf["upstream_commits"][:10]:
                    lines.append(f"    {c_['sha']} ({c_['author']}): {c_['message']}")
            lines.append("")

    if report["fixed"]:
        lines.append("FIXED")
        lines.append("-" * 5)
        for j in report["fixed"]:
            lines.append(f"* {j['name']}")
        lines.append("")

    if report["still_failing"]:
        lines.append("STILL FAILING (chronic)")
        lines.append("-" * 23)
        for sf in report["still_failing"]:
            job = sf["job"]
            lines.append(f"* {job['name']}  (failed step: {job.get('failed_step') or 'Unknown'})")
            lines.append(f"  Verdict: {sf['verdict']}")
            snippet_head = "\n    ".join(sf["snippet"].splitlines()[-10:])
            lines.append(f"  Error tail:\n    {snippet_head}")
            lines.append("")

    return "\n".join(lines)


def render_html(report):
    c = report["counts"]
    curr = report["curr_run"]
    prev = report["prev_run"]
    curr_url, curr_id, curr_sha, curr_date = _run_link(curr)
    prev_url, prev_id, prev_sha, prev_date = _run_link(prev)

    def esc(s):
        return html.escape(str(s or ""))

    parts = []
    parts.append(f"<h2>{esc(report['display_name'])}</h2>")
    parts.append("<p>")
    parts.append(
        f"Current run: <a href='{esc(curr_url)}'>#{esc(curr_id)}</a> "
        f"<b>{esc(curr['result'])}</b> @ <code>{esc(curr_sha)}</code> ({esc(curr_date)})<br>"
    )
    parts.append(
        f"Previous run: <a href='{esc(prev_url)}'>#{esc(prev_id)}</a> "
        f"<b>{esc(prev['result'])}</b> @ <code>{esc(prev_sha)}</code> ({esc(prev_date)})"
    )
    parts.append("</p>")

    parts.append("<table border='1' cellpadding='6' style='border-collapse:collapse'>")
    parts.append("<tr>"
                 "<th>Passing</th><th>Still failing</th>"
                 "<th>New failures</th><th>Fixed</th>"
                 "</tr>")
    parts.append(
        f"<tr>"
        f"<td>{c['still_passing']}</td>"
        f"<td>{c['still_failing']}</td>"
        f"<td style='background:#ffd0d0'><b>{c['new_failures']}</b></td>"
        f"<td style='background:#d0ffd0'>{c['fixed']}</td>"
        f"</tr>"
    )
    parts.append("</table>")

    if report["new_failures"]:
        parts.append("<h3>New failures (regressions)</h3>")
        for nf in report["new_failures"]:
            job = nf["job"]
            parts.append(
                f"<details open><summary><b>{esc(job['name'])}</b> "
                f"&mdash; failed step: <code>{esc(job.get('failed_step') or 'Unknown')}</code>"
                f"</summary>"
            )
            parts.append(f"<p><b>Verdict:</b> {esc(nf['verdict'])}</p>")
            parts.append(
                f"<pre style='background:#f5f5f5;padding:8px;overflow:auto;"
                f"max-height:400px;font-size:12px'>{esc(nf['snippet'][-4000:])}</pre>"
            )
            if nf["repo_commits"]:
                parts.append(f"<p><b>Commits in <code>{esc(report['scope_name'])}</code> window:</b></p><ul>")
                for c_ in nf["repo_commits"][:15]:
                    parts.append(
                        f"<li><a href='{esc(c_['url'])}'><code>{esc(c_['sha'])}</code></a> "
                        f"({esc(c_['author'])}): {esc(c_['message'])}</li>"
                    )
                parts.append("</ul>")
            if nf["upstream_commits"]:
                parts.append("<p><b>Upstream commits in window:</b></p><ul>")
                for c_ in nf["upstream_commits"][:15]:
                    parts.append(
                        f"<li><a href='{esc(c_['url'])}'><code>{esc(c_['sha'])}</code></a> "
                        f"({esc(c_['author'])}): {esc(c_['message'])}</li>"
                    )
                parts.append("</ul>")
            parts.append("</details>")

    if report["fixed"]:
        parts.append("<h3>Fixed since last run</h3><ul>")
        for j in report["fixed"]:
            parts.append(f"<li>{esc(j['name'])}</li>")
        parts.append("</ul>")

    if report["still_failing"]:
        parts.append("<h3>Still failing (chronic)</h3>")
        for sf in report["still_failing"]:
            job = sf["job"]
            parts.append(
                f"<details><summary><b>{esc(job['name'])}</b> "
                f"&mdash; failed step: <code>{esc(job.get('failed_step') or 'Unknown')}</code>"
                f"</summary>"
            )
            parts.append(f"<p><b>Verdict:</b> {esc(sf['verdict'])}</p>")
            parts.append(
                f"<pre style='background:#f5f5f5;padding:8px;overflow:auto;"
                f"max-height:300px;font-size:12px'>{esc(sf['snippet'][-3000:])}</pre>"
            )
            parts.append("</details>")

    return "<html><body>" + "".join(parts) + "</body></html>"

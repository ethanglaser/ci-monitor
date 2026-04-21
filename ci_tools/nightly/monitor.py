"""Nightly CI monitor — report status + regressions via email.

Usage:
    python -m ci_tools.nightly.monitor --scope sklearnex-azure [--dry-run]

Required env vars for all scopes:
    AWS_BEARER_TOKEN_BEDROCK, AWS_REGION, GITHUB_TOKEN

Azure-Pipelines scopes also need:
    AZURE_DEVOPS_PAT, AZURE_DEVOPS_ORG, AZURE_DEVOPS_PROJECT

For sending email (non-dry-run):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
"""

import argparse
import sys

from ci_tools.log_parser import extract_error_snippet
from ci_tools.nightly.attribution import (
    attribute,
    get_commits_between,
    get_commits_in_window,
)
from ci_tools.nightly.config import SCOPES
from ci_tools.nightly.regression import diff_runs
from ci_tools.providers import azure_pipelines


def collect_azure_scope(scope):
    """Fetch latest two completed nightly runs + jobs for an Azure scope."""
    runs = azure_pipelines.get_nightly_runs(
        definition_id=scope["definition_id"],
        branch=scope["branch"],
        top=5,
    )
    if len(runs) < 2:
        return None, None, None, None

    curr_run, prev_run = runs[0], runs[1]
    curr_jobs = azure_pipelines.get_all_jobs(curr_run["id"])
    prev_jobs = azure_pipelines.get_all_jobs(prev_run["id"])
    return curr_run, prev_run, curr_jobs, prev_jobs


def _enrich_failure(job, snippet, repo_commits, upstream_commits, scope, errors):
    """Attach snippet + commits + LLM verdict to a failing job.

    Attribution failures are caught here (not in attribute() itself) so the
    email still gets sent, but the run is flagged as errored via `errors`.
    """
    try:
        verdict = attribute(
            job, snippet, repo_commits, upstream_commits,
            scope["repo"], scope.get("upstream_repo"),
        )
    except Exception as e:
        errors.append(f"attribution failed for '{job['name']}': {e}")
        verdict = f"(attribution failed: {e})"
    return {
        "job": job,
        "snippet": snippet,
        "repo_commits": repo_commits,
        "upstream_commits": upstream_commits,
        "verdict": verdict,
    }


def _fetch_snippet(job, errors):
    try:
        log = azure_pipelines.get_job_logs(
            "", job["id"], _log_id=job.get("_log_id")
        )
    except Exception as e:
        errors.append(f"log fetch failed for '{job['name']}': {e}")
        log = f"(could not fetch log: {e})"
    return extract_error_snippet(log, job.get("failed_step"))


def build_report(scope_name, scope, curr_run, prev_run, diff, errors):
    """Assemble a plain dict that the email renderer consumes.

    `errors` is a list that collects non-fatal errors encountered during
    report construction (log fetches, attribution). The caller exits
    non-zero if it's non-empty.
    """
    has_any_failure = bool(diff["new_failures"] or diff["still_failing"])
    status = "OK" if not has_any_failure else (
        "REGRESSION" if diff["new_failures"] else "RED"
    )

    report = {
        "scope_name": scope_name,
        "display_name": scope["display_name"],
        "curr_run": curr_run,
        "prev_run": prev_run,
        "status": status,
        "counts": {
            "total": sum(len(v) for v in diff.values()),
            "new_failures": len(diff["new_failures"]),
            "fixed": len(diff["fixed"]),
            "still_failing": len(diff["still_failing"]),
            "still_passing": len(diff["still_passing"]),
        },
        "new_failures": [],
        "still_failing": [],
        "fixed": diff["fixed"],
    }

    # Attribute NEW regressions against the narrow prev→curr commit window
    repo_commits_narrow = get_commits_between(
        scope["repo"], prev_run["source_sha"], curr_run["source_sha"]
    )
    upstream_commits_narrow = []
    if scope.get("upstream_repo"):
        upstream_commits_narrow = get_commits_in_window(
            scope["upstream_repo"],
            prev_run["finished_at"],
            curr_run["finished_at"],
        )

    for job in diff["new_failures"]:
        snippet = _fetch_snippet(job, errors)
        report["new_failures"].append(
            _enrich_failure(job, snippet, repo_commits_narrow,
                            upstream_commits_narrow, scope, errors)
        )

    # For chronic failures we skip the commit list (we don't know when it
    # last passed) and just summarize what's broken.
    for job in diff["still_failing"]:
        snippet = _fetch_snippet(job, errors)
        report["still_failing"].append(
            _enrich_failure(job, snippet, [], [], scope, errors)
        )

    return report


def render_email(report):
    """Return (subject, html_body, text_body)."""
    from ci_tools.email_sender import render_html, render_text

    date = report["curr_run"]["started_at"][:10]
    subject = (
        f"[CI Monitor] {report['status']} — {report['scope_name']} — {date} "
        f"({report['counts']['new_failures']} new failure(s), "
        f"{report['counts']['still_failing']} still failing)"
    )
    return subject, render_html(report), render_text(report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", required=True, choices=sorted(SCOPES.keys()))
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the email to stdout instead of sending."
    )
    args = parser.parse_args()

    scope = SCOPES[args.scope]
    print(f"Running nightly monitor for scope: {args.scope}")
    print(f"Provider: {scope['provider']}")

    if scope["provider"] == "azure_pipelines":
        curr_run, prev_run, curr_jobs, prev_jobs = collect_azure_scope(scope)
    else:
        print(f"Unsupported provider: {scope['provider']}")
        sys.exit(1)

    if curr_run is None:
        print("Fewer than 2 completed nightly runs found — nothing to compare.")
        sys.exit(0)

    print(f"Current run:  {curr_run['id']} ({curr_run['result']}) "
          f"@ {curr_run['source_sha'][:10]} {curr_run['started_at']}")
    print(f"Previous run: {prev_run['id']} ({prev_run['result']}) "
          f"@ {prev_run['source_sha'][:10]} {prev_run['started_at']}")
    print(f"Jobs: {len(curr_jobs)} current, {len(prev_jobs)} previous")

    diff = diff_runs(prev_jobs, curr_jobs)
    print(f"Diff: {len(diff['new_failures'])} new failures, "
          f"{len(diff['fixed'])} fixed, "
          f"{len(diff['still_failing'])} still failing, "
          f"{len(diff['still_passing'])} still passing")

    errors = []
    report = build_report(args.scope, scope, curr_run, prev_run, diff, errors)
    subject, html_body, text_body = render_email(report)

    if args.dry_run:
        print("\n" + "=" * 72)
        print(f"Subject: {subject}")
        print("=" * 72)
        print(text_body)
        print("=" * 72)
    else:
        from ci_tools.email_sender import send_email
        send_email(subject, html_body, text_body)
        print("Email sent.")

    if errors:
        print(f"\nERROR: {len(errors)} non-fatal error(s) during run:",
              file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

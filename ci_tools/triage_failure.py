"""CI failure triage tool.

Finds failed GitHub Actions runs for a PR, extracts error logs,
checks for similar failures across the repo, and uses Claude to
analyze root cause. Posts findings as a PR comment.
"""

import os
import re
import sys
import time

import requests

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
AWS_BEARER_TOKEN = os.environ["AWS_BEARER_TOKEN_BEDROCK"]
AWS_REGION = os.environ["AWS_REGION"]
PR_NUMBER = int(os.environ["PR_NUMBER"])
REPO = os.environ["REPO"]

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

MAX_LOG_LINES = 300
MAX_DIFF_CHARS = 12000
MAX_SIMILAR_RUNS = 5
BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-6-v1:0"


def github_get(endpoint, accept=None):
    """Make a GET request to the GitHub API with retry on rate limit."""
    headers = dict(HEADERS)
    if accept:
        headers["Accept"] = accept
    for attempt in range(3):
        resp = requests.get(f"{GITHUB_API}{endpoint}", headers=headers)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            wait = int(resp.headers.get("Retry-After", 30))
            print(f"Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()


def github_post(endpoint, json_body):
    """Make a POST request to the GitHub API."""
    resp = requests.post(
        f"{GITHUB_API}{endpoint}", headers=HEADERS, json=json_body
    )
    resp.raise_for_status()
    return resp


def get_pr_info(repo, pr_number):
    """Get PR details including head branch ref."""
    resp = github_get(f"/repos/{repo}/pulls/{pr_number}")
    data = resp.json()
    return {
        "head_ref": data["head"]["ref"],
        "head_sha": data["head"]["sha"],
        "title": data["title"],
        "html_url": data["html_url"],
    }


def get_failed_run(repo, pr_number):
    """Find the most recent failed CI run for a PR."""
    pr_info = get_pr_info(repo, pr_number)
    head_ref = pr_info["head_ref"]

    # Search for failed runs on the PR's head branch with workflow name "CI"
    resp = github_get(
        f"/repos/{repo}/actions/runs"
        f"?branch={head_ref}&status=failure&per_page=20"
    )
    runs = resp.json().get("workflow_runs", [])

    # Also check completed runs with failure conclusion
    if not runs:
        resp = github_get(
            f"/repos/{repo}/actions/runs"
            f"?branch={head_ref}&status=completed&per_page=20"
        )
        runs = [
            r for r in resp.json().get("workflow_runs", [])
            if r.get("conclusion") == "failure"
        ]

    # Filter to CI workflow runs
    ci_runs = [r for r in runs if r.get("name") == "CI"]
    if not ci_runs:
        # Fall back to any failed run
        ci_runs = runs

    if not ci_runs:
        return None, pr_info

    # Return the most recent one
    return ci_runs[0], pr_info


def get_failed_jobs(repo, run_id):
    """Get failed jobs from a workflow run."""
    resp = github_get(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    jobs = resp.json().get("jobs", [])

    failed_jobs = []
    for job in jobs:
        if job.get("conclusion") != "failure":
            continue

        # Find the failed step
        failed_step = None
        for step in job.get("steps", []):
            if step.get("conclusion") == "failure":
                failed_step = step["name"]
                break

        failed_jobs.append({
            "id": job["id"],
            "name": job["name"],
            "failed_step": failed_step,
            "html_url": job["html_url"],
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
        })

    return failed_jobs


def get_job_logs(repo, job_id):
    """Download logs for a specific job."""
    try:
        resp = github_get(
            f"/repos/{repo}/actions/jobs/{job_id}/logs",
            accept="application/vnd.github+json",
        )
        return resp.text
    except requests.HTTPError as e:
        print(f"Warning: Could not fetch logs for job {job_id}: {e}")
        return ""


def strip_timestamp(line):
    """Strip GitHub Actions timestamp prefix from a log line."""
    # Format: 2024-01-15T10:30:45.1234567Z <content>
    return re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s?", "", line)


def extract_error_snippet(raw_log, failed_step_name=None):
    """Extract relevant error portions from a GitHub Actions job log."""
    if not raw_log:
        return "No logs available."

    lines = raw_log.split("\n")
    stripped = [strip_timestamp(line) for line in lines]

    # Try to find the failed step section
    if failed_step_name:
        step_lines = _extract_step_section(stripped, failed_step_name)
        if step_lines:
            return _build_snippet(step_lines)

    # Fall back to scanning the entire log for errors
    return _build_snippet(stripped)


def _extract_step_section(lines, step_name):
    """Extract lines belonging to a specific step."""
    in_section = False
    section_lines = []

    for line in lines:
        # Step boundaries use ##[group] markers
        if f"##[group]" in line and step_name.lower() in line.lower():
            in_section = True
            section_lines = []
            continue
        if in_section and "##[group]" in line:
            # Next step started
            break
        if in_section:
            section_lines.append(line)

    return section_lines if section_lines else None


def _build_snippet(lines):
    """Build an error snippet from log lines by finding error regions."""
    error_patterns = [
        r"(?i)error[:\s]",
        r"(?i)failed",
        r"(?i)traceback",
        r"(?i)exception",
        r"(?i)assert(?:ion)?(?:error)?",
        r"= FAILURES =",
        r"= ERRORS =",
        r"short test summary",
        r"(?i)fatal",
        r"Process completed with exit code [1-9]",
    ]
    combined_pattern = re.compile("|".join(error_patterns))

    # Collect error regions (line index + context)
    error_indices = set()
    for i, line in enumerate(lines):
        if combined_pattern.search(line):
            # Add 5 lines of context before and after
            for j in range(max(0, i - 5), min(len(lines), i + 6)):
                error_indices.add(j)

    # Always include the last 50 lines (often contain the summary)
    tail_start = max(0, len(lines) - 50)
    for i in range(tail_start, len(lines)):
        error_indices.add(i)

    if not error_indices:
        # No errors found, return last 100 lines
        return "\n".join(lines[-100:])

    # Build output from sorted indices, adding "..." for gaps
    sorted_indices = sorted(error_indices)
    result_lines = []
    prev_idx = -2

    for idx in sorted_indices:
        if idx > prev_idx + 1:
            result_lines.append("...")
        result_lines.append(lines[idx])
        prev_idx = idx

    # Cap output
    if len(result_lines) > MAX_LOG_LINES:
        result_lines = result_lines[-MAX_LOG_LINES:]

    return "\n".join(result_lines)


def extract_error_signatures(snippet):
    """Extract normalized error signatures for comparison."""
    signatures = set()

    for line in snippet.split("\n"):
        line = line.strip()
        # Python exceptions
        match = re.search(r"(\w+Error|\w+Exception):\s*(.+)", line)
        if match:
            signatures.add(f"{match.group(1)}: {match.group(2)[:80]}")
            continue
        # pytest FAILED lines
        match = re.search(r"FAILED\s+(\S+)", line)
        if match:
            signatures.add(f"FAILED {match.group(1)}")
            continue
        # Generic error messages
        match = re.search(r"(?i)error:\s*(.+)", line)
        if match and len(match.group(1)) > 10:
            signatures.add(f"error: {match.group(1)[:80]}")

    return signatures


def find_similar_failures(repo, error_info):
    """Search recent runs on main and other PRs for similar failures."""
    # Collect error signatures from current failure
    current_signatures = set()
    failed_job_names = set()
    for info in error_info:
        current_signatures.update(extract_error_signatures(info["snippet"]))
        failed_job_names.add(info["job_name"])

    if not current_signatures:
        return []

    similar = []

    # Check recent failed runs on main
    try:
        resp = github_get(
            f"/repos/{repo}/actions/runs"
            f"?branch=main&status=failure&per_page={MAX_SIMILAR_RUNS}"
        )
        main_runs = resp.json().get("workflow_runs", [])
    except requests.HTTPError:
        main_runs = []

    # Check recent failed PR runs
    try:
        resp = github_get(
            f"/repos/{repo}/actions/runs"
            f"?event=pull_request&status=failure&per_page=10"
        )
        pr_runs = [
            r for r in resp.json().get("workflow_runs", [])
            if not any(
                p.get("number") == PR_NUMBER
                for p in r.get("pull_requests", [])
            )
        ][:MAX_SIMILAR_RUNS]
    except requests.HTTPError:
        pr_runs = []

    all_runs = main_runs + pr_runs

    for run in all_runs:
        run_id = run["id"]
        try:
            jobs = get_failed_jobs(repo, run_id)
        except requests.HTTPError:
            continue

        # Only check jobs with matching names
        matching_jobs = [j for j in jobs if j["name"] in failed_job_names]
        if not matching_jobs:
            continue

        for job in matching_jobs[:2]:  # Limit per run
            try:
                log = get_job_logs(repo, job["id"])
            except requests.HTTPError:
                continue
            snippet = extract_error_snippet(log, job.get("failed_step"))
            other_sigs = extract_error_signatures(snippet)

            overlap = current_signatures & other_sigs
            if overlap:
                # Determine source PR if any
                source_prs = run.get("pull_requests", [])
                source_pr = source_prs[0]["number"] if source_prs else None

                similar.append({
                    "run_id": run_id,
                    "run_url": run["html_url"],
                    "job_name": job["name"],
                    "branch": run.get("head_branch", "unknown"),
                    "source_pr": source_pr,
                    "matching_signatures": list(overlap),
                    "created_at": run["created_at"],
                })

    return similar


def get_pr_diff(repo, pr_number):
    """Get the PR's code diff."""
    try:
        resp = github_get(
            f"/repos/{repo}/pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        diff = resp.text
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n\n... [diff truncated] ..."
        return diff
    except requests.HTTPError as e:
        print(f"Warning: Could not fetch PR diff: {e}")
        return "Diff unavailable."


def analyze_with_claude(error_info, similar_failures, pr_diff, pr_number):
    """Send failure data to Claude for analysis via AWS Bedrock."""
    system_prompt = """\
You are a CI failure triage specialist. Your job is to analyze CI test failures \
and determine their root cause.

Common failure categories:
1. PR-specific: The PR's code changes directly caused the failure
2. Infrastructure/flaky: Network timeouts, runner issues, dependency install failures
3. Upstream breakage: A dependency introduced a regression
4. Pre-existing: The same failure occurs on main or other PRs (not caused by this PR)

Be concise and actionable. Use markdown formatting."""

    # Build error section
    error_section = ""
    for info in error_info:
        error_section += f"\n#### Job: {info['job_name']}\n"
        error_section += f"**Failed step**: {info['failed_step'] or 'Unknown'}\n"
        error_section += f"```\n{info['snippet'][:4000]}\n```\n"

    # Build similar failures section
    if similar_failures:
        similar_section = "### Similar Failures Found\n"
        for sf in similar_failures:
            source = f"PR #{sf['source_pr']}" if sf["source_pr"] else f"branch `{sf['branch']}`"
            similar_section += f"- **{sf['job_name']}** on {source} ({sf['created_at'][:10]}): "
            similar_section += ", ".join(sf["matching_signatures"][:3]) + "\n"
    else:
        similar_section = "### Similar Failures\nNo similar failures found in recent runs on main or other PRs.\n"

    user_prompt = f"""\
## PR #{pr_number} CI Failure Analysis (repo: {REPO})

### Failed Jobs and Error Logs
{error_section}

{similar_section}

### PR Code Changes (diff)
```diff
{pr_diff[:8000]}
```

---
Please analyze this CI failure and provide:

1. **Failure Classification**: Is this PR-specific, infrastructure/flaky, upstream, or pre-existing?
2. **Root Cause**: What is the most likely cause of the failure?
3. **Evidence**: What specific log lines or patterns support your conclusion?
4. **Relevant Code Changes**: If PR-specific, which changes in the diff likely caused it?
5. **Recommendation**: What should the PR author do next?"""

    # Call Bedrock invoke-model API directly with bearer token
    url = (
        f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com"
        f"/model/{BEDROCK_MODEL}/invoke"
    )
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {AWS_BEARER_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "anthropic_version": "bedrock-2023-10-16",
            "max_tokens": 2048,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
    )
    resp.raise_for_status()
    result = resp.json()
    return result["content"][0]["text"]


def format_comment(pr_number, run_info, error_info, analysis, similar_failures):
    """Format the triage results as a PR comment."""
    run_url = run_info.get("html_url", "")
    run_id = run_info.get("id", "")
    created = run_info.get("created_at", "")[:10]

    job_names = ", ".join(info["job_name"] for info in error_info)
    job_details = ""
    for info in error_info:
        job_url = info.get("html_url", "")
        job_details += f"- [{info['job_name']}]({job_url}) "
        job_details += f"(failed step: {info['failed_step'] or 'Unknown'})\n"

    similar_section = ""
    if similar_failures:
        similar_section = "| Job | Source | Date | Matching Errors |\n"
        similar_section += "|-----|--------|------|-----------------|\n"
        for sf in similar_failures:
            source = f"PR #{sf['source_pr']}" if sf["source_pr"] else f"`{sf['branch']}`"
            sigs = ", ".join(s[:50] for s in sf["matching_signatures"][:2])
            similar_section += f"| {sf['job_name']} | {source} | {sf['created_at'][:10]} | {sigs} |\n"
    else:
        similar_section = "No similar failures found in recent runs.\n"

    return f"""\
## CI Failure Triage Report

**Run**: [{run_id}]({run_url}) | **Date**: {created}

<details>
<summary><b>Failed Jobs</b>: {job_names}</summary>

{job_details}
</details>

### Analysis

{analysis}

---

<details>
<summary>Similar failures in recent runs</summary>

{similar_section}
</details>

---
_Generated by CI Triage Bot_
"""


def post_pr_comment(repo, pr_number, body):
    """Post a comment on the PR."""
    github_post(f"/repos/{repo}/issues/{pr_number}/comments", {"body": body})
    print(f"Comment posted on PR #{pr_number}")


def main():
    print(f"Triaging CI failure for PR #{PR_NUMBER} in {REPO}")

    # 1. Find failed run
    run, pr_info = get_failed_run(REPO, PR_NUMBER)
    if not run:
        print("No failed CI runs found for this PR.")
        post_pr_comment(
            REPO, PR_NUMBER,
            "## CI Failure Triage Report\n\n"
            "No failed CI runs found for this PR.\n\n"
            "_Generated by CI Triage Bot_"
        )
        return

    print(f"Found failed run: {run['id']} ({run['html_url']})")

    # 2. Get failed jobs and logs
    failed_jobs = get_failed_jobs(REPO, run["id"])
    if not failed_jobs:
        print("CI run found but no failed jobs detected.")
        post_pr_comment(
            REPO, PR_NUMBER,
            "## CI Failure Triage Report\n\n"
            f"CI run [{run['id']}]({run['html_url']}) found but no failed jobs detected.\n\n"
            "_Generated by CI Triage Bot_"
        )
        return

    print(f"Found {len(failed_jobs)} failed job(s)")
    error_info = []
    for job in failed_jobs:
        print(f"  Fetching logs for: {job['name']}")
        raw_log = get_job_logs(REPO, job["id"])
        snippet = extract_error_snippet(raw_log, job.get("failed_step"))
        error_info.append({
            "job_name": job["name"],
            "job_id": job["id"],
            "failed_step": job.get("failed_step"),
            "snippet": snippet,
            "html_url": job.get("html_url"),
        })

    # 3. Search for similar failures
    print("Searching for similar failures...")
    similar = find_similar_failures(REPO, error_info)
    print(f"Found {len(similar)} similar failure(s)")

    # 4. Get PR diff
    print("Fetching PR diff...")
    diff = get_pr_diff(REPO, PR_NUMBER)

    # 5. Analyze with Claude
    print("Analyzing with Claude...")
    analysis = analyze_with_claude(error_info, similar, diff, PR_NUMBER)

    # 6. Post comment
    comment = format_comment(PR_NUMBER, run, error_info, analysis, similar)
    post_pr_comment(REPO, PR_NUMBER, comment)

    print("Triage complete.")


if __name__ == "__main__":
    main()

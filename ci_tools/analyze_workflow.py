"""Cross-branch workflow failure analysis.

Given a repository and a GitHub Actions workflow file, analyzes whether
failures are recurring across branches (repo-wide issue) or specific
to one branch. Can optionally anchor to a specific failed run URL.
"""

import os
import re
import sys
from collections import defaultdict

import requests

from ci_tools.log_parser import extract_error_snippet, extract_error_signatures
from ci_tools.providers.github_actions import (
    _get as github_get,
    get_failed_jobs,
    get_job_logs,
)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
AWS_BEARER_TOKEN = os.environ["AWS_BEARER_TOKEN_BEDROCK"]
AWS_REGION = os.environ["AWS_REGION"]
REPO = os.environ["REPO"]
WORKFLOW_FILE = os.environ["WORKFLOW_FILE"]
RUN_URL = os.environ.get("RUN_URL", "")

BEDROCK_MODEL = os.environ.get(
    "BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-20250514-v1:0"
)

MAX_RECENT_RUNS = 30
MAX_BRANCH_LOG_FETCHES = 8


def parse_run_url(url):
    """Extract owner/repo and run_id from a GitHub Actions run URL."""
    match = re.match(
        r"https://github\.com/([^/]+/[^/]+)/actions/runs/(\d+)", url
    )
    if not match:
        return None, None
    return match.group(1), int(match.group(2))


def get_run_by_id(repo, run_id):
    """Fetch a specific workflow run."""
    resp = github_get(f"/repos/{repo}/actions/runs/{run_id}")
    return resp.json()


def get_workflow_runs(repo, workflow_file, per_page=30):
    """Fetch recent runs of a workflow across all branches."""
    resp = github_get(
        f"/repos/{repo}/actions/workflows/{workflow_file}/runs"
        f"?per_page={per_page}"
    )
    return resp.json().get("workflow_runs", [])


def resolve_anchor_run(repo, workflow_file, run_url):
    """Resolve the anchor run — either from URL or most recent failure."""
    if run_url:
        url_repo, run_id = parse_run_url(run_url)
        if run_id:
            actual_repo = url_repo or repo
            run = get_run_by_id(actual_repo, run_id)
            return run, actual_repo
        else:
            print(f"Warning: Could not parse run URL: {run_url}")

    # Fall back to most recent failed run
    resp = github_get(
        f"/repos/{repo}/actions/workflows/{workflow_file}/runs"
        f"?status=failure&per_page=1"
    )
    runs = resp.json().get("workflow_runs", [])
    if not runs:
        # Try completed with failure conclusion
        resp = github_get(
            f"/repos/{repo}/actions/workflows/{workflow_file}/runs"
            f"?status=completed&per_page=10"
        )
        runs = [
            r for r in resp.json().get("workflow_runs", [])
            if r.get("conclusion") == "failure"
        ]

    if not runs:
        return None, repo

    return runs[0], repo


def build_branch_matrix(runs):
    """Group runs by branch and compute pass/fail status."""
    branches = defaultdict(list)
    for run in runs:
        branch = run.get("head_branch", "unknown")
        branches[branch].append({
            "id": run["id"],
            "conclusion": run.get("conclusion", "unknown"),
            "created_at": run.get("created_at", ""),
            "html_url": run.get("html_url", ""),
            "event": run.get("event", ""),
        })

    matrix = {}
    for branch, branch_runs in branches.items():
        total = len(branch_runs)
        failures = sum(1 for r in branch_runs if r["conclusion"] == "failure")
        successes = sum(1 for r in branch_runs if r["conclusion"] == "success")
        latest = branch_runs[0]  # runs are sorted newest first
        matrix[branch] = {
            "total": total,
            "failures": failures,
            "successes": successes,
            "latest_conclusion": latest["conclusion"],
            "latest_run_id": latest["id"],
            "latest_url": latest["html_url"],
            "latest_date": latest["created_at"][:10] if latest["created_at"] else "",
            "runs": branch_runs,
        }

    return matrix


def analyze_failing_branches(repo, matrix, anchor_signatures):
    """For branches with failures, fetch logs and compare signatures."""
    branch_analysis = {}
    fetches = 0

    for branch, info in matrix.items():
        if info["failures"] == 0:
            continue
        if fetches >= MAX_BRANCH_LOG_FETCHES:
            branch_analysis[branch] = {
                "matching_signatures": [],
                "note": "skipped (log fetch limit reached)",
            }
            continue

        # Find the most recent failed run on this branch
        failed_run = None
        for run in info["runs"]:
            if run["conclusion"] == "failure":
                failed_run = run
                break

        if not failed_run:
            continue

        try:
            jobs = get_failed_jobs(repo, failed_run["id"])
        except Exception as e:
            branch_analysis[branch] = {
                "matching_signatures": [],
                "note": f"error fetching jobs: {e}",
            }
            continue

        branch_sigs = set()
        snippets = []
        for job in jobs[:3]:  # Limit jobs per branch
            fetches += 1
            try:
                log = get_job_logs(repo, job["id"])
            except Exception:
                continue
            snippet = extract_error_snippet(log, job.get("failed_step"))
            snippets.append(f"[{job['name']}] {snippet[:1000]}")
            branch_sigs.update(extract_error_signatures(snippet))

        overlap = anchor_signatures & branch_sigs
        branch_analysis[branch] = {
            "matching_signatures": list(overlap),
            "all_signatures": list(branch_sigs)[:10],
            "snippets": snippets,
        }

    return branch_analysis


def format_matrix_text(matrix, branch_analysis, anchor_branch):
    """Format the cross-branch matrix as readable text for Claude."""
    lines = []
    lines.append("| Branch | Runs | Pass | Fail | Latest | Matches Anchor? |")
    lines.append("|--------|------|------|------|--------|-----------------|")

    for branch, info in sorted(matrix.items(), key=lambda x: -x[1]["failures"]):
        marker = " (anchor)" if branch == anchor_branch else ""
        ba = branch_analysis.get(branch, {})
        matches = ba.get("matching_signatures", [])
        match_str = f"Yes ({len(matches)} sigs)" if matches else "No"
        if info["failures"] == 0:
            match_str = "N/A (passing)"
        lines.append(
            f"| {branch}{marker} | {info['total']} | {info['successes']} | "
            f"{info['failures']} | {info['latest_conclusion']} | {match_str} |"
        )

    return "\n".join(lines)


def analyze_with_claude(anchor_info, matrix_text, branch_analysis, anchor_branch, repo, workflow_file):
    """Send cross-branch analysis to Claude."""
    system_prompt = """\
You are a CI workflow failure analyst. Your job is to analyze GitHub Actions \
workflow failures across multiple branches and determine whether a failure \
is a repo-wide systemic issue or specific to one branch.

Focus on:
- Is this failure happening across many branches or just one?
- Do the error signatures match across branches?
- Is this an infrastructure/flaky issue, a code issue, or an upstream dependency issue?
- How long has this been going on (based on the run history)?

Be concise and actionable. Use markdown formatting."""

    # Anchor failure details
    anchor_section = f"### Anchor Failure\n"
    anchor_section += f"**Repository**: {repo}\n"
    anchor_section += f"**Workflow**: {workflow_file}\n"
    anchor_section += f"**Branch**: {anchor_branch}\n"
    for info in anchor_info:
        anchor_section += f"\n#### Job: {info['job_name']}\n"
        anchor_section += f"**Failed step**: {info['failed_step'] or 'Unknown'}\n"
        anchor_section += f"```\n{info['snippet'][:3000]}\n```\n"

    # Branch-specific error details for failing branches
    branch_details = ""
    for branch, ba in branch_analysis.items():
        if branch == anchor_branch:
            continue
        if not ba.get("matching_signatures") and not ba.get("all_signatures"):
            continue
        branch_details += f"\n#### Branch: {branch}\n"
        if ba.get("matching_signatures"):
            branch_details += f"**Matching signatures**: {', '.join(ba['matching_signatures'][:5])}\n"
        if ba.get("all_signatures"):
            branch_details += f"**All error signatures**: {', '.join(ba['all_signatures'][:5])}\n"
        for s in ba.get("snippets", [])[:1]:
            branch_details += f"```\n{s[:1500]}\n```\n"

    user_prompt = f"""\
## Cross-Branch Workflow Failure Analysis

{anchor_section}

### Cross-Branch Matrix
{matrix_text}

### Error Details from Other Failing Branches
{branch_details if branch_details else "No other failing branches had logs fetched."}

---
Please analyze:

1. **Scope**: Is this a repo-wide issue or branch-specific?
2. **Pattern**: Do the failures share the same root cause across branches?
3. **Root Cause**: What is the most likely cause?
4. **Timeline**: Based on the run history, how long has this been happening?
5. **Recommendation**: What should be done to resolve this?"""

    url = (
        f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com"
        f"/model/{BEDROCK_MODEL}/converse"
    )
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {AWS_BEARER_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "system": [{"text": system_prompt}],
            "messages": [
                {"role": "user", "content": [{"text": user_prompt}]},
            ],
            "inferenceConfig": {"maxTokens": 2048},
        },
    )
    if not resp.ok:
        print(f"Bedrock API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    result = resp.json()
    return result["output"]["message"]["content"][0]["text"]


def main():
    repo = REPO
    workflow_file = WORKFLOW_FILE

    print(f"Analyzing workflow '{workflow_file}' in {repo}")

    # 1. Resolve anchor run
    print("Resolving anchor run...")
    anchor_run, repo = resolve_anchor_run(repo, workflow_file, RUN_URL)
    if not anchor_run:
        print("No failed runs found for this workflow.")
        return

    anchor_branch = anchor_run.get("head_branch", "unknown")
    print(f"Anchor run: {anchor_run['id']} on branch '{anchor_branch}' ({anchor_run.get('html_url', '')})")

    # 2. Get failed jobs + error snippets from anchor
    print("Fetching anchor run failures...")
    failed_jobs = get_failed_jobs(repo, anchor_run["id"])
    if not failed_jobs:
        print("Anchor run has no failed jobs.")
        return

    anchor_info = []
    anchor_signatures = set()
    for job in failed_jobs:
        print(f"  Fetching logs for: {job['name']}")
        raw_log = get_job_logs(repo, job["id"])
        snippet = extract_error_snippet(raw_log, job.get("failed_step"))
        anchor_info.append({
            "job_name": job["name"],
            "failed_step": job.get("failed_step"),
            "snippet": snippet,
        })
        anchor_signatures.update(extract_error_signatures(snippet))

    print(f"Anchor signatures: {len(anchor_signatures)}")

    # 3. Fetch recent runs across all branches
    print(f"Fetching recent runs of '{workflow_file}'...")
    all_runs = get_workflow_runs(repo, workflow_file, per_page=MAX_RECENT_RUNS)
    print(f"Found {len(all_runs)} recent runs")

    # 4. Build cross-branch matrix
    matrix = build_branch_matrix(all_runs)
    failing_branches = sum(1 for info in matrix.values() if info["failures"] > 0)
    total_branches = len(matrix)
    print(f"Branches: {total_branches} total, {failing_branches} with failures")

    # 5. Analyze failing branches
    print("Analyzing failing branches...")
    branch_analysis = analyze_failing_branches(repo, matrix, anchor_signatures)

    matching_branches = sum(
        1 for ba in branch_analysis.values()
        if ba.get("matching_signatures")
    )
    print(f"Branches with matching error signatures: {matching_branches}")

    # 6. Format matrix text
    matrix_text = format_matrix_text(matrix, branch_analysis, anchor_branch)

    # 7. Analyze with Claude
    print("Analyzing with Claude...")
    analysis = analyze_with_claude(
        anchor_info, matrix_text, branch_analysis, anchor_branch, repo, workflow_file
    )

    # 8. Output results
    separator = "=" * 72
    report = f"""
{separator}
WORKFLOW FAILURE ANALYSIS REPORT
{separator}

Repository:  {repo}
Workflow:    {workflow_file}
Anchor Run:  {anchor_run['id']} (branch: {anchor_branch})
URL:         {anchor_run.get('html_url', 'N/A')}

Branches analyzed: {total_branches}
Branches failing:  {failing_branches}
With matching errors: {matching_branches}

{separator}
CROSS-BRANCH MATRIX
{separator}

{matrix_text}

{separator}
ANALYSIS
{separator}

{analysis}

{separator}
"""
    print(report)

    # Write to GitHub step summary if available
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(f"## Workflow Failure Analysis: {workflow_file}\n\n")
            f.write(f"**Repository**: {repo} | **Anchor branch**: {anchor_branch}\n\n")
            f.write(f"**Branches**: {total_branches} total, {failing_branches} failing, ")
            f.write(f"{matching_branches} with matching errors\n\n")
            f.write("### Cross-Branch Matrix\n\n")
            f.write(matrix_text + "\n\n")
            f.write("### Analysis\n\n")
            f.write(analysis + "\n")

    print("Analysis complete.")


if __name__ == "__main__":
    main()

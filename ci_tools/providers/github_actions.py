"""GitHub Actions CI provider."""

import os
import time

import requests

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

PROVIDER_NAME = "GitHub Actions"


def is_available():
    return bool(GITHUB_TOKEN)


def _get(endpoint, accept=None):
    headers = dict(HEADERS)
    if accept:
        headers["Accept"] = accept
    for _ in range(3):
        resp = requests.get(f"{GITHUB_API}{endpoint}", headers=headers)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            wait = int(resp.headers.get("Retry-After", 30))
            print(f"  [GitHub] Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()


def _post(endpoint, json_body):
    resp = requests.post(
        f"{GITHUB_API}{endpoint}", headers=HEADERS, json=json_body
    )
    resp.raise_for_status()
    return resp


def get_pr_info(repo, pr_number):
    """Get PR details including head branch ref."""
    resp = _get(f"/repos/{repo}/pulls/{pr_number}")
    data = resp.json()
    return {
        "head_ref": data["head"]["ref"],
        "head_sha": data["head"]["sha"],
        "title": data["title"],
        "html_url": data["html_url"],
    }


def get_pr_diff(repo, pr_number, max_chars=12000):
    """Get the PR's code diff."""
    try:
        resp = _get(
            f"/repos/{repo}/pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        diff = resp.text
        if len(diff) > max_chars:
            diff = diff[:max_chars] + "\n\n... [diff truncated] ..."
        return diff
    except requests.HTTPError as e:
        print(f"  [GitHub] Warning: Could not fetch PR diff: {e}")
        return "Diff unavailable."


def post_pr_comment(repo, pr_number, body):
    """Post a comment on the PR."""
    _post(f"/repos/{repo}/issues/{pr_number}/comments", {"body": body})
    print(f"  [GitHub] Comment posted on PR #{pr_number}")


def get_failed_runs(repo, pr_number, head_ref):
    """Find failed CI runs for a PR."""
    resp = _get(
        f"/repos/{repo}/actions/runs"
        f"?branch={head_ref}&status=failure&per_page=20"
    )
    runs = resp.json().get("workflow_runs", [])

    if not runs:
        resp = _get(
            f"/repos/{repo}/actions/runs"
            f"?branch={head_ref}&status=completed&per_page=20"
        )
        runs = [
            r for r in resp.json().get("workflow_runs", [])
            if r.get("conclusion") == "failure"
        ]

    ci_runs = [r for r in runs if r.get("name") == "CI"]
    if not ci_runs:
        ci_runs = runs

    if not ci_runs:
        return []

    run = ci_runs[0]
    return [{
        "id": run["id"],
        "html_url": run["html_url"],
        "created_at": run["created_at"],
        "provider": PROVIDER_NAME,
    }]


def get_failed_jobs(repo, run_id):
    """Get failed jobs from a workflow run."""
    resp = _get(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    jobs = resp.json().get("jobs", [])

    failed_jobs = []
    for job in jobs:
        if job.get("conclusion") != "failure":
            continue

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
            "provider": PROVIDER_NAME,
        })

    return failed_jobs


def get_job_logs(repo, job_id, **kwargs):
    """Download logs for a specific job."""
    try:
        resp = _get(
            f"/repos/{repo}/actions/jobs/{job_id}/logs",
            accept="application/vnd.github+json",
        )
        return resp.text
    except requests.HTTPError as e:
        print(f"  [GitHub] Warning: Could not fetch logs for job {job_id}: {e}")
        return ""


def find_similar_failures(repo, error_info, pr_number, max_runs=5):
    """Search recent GitHub Actions runs for similar failures."""
    from ci_tools.log_parser import extract_error_signatures

    current_signatures = set()
    failed_job_names = set()
    for info in error_info:
        current_signatures.update(extract_error_signatures(info["snippet"]))
        failed_job_names.add(info["job_name"])

    if not current_signatures:
        return []

    similar = []

    try:
        resp = _get(
            f"/repos/{repo}/actions/runs"
            f"?branch=main&status=failure&per_page={max_runs}"
        )
        main_runs = resp.json().get("workflow_runs", [])
    except requests.HTTPError:
        main_runs = []

    try:
        resp = _get(
            f"/repos/{repo}/actions/runs"
            f"?event=pull_request&status=failure&per_page=10"
        )
        pr_runs = [
            r for r in resp.json().get("workflow_runs", [])
            if not any(
                p.get("number") == pr_number
                for p in r.get("pull_requests", [])
            )
        ][:max_runs]
    except requests.HTTPError:
        pr_runs = []

    from ci_tools.log_parser import extract_error_snippet

    for run in main_runs + pr_runs:
        run_id = run["id"]
        try:
            jobs = get_failed_jobs(repo, run_id)
        except requests.HTTPError:
            continue

        matching_jobs = [j for j in jobs if j["name"] in failed_job_names]
        if not matching_jobs:
            continue

        for job in matching_jobs[:2]:
            try:
                log = get_job_logs(repo, job["id"])
            except requests.HTTPError:
                continue
            snippet = extract_error_snippet(log, job.get("failed_step"))
            other_sigs = extract_error_signatures(snippet)

            overlap = current_signatures & other_sigs
            if overlap:
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
                    "provider": PROVIDER_NAME,
                })

    return similar

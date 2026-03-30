"""Azure Pipelines CI provider."""

import base64
import os

import requests

AZURE_PAT = os.environ.get("AZURE_DEVOPS_PAT", "")
AZURE_ORG = os.environ.get("AZURE_DEVOPS_ORG", "")
AZURE_PROJECT = os.environ.get("AZURE_DEVOPS_PROJECT", "")

PROVIDER_NAME = "Azure Pipelines"
API_VERSION = "7.1"


def is_available():
    return bool(AZURE_PAT and AZURE_ORG and AZURE_PROJECT)


def _base_url():
    return f"https://dev.azure.com/{AZURE_ORG}/{AZURE_PROJECT}/_apis"


def _headers():
    encoded = base64.b64encode(f":{AZURE_PAT}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
    }


def _get(endpoint):
    sep = "&" if "?" in endpoint else "?"
    url = f"{_base_url()}{endpoint}{sep}api-version={API_VERSION}"
    resp = requests.get(url, headers=_headers())
    resp.raise_for_status()
    return resp


def get_failed_runs(repo, pr_number, head_ref):
    """Find failed Azure Pipelines builds for a PR."""
    # Azure Pipelines uses refs/pull/{number}/merge for PR builds
    try:
        resp = _get(
            f"/build/builds?branchName=refs/pull/{pr_number}/merge"
            f"&statusFilter=completed&resultFilter=failed&$top=5"
        )
    except requests.HTTPError as e:
        print(f"  [Azure] Warning: Could not fetch builds: {e}")
        return []

    builds = resp.json().get("value", [])
    if not builds:
        return []

    # Return the most recent failed build
    build = builds[0]
    build_url = build.get("_links", {}).get("web", {}).get("href", "")
    return [{
        "id": build["id"],
        "html_url": build_url,
        "created_at": build.get("startTime", ""),
        "provider": PROVIDER_NAME,
    }]


def get_failed_jobs(repo, run_id):
    """Get failed jobs from a build's timeline."""
    try:
        resp = _get(f"/build/builds/{run_id}/timeline")
    except requests.HTTPError as e:
        print(f"  [Azure] Warning: Could not fetch timeline for build {run_id}: {e}")
        return []

    records = resp.json().get("records", [])

    # Find failed Job-type records
    failed_jobs = []
    for record in records:
        if record.get("type") != "Job":
            continue
        if record.get("result") != "failed":
            continue

        # Find the failed task within this job
        job_id = record["id"]
        failed_step = None
        for task in records:
            if task.get("type") != "Task":
                continue
            if task.get("parentId") != job_id:
                continue
            if task.get("result") == "failed":
                failed_step = task.get("name")
                break

        build_url = ""
        log_info = record.get("log", {})

        failed_jobs.append({
            "id": run_id,  # build ID needed for log fetching
            "name": record.get("name", "Unknown"),
            "failed_step": failed_step,
            "html_url": build_url,
            "started_at": record.get("startTime"),
            "completed_at": record.get("finishTime"),
            "provider": PROVIDER_NAME,
            "_log_id": log_info.get("id"),
            "_timeline_id": record["id"],
        })

    return failed_jobs


def get_job_logs(repo, job_id, **kwargs):
    """Download logs for a job.

    For Azure, job_id is the build ID and _log_id must be passed via kwargs
    or extracted from the job dict.
    """
    log_id = kwargs.get("_log_id")
    build_id = job_id
    if not log_id:
        # Try to get all logs for the build and find the failed one
        try:
            resp = _get(f"/build/builds/{build_id}/timeline")
            records = resp.json().get("records", [])
            # Collect all failed task logs
            log_lines = []
            for record in records:
                if record.get("type") != "Task" or record.get("result") != "failed":
                    continue
                task_log_id = record.get("log", {}).get("id")
                if task_log_id:
                    try:
                        log_resp = _get(f"/build/builds/{build_id}/logs/{task_log_id}")
                        log_lines.append(f"=== {record.get('name', 'Unknown')} ===")
                        log_lines.append(log_resp.text)
                    except requests.HTTPError:
                        continue
            return "\n".join(log_lines) if log_lines else ""
        except requests.HTTPError as e:
            print(f"  [Azure] Warning: Could not fetch logs for build {build_id}: {e}")
            return ""

    try:
        resp = _get(f"/build/builds/{build_id}/logs/{log_id}")
        return resp.text
    except requests.HTTPError as e:
        print(f"  [Azure] Warning: Could not fetch log {log_id}: {e}")
        return ""


def find_similar_failures(repo, error_info, pr_number, max_runs=5):
    """Search recent Azure Pipelines builds for similar failures."""
    from ci_tools.log_parser import extract_error_signatures, extract_error_snippet

    current_signatures = set()
    failed_job_names = set()
    for info in error_info:
        current_signatures.update(extract_error_signatures(info["snippet"]))
        failed_job_names.add(info["job_name"])

    if not current_signatures:
        return []

    similar = []

    # Check recent failed builds on main
    try:
        resp = _get(
            f"/build/builds?branchName=refs/heads/main"
            f"&statusFilter=completed&resultFilter=failed&$top={max_runs}"
        )
        main_builds = resp.json().get("value", [])
    except requests.HTTPError:
        main_builds = []

    for build in main_builds:
        build_id = build["id"]
        try:
            jobs = get_failed_jobs(repo, build_id)
        except requests.HTTPError:
            continue

        matching_jobs = [j for j in jobs if j["name"] in failed_job_names]
        if not matching_jobs:
            continue

        for job in matching_jobs[:2]:
            log = get_job_logs(repo, build_id, _log_id=job.get("_log_id"))
            snippet = extract_error_snippet(log, job.get("failed_step"))
            other_sigs = extract_error_signatures(snippet)

            overlap = current_signatures & other_sigs
            if overlap:
                build_url = build.get("_links", {}).get("web", {}).get("href", "")
                similar.append({
                    "run_id": build_id,
                    "run_url": build_url,
                    "job_name": job["name"],
                    "branch": build.get("sourceBranch", "unknown").replace("refs/heads/", ""),
                    "source_pr": None,
                    "matching_signatures": list(overlap),
                    "created_at": build.get("startTime", ""),
                    "provider": PROVIDER_NAME,
                })

    return similar

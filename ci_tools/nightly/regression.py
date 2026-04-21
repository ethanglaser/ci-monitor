"""Compare two nightly runs to find regressions/fixes."""


def diff_runs(prev_jobs, curr_jobs):
    """Classify jobs by status change between two runs.

    Keyed on job name. A job in curr that's missing from prev is reported
    as new_failure if failing (can't regress something that didn't exist,
    but we treat unknown history as worth surfacing).
    """
    prev_by_name = {j["name"]: j for j in prev_jobs}
    curr_by_name = {j["name"]: j for j in curr_jobs}

    new_failures = []
    fixed = []
    still_failing = []
    still_passing = []

    for name, curr in curr_by_name.items():
        curr_failed = curr.get("result") == "failed"
        prev = prev_by_name.get(name)
        prev_failed = prev is not None and prev.get("result") == "failed"

        if curr_failed and not prev_failed:
            new_failures.append(curr)
        elif curr_failed and prev_failed:
            still_failing.append(curr)
        elif not curr_failed and prev_failed:
            fixed.append(curr)
        else:
            still_passing.append(curr)

    return {
        "new_failures": new_failures,
        "fixed": fixed,
        "still_failing": still_failing,
        "still_passing": still_passing,
    }

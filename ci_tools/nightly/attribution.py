"""Attribute a new regression to commits merged in the regression window."""

from ci_tools.bedrock import call_bedrock
from ci_tools.providers.github_actions import _get as github_get


def get_commits_between(repo, base_sha, head_sha, max_commits=30):
    """List commits between base_sha (exclusive) and head_sha via GitHub compare.

    Works for any public repo. Returns newest-first list of
    {sha, author, message, url}.
    """
    if not base_sha or not head_sha:
        return []
    try:
        resp = github_get(f"/repos/{repo}/compare/{base_sha}...{head_sha}")
    except Exception as e:
        print(f"  [attribution] compare {repo} failed: {e}")
        return []

    commits = resp.json().get("commits", [])[-max_commits:]
    return [
        {
            "sha": c["sha"][:10],
            "author": c.get("commit", {}).get("author", {}).get("name", "?"),
            "message": c["commit"]["message"].split("\n")[0][:120],
            "url": c.get("html_url", ""),
        }
        for c in reversed(commits)
    ]


def get_commits_in_window(repo, since_iso, until_iso, branch="main", max_commits=30):
    """List commits on a branch within a wall-clock window.

    Used for upstream repos where we don't have a SHA pair (just the
    wall-clock window between two nightly runs).
    """
    if not since_iso or not until_iso:
        return []
    try:
        resp = github_get(
            f"/repos/{repo}/commits?sha={branch}"
            f"&since={since_iso}&until={until_iso}&per_page={max_commits}"
        )
    except Exception as e:
        print(f"  [attribution] commits {repo} failed: {e}")
        return []

    return [
        {
            "sha": c["sha"][:10],
            "author": c.get("commit", {}).get("author", {}).get("name", "?"),
            "message": c["commit"]["message"].split("\n")[0][:120],
            "url": c.get("html_url", ""),
        }
        for c in resp.json()
    ]


def _fmt_commits(commits, label):
    if not commits:
        return f"_No commits in {label}._"
    lines = [f"**Commits in {label}:**"]
    for c in commits[:20]:
        lines.append(f"- `{c['sha']}` ({c['author']}): {c['message']}")
    return "\n".join(lines)


def attribute_regression(failure, error_snippet, repo_commits,
                         upstream_commits, repo, upstream_repo):
    """For NEW regressions: pin to a culprit commit in the prev→curr window."""
    system_prompt = (
        "You are a CI regression triage assistant. Given a failing job's "
        "error snippet and the commits merged between the last passing run "
        "and this failing run, identify the single most likely culprit "
        "commit. If the failure looks like infrastructure/flake say so "
        "instead of guessing. Be concise — 3-4 sentences."
    )
    upstream_section = (
        _fmt_commits(upstream_commits, f"`{upstream_repo}` (upstream)")
        if upstream_repo else ""
    )
    user_prompt = f"""\
## Failed job: {failure['name']}
**Failed step:** {failure.get('failed_step') or 'Unknown'}

### Error snippet
```
{error_snippet[:4000]}
```

### {_fmt_commits(repo_commits, f'`{repo}`')}

### {upstream_section}

---
What's the most likely cause? Call out a specific commit SHA if one fits,
otherwise say "infra/flake" or "unclear — needs human review".
"""
    return call_bedrock(system_prompt, user_prompt, max_tokens=768)


def deep_triage(failure, error_snippet, similar_failures):
    """For chronic (still-failing) jobs: structured deep analysis.

    Same shape as triage_failure.py's approach: bigger snippet, 5-question
    structured prompt, and a "similar failures in recent nightly runs"
    section so the model knows this is long-standing, not fresh.
    """
    system_prompt = (
        "You are a CI failure triage specialist analyzing a chronically "
        "failing CI job. Read the FULL error snippet carefully. Python "
        "tracebacks wrap the real cause — 'CellExecutionError' or "
        "'RuntimeError' at the top is almost never the actual problem. "
        "Find the DEEPEST exception line and quote it verbatim. "
        "\n\nCommon failure categories:\n"
        "1. Upstream API breakage: scikit-learn or another dep changed an "
        "API that sklearnex uses.\n"
        "2. Test assertion / numerical: test failure from model output "
        "mismatch.\n"
        "3. Data/checksum mismatch: fetched dataset hash doesn't match.\n"
        "4. Environment/install: dependency install failure, version "
        "conflict.\n"
        "5. Infra/flake: network timeout, runner issue, transient.\n"
        "\nBe specific. A checksum mismatch is NOT a 'network issue'. "
        "Use markdown formatting."
    )

    if similar_failures:
        matching = [sf for sf in similar_failures if sf.get("matching")]
        same_step = [
            sf for sf in similar_failures
            if not sf.get("matching") and sf.get("failed_step") == failure.get("failed_step")
        ]
        other = [
            sf for sf in similar_failures
            if not sf.get("matching") and sf.get("failed_step") != failure.get("failed_step")
        ]

        history_lines = ["### Recent history (this job in prior nightly runs)"]
        if matching:
            history_lines.append("\n**Same error signature** (confirmed same root cause):")
            for sf in matching[:5]:
                history_lines.append(
                    f"- Run [{sf['run_id']}]({sf['run_url']}) on "
                    f"{sf['started_at'][:10]} — matched: "
                    f"`{', '.join(sf['matching'][:2])}`"
                )
        if same_step:
            history_lines.append(
                "\n**Same failed step** (likely same issue, sig not extractable):"
            )
            for sf in same_step[:5]:
                history_lines.append(
                    f"- Run [{sf['run_id']}]({sf['run_url']}) on "
                    f"{sf['started_at'][:10]} — step: "
                    f"`{sf.get('failed_step') or 'Unknown'}`"
                )
        if other:
            history_lines.append(
                "\n**Job failed for a different reason** (different step):"
            )
            for sf in other[:5]:
                history_lines.append(
                    f"- Run [{sf['run_id']}]({sf['run_url']}) on "
                    f"{sf['started_at'][:10]} — step: "
                    f"`{sf.get('failed_step') or 'Unknown'}`"
                )
        history_section = "\n".join(history_lines)
    else:
        history_section = (
            "### Recent history\n"
            "_This job has not failed in the last ~10 nightly runs — "
            "this failure is new or rare._"
        )

    user_prompt = f"""\
## Chronic failure: {failure['name']}
**Failed step:** {failure.get('failed_step') or 'Unknown'}

### Error snippet
```
{error_snippet[:4000]}
```

{history_section}

---
Please provide:

1. **Deepest error line**: Quote the root-cause line verbatim (the innermost exception, not the wrapper).
2. **Classification**: Which category above?
3. **Root cause**: What's actually wrong?
4. **How long has this been happening**: Use the recent-history section. "Same error signature" → persistent same root cause. "Same failed step" (no signature match) → likely same issue but signatures aren't extractable (common for non-Python failures like Coverity or shell scripts — do NOT call it "new" just because signatures didn't match). "Different reason" → job is flaky across multiple issues. No history → new/rare.
5. **Recommended next step**: What should someone do to fix it?
"""
    return call_bedrock(system_prompt, user_prompt, max_tokens=1024)


# Backwards-compat shim: old name still works for callers that expect it.
def attribute(failure, error_snippet, repo_commits, upstream_commits,
              repo, upstream_repo):
    if repo_commits or upstream_commits:
        return attribute_regression(
            failure, error_snippet, repo_commits, upstream_commits,
            repo, upstream_repo,
        )
    return deep_triage(failure, error_snippet, [])

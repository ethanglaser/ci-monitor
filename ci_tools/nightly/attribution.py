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


def attribute(failure, error_snippet, repo_commits, upstream_commits, repo, upstream_repo):
    """Ask Claude to characterize a failure.

    With commits (new regression): try to pin to a culprit.
    Without commits (chronic failure): just classify the error.
    """
    def fmt_commits(commits, label):
        if not commits:
            return f"_No commits in {label}._"
        lines = [f"**Commits in {label}:**"]
        for c in commits[:20]:
            lines.append(f"- `{c['sha']}` ({c['author']}): {c['message']}")
        return "\n".join(lines)

    has_commits = bool(repo_commits or upstream_commits)
    if has_commits:
        system_prompt = (
            "You are a CI regression triage assistant. Given a failing job's "
            "error snippet and the list of commits merged between the last "
            "passing run and this failing run, identify the single most likely "
            "culprit commit. If the failure looks like infrastructure/flake "
            "(network timeout, runner issue, transient install error) say so "
            "instead of guessing a commit. Be concise — 3-4 sentences max."
        )
        commit_sections = (
            f"### {fmt_commits(repo_commits, f'`{repo}`')}\n\n"
            f"### {fmt_commits(upstream_commits, f'`{upstream_repo}` (upstream)') if upstream_repo else ''}"
        )
        task = (
            "What's the most likely cause? Call out a specific commit SHA if "
            "one fits, otherwise say \"infra/flake\" or \"unclear — needs "
            "human review\"."
        )
    else:
        system_prompt = (
            "You are a CI failure triage assistant. Given a failing job's "
            "error snippet from a chronic (long-standing) failure, classify "
            "the root cause in 1-2 sentences: is this an upstream API change, "
            "a numerical/assertion test failure, an environment/install "
            "issue, or infra/flake? Quote the key error line."
        )
        commit_sections = ""
        task = (
            "Classify the failure (upstream API change / test assertion / "
            "env issue / infra). Quote the key error line."
        )

    user_prompt = f"""\
## Failed job: {failure['name']}
**Failed step:** {failure.get('failed_step') or 'Unknown'}

### Error snippet
```
{error_snippet[:2500]}
```

{commit_sections}

---
{task}
"""
    try:
        return call_bedrock(system_prompt, user_prompt, max_tokens=512)
    except Exception as e:
        return f"_(attribution failed: {e})_"

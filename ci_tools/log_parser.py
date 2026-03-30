"""Shared log parsing utilities for CI failure triage."""

import re

MAX_LOG_LINES = 300


def strip_timestamp(line):
    """Strip CI timestamp prefix from a log line."""
    return re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s?", "", line)


def extract_error_snippet(raw_log, failed_step_name=None):
    """Extract relevant error portions from a CI job log."""
    if not raw_log:
        return "No logs available."

    lines = raw_log.split("\n")
    stripped = [strip_timestamp(line) for line in lines]

    if failed_step_name:
        step_lines = _extract_step_section(stripped, failed_step_name)
        if step_lines:
            return _build_snippet(step_lines)

    return _build_snippet(stripped)


def _extract_step_section(lines, step_name):
    """Extract lines belonging to a specific step.

    Handles both GitHub Actions (##[group]) and Azure Pipelines (##[section])
    markers.
    """
    in_section = False
    section_lines = []
    step_lower = step_name.lower()

    for line in lines:
        if ("##[group]" in line or "##[section]" in line) and step_lower in line.lower():
            in_section = True
            section_lines = []
            continue
        if in_section and ("##[group]" in line or "##[section]" in line):
            break
        if in_section:
            section_lines.append(line)

    return section_lines if section_lines else None


def _build_snippet(lines):
    """Build an error snippet from log lines by finding error regions."""
    error_patterns = [
        r"error[:\s]",
        r"failed",
        r"traceback",
        r"exception",
        r"assert(?:ion)?(?:error)?",
        r"FAILURES =",
        r"ERRORS =",
        r"short test summary",
        r"fatal",
        r"Process completed with exit code [1-9]",
    ]
    combined_pattern = re.compile("|".join(error_patterns), re.IGNORECASE)

    error_indices = set()
    for i, line in enumerate(lines):
        if combined_pattern.search(line):
            for j in range(max(0, i - 5), min(len(lines), i + 6)):
                error_indices.add(j)

    tail_start = max(0, len(lines) - 50)
    for i in range(tail_start, len(lines)):
        error_indices.add(i)

    if not error_indices:
        return "\n".join(lines[-100:])

    sorted_indices = sorted(error_indices)
    result_lines = []
    prev_idx = -2

    for idx in sorted_indices:
        if idx > prev_idx + 1:
            result_lines.append("...")
        result_lines.append(lines[idx])
        prev_idx = idx

    if len(result_lines) > MAX_LOG_LINES:
        result_lines = result_lines[-MAX_LOG_LINES:]

    return "\n".join(result_lines)


def extract_error_signatures(snippet):
    """Extract normalized error signatures for comparison."""
    signatures = set()

    for line in snippet.split("\n"):
        line = line.strip()
        match = re.search(r"(\w+Error|\w+Exception):\s*(.+)", line)
        if match:
            signatures.add(f"{match.group(1)}: {match.group(2)[:80]}")
            continue
        match = re.search(r"FAILED\s+(\S+)", line)
        if match:
            signatures.add(f"FAILED {match.group(1)}")
            continue
        match = re.search(r"error:\s*(.+)", line, re.IGNORECASE)
        if match and len(match.group(1)) > 10:
            signatures.add(f"error: {match.group(1)[:80]}")

    return signatures

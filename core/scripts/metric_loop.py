# -*- coding: utf-8 -*-
"""Metric loop: try a change, measure before and after, keep it or revert it by the number.

    run_experiment(name, measure, apply, revert, direction, min_delta=0.0, log_path=None) -> {"name", "before", "after", "verdict"[, "error"][, "revert_error"]}
    decide(before, after, direction, min_delta) -> "keep" | "drop"
        direction "lower" or "higher" says which way is better; the gain must be strictly greater than min_delta to keep the change.

The caller supplies four small functions: measure() returns a number, apply() makes the change, revert() puts everything back. Verdicts:
    keep    the number improved by more than min_delta; the change stays
    drop    it did not; revert() is called
    error   apply() or the second measure() raised; revert() is called and the message is in result["error"]
If revert() itself raises, the change may still be in place: the message is in result["revert_error"] and the caller must look. A measure() that raises BEFORE the change
propagates, because nothing was changed yet.
When log_path is given, one line per experiment is appended to that file (UTF-8, Markdown bullet): "- **name.** before 10, after 5, verdict keep."
This is a library: nothing here runs on its own. Tests: test_metric_loop.py.
"""
import os


def decide(before, after, direction, min_delta=0.0):
    """-> "keep" when the change improved the number by strictly more than min_delta, otherwise "drop". Raises ValueError for a direction other than "lower" or "higher"."""
    if direction == "lower":
        improvement = before - after
    elif direction == "higher":
        improvement = after - before
    else:
        raise ValueError(f"Invalid direction: {direction}. Must be 'lower' or 'higher'.")
    return "keep" if improvement > min_delta else "drop"


def _log(log_path, line):
    """Append one line to the log file (UTF-8), creating the folder if needed."""
    if log_path is None:
        return
    folder = os.path.dirname(os.fspath(log_path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:     # "\n" on every system, so the log looks the same everywhere
        f.write(line + "\n")


def _revert(revert):
    """Call revert(). -> the error text when it raised, otherwise None."""
    try:
        revert()
    except Exception as e:                                  # noqa: BLE001 - the caller needs to know, not crash
        return f"{type(e).__name__}: {e}"
    return None


def _error_result(name, before, error_text, revert, log_path):
    revert_error = _revert(revert)
    result = {"name": name, "before": before, "after": None, "verdict": "error", "error": error_text}
    line = f"- **{name}.** before {before}, after N/A, verdict error. Error: {error_text}."
    if revert_error:
        result["revert_error"] = revert_error
        line += f" Revert failed: {revert_error}."
    _log(log_path, line)
    return result


def run_experiment(name, measure, apply, revert, direction, min_delta=0.0, log_path=None, now=None):
    """Measure, apply the change, measure again, keep or revert by the number. `now` is accepted and ignored (kept for older callers).
    A direction other than "lower" or "higher" raises ValueError before anything is measured or changed."""
    if direction not in ("lower", "higher"):
        raise ValueError(f"Invalid direction: {direction}. Must be 'lower' or 'higher'.")
    before = measure()

    try:
        apply()
    except Exception as e:                                  # noqa: BLE001
        return _error_result(name, before, str(e), revert, log_path)

    try:
        after = measure()
    except Exception as e:                                  # noqa: BLE001
        return _error_result(name, before, str(e), revert, log_path)

    verdict = decide(before, after, direction, min_delta)
    result = {"name": name, "before": before, "after": after, "verdict": verdict}
    line = f"- **{name}.** before {before}, after {after}, verdict {verdict}."
    if verdict == "drop":
        revert_error = _revert(revert)
        if revert_error:
            result["revert_error"] = revert_error
            line += f" Revert failed: {revert_error}."
    _log(log_path, line)
    return result

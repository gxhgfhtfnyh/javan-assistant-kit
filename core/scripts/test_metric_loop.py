# -*- coding: utf-8 -*-
"""Tests for metric_loop.py. The first 16 tests are the original suite, unchanged; the class at the end covers the additions."""
import pytest
from metric_loop import decide, run_experiment


class TestDecide:
    def test_keep_lower_better(self):
        assert decide(10.0, 5.0, "lower", 0.0) == "keep"
        assert decide(10.0, 5.0, "lower", 3.0) == "keep"

    def test_drop_lower_not_better(self):
        assert decide(10.0, 8.0, "lower", 5.0) == "drop"
        assert decide(10.0, 10.0, "lower", 0.0) == "drop"
        assert decide(10.0, 9.0, "lower", 1.0) == "drop"
        assert decide(10.0, 5.0, "lower", 5.0) == "drop"

    def test_keep_higher_better(self):
        assert decide(5.0, 10.0, "higher", 0.0) == "keep"
        assert decide(5.0, 10.0, "higher", 3.0) == "keep"

    def test_drop_higher_not_better(self):
        assert decide(10.0, 8.0, "higher", 5.0) == "drop"
        assert decide(10.0, 10.0, "higher", 0.0) == "drop"
        assert decide(9.0, 10.0, "higher", 1.0) == "drop"
        assert decide(5.0, 10.0, "higher", 5.0) == "drop"

    def test_bad_direction_raises(self):
        with pytest.raises(ValueError):
            decide(10.0, 5.0, "invalid", 0.0)
        with pytest.raises(ValueError):
            decide(10.0, 5.0, "up", 0.0)


class TestRunExperiment:
    def test_keep_no_revert(self, tmp_path):
        revert_called = []
        measure_called = []
        apply_called = []

        def measure():
            measure_called.append(True)
            if len(measure_called) == 1:
                return 10.0
            return 5.0

        def apply():
            apply_called.append(True)

        def revert():
            revert_called.append(True)

        result = run_experiment("test", measure, apply, revert, "lower", 0.0, log_path=None)
        assert result["verdict"] == "keep"
        assert result["before"] == 10.0
        assert result["after"] == 5.0
        assert "error" not in result
        assert len(revert_called) == 0
        assert len(apply_called) == 1
        assert len(measure_called) == 2

    def test_drop_revert_called_once(self, tmp_path):
        revert_called = []
        measure_called = []
        apply_called = []

        def measure():
            measure_called.append(True)
            if len(measure_called) == 1:
                return 10.0
            return 9.0

        def apply():
            apply_called.append(True)

        def revert():
            revert_called.append(True)

        result = run_experiment("test", measure, apply, revert, "lower", 5.0, log_path=None)
        assert result["verdict"] == "drop"
        assert result["before"] == 10.0
        assert result["after"] == 9.0
        assert "error" not in result
        assert len(revert_called) == 1
        assert len(apply_called) == 1
        assert len(measure_called) == 2

    def test_revert_on_apply_exception(self, tmp_path):
        revert_called = []
        measure_called = []
        apply_called = []

        def measure():
            measure_called.append(True)
            return 10.0

        def apply():
            apply_called.append(True)
            raise RuntimeError("apply failed")

        def revert():
            revert_called.append(True)

        result = run_experiment("test", measure, apply, revert, "lower", 0.0, log_path=None)
        assert result["verdict"] == "error"
        assert result["error"] == "apply failed"
        assert result["before"] == 10.0
        assert result["after"] is None
        assert len(revert_called) == 1
        assert len(apply_called) == 1
        assert len(measure_called) == 1

    def test_revert_on_measure_exception_after_apply(self, tmp_path):
        revert_called = []
        measure_called = []
        apply_called = []

        def measure():
            measure_called.append(True)
            if len(measure_called) == 1:
                return 10.0
            raise RuntimeError("measure failed")

        def apply():
            apply_called.append(True)

        def revert():
            revert_called.append(True)

        result = run_experiment("test", measure, apply, revert, "lower", 0.0, log_path=None)
        assert result["verdict"] == "error"
        assert result["error"] == "measure failed"
        assert result["before"] == 10.0
        assert result["after"] is None
        assert len(revert_called) == 1
        assert len(apply_called) == 1
        assert len(measure_called) == 2

    def test_log_line_format_keep(self, tmp_path):
        log_path = tmp_path / "log.md"
        revert_called = []

        def measure():
            if len(measure_called) == 0:
                measure_called.append(True)
                return 10.0
            return 5.0

        measure_called = []

        def apply():
            apply_called.append(True)

        apply_called = []

        def revert():
            revert_called.append(True)

        run_experiment("my_exp", measure, apply, revert, "lower", 0.0, log_path=log_path)

        content = log_path.read_text()
        assert content == "- **my_exp.** before 10.0, after 5.0, verdict keep.\n"

    def test_log_line_format_drop(self, tmp_path):
        log_path = tmp_path / "log.md"
        revert_called = []

        def measure():
            if len(measure_called) == 0:
                measure_called.append(True)
                return 10.0
            return 9.0

        measure_called = []

        def apply():
            apply_called.append(True)

        apply_called = []

        def revert():
            revert_called.append(True)

        run_experiment("my_exp", measure, apply, revert, "lower", 5.0, log_path=log_path)

        content = log_path.read_text()
        assert content == "- **my_exp.** before 10.0, after 9.0, verdict drop.\n"

    def test_log_line_format_error_apply(self, tmp_path):
        log_path = tmp_path / "log.md"
        revert_called = []

        def measure():
            return 10.0

        def apply():
            raise RuntimeError("apply failed")

        def revert():
            revert_called.append(True)

        run_experiment("my_exp", measure, apply, revert, "lower", 0.0, log_path=log_path)

        content = log_path.read_text()
        assert content == "- **my_exp.** before 10.0, after N/A, verdict error. Error: apply failed.\n"

    def test_log_line_format_error_measure(self, tmp_path):
        log_path = tmp_path / "log.md"
        revert_called = []

        def measure():
            if len(measure_called) == 0:
                measure_called.append(True)
                return 10.0
            raise RuntimeError("measure failed")

        measure_called = []

        def apply():
            pass

        def revert():
            revert_called.append(True)

        run_experiment("my_exp", measure, apply, revert, "lower", 0.0, log_path=log_path)

        content = log_path.read_text()
        assert content == "- **my_exp.** before 10.0, after N/A, verdict error. Error: measure failed.\n"

    def test_no_log_file_when_none(self, tmp_path):
        revert_called = []
        measure_called = []

        def measure():
            if len(measure_called) == 0:
                measure_called.append(True)
                return 10.0
            return 5.0

        measure_called = []

        def apply():
            pass

        def revert():
            revert_called.append(True)

        result = run_experiment("test", measure, apply, revert, "lower", 0.0, log_path=None)
        assert result["verdict"] == "keep"
        assert "error" not in result

    def test_direction_higher_keep(self, tmp_path):
        revert_called = []
        measure_called = []

        def measure():
            if len(measure_called) == 0:
                measure_called.append(True)
                return 5.0
            return 10.0

        measure_called = []

        def apply():
            pass

        def revert():
            revert_called.append(True)

        result = run_experiment("test", measure, apply, revert, "higher", 0.0, log_path=None)
        assert result["verdict"] == "keep"
        assert len(revert_called) == 0

    def test_direction_higher_drop(self, tmp_path):
        revert_called = []
        measure_called = []

        def measure():
            if len(measure_called) == 0:
                measure_called.append(True)
                return 10.0
            return 8.0

        measure_called = []

        def apply():
            pass

        def revert():
            revert_called.append(True)

        result = run_experiment("test", measure, apply, revert, "higher", 5.0, log_path=None)
        assert result["verdict"] == "drop"
        assert len(revert_called) == 1


class TestAdditions:
    def test_a_bad_direction_is_refused_before_anything_is_changed(self):
        calls = []
        with pytest.raises(ValueError):
            run_experiment("t", lambda: calls.append("measure") or 1, lambda: calls.append("apply"), lambda: calls.append("revert"), "sideways")
        assert calls == []

    def test_a_failing_revert_after_a_drop_is_reported_not_raised(self, tmp_path):
        log = tmp_path / "log.md"
        values = iter([10.0, 12.0])

        def revert():
            raise OSError("disk full")
        result = run_experiment("t", lambda: next(values), lambda: None, revert, "lower", log_path=log)
        assert result["verdict"] == "drop" and "OSError: disk full" in result["revert_error"]
        assert "Revert failed: OSError: disk full." in log.read_text(encoding="utf-8")

    def test_a_failing_revert_after_an_apply_error_is_reported_too(self):
        def apply():
            raise RuntimeError("apply failed")

        def revert():
            raise RuntimeError("revert failed")
        result = run_experiment("t", lambda: 1.0, apply, revert, "lower")
        assert result["verdict"] == "error" and result["error"] == "apply failed" and result["revert_error"] == "RuntimeError: revert failed"

    def test_a_kept_change_has_no_revert_error_key(self):
        values = iter([10.0, 1.0])
        result = run_experiment("t", lambda: next(values), lambda: None, lambda: None, "lower")
        assert result["verdict"] == "keep" and "revert_error" not in result

    def test_the_log_is_utf8_and_the_folder_is_created(self, tmp_path):
        log = tmp_path / "deep" / "er" / "log.md"
        values = iter([10.0, 5.0])
        run_experiment("café — test", lambda: next(values), lambda: None, lambda: None, "lower", log_path=log)
        assert log.read_bytes().decode("utf-8") == "- **café — test.** before 10.0, after 5.0, verdict keep.\n"

    def test_the_log_is_appended_not_overwritten(self, tmp_path):
        log = tmp_path / "log.md"
        for n in range(3):
            values = iter([10.0, 5.0])
            run_experiment("e%d" % n, lambda: next(values), lambda: None, lambda: None, "lower", log_path=str(log))
        assert len(log.read_text(encoding="utf-8").splitlines()) == 3

    def test_a_measure_that_fails_before_the_change_propagates_and_nothing_is_applied(self):
        applied = []

        def measure():
            raise RuntimeError("no baseline")
        with pytest.raises(RuntimeError):
            run_experiment("t", measure, lambda: applied.append(1), lambda: None, "lower")
        assert applied == []

    def test_equal_numbers_are_a_drop_even_with_zero_min_delta(self):
        assert decide(5, 5, "higher") == "drop" and decide(5, 5, "lower") == "drop"

    def test_integers_and_negative_numbers_work(self):
        assert decide(-1, -5, "lower") == "keep" and decide(0, 3, "higher", 2) == "keep" and decide(0, 2, "higher", 2) == "drop"

# -*- coding: utf-8 -*-
"""Tests added in review (2026-10-31) for job_runner.py: the schedule slack, the time budget of one tick, and where a script named in the table is looked for."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import job_runner as jr  # noqa: E402

NOW = 1_800_000_000.0


def test_an_every_n_minutes_job_is_due_a_little_early_but_not_much_early():
    job = {"id": "j", "every_min": 10, "argv": ["x.py"]}
    assert jr.due(job, {"j": {"last_run": NOW - 600 + 30}}, NOW)[0] is True        # 30 s short of 10 minutes: inside the 60 s slack, so it is not skipped to the next tick
    assert jr.due(job, {"j": {"last_run": NOW - 600 + 90}}, NOW)[0] is False       # 90 s short: still too soon
    assert jr.due(job, {"j": {"last_run": NOW - 600 - 5}}, NOW)[0] is True
    assert jr.due(job, {}, NOW)[0] is True                                        # never ran


def test_a_tick_stops_starting_new_jobs_once_its_time_budget_is_spent(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    ran = []

    def fake_run(job):
        ran.append(job["id"])
        return 0, 150.0, "ok"                                                      # each job "takes" 150 s

    monkeypatch.setattr(jr, "run_job", fake_run)
    monkeypatch.setattr(jr, "BUDGET_S", 200)
    jobs = [{"id": "j%d" % n, "every_min": 1, "argv": ["x.py"]} for n in range(5)]
    count, bad, _errors, _state = jr._run_once(jobs, {}, NOW, logs)
    assert ran == ["j0", "j1"] and count == 2                                      # 150 s spent after j0 (not over 200), 300 s after j1 (over): j2 never starts


def test_a_forced_single_job_ignores_the_budget(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(jr, "run_job", lambda job: (0, 9999.0, "ok"))
    jobs = [{"id": "a", "every_min": 1, "argv": ["x.py"]}]
    assert jr._run_once(jobs, {}, NOW, logs, only="a", force=True)[0] == 1


def test_the_assistant_folder_is_searched_before_the_runner_folder(tmp_path, monkeypatch):
    root, here = tmp_path / "root", tmp_path / "here"
    (root / "scripts").mkdir(parents=True)
    here.mkdir()
    (root / "scripts" / "same.py").write_text("")
    (here / "scripts").mkdir()
    (here / "scripts" / "same.py").write_text("")                                  # the same relative name exists in both places
    monkeypatch.setattr(jr, "ROOT", root)
    monkeypatch.setattr(jr, "HERE", here)
    assert jr._script_path("scripts/same.py") == str(root / "scripts" / "same.py")

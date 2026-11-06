# -*- coding: utf-8 -*-
"""Tests for the loop lock: the pure core, then the file layer in a tmp folder."""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loop_lock as ll  # noqa: E402

T0 = 1_793_000_000.0


def test_free_lock_is_acquired_and_holds_until_expiry():
    ok, st = ll.acquire(None, "a", 60, now=T0)
    assert ok and st["holder"] == "a" and st["until"] == T0 + 60
    assert ll.is_held(st, now=T0 + 59) and not ll.is_held(st, now=T0 + 61)


def test_someone_else_cannot_take_a_live_lock_but_can_take_an_expired_one():
    _, st = ll.acquire(None, "a", 60, now=T0)
    ok, same = ll.acquire(st, "b", 60, now=T0 + 10)
    assert not ok and same == st
    ok, new = ll.acquire(st, "b", 60, now=T0 + 61)
    assert ok and new["holder"] == "b"


def test_the_holder_can_renew_and_keeps_its_since_time():
    _, st = ll.acquire(None, "a", 60, now=T0)
    ok, renewed = ll.acquire(st, "a", 60, now=T0 + 30)
    assert ok and renewed["until"] == T0 + 90 and renewed["since"] == T0


def test_only_the_holder_releases():
    _, st = ll.acquire(None, "a", 60, now=T0)
    assert ll.release(st, "b") == st
    assert ll.release(st, "a") is None
    assert ll.release(None, "a") is None


def test_file_layer_blocks_the_second_actor_then_frees_on_release(tmp_path):
    assert ll.try_acquire("intents", "live", 60, logs=str(tmp_path))
    assert not ll.try_acquire("intents", "scheduled", 60, logs=str(tmp_path))
    assert not ll.release_file("intents", "scheduled", logs=str(tmp_path))
    assert ll.release_file("intents", "live", logs=str(tmp_path))
    assert ll.try_acquire("intents", "scheduled", 60, logs=str(tmp_path))


def test_file_layer_takes_over_an_expired_lock(tmp_path):
    assert ll.try_acquire("v", "dead", 1, logs=str(tmp_path), now=T0)
    assert not ll.try_acquire("v", "live", 60, logs=str(tmp_path), now=T0 + 0.5)
    assert ll.try_acquire("v", "live", 60, logs=str(tmp_path), now=T0 + 5)


def test_context_manager_releases_and_raises_when_held(tmp_path):
    with ll.locked("n", "a", ttl=60, logs=str(tmp_path)):
        with pytest.raises(ll.LockHeld):
            with ll.locked("n", "b", ttl=60, wait=0, logs=str(tmp_path)):
                pass
    assert ll.try_acquire("n", "b", 60, logs=str(tmp_path))


def test_racing_threads_only_one_wins(tmp_path):
    wins = []

    def go(who):
        if ll.try_acquire("race", who, 60, logs=str(tmp_path)):
            wins.append(who)

    ts = [threading.Thread(target=go, args=("t%d" % i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(wins) == 1


def test_expired_takeover_has_one_winner_even_with_many_takers(tmp_path):
    assert ll.try_acquire("e", "dead", 1, logs=str(tmp_path), now=T0)
    wins = []

    def go(who):
        if ll.try_acquire("e", who, 60, logs=str(tmp_path), now=T0 + 10):
            wins.append(who)

    ts = [threading.Thread(target=go, args=("t%d" % i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(wins) == 1


def test_an_empty_fresh_lock_file_counts_as_held_not_free(tmp_path):
    p = ll._path("m", str(tmp_path))
    os.makedirs(os.path.dirname(p))
    open(p, "w").close()                                  # a creator that has not written its state yet
    assert ll.try_acquire("m", "other", 60, logs=str(tmp_path)) is False
    old = os.path.getmtime(p) - 60
    os.utime(p, (old, old))                               # an old empty file is junk and can be taken
    assert ll.try_acquire("m", "other", 60, logs=str(tmp_path)) is True


def test_two_holders_with_the_same_prefix_but_different_pids_do_not_share(tmp_path):
    assert ll.try_acquire("run", "scheduled-111", 60, logs=str(tmp_path))
    assert not ll.try_acquire("run", "scheduled-222", 60, logs=str(tmp_path))
    assert not ll.release_file("run", "scheduled-222", logs=str(tmp_path))
    assert ll.release_file("run", "scheduled-111", logs=str(tmp_path))

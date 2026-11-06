# -*- coding: utf-8 -*-
"""Tests for discord_typing.py (TypingKeeper). Only fake channels are used; nothing connects to Discord."""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from discord_typing import TypingKeeper  # noqa: E402


class FakeTypingContext:
    def __init__(self, channel):
        self.channel = channel

    async def __aenter__(self):
        self.channel.entered = True
        self.channel.enter_count += 1
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.channel.exited = True
        self.channel.exit_count += 1
        return False


class FakeChannel:
    """Records how often the typing context was entered and exited."""

    def __init__(self, channel_id):
        self.id = channel_id
        self.entered = False
        self.exited = False
        self.enter_count = 0
        self.exit_count = 0

    def typing(self):
        return FakeTypingContext(self)


class RaisingTypingContext:
    async def __aenter__(self):
        raise RuntimeError("typing enter failed")

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


class RaisingChannel:
    def __init__(self, channel_id):
        self.id = channel_id

    def typing(self):
        return RaisingTypingContext()


def test_enter_then_exit_recorded_on_stop():
    async def run():
        log_calls = []
        keeper = TypingKeeper(log=log_calls.append)
        channel = FakeChannel(1)
        assert keeper.start(channel, seconds=10) is True
        await asyncio.sleep(0.05)
        assert keeper.stop(1) is True
        await asyncio.sleep(0.05)
        assert (channel.entered, channel.exited) == (True, True)
        assert channel.enter_count == 1 and channel.exit_count == 1

    asyncio.run(run())


def test_two_starts_same_channel_enter_once_and_extend_the_deadline():
    async def run():
        keeper = TypingKeeper(max_seconds=10, poll=0.1)
        channel = FakeChannel(1)
        assert keeper.start(channel, seconds=5) is True
        await asyncio.sleep(0.05)
        assert channel.enter_count == 1
        assert keeper.start(channel, seconds=10) is True
        await asyncio.sleep(0.05)
        assert channel.enter_count == 1
        keeper.stop(1)
        await asyncio.sleep(0.05)
        assert channel.exit_count == 1

    asyncio.run(run())


def test_auto_stop_after_max_seconds():
    async def run():
        keeper = TypingKeeper(max_seconds=0.05, poll=0.01)
        channel = FakeChannel(1)
        assert keeper.start(channel) is True
        await asyncio.sleep(0.2)
        assert channel.entered is True and channel.exited is True
        assert 1 not in keeper.active()

    asyncio.run(run())


def test_off_switch_file_present_start_returns_false(tmp_path):
    async def run():
        off_switch = tmp_path / "off.txt"
        off_switch.write_text("off")
        keeper = TypingKeeper(off_switch=off_switch)
        channel = FakeChannel(1)
        assert keeper.start(channel, seconds=10) is False
        assert channel.enter_count == 0 and keeper.active() == set()

    asyncio.run(run())


def test_off_switch_absent_allows_start(tmp_path):
    async def run():
        keeper = TypingKeeper(off_switch=tmp_path / "off.txt")
        channel = FakeChannel(1)
        assert keeper.start(channel, seconds=10) is True
        await asyncio.sleep(0.05)
        assert channel.entered is True
        keeper.stop(1)
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_off_switch_that_cannot_be_checked_is_logged_and_start_returns_false():
    class Broken:
        def exists(self):
            raise OSError("permission denied")

    async def run():
        logged = []
        keeper = TypingKeeper(off_switch=Broken(), log=logged.append)
        assert keeper.start(FakeChannel(1)) is False
        assert logged and "off-switch check failed" in logged[0]

    asyncio.run(run())


def test_typing_raising_on_enter_does_not_raise_out_of_start_and_is_logged():
    async def run():
        log_calls = []
        keeper = TypingKeeper(log=log_calls.append)
        assert keeper.start(RaisingChannel(1), seconds=10) is True
        await asyncio.sleep(0.05)
        assert keeper.active() == set()
        assert log_calls and "Typing task error" in log_calls[0]

    asyncio.run(run())


def test_a_channel_without_an_id_is_refused_quietly():
    async def run():
        logged = []
        keeper = TypingKeeper(log=logged.append)
        assert keeper.start(object()) is False
        assert logged and "needs a channel with an id" in logged[0]

    asyncio.run(run())


def test_stop_unknown_channel_returns_false():
    assert TypingKeeper().stop(999) is False


def test_two_channels_are_independent():
    async def run():
        keeper = TypingKeeper(max_seconds=10, poll=0.1)
        one, two = FakeChannel(1), FakeChannel(2)
        keeper.start(one, seconds=10)
        keeper.start(two, seconds=10)
        await asyncio.sleep(0.05)
        assert keeper.active() == {1, 2}
        keeper.stop(1)
        await asyncio.sleep(0.05)
        assert one.exited is True and two.exited is False
        assert keeper.active() == {2}
        keeper.stop(2)
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_aclose_exits_every_context():
    async def run():
        keeper = TypingKeeper(max_seconds=10, poll=0.1)
        one, two = FakeChannel(1), FakeChannel(2)
        keeper.start(one, seconds=10)
        keeper.start(two, seconds=10)
        await asyncio.sleep(0.05)
        await keeper.aclose()
        assert one.exited is True and two.exited is True
        assert keeper.active() == set()

    asyncio.run(run())


def test_start_with_no_running_loop_returns_false():
    channel = FakeChannel(1)
    assert TypingKeeper().start(channel, seconds=10) is False
    assert channel.entered is False


def test_start_again_after_stop_enters_a_second_time():
    async def run():
        keeper = TypingKeeper(max_seconds=10, poll=0.1)
        channel = FakeChannel(1)
        keeper.start(channel, seconds=10)
        await asyncio.sleep(0.05)
        keeper.stop(1)
        await asyncio.sleep(0.05)
        assert channel.exit_count == 1
        keeper.start(channel, seconds=10)
        await asyncio.sleep(0.05)
        assert channel.enter_count == 2
        keeper.stop(1)
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_seconds_argument_overrides_max_seconds():
    async def run():
        keeper = TypingKeeper(max_seconds=100, poll=0.01)
        channel = FakeChannel(1)
        keeper.start(channel, seconds=0.05)
        await asyncio.sleep(0.2)
        assert channel.exited is True and 1 not in keeper.active()

    asyncio.run(run())


def test_stop_right_after_start_before_the_task_runs_leaves_no_entry():
    async def run():
        keeper = TypingKeeper(max_seconds=10, poll=0.1)
        channel = FakeChannel(1)
        assert keeper.start(channel, seconds=10) is True
        assert keeper.stop(1) is True
        await asyncio.sleep(0.05)
        assert 1 not in keeper.active()
        if channel.entered:
            assert channel.exited is True

    asyncio.run(run())


def test_restart_right_after_stop_before_first_run_still_types():
    async def run():
        keeper = TypingKeeper(max_seconds=10, poll=0.01)
        channel = FakeChannel(1)
        keeper.start(channel)
        assert keeper.stop(1) is True            # cancelled before the task ever ran
        assert keeper.start(channel) is True
        await asyncio.sleep(0.05)
        assert channel.enter_count == 1
        assert keeper.active() == {1}
        keeper.stop(1)
        await asyncio.sleep(0.05)
        assert channel.exit_count == 1

    asyncio.run(run())


def test_old_task_finishing_late_does_not_clear_the_newer_task():
    async def run():
        keeper = TypingKeeper(max_seconds=10, poll=0.01)
        channel = FakeChannel(1)
        keeper.start(channel)
        await asyncio.sleep(0.05)
        keeper.stop(1)
        keeper.start(channel)                    # registered before the old task has processed its cancellation
        await asyncio.sleep(0.05)
        assert keeper.active() == {1}
        assert channel.enter_count == 2
        keeper.start(channel)                    # one live task: extends, does not enter a third time
        await asyncio.sleep(0.05)
        assert channel.enter_count == 2
        keeper.stop(1)
        await asyncio.sleep(0.05)
        assert channel.exit_count == 2 and keeper.active() == set()

    asyncio.run(run())


def test_second_stop_is_false_and_stop_after_expiry_is_false():
    async def run():
        keeper = TypingKeeper(max_seconds=0.03, poll=0.01)
        channel = FakeChannel(1)
        keeper.start(channel)
        await asyncio.sleep(0.02)
        assert keeper.stop(1) is True
        assert keeper.stop(1) is False
        keeper.start(channel)
        await asyncio.sleep(0.15)                # expires by itself
        assert keeper.stop(1) is False
        assert channel.exit_count == 2

    asyncio.run(run())


def test_deadline_extension_keeps_typing_past_the_first_deadline():
    async def run():
        keeper = TypingKeeper(max_seconds=0.1, poll=0.01)
        channel = FakeChannel(1)
        keeper.start(channel)
        await asyncio.sleep(0.07)
        keeper.start(channel)                    # pushes the deadline out
        await asyncio.sleep(0.07)                # past the original deadline
        assert channel.exit_count == 0 and keeper.active() == {1}
        await asyncio.sleep(0.2)
        assert channel.exit_count == 1

    asyncio.run(run())


def test_injected_clock_decides_the_deadline():
    async def run():
        now = [0.0]
        keeper = TypingKeeper(max_seconds=5, poll=0.01, clock=lambda: now[0])
        channel = FakeChannel(1)
        keeper.start(channel)
        await asyncio.sleep(0.05)
        assert keeper.active() == {1}
        now[0] = 6.0                             # the fake clock passes the deadline
        await asyncio.sleep(0.05)
        assert channel.exit_count == 1 and keeper.active() == set()

    asyncio.run(run())


def test_active_reflects_start_and_stop():
    async def run():
        keeper = TypingKeeper(max_seconds=10, poll=0.1)
        assert keeper.active() == set()
        keeper.start(FakeChannel(1), seconds=10)
        await asyncio.sleep(0.05)
        assert keeper.active() == {1}
        keeper.start(FakeChannel(2), seconds=10)
        await asyncio.sleep(0.05)
        assert keeper.active() == {1, 2}
        keeper.stop(1)
        keeper.stop(2)
        await asyncio.sleep(0.05)
        assert keeper.active() == set()

    asyncio.run(run())

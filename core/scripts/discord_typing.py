# -*- coding: utf-8 -*-
"""Discord typing indicator keeper (part of the OPTIONAL Discord chat channel).

Shows the "typing..." indicator while a slow reply is being prepared, so a long silence followed by a block of text feels more natural.
discord_daemon.py starts it when the owner's DM arrives and stops it right before a reply is sent (a send clears the indicator on Discord's side, and a task that kept pinging would bring it back).
One asyncio task per channel holds `async with channel.typing():` (discord.py posts the indicator once on enter and re-posts every 5 s in the background); the task ends by itself at the deadline,
so an indicator can never outlive max_seconds. Kill switch: pass an off_switch path (the daemon passes logs/discord_typing_off.txt); while that file exists start() does nothing.
Pure of any discord import: it only needs a channel object with an `id` and a `typing()` async context manager, so it is tested with fake channels.
    keeper = TypingKeeper(off_switch=Path("logs/discord_typing_off.txt"), max_seconds=120)
    keeper.start(channel)      # inside a running event loop; True when a task is running for that channel
    keeper.stop(channel.id)    # right before sending the reply
Never raises out of start() or stop(). A task cancelled before it ever ran leaves no dead entry, and a late-finishing old task only clears the entry that is still its own."""

import asyncio
import time
from pathlib import Path
from typing import Callable, Optional, Set


class TypingKeeper:
    """Shows the Discord 'typing...' indicator while a slow reply is being prepared."""

    def __init__(
        self,
        off_switch: Optional[Path] = None,
        max_seconds: float = 120.0,
        poll: float = 0.25,
        log: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.off_switch = off_switch
        self.max_seconds = max_seconds
        self.poll = poll
        self.log = log if log is not None else lambda _: None
        self.clock = clock
        self._tasks: dict[int, asyncio.Task] = {}
        self._deadlines: dict[int, float] = {}

    def start(self, channel, seconds: Optional[float] = None) -> bool:
        """Start (or extend) the typing indicator for a channel.

        Returns False if the off_switch file exists, no event loop is running, or on any error."""
        try:
            if self.off_switch is not None and self.off_switch.exists():
                return False
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        except OSError as e:
            self.log(f"typing off-switch check failed: {e}")
            return False

        try:
            channel_id = channel.id
        except Exception as e:
            self.log(f"typing needs a channel with an id: {e}")
            return False
        deadline = self.clock() + (seconds if seconds is not None else self.max_seconds)

        # A live task for this channel only gets its deadline pushed out; a finished or cancelled one does not count.
        existing = self._tasks.get(channel_id)
        if existing is not None and not existing.done():
            self._deadlines[channel_id] = deadline
            return True

        async def typing_task():
            me = asyncio.current_task()
            try:
                async with channel.typing():
                    while self.clock() < self._deadlines.get(channel_id, 0):
                        await asyncio.sleep(self.poll)
            except Exception as e:
                self.log(f"Typing task error for channel {channel_id}: {e}")
            finally:
                if self._tasks.get(channel_id) is me:  # stop() or a newer start may have replaced our entry already
                    self._tasks.pop(channel_id, None)
                    self._deadlines.pop(channel_id, None)

        try:
            self._tasks[channel_id] = loop.create_task(typing_task())
            self._deadlines[channel_id] = deadline
            return True
        except Exception as e:
            self.log(f"Failed to start typing task for channel {channel_id}: {e}")
            return False

    def stop(self, channel_id: int) -> bool:
        """Stop the indicator for a channel. True if a live task was running, else False. Never raises."""
        task = self._tasks.pop(channel_id, None)
        self._deadlines.pop(channel_id, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def active(self) -> Set[int]:
        """Channel ids that currently have a live task."""
        for cid in [c for c, t in self._tasks.items() if t.done()]:
            self._tasks.pop(cid, None)
            self._deadlines.pop(cid, None)
        return set(self._tasks)

    async def aclose(self) -> None:
        """Cancel every task and wait until each typing context has exited."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._deadlines.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

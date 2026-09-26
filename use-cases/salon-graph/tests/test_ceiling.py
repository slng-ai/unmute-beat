"""Stage B4: the call ceiling belongs to its call, and its owner always stops it.

`core.processor.Ceiling`, as `bot.py` owns it, with a controlled clock (an injected `sleep` that waits on an
event the test releases), so nothing here waits for real time:

* C1: a call that ends before its ceiling, however it ends (normal end, the
  caller's disconnect, a cancellation, an exception), leaves no timer, and a
  ceiling released afterwards never hangs anybody up;
* C2: a call that reaches its ceiling hangs up once;
* C3: two calls on one bot: stopping one call's ceiling, or reaching it,
  never touches the other's.

Before the repair, the ceiling was cancelled only on a client disconnect, so
a call the agent ended left its timer running (session 4b stage A, the
shared-bot log lines that all fired at one deadline).
"""

import asyncio
import contextlib

from core.processor import Ceiling


class Clock:
    """An injected `sleep`: it returns only when the test says time is up."""

    def __init__(self) -> None:
        self.up = asyncio.Event()
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await self.up.wait()


def ceiling(clock: Clock, hung_up: list[str], who: str) -> Ceiling:
    async def reached() -> None:
        hung_up.append(who)

    return Ceiling(300, reached, sleep=clock.sleep)


async def owned_run(ending: str, clock: Clock, hung_up: list[str]) -> None:
    """The owner's shape in `bot.run_bot`: start on connect, stop in `finally`, whatever ends the call."""
    limit = ceiling(clock, hung_up, ending)
    limit.start()
    await asyncio.wait_for(clock.sleeping.wait(), 1)
    try:
        if ending == 'exception':
            raise RuntimeError('the pipeline failed')
        if ending == 'cancelled':
            raise asyncio.CancelledError
        if ending == 'disconnect':
            await limit.stop()
    finally:
        await limit.stop()


async def ends_before_the_ceiling():
    """C1."""
    for ending in ('normal', 'disconnect', 'cancelled', 'exception'):
        clock, hung_up = Clock(), []
        with contextlib.suppress(RuntimeError, asyncio.CancelledError):
            await owned_run(ending, clock, hung_up)
        clock.up.set()
        await asyncio.sleep(0.01)
        assert hung_up == [], (ending, 'a ceiling fired after its call had ended')
        assert not [t for t in asyncio.all_tasks() if 'Ceiling._run' in repr(t.get_coro())], ending


async def reaches_the_ceiling():
    """C2."""
    clock, hung_up = Clock(), []
    limit = ceiling(clock, hung_up, 'long call')
    limit.start()
    limit.start()
    clock.up.set()
    await asyncio.sleep(0.01)
    await limit.stop()
    assert hung_up == ['long call'], hung_up


async def one_call_never_ends_another():
    """C3."""
    first_clock, second_clock, hung_up = Clock(), Clock(), []
    first = ceiling(first_clock, hung_up, 'first')
    second = ceiling(second_clock, hung_up, 'second')
    first.start()
    second.start()
    await asyncio.sleep(0)
    await first.stop()
    second_clock.up.set()
    await asyncio.sleep(0.01)
    assert hung_up == ['second'], hung_up
    first_clock.up.set()
    await asyncio.sleep(0.01)
    assert hung_up == ['second'], ('the stopped call hung up later', hung_up)


async def main() -> None:
    await ends_before_the_ceiling()
    await reaches_the_ceiling()
    await one_call_never_ends_another()
    print('ceiling ok: owned, stopped on every ending, one per call')


if __name__ == '__main__':
    asyncio.run(main())

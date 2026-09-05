"""Cancellation-safe scheduling for shared transcription resources."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum, auto


class TranscriptionResource(Enum):
    """A single-capacity resource used by the transcription pipeline."""

    INFERENCE = "inference"
    GIGAAM = "gigaam"


class _WaiterState(Enum):
    PENDING = auto()
    ASSIGNED = auto()
    ACTIVE = auto()
    CANCELLED = auto()
    RELEASED = auto()


@dataclass(eq=False)
class _Waiter:
    choices: tuple[TranscriptionResource, ...]
    future: asyncio.Future[TranscriptionResource]
    state: _WaiterState = _WaiterState.PENDING
    resource: TranscriptionResource | None = None


class TranscriptionScheduler:
    """Assign transcription resources to eligible requests in FIFO order."""

    def __init__(self) -> None:
        self._mutex = asyncio.Lock()
        self._waiters: deque[_Waiter] = deque()
        self._owners: dict[TranscriptionResource, _Waiter] = {}

    @asynccontextmanager
    async def reserve(
        self,
        choices: Sequence[TranscriptionResource],
        *,
        on_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[TranscriptionResource]:
        """Reserve the preferred available resource until the context exits."""
        normalized_choices = tuple(dict.fromkeys(choices))
        if not normalized_choices:
            raise ValueError("At least one transcription resource must be requested.")

        loop = asyncio.get_running_loop()
        waiter = _Waiter(normalized_choices, loop.create_future())
        async with self._mutex:
            self._waiters.append(waiter)
            self._dispatch_locked()
            initially_waiting = waiter.state is _WaiterState.PENDING

        try:
            if initially_waiting and on_wait is not None:
                await on_wait()
            resource = await asyncio.shield(waiter.future)
        except BaseException:
            await self._finish_uninterruptibly(waiter, cancelled=True)
            raise

        # There is no suspension point between receiving the assignment and
        # marking it active. Cancellation before this point is handled above;
        # cancellation after it is handled by the context manager's finally.
        waiter.state = _WaiterState.ACTIVE
        try:
            yield resource
        finally:
            await self._finish_uninterruptibly(waiter, cancelled=False)

    async def _finish_uninterruptibly(
        self, waiter: _Waiter, *, cancelled: bool
    ) -> None:
        cleanup = asyncio.create_task(self._finish(waiter, cancelled=cancelled))
        cancelled_during_cleanup = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled_during_cleanup = True
                continue
        cleanup.result()
        if cancelled_during_cleanup:
            raise asyncio.CancelledError

    async def _finish(self, waiter: _Waiter, *, cancelled: bool) -> None:
        async with self._mutex:
            if waiter.state is _WaiterState.PENDING:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                waiter.future.cancel()
            elif (
                waiter.resource is not None
                and self._owners.get(waiter.resource) is waiter
            ):
                del self._owners[waiter.resource]

            waiter.state = (
                _WaiterState.CANCELLED if cancelled else _WaiterState.RELEASED
            )
            self._dispatch_locked()

    def _dispatch_locked(self) -> None:
        for waiter in tuple(self._waiters):
            resource = next(
                (choice for choice in waiter.choices if choice not in self._owners),
                None,
            )
            if resource is None:
                continue

            self._waiters.remove(waiter)
            waiter.resource = resource
            waiter.state = _WaiterState.ASSIGNED
            self._owners[resource] = waiter
            waiter.future.set_result(resource)

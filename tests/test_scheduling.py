import asyncio
import unittest

from vorec.scheduling import TranscriptionResource, TranscriptionScheduler


INFERENCE = TranscriptionResource.INFERENCE
GIGAAM = TranscriptionResource.GIGAAM


class TranscriptionSchedulerTests(unittest.TestCase):
    def test_two_recordings_swap_resources_without_overlapping_their_asr(self) -> None:
        async def scenario() -> None:
            scheduler = TranscriptionScheduler()
            started: asyncio.Queue[tuple[str, TranscriptionResource]] = asyncio.Queue()
            releases = {
                (recording, resource): asyncio.Event()
                for recording in ("A", "B")
                for resource in (INFERENCE, GIGAAM)
            }

            async def recording(name: str) -> None:
                remaining = (INFERENCE, GIGAAM)
                for _ in range(2):
                    async with scheduler.reserve(remaining) as resource:
                        await started.put((name, resource))
                        await releases[name, resource].wait()
                    remaining = tuple(item for item in remaining if item is not resource)

            first = asyncio.create_task(recording("A"))
            self.assertEqual(await started.get(), ("A", INFERENCE))
            second = asyncio.create_task(recording("B"))
            self.assertEqual(await started.get(), ("B", GIGAAM))

            releases["A", INFERENCE].set()
            await asyncio.sleep(0)
            self.assertTrue(started.empty())

            releases["B", GIGAAM].set()
            swapped = {await started.get(), await started.get()}
            self.assertEqual(swapped, {("A", GIGAAM), ("B", INFERENCE)})

            releases["A", GIGAAM].set()
            releases["B", INFERENCE].set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

        asyncio.run(scenario())

    def test_fifo_skips_requests_that_cannot_use_the_free_resource(self) -> None:
        async def scenario() -> None:
            scheduler = TranscriptionScheduler()
            entered: asyncio.Queue[tuple[str, TranscriptionResource]] = asyncio.Queue()
            releases = {name: asyncio.Event() for name in ("giga", "flexible", "inference")}

            inference_context = scheduler.reserve((INFERENCE,))
            gigaam_context = scheduler.reserve((GIGAAM,))
            await inference_context.__aenter__()
            await gigaam_context.__aenter__()

            async def request(
                name: str, choices: tuple[TranscriptionResource, ...]
            ) -> None:
                async with scheduler.reserve(choices) as resource:
                    await entered.put((name, resource))
                    await releases[name].wait()

            oldest = asyncio.create_task(request("giga", (GIGAAM,)))
            flexible = asyncio.create_task(
                request("flexible", (INFERENCE, GIGAAM))
            )
            inference = asyncio.create_task(request("inference", (INFERENCE,)))
            await asyncio.sleep(0)

            await inference_context.__aexit__(None, None, None)
            self.assertEqual(await entered.get(), ("flexible", INFERENCE))
            await gigaam_context.__aexit__(None, None, None)
            self.assertEqual(await entered.get(), ("giga", GIGAAM))

            releases["flexible"].set()
            self.assertEqual(await entered.get(), ("inference", INFERENCE))
            releases["giga"].set()
            releases["inference"].set()
            await asyncio.wait_for(
                asyncio.gather(oldest, flexible, inference), timeout=1
            )

        asyncio.run(scenario())

    def test_cancelling_active_owner_releases_resource_for_next_waiter(self) -> None:
        async def scenario() -> None:
            scheduler = TranscriptionScheduler()
            owner_entered = asyncio.Event()
            next_entered = asyncio.Event()

            async def owner() -> None:
                async with scheduler.reserve((INFERENCE,)):
                    owner_entered.set()
                    await asyncio.Event().wait()

            async def successor() -> None:
                async with scheduler.reserve((INFERENCE,)):
                    next_entered.set()

            owner_task = asyncio.create_task(owner())
            await owner_entered.wait()
            successor_task = asyncio.create_task(successor())
            await asyncio.sleep(0)
            owner_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(owner_task, timeout=1)
            await asyncio.wait_for(next_entered.wait(), timeout=1)
            await asyncio.wait_for(successor_task, timeout=1)

        asyncio.run(scenario())

    def test_cancellation_after_flexible_assignment_returns_capacity(self) -> None:
        asyncio.run(self._cancel_during_handoff((INFERENCE, GIGAAM), INFERENCE))

    def test_cancellation_after_fixed_assignment_returns_capacity(self) -> None:
        asyncio.run(self._cancel_during_handoff((GIGAAM,), GIGAAM))

    async def _cancel_during_handoff(
        self,
        choices: tuple[TranscriptionResource, ...],
        released_resource: TranscriptionResource,
    ) -> None:
        scheduler = TranscriptionScheduler()
        holder_started = asyncio.Event()
        release_holder = asyncio.Event()
        target_entered = asyncio.Event()
        successor_entered = asyncio.Event()

        async def hold(resource: TranscriptionResource) -> None:
            async with scheduler.reserve((resource,)):
                holder_started.set()
                await release_holder.wait()

        holders = [asyncio.create_task(hold(released_resource))]
        other_resource = (
            GIGAAM if released_resource is INFERENCE else INFERENCE
        )
        if other_resource in choices:
            other_started = asyncio.Event()
            other_release = asyncio.Event()

            async def hold_other() -> None:
                async with scheduler.reserve((other_resource,)):
                    other_started.set()
                    await other_release.wait()

            holders.append(asyncio.create_task(hold_other()))
            await other_started.wait()
        await holder_started.wait()

        async def target() -> None:
            async with scheduler.reserve(choices):
                target_entered.set()

        async def successor() -> None:
            async with scheduler.reserve((released_resource,)):
                successor_entered.set()

        target_task = asyncio.create_task(target())
        successor_task = asyncio.create_task(successor())
        await asyncio.sleep(0)

        original_dispatch = scheduler._dispatch_locked
        cancellation_armed = True

        def cancel_after_assignment() -> None:
            nonlocal cancellation_armed
            before = scheduler._owners.get(released_resource)
            original_dispatch()
            after = scheduler._owners.get(released_resource)
            if cancellation_armed and before is None and after is not None:
                cancellation_armed = False
                target_task.cancel()

        scheduler._dispatch_locked = cancel_after_assignment
        release_holder.set()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(target_task, timeout=1)
        self.assertFalse(target_entered.is_set())
        await asyncio.wait_for(successor_entered.wait(), timeout=1)
        await asyncio.wait_for(successor_task, timeout=1)

        if other_resource in choices:
            other_release.set()
        await asyncio.wait_for(asyncio.gather(*holders), timeout=1)
        self.assertEqual(scheduler._owners, {})
        self.assertEqual(len(scheduler._waiters), 0)


if __name__ == "__main__":
    unittest.main()

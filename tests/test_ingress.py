"""Synthetic thread-to-loop advertisement pressure; no radio or network."""
import asyncio
import threading

import pytest

from linptech_ble._ingress import AdvertisementIngress


async def settled(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def test_twenty_thousand_foreign_thread_packets_use_one_wakeup_and_bounded_storage():
    async def run():
        loop = asyncio.get_running_loop()
        values = []
        mailbox = AdvertisementIngress(loop, values.append, capacity=8)
        before = len(loop._ready)

        def burst():
            for value in range(20000):
                mailbox.put(value)

        producer = threading.Thread(target=burst)
        producer.start()
        # Deliberately keep the consumer loop busy while the producer floods it.
        producer.join()
        assert len(loop._ready) - before == 1
        before_dispatch = mailbox.snapshot()
        assert before_dispatch["queued"] == 8 and before_dispatch["dropped"] == 19992
        assert before_dispatch["received"] == 20000 and before_dispatch["delivered"] == 0
        await settled(lambda: len(values) == 8)
        assert values == list(range(19992, 20000))
        assert mailbox.snapshot()["queued"] == 0
        mailbox.close()

    asyncio.run(run())


def test_multiple_producers_cannot_overfill_or_create_per_packet_handles():
    async def run():
        loop = asyncio.get_running_loop()
        values = []
        mailbox = AdvertisementIngress(loop, values.append, capacity=32)
        before = len(loop._ready)

        def burst(base):
            for value in range(5000):
                mailbox.put(base + value)

        producers = [threading.Thread(target=burst, args=(index * 5000,)) for index in range(4)]
        for producer in producers:
            producer.start()
        for producer in producers:
            producer.join()
        assert len(loop._ready) - before == 1
        status = mailbox.snapshot()
        assert status["received"] == 20000 and status["queued"] == 32 and status["dropped"] == 19968
        await settled(lambda: len(values) == 32)
        mailbox.close()

    asyncio.run(run())


def test_drain_yields_after_sixty_four_advertisements():
    async def run():
        loop = asyncio.get_running_loop()
        values, observed = [], []
        mailbox = AdvertisementIngress(loop, values.append, capacity=256)
        for value in range(200):
            mailbox.put(value)
        loop.call_soon(lambda: observed.append(len(values)))
        await settled(lambda: len(values) == 200)
        assert observed == [64] and values == list(range(200))
        mailbox.close()

    asyncio.run(run())


def test_close_discards_pending_packets_and_blocks_late_delivery():
    async def run():
        values = []
        mailbox = AdvertisementIngress(asyncio.get_running_loop(), values.append, capacity=8)
        for value in range(5):
            mailbox.put(value)
        mailbox.close()
        mailbox.put("late")
        await asyncio.sleep(0)
        assert values == []
        assert mailbox.snapshot() == {"capacity": 8, "queued": 0, "closed": True, "wakeup_failed": False,
            "received": 5, "delivered": 0, "dropped": 0, "discarded_on_close": 5, "callback_errors": 0}
        mailbox.close()

    asyncio.run(run())


@pytest.mark.parametrize("error", [RuntimeError("private-payload"), asyncio.CancelledError()])
def test_callback_failure_does_not_stall_future_mailbox_delivery(error):
    async def run():
        values = []

        def receive(value):
            if value == 0:
                raise error
            values.append(value)

        mailbox = AdvertisementIngress(asyncio.get_running_loop(), receive, capacity=8)
        mailbox.put(0)
        mailbox.put(1)
        await settled(lambda: values == [1])
        assert mailbox.snapshot()["callback_errors"] == 1
        assert "private-payload" not in str(mailbox.snapshot())
        mailbox.close()

    asyncio.run(run())


def test_callback_may_put_or_close_without_lock_reentrancy_deadlock():
    async def run():
        values = []

        def receive(value):
            values.append(value)
            if value < 3:
                mailbox.put(value + 1)
            else:
                mailbox.close()

        mailbox = AdvertisementIngress(asyncio.get_running_loop(), receive, capacity=8)
        mailbox.put(0)
        await settled(lambda: mailbox.snapshot()["closed"])
        assert values == [0, 1, 2, 3]

    asyncio.run(run())


def test_closed_event_loop_failure_is_visible_without_echoing_packet():
    loop = asyncio.new_event_loop()
    loop.close()
    mailbox = AdvertisementIngress(loop, lambda *_: None, capacity=8)
    mailbox.put("private-payload")
    status = mailbox.snapshot()
    assert status["wakeup_failed"] and status["closed"] and status["queued"] == 0
    assert status["discarded_on_close"] == 1 and "private-payload" not in str(status)

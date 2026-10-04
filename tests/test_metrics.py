"""进程内指标：计数器、仪表、直方图与周期汇总日志。"""

import asyncio
import logging
import threading

import pytest

from core import metrics


@pytest.fixture
def registry():
    return metrics.MetricsRegistry()


def test_the_same_name_and_labels_return_the_same_metric(registry):
    first = registry.counter("provider.failures", provider="gemini")
    second = registry.counter("provider.failures", provider="gemini")
    other = registry.counter("provider.failures", provider="openai")

    first.inc()
    second.inc(2)

    assert first is second
    assert first.value == 3
    assert other.value == 0


def test_labels_are_order_independent(registry):
    registry.counter("x", a=1, b=2).inc()
    registry.counter("x", b=2, a=1).inc()

    assert registry.snapshot().counter("x", a=1, b=2) == 2


def test_gauges_move_both_ways(registry):
    depth = registry.gauge("admission.queued")

    depth.inc()
    depth.inc()
    depth.dec()
    assert registry.snapshot().gauge("admission.queued") == 1
    depth.set(7)
    assert registry.snapshot().gauge("admission.queued") == 7


def test_counters_are_thread_safe(registry):
    counter = registry.counter("calls")

    def hammer():
        for _ in range(2000):
            counter.inc()

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert counter.value == 16000


def test_histogram_percentiles_come_from_the_buckets(registry):
    latency = registry.histogram("turn.seconds")
    for value in [0.02] * 90 + [8.0] * 10:
        latency.observe(value)

    snapshot = registry.snapshot().histogram("turn.seconds")

    assert snapshot is not None
    assert snapshot.count == 100
    assert snapshot.mean == pytest.approx((0.02 * 90 + 8.0 * 10) / 100)
    assert snapshot.percentile(0.5) <= 0.025
    assert 5.0 <= snapshot.percentile(0.99) <= 8.0
    assert snapshot.maximum == 8.0


def test_percentile_never_exceeds_the_observed_maximum(registry):
    histogram = registry.histogram("one")
    histogram.observe(0.3)

    snapshot = registry.snapshot().histogram("one")

    assert snapshot is not None
    assert snapshot.percentile(0.99) <= 0.3
    assert registry.snapshot().histogram("never-recorded") is None


def test_interval_view_only_contains_new_samples(registry):
    histogram = registry.histogram("queue.seconds")
    for _ in range(5):
        histogram.observe(0.01)
    first = registry.snapshot(reset_window=True)
    for _ in range(3):
        histogram.observe(2.0)
    second = registry.snapshot(reset_window=True)

    interval = second.histograms[("queue.seconds", ())].minus(first.histograms[("queue.seconds", ())])

    assert interval.count == 3
    assert interval.percentile(0.5) > 1.0
    assert interval.maximum == 2.0


def test_summary_line_reports_changes_since_the_previous_snapshot(registry):
    registry.counter("admission.rejected", reason="queue_timeout").inc(2)
    registry.histogram("admission.queue_seconds").observe(0.4)
    registry.gauge("admission.queued").set(3)
    before = registry.snapshot(reset_window=True)

    registry.counter("admission.rejected", reason="queue_timeout").inc(1)
    registry.counter("turn.finished", status="completed").inc(4)
    registry.histogram("turn.run_seconds").observe(1.5)
    after = registry.snapshot(reset_window=True)

    line = metrics.format_summary(after, before, interval_seconds=300)

    assert "admission.rejected{reason=queue_timeout}=1" in line
    assert "turn.finished{status=completed}=4" in line
    assert "turn.run_seconds[n=1" in line
    assert "admission.queue_seconds[" not in line  # 这一段没有新样本
    assert "admission.queued=3" in line
    assert line.startswith("runtime metrics (last 300s):")


def test_summary_line_says_idle_when_nothing_happened(registry):
    snapshot = registry.snapshot()

    assert metrics.format_summary(snapshot, snapshot, interval_seconds=60).endswith("idle")


def test_reporter_logs_a_line_per_interval_and_a_final_one_on_cancel(registry, caplog):
    reporter = metrics.MetricsReporter(0.05, registry=registry, log=logging.getLogger("test.metrics"))

    async def scenario():
        task = asyncio.create_task(reporter.run())
        await asyncio.sleep(0.02)
        registry.counter("turn.finished", status="completed").inc()
        await asyncio.sleep(0.12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.INFO, logger="test.metrics"):
        asyncio.run(scenario())

    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) >= 2
    assert any("turn.finished{status=completed}=1" in line for line in lines)
    assert all(line.startswith("runtime metrics") for line in lines)

"""进程内的轻量指标：计数器、仪表与直方图，只用标准库。

用法与含义见 docs/runtime.md 的「指标」。要点：

- 指标按「名字 + 标签」取用，第一次取用时创建，之后拿到同一个对象；标签的取值必须是有限集合
  （provider 名、工具名、拒绝原因），不要把用户 id 之类放进标签。
- 直方图用固定的桶，只记计数、总和、最大值，分位数由桶估算（线性插值），不保留每一个样本。
- `MetricsReporter` 周期性把「这一段时间的变化」写成一行日志；`snapshot()` 返回当前累计值。
- 线程安全：阻塞适配器的工作线程也会更新指标。
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 秒；覆盖从毫秒级的排队到十分钟级的整轮耗时。
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)

LabelKey = tuple[tuple[str, str], ...]
MetricKey = tuple[str, LabelKey]


def _label_key(labels: Mapping[str, object]) -> LabelKey:
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


def format_key(key: MetricKey) -> str:
    name, labels = key
    if not labels:
        return name
    return name + "{" + ",".join(f"{label}={value}" for label, value in labels) + "}"


class Counter:
    """只增不减的计数。"""

    __slots__ = ("_lock", "_value")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def inc(self, amount: int = 1) -> None:
        with self._lock:
            self._value += amount

    @property
    def value(self) -> int:
        return self._value


class Gauge:
    """可增可减的当前值（排队深度、在途数量）。"""

    __slots__ = ("_lock", "_value")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0.0

    def set(self, value: float) -> None:
        with self._lock:
            self._value = float(value)

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    def dec(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value -= amount

    @property
    def value(self) -> float:
        return self._value


@dataclass(frozen=True, slots=True)
class HistogramSnapshot:
    bounds: tuple[float, ...]
    counts: tuple[int, ...]  # len(bounds) + 1，最后一格是超过最大边界的样本
    count: int
    total: float
    # 自上一次「重置窗口的快照」以来的最大值；没有重置过就是累计最大值。
    maximum: float

    def percentile(self, quantile: float) -> float:
        """按桶估算分位数：在命中的桶里线性插值，上限是实际观察到的最大值。"""
        if self.count <= 0:
            return 0.0
        target = max(1.0, quantile * self.count)
        seen = 0
        for index, bucket_count in enumerate(self.counts):
            if bucket_count == 0:
                continue
            if seen + bucket_count >= target:
                lower = self.bounds[index - 1] if index > 0 else 0.0
                upper = self.bounds[index] if index < len(self.bounds) else self.maximum
                upper = min(upper, self.maximum) if self.maximum > 0 else upper
                lower = min(lower, upper)
                fraction = (target - seen) / bucket_count
                return lower + (upper - lower) * fraction
            seen += bucket_count
        return self.maximum

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    def minus(self, previous: HistogramSnapshot | None) -> HistogramSnapshot:
        """两次快照之间新增的样本；最大值沿用本次快照的窗口最大值。"""
        if previous is None or previous.bounds != self.bounds:
            return self
        return HistogramSnapshot(
            bounds=self.bounds,
            counts=tuple(
                max(current - old, 0) for current, old in zip(self.counts, previous.counts)
            ),
            count=max(self.count - previous.count, 0),
            total=max(self.total - previous.total, 0.0),
            maximum=self.maximum,
        )


class Histogram:
    """固定桶的直方图。"""

    __slots__ = ("_bounds", "_counts", "_count", "_lock", "_maximum", "_total", "_window_maximum")

    def __init__(self, bounds: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._bounds = tuple(sorted(bounds))
        self._counts = [0] * (len(self._bounds) + 1)
        self._count = 0
        self._total = 0.0
        self._maximum = 0.0
        self._window_maximum = 0.0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        value = max(float(value), 0.0)
        index = bisect.bisect_left(self._bounds, value)
        with self._lock:
            self._counts[index] += 1
            self._count += 1
            self._total += value
            if value > self._maximum:
                self._maximum = value
            if value > self._window_maximum:
                self._window_maximum = value

    def snapshot(self, *, reset_window: bool = False) -> HistogramSnapshot:
        with self._lock:
            snapshot = HistogramSnapshot(
                bounds=self._bounds,
                counts=tuple(self._counts),
                count=self._count,
                total=self._total,
                maximum=self._window_maximum if reset_window else self._maximum,
            )
            if reset_window:
                self._window_maximum = 0.0
        return snapshot


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    counters: Mapping[MetricKey, int] = field(default_factory=dict)
    gauges: Mapping[MetricKey, float] = field(default_factory=dict)
    histograms: Mapping[MetricKey, HistogramSnapshot] = field(default_factory=dict)

    def counter(self, name: str, **labels: object) -> int:
        return self.counters.get((name, _label_key(labels)), 0)

    def gauge(self, name: str, **labels: object) -> float:
        return self.gauges.get((name, _label_key(labels)), 0.0)

    def histogram(self, name: str, **labels: object) -> HistogramSnapshot | None:
        return self.histograms.get((name, _label_key(labels)))


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[MetricKey, Counter] = {}
        self._gauges: dict[MetricKey, Gauge] = {}
        self._histograms: dict[MetricKey, Histogram] = {}

    def counter(self, name: str, **labels: object) -> Counter:
        key = (name, _label_key(labels))
        with self._lock:
            metric = self._counters.get(key)
            if metric is None:
                metric = self._counters[key] = Counter()
        return metric

    def gauge(self, name: str, **labels: object) -> Gauge:
        key = (name, _label_key(labels))
        with self._lock:
            metric = self._gauges.get(key)
            if metric is None:
                metric = self._gauges[key] = Gauge()
        return metric

    def histogram(
        self,
        name: str,
        *,
        buckets: tuple[float, ...] | None = None,
        **labels: object,
    ) -> Histogram:
        key = (name, _label_key(labels))
        with self._lock:
            metric = self._histograms.get(key)
            if metric is None:
                metric = self._histograms[key] = Histogram(buckets or DEFAULT_BUCKETS)
        return metric

    def snapshot(self, *, reset_window: bool = False) -> MetricsSnapshot:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = dict(self._histograms)
        return MetricsSnapshot(
            counters={key: metric.value for key, metric in counters.items()},
            gauges={key: metric.value for key, metric in gauges.items()},
            histograms={
                key: metric.snapshot(reset_window=reset_window)
                for key, metric in histograms.items()
            },
        )

    def reset(self) -> None:
        """清空所有指标。只给测试用。"""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


# 进程唯一的注册表。
REGISTRY = MetricsRegistry()


def counter(name: str, **labels: object) -> Counter:
    return REGISTRY.counter(name, **labels)


def gauge(name: str, **labels: object) -> Gauge:
    return REGISTRY.gauge(name, **labels)


def histogram(name: str, **labels: Any) -> Histogram:
    return REGISTRY.histogram(name, **labels)


def snapshot(*, reset_window: bool = False) -> MetricsSnapshot:
    return REGISTRY.snapshot(reset_window=reset_window)


def format_summary(
    current: MetricsSnapshot,
    previous: MetricsSnapshot | None,
    *,
    interval_seconds: float,
) -> str:
    """一行汇总：计数器与直方图是这一段时间的变化，仪表是当前值。没有任何变化时只写 idle。"""
    parts: list[str] = []

    counter_parts = []
    for key in sorted(current.counters):
        delta = current.counters[key] - (previous.counters.get(key, 0) if previous else 0)
        if delta:
            counter_parts.append(f"{format_key(key)}={delta}")
    parts.extend(counter_parts)

    histogram_parts = []
    for key in sorted(current.histograms):
        delta_snapshot = current.histograms[key].minus(
            previous.histograms.get(key) if previous else None
        )
        if delta_snapshot.count:
            histogram_parts.append(
                f"{format_key(key)}[n={delta_snapshot.count}"
                f" p50={delta_snapshot.percentile(0.5):.3f}"
                f" p95={delta_snapshot.percentile(0.95):.3f}"
                f" max={delta_snapshot.maximum:.3f}]"
            )
    parts.extend(histogram_parts)

    gauge_parts = [
        f"{format_key(key)}={value:g}"
        for key, value in sorted(current.gauges.items())
        if value
    ]
    parts.extend(gauge_parts)

    body = " ".join(parts) if parts else "idle"
    return f"runtime metrics (last {interval_seconds:g}s): {body}"


class MetricsReporter:
    """周期性地把指标变化写成一行 INFO 日志。取消时再写一行，让最后一段不丢。"""

    def __init__(
        self,
        interval_seconds: float,
        *,
        registry: MetricsRegistry = REGISTRY,
        log: logging.Logger = logger,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.registry = registry
        self.log = log
        self._previous: MetricsSnapshot | None = None

    def report(self) -> str:
        current = self.registry.snapshot(reset_window=True)
        line = format_summary(current, self._previous, interval_seconds=self.interval_seconds)
        self._previous = current
        self.log.info(line)
        return line

    async def run(self) -> None:
        self._previous = self.registry.snapshot(reset_window=True)
        try:
            while True:
                await asyncio.sleep(self.interval_seconds)
                self.report()
        except asyncio.CancelledError:
            self.report()
            raise

from __future__ import annotations

from daneion.controller.fitcheck import MIN_GAIN, Node, decide
from daneion.workloads.base import Estimate

WIFI = 40e6 / 8  # 40 Mbit/s hotspot


def laptop(name: str, local: bool = False, slots: int = 8, rate: float = 7.0,
           source: str = "observed") -> Node:
    return Node(name=name, is_local=local, slots=slots, rate_per_slot=rate, rate_source=source,
                bandwidth_bps=None if local else WIFI, rtt_s=None if local else 0.008)


FRACTAL = Estimate(work_units=1800, task_count=160, bytes_in=96_000, bytes_out=4.6e6,
                   quality=0.85)
PHOTOS = Estimate(work_units=330, task_count=20, bytes_in=160e6, bytes_out=16e6, quality=0.9)


def test_alone_runs_locally() -> None:
    d = decide(FRACTAL, [laptop("me", local=True)])
    assert d.placement == "local" and "No other laptop" in d.reason


def test_cpu_heavy_job_is_distributed_with_a_real_gain() -> None:
    d = decide(FRACTAL, [laptop("me", local=True), laptop("victus"), laptop("omen")])
    assert d.placement == "distributed", d.reason
    assert d.predicted_gain is not None and d.predicted_gain > 0.5
    assert d.predicted_local_s and d.predicted_distributed_s
    assert d.predicted_distributed_s < d.predicted_local_s
    assert abs(sum(n["share"] for n in d.nodes) - 1) < 1e-3


def test_transfer_heavy_job_stays_local() -> None:
    d = decide(PHOTOS, [laptop("me", local=True, rate=60), laptop("victus", rate=60)])
    assert d.placement == "local"
    assert "moving the data" in d.reason


def test_tiny_job_stays_local() -> None:
    tiny = Estimate(work_units=10, task_count=6, bytes_in=0, bytes_out=1e4, quality=0.9)
    d = decide(tiny, [laptop("me", local=True), laptop("victus")])
    assert d.placement == "local" and "Too small" in d.reason


def test_slow_extra_laptop_below_threshold_stays_local() -> None:
    d = decide(FRACTAL, [laptop("me", local=True, slots=16), laptop("old", slots=1, rate=2)])
    assert d.placement == "local"
    assert d.predicted_gain is not None and d.predicted_gain < MIN_GAIN


def test_unknown_speeds_mean_low_confidence() -> None:
    d = decide(FRACTAL, [laptop("me", local=True, source="default"),
                         laptop("victus", source="default")])
    assert d.placement == "local" and "confidence" in d.reason


def test_explicit_mode_overrides_but_keeps_the_advice() -> None:
    d = decide(PHOTOS, [laptop("me", local=True, rate=60), laptop("victus", rate=60)],
               mode="distributed")
    assert d.placement == "distributed" and "override" in d.reason
    assert d.reasons[0].startswith("FitCheck would choose local")


def test_unmeasured_network_lowers_confidence() -> None:
    nodes = [laptop("me", local=True), laptop("victus")]
    nodes[1].bandwidth_bps = None
    measured = decide(FRACTAL, [laptop("me", local=True), laptop("victus")])
    assert decide(FRACTAL, nodes).confidence < measured.confidence

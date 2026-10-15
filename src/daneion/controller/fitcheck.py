"""FitCheck v0: should this job run on this laptop alone, or across the cluster? (ADR-0006)

Deterministic and explainable. It predicts both wall times with one cost model

    time = startup + compute + transfer + per-task round trips + tail + risk

and distributes only when the predicted gain is at least MIN_GAIN *and* the confidence in the
prediction is at least MIN_CONFIDENCE. Otherwise the honest answer is "run it locally".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from daneion.workloads.base import Estimate

MIN_GAIN = 0.15
MIN_CONFIDENCE = 0.60
MIN_LOCAL_S = 3.0  # below this, coordination overhead eats any gain
# Calibration runs on one idle core at boost clock; with every core busy a core delivers less.
DEFAULT_PARALLEL_EFFICIENCY = 0.65
WIFI_RISK = 0.10  # extra margin on distributed time for Wi-Fi variance
STARTUP_LOCAL_S = 0.3
STARTUP_DIST_S = 0.8
ROUND_TRIPS_PER_TASK = 4  # lease, accept, start, result
# Starting a fresh Python process per task (isolation) and importing the handler. Calibration
# does not see this; speeds learned from real jobs already include it.
TASK_STARTUP_S = 0.35
DEFAULT_RTT_S = 0.010
SOURCE_CONFIDENCE = {"observed": 1.0, "calibrated": 0.85, "default": 0.5}


@dataclass
class Node:
    name: str
    is_local: bool
    slots: int
    rate_per_slot: float | None  # work units / s on one busy slot
    rate_source: str  # observed | calibrated | default
    bandwidth_bps: float | None = None  # measured link to the controller (bytes/s)
    rtt_s: float | None = None
    healthy: bool = True
    effective_per_slot: float = 0.0  # set by decide(): rate after per-task startup cost

    @property
    def rate(self) -> float:
        return (self.effective_per_slot or self.rate_per_slot or 0.0) * self.slots


@dataclass
class Decision:
    placement: str
    reason: str
    reasons: list[str] = field(default_factory=list)
    predicted_local_s: float | None = None
    predicted_distributed_s: float | None = None
    predicted_gain: float | None = None
    confidence: float = 0.0
    nodes: list[dict[str, Any]] = field(default_factory=list)
    estimate: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _fmt(s: float) -> str:
    return f"{s:.1f}s" if s < 120 else f"{s / 60:.1f} min"


def decide(est: Estimate, nodes: list[Node], mode: str = "auto") -> Decision:
    local = next((n for n in nodes if n.is_local and n.healthy and n.rate_per_slot), None)
    remotes = [n for n in nodes if not n.is_local and n.healthy and n.rate_per_slot]
    reasons: list[str] = []
    per_task = est.work_units / max(1, est.task_count)
    for n in [local, *remotes]:
        if n is None:
            continue
        startup = 0.0 if n.rate_source == "observed" else TASK_STARTUP_S
        n.effective_per_slot = per_task / (per_task / n.rate_per_slot + startup)  # type: ignore[operator]

    t_local = None
    if local:
        tail = per_task / local.effective_per_slot
        t_local = STARTUP_LOCAL_S + est.work_units / local.rate + 0.5 * tail + est.merge_s
        reasons.append(f"This laptop alone: {local.slots} slots -> about {_fmt(t_local)}")

    t_dist = None
    t_transfer = 0.0
    moved = 0.0
    confidence = est.quality
    shares: list[dict[str, Any]] = []
    if remotes:
        pool = ([local] if local else []) + remotes
        total_rate = sum(n.rate for n in pool)
        remote_rate = sum(n.rate for n in remotes)
        remote_share = remote_rate / total_rate
        moved = (est.bytes_in + est.bytes_out) * remote_share
        bws = sorted(n.bandwidth_bps for n in remotes if n.bandwidth_bps)
        link = bws[len(bws) // 2] if bws else None  # remotes share the controller's radio
        t_transfer = moved / link if link else 0.0
        rtt = max((n.rtt_s or DEFAULT_RTT_S) for n in remotes)
        remote_tasks = est.task_count * remote_share
        remote_slots = sum(n.slots for n in remotes)
        t_rtt = remote_tasks * ROUND_TRIPS_PER_TASK * rtt / max(1, remote_slots)
        slowest = min(n.effective_per_slot for n in pool)
        tail = per_task / slowest
        t_compute = est.work_units / total_rate
        t_dist = ((STARTUP_DIST_S + t_compute + t_transfer + t_rtt + 0.5 * tail)
                  * (1 + WIFI_RISK) + est.merge_s)
        reasons.append(f"With {len(remotes)} other laptop(s): compute {_fmt(t_compute)}, "
                       f"moving {moved / 1e6:.1f} MB {_fmt(t_transfer)}, "
                       f"round trips {_fmt(t_rtt)} -> about {_fmt(t_dist)}")
        if link is None:
            confidence *= 0.7
            reasons.append("Network speed not measured yet: lower confidence")
        else:
            reasons.append(f"Measured link {link * 8 / 1e6:.0f} Mbit/s, RTT {rtt * 1000:.0f} ms")
        for n in pool:
            confidence *= SOURCE_CONFIDENCE.get(n.rate_source, 0.5) ** (1 / len(pool))
            shares.append({"name": n.name, "local": n.is_local, "slots": n.slots,
                           "rate": round(n.rate, 3), "source": n.rate_source,
                           "share": round(n.rate / total_rate, 3)})
    elif local:
        confidence *= SOURCE_CONFIDENCE.get(local.rate_source, 0.5)
        shares.append({"name": local.name, "local": True, "slots": local.slots,
                       "rate": round(local.rate, 3), "source": local.rate_source, "share": 1.0})

    gain = (1 - t_dist / t_local) if (t_local and t_dist) else None
    d = Decision(placement="local", reason="", reasons=reasons, predicted_local_s=t_local,
                 predicted_distributed_s=t_dist, predicted_gain=gain,
                 confidence=round(confidence, 3), nodes=shares, estimate=est.as_dict())

    # Decision rules, hard filters first.
    if not remotes:
        d.placement, d.reason = (("distributed", "No local worker; using the cluster")
                                 if not local else
                                 ("local", "No other laptop is online: run here"))
    elif not local:
        d.placement, d.reason = "distributed", "This laptop has no worker; using the others"
    elif (gain is not None and gain < MIN_GAIN and t_dist and t_local
          and t_transfer >= 0.5 * t_local):
        d.placement, d.reason = "local", (
            f"Stay local: moving the data costs more than the extra CPUs save "
            f"({moved / 1e6:.0f} MB takes about {_fmt(t_transfer)}; "
            f"the whole job takes {_fmt(t_local)} here)")
    elif t_local is not None and t_local < MIN_LOCAL_S:
        d.placement, d.reason = "local", (f"Too small to share: about {_fmt(t_local)} here, "
                                          "coordination would cost more than it saves")
    elif gain is not None and gain < MIN_GAIN:
        d.placement, d.reason = "local", (f"Stay local: predicted gain {gain:.0%} is below "
                                          f"the {MIN_GAIN:.0%} threshold")
    elif confidence < MIN_CONFIDENCE:
        d.placement, d.reason = "local", (f"Stay local: predicted gain {gain:.0%} but "
                                          f"confidence {confidence:.0%} is too low")
    else:
        d.placement, d.reason = "distributed", (
            f"Distribute: about {_fmt(t_dist or 0)} instead of {_fmt(t_local or 0)} "
            f"({gain:.0%} faster, confidence {confidence:.0%})")

    if mode in ("local", "distributed") and mode != d.placement:
        d.reasons.insert(0, f"FitCheck would choose {d.placement}: {d.reason}")
        d.placement, d.reason = mode, f"{mode.capitalize()} run requested (override)"
    elif mode in ("local", "distributed"):
        d.reason = f"{mode.capitalize()} run requested; FitCheck agrees. {d.reason}"
    return d

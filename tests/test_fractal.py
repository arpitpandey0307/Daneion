from __future__ import annotations

import hashlib

import pytest

from daneion.workloads import WorkloadError, get_workload
from tests.conftest import SMALL

wl = get_workload("fractal_tiles_v1")


def test_tiles_cover_image_exactly_once() -> None:
    p = wl.validate({"width": 100, "height": 70, "tile": 32})
    covered = set()
    for t in wl.split(p):
        for y in range(t["y0"], t["y0"] + t["th"]):
            for x in range(t["x0"], t["x0"] + t["tw"]):
                assert (x, y) not in covered
                covered.add((x, y))
    assert len(covered) == 100 * 70


def test_run_is_deterministic_and_verifies() -> None:
    task = wl.split(wl.validate(SMALL))[1]
    a, b = wl.run(task), wl.run(task)
    assert hashlib.sha256(a).digest() == hashlib.sha256(b).digest()
    wl.verify(task, a)
    with pytest.raises(WorkloadError):
        wl.verify(task, a[:-2])


@pytest.mark.parametrize("bad", [
    {"width": 10}, {"tile": "32"}, {"max_iter": True}, {"scale": 0}, {"rm -rf": 1},
])
def test_validate_rejects_bad_params(bad: dict) -> None:
    with pytest.raises(WorkloadError):
        wl.validate(bad)


def test_merge_produces_png() -> None:
    p = wl.validate(SMALL)
    png, name = wl.merge(p, [wl.run(t) for t in wl.split(p)])
    assert name.endswith(".png") and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_unknown_workload_is_rejected() -> None:
    with pytest.raises(WorkloadError):
        get_workload("shell")


def test_presets_are_valid() -> None:
    for params in wl.presets.values():
        wl.validate(params)


def test_huge_image_is_rejected() -> None:
    with pytest.raises(WorkloadError, match="too large"):
        wl.validate({"width": 8192, "height": 8192})


def test_palette_is_valid_rgb() -> None:
    from daneion.workloads.fractal import _palette

    pal = _palette()
    assert len(pal) == 256 and all(0 <= c <= 255 for rgb in pal for c in rgb)

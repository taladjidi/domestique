"""The 3D strain-score ingest: ride_storage must be able to call strain_score.

The energy-system breakdown never worked. `ride_storage.compute_ride_xss`
passed `wprime_j=` while `strain_score.compute_xss_components` declares
`w_prime=`, both written on 2026-05-05 in the same feature, so every call
raised TypeError, was swallowed by a bare `except Exception`, and returned {}.
The backfill then reported the ride as `no_power_stream`, which is how a rider
with 1 Hz power on every ride was told their rides had no power.

Two rules, then: the call must fit the signature, and a compute failure must
not be reported as missing data.
"""
from __future__ import annotations

import inspect

import ride_storage as rs
import strain_score


def _ramp(n: int = 600) -> list[float]:
    """A power trace with real variation: 10 min alternating 150/320 W."""
    return [150.0 if (i // 30) % 2 == 0 else 320.0 for i in range(n)]


def test_the_call_fits_the_signature():
    """Pinned by name, so renaming either side without the other fails here
    rather than four months later in a rider's dashboard."""
    params = inspect.signature(strain_score.compute_xss_components).parameters
    assert "w_prime" in params, sorted(params)
    src = inspect.getsource(rs.compute_ride_xss)
    assert "wprime_j=wprime_j" not in src, (
        "compute_ride_xss is passing wprime_j= to a function that takes w_prime=")


def test_a_real_power_trace_produces_components():
    """The end of the chain: a stream in, numbers out. This is what the
    backfill needs and what it never got."""
    out = rs.compute_ride_xss(_ramp(), cp=250, wprime_j=20000, pmax=1000,
                              started_at="2026-09-16T10:00:00")
    assert isinstance(out, dict) and out, "compute_ride_xss returned nothing"
    assert out.get("xss_total"), f"no xss_total in {sorted(out)}"
    for key in ("xss_cp", "xss_w_prime", "xss_pmax"):
        assert key in out, f"{key} missing from {sorted(out)}"

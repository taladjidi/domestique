"""FTP-test detection on rides as they are ridden (2026-10-15).

The owner's history: a 1-min-step ramp inside a 75-min ride (ridden to and
from) was never detected, while eight ordinary rides (a neuromuscular session
ridden long, tempo stretches) were offered as 20-min tests reading 6-45% below
FTP. These shapes are synthetic copies of those rides' profiles.
"""
import power_curve
import fitness_estimation as fe

FTP = 257


def flat(watts, minutes):
    return [watts] * (60 * minutes)


def ramp_inside_a_ride():
    """20 min easy, a ramp 120 -> 320 W in 10 W one-minute steps, collapse,
    30 min riding home."""
    ride = flat(130, 20)
    for w in range(120, 321, 10):
        ride += [w] * 60
    return ride + flat(150, 1) + flat(70, 2) + flat(100, 30)


def hard_finish():
    """20 min steady, then a 4-min climb to 296 W and a stop (09-07)."""
    ride = flat(195, 20) + flat(175, 3)
    for w in (204, 241, 272, 296):
        ride += [w] * 60
    return ride + flat(64, 3)


def neuromuscular_ridden_long():
    """A 20-min tempo stretch with surges and easy minutes in it, after hard
    bursts (10-13)."""
    ride = flat(150, 10) + flat(290, 4) + flat(160, 3)
    block = []
    for i in range(20):
        block += [190 if i in (4, 11) else 262] * 60
    return ride + block + flat(100, 15)


def coggan_test(level):
    """Warm-up, a 5-min blow-out, recovery, 20 min steady at ``level``."""
    return (flat(150, 15) + flat(int(level * 1.15), 5) + flat(120, 10)
            + flat(level, 20) + flat(110, 10))


def test_a_ramp_inside_a_longer_ride_is_found():
    r = fe.evaluate_ftp_test(ramp_inside_a_ride(), prior_ftp=FTP)
    assert r and r["ftp_test_type"] == "ramp"
    assert r["ftp_test_suggestion"]["ftp"] == round(0.75 * 320)


def test_a_hard_finish_is_not_a_ramp():
    assert fe.evaluate_ftp_test(hard_finish(), prior_ftp=FTP) is None


def test_a_ramp_peaking_below_ftp_is_not_a_test():
    ride = flat(100, 10)
    for w in range(100, 241, 10):
        ride += [w] * 60
    assert fe.evaluate_ftp_test(ride + flat(60, 5), prior_ftp=FTP) is None


def test_a_broken_tempo_stretch_is_not_a_20min_test():
    assert fe.evaluate_ftp_test(neuromuscular_ridden_long(), prior_ftp=FTP) is None


def test_an_unnamed_20min_block_well_below_ftp_is_not_a_test():
    assert fe.evaluate_ftp_test(coggan_test(200), prior_ftp=FTP) is None


def test_an_unnamed_near_maximal_20min_test_is_found():
    r = fe.evaluate_ftp_test(coggan_test(265), prior_ftp=FTP)
    assert r and r["ftp_test_type"] == "coggan_20min"


def test_a_named_test_is_scored_whatever_it_reads():
    r = fe.evaluate_ftp_test(
        coggan_test(200), prior_ftp=FTP,
        filename_hint=["ftp_test_coggan_3x1min-1min_95pct_59min.zwo"])
    assert r and r["ftp_test_type"] == "coggan_20min"
    assert r["ftp_test_suggestion"]["ftp"] < 0.92 * FTP


def test_sync_path_counts_seconds_not_samples(monkeypatch):
    """A ride recorded every 2 s: 25 min of ramp ride is 750 samples. On the
    1 Hz grid it is detected; counting samples it was skipped (< 1200)."""
    import config
    monkeypatch.setattr(config, "ATHLETE_FTP_W", FTP, raising=False)
    ride = ramp_inside_a_ride()
    t = list(range(0, len(ride), 2))
    w = [ride[i] for i in t]
    data = {"name": "Afternoon Ride", "started_at": "2026-10-06T14:41:42"}
    power_curve._maybe_detect_ftp_test(data, {"watts": w, "time": t})
    assert data.get("ftp_test_type") == "ramp"


def test_a_build_held_after_its_peak_is_not_a_ramp():
    """A ramp ends in failure; a progressive build that is held after its
    peak minute (here 3 more minutes at 95% of it) is not one."""
    ride = flat(130, 15)
    for w in range(150, 321, 15):
        ride += [w] * 60
    ride += flat(300, 3) + flat(120, 15)
    assert fe.evaluate_ftp_test(ride, prior_ftp=FTP) is None

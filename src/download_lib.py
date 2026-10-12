"""Serving a .zwo: the served workout, the capacity cap, the outdoor wrapper,
the response.

Helpers shared by the download endpoints, both FIT builders and the calendar
push. They are here rather than in the endpoints because `served_zwo_bytes` is
the one place the workout a planned session serves is made (fitted to its
slot, slim/9.1) and `_cap_zwo_bytes` the one place the capacity ceiling is
applied -- the .zwo the athlete downloads and the .fit pushed to the head
unit must be the same workout or the two disagree.

Unlike the other extracted modules this one imports fastapi, because building
the response is what it does.
"""
import collections
import functools
import io
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

from fastapi.responses import FileResponse, Response

import zones

log = logging.getLogger("domestique.download")


def _capacity_cap_active(pm, force: bool = False) -> bool:
    """task #24: True when the measured-capacity short-rep cap should apply to a
    served ZWO/FIT for this profile. Requires a trustworthy MEASURED Pmax
    (pmax_is_set), power target_mode (hr prescriptions are untouched --
    target_mode wins), and the toggle == "on" (or ``force`` for a PROMPT
    APPROVE with ?cap=1). "prompt"/"off" do NOT auto-apply on their own."""
    try:
        if not pm.pmax_is_set:
            return False
        if pm.target_mode == "hr":
            return False
        if force:
            return True
        return pm.cap_short_intervals == "on"
    except Exception:
        return False

def _cap_zwo_bytes(raw: bytes, filename: str, pm) -> bytes:
    """Apply the measured-capacity cap to ZWO bytes, or return them UNCHANGED.

    Gate is the CALLER's responsibility via ``_capacity_cap_active`` -- this
    only does the transform once the caller decided to. Ramp-test exemption is
    handled inside ``cap_zwo_text`` via the ``ftp_test_ramp*`` filename guard
    (the grill-validated ramp marker -- the content classifier has no distinct
    "ramp" primary to add). On any decode hiccup the original bytes are
    returned (never corrupt a download)."""
    import capacity_cap as _cc
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    txt2, n, _ = _cc.cap_zwo_text(
        txt, float(pm.ftp), float(pm.cp), float(pm.pmax_w),
        filename=filename)
    if n == 0:
        return raw  # byte-identical no-op
    return txt2.encode("utf-8")

# The served workout (contract slim/9.1, S1/S2). A planned easy slot keeps the
# length the plan gave it (6.8 / R4a) and the head unit ends a workout at the
# file's last block, so the file served for an easy slot is fitted to the
# slot: one owner for the session's length. Hard slots, and any download that
# is not a planned session's (no slot), are served as the library wrote them.
_EASY_SLOTS = frozenset({"recovery", "z2", "long_z2", "endurance"})
_FIT_FROM_S = 120          # a file within 2 min of its slot is served as is
_MIN_BLOCK_S = 600         # shortening never takes a block below 10 min
_DEFAULT_EASY_POWER = "0.65"

# Files an easy slot could not be fitted to, by file name: the evaluator's
# counter (S2). Logged once per serve as well.
served_unfitted: "collections.Counter[str]" = collections.Counter()


# A length a title or description states ("160min", "(119min)"), not a rep
# or a rest ("8x1min", "-1min"): training_planner._DESC_MIN's rule.
_MIN_TOKEN = re.compile(r"(?<![\d\-–×x~])(\d+)min")


class Fit(NamedTuple):
    data: bytes
    status: str            # "verbatim" | "within" | "fitted" | "unfitted"
    file_min: int = 0      # the library file's own length, rounded


def _restate(text: str, old_min: int, new_min: int) -> str:
    """Every length in ``text`` that is ``old_min`` minutes reads ``new_min``."""
    return _MIN_TOKEN.sub(
        lambda m: f"{new_min}min" if int(m.group(1)) == old_min else m.group(0),
        text)


def restate_minutes(text: str, fit: Fit, slot_min) -> str:
    """S3: a title for a fitted session states the slot's minutes where it
    stated the file's; any other title is returned as it is."""
    if fit.status != "fitted" or not text:
        return text
    return _restate(text, fit.file_min, int(slot_min))


def fit_zwo_to_slot(raw: bytes, slot_min, session_type: str) -> Fit:
    """The library bytes fitted to an easy slot (S2), or the bytes as they are.

    For a recovery / z2 / long_z2 / endurance slot whose file is 2 min or more
    off the slot: the longest SteadyState block in Z1-Z2 is lengthened or
    shortened so the workout's total is the slot to the second; a file with no
    such block and a slot longer than the file gets one SteadyState block,
    before its Cooldown, at the power the file holds longest in Z2 (0.65 FTP
    when none). Shortening never takes the block below 10 min; a file that
    cannot reach the slot that way, or whose edited text does not read back
    as a workout of the slot's length, is "unfitted" and served as it is. Only
    the one block's Duration changes (or the one block is added), and the
    minutes the file's <name>/<description> state for its length (and, when
    the description has one part per block, for that block) are restated
    (S3): every other byte is the library's. Pure: no clock, no profile,
    no file system, so the same session serves the same bytes every time."""
    import zones
    import zwo_parse
    if (session_type or "") not in _EASY_SLOTS:
        return Fit(raw, "verbatim")
    try:
        slot_s = int(slot_min or 0) * 60
    except (TypeError, ValueError):
        return Fit(raw, "verbatim")
    if slot_s <= 0:
        return Fit(raw, "verbatim")
    try:
        text = raw.decode("utf-8")
        root = ET.fromstring(raw)
    except (UnicodeDecodeError, ET.ParseError):
        return Fit(raw, "unfitted")
    workout = root.find("workout")
    facts = zwo_parse.scan_zwo(_NamedBytes(raw)) if workout is not None else None
    if facts is None:
        return Fit(raw, "unfitted")
    total_s = int(facts["total_sec"])
    file_min = int(round(total_s / 60))
    delta = slot_s - total_s
    if abs(delta) < _FIT_FROM_S:
        return Fit(raw, "within", file_min)

    segs = list(workout)
    w_open = text.find("<workout>")
    w_close = text.rfind("</workout>")
    if w_open < 0 or w_close < w_open:
        return Fit(raw, "unfitted", file_min)
    names = "|".join(sorted({re.escape(el.tag) for el in segs})) or "(?!)"
    tags = list(re.finditer(rf"<({names})\b[^>]*>", text[w_open:w_close]))
    if [m.group(1) for m in tags] != [el.tag for el in segs]:
        return Fit(raw, "unfitted", file_min)   # the text and the tree disagree

    z1_top, z2_top = (e / 100.0 for e in zones.power_zone_edges_pct()[:2])

    def _f(el, attr):
        try:
            return float(el.get(attr))
        except (TypeError, ValueError):
            return None

    best = None                                  # (index, seconds)
    for i, el in enumerate(segs):
        p, d = _f(el, "Power"), _f(el, "Duration")
        if el.tag == "SteadyState" and p is not None and d and p < z2_top:
            if best is None or int(d) > best[1]:
                best = (i, int(d))

    # The description, when it is one " | " part per block (the library's
    # generated form), restates the block that moved.
    dm = re.search(r"<description>(.*?)</description>", text, re.S)
    parts = dm.group(1).split(" | ") if dm else None
    if parts is not None and len(parts) != len(segs):
        parts = None
    moved = -1

    if best is not None:
        i, d = best
        new_d = d + delta
        if delta < 0 and new_d < _MIN_BLOCK_S:
            return Fit(raw, "unfitted", file_min)
        m = tags[i]
        a, b = w_open + m.start(), w_open + m.end()
        tag = re.sub(r'\bDuration="[^"]*"', f'Duration="{new_d}"', text[a:b], count=1)
        text = text[:a] + tag + text[b:]
        if parts is not None:
            parts[i] = _MIN_TOKEN.sub(f"{int(round(new_d / 60))}min", parts[i], count=1)
            moved = i
    elif delta > 0:
        power = _main_easy_power(segs, z1_top, z2_top)
        at = len(segs) - 1 if segs and segs[-1].tag == "Cooldown" else len(segs)
        if parts is not None:
            parts.insert(at, f"Steady: {int(round(delta / 60))}min @ "
                             f"{int(round(float(power) * 100))}% FTP")
            moved = at
        block = f'<SteadyState Duration="{delta}" Power="{power}" />'
        if segs and segs[-1].tag == "Cooldown":
            pos = w_open + tags[-1].start()
        else:
            pos = w_close
        line = text.rfind("\n", 0, pos) + 1
        indent = text[line:pos]
        if indent.strip():                       # not on a line of its own
            text = text[:pos] + block + text[pos:]
        elif pos == w_close:                     # last in the workout
            seg_indent = indent + "    "
            if tags:
                t0 = w_open + tags[-1].start()
                seg_indent = text[text.rfind("\n", 0, t0) + 1:t0]
            text = text[:line] + seg_indent + block + "\n" + text[line:]
        else:                                    # before the cooldown
            text = text[:pos] + block + "\n" + indent + text[pos:]
    else:
        return Fit(raw, "unfitted", file_min)

    slot_m = slot_s // 60
    dm = re.search(r"<description>(.*?)</description>", text, re.S)
    if dm:
        desc = (" | ".join(p if k == moved else _restate(p, file_min, slot_m)
                           for k, p in enumerate(parts))
                if parts is not None else _restate(dm.group(1), file_min, slot_m))
        text = text[:dm.start(1)] + desc + text[dm.end(1):]
    nm = re.search(r"<name>(.*?)</name>", text, re.S)
    if nm:
        text = text[:nm.start(1)] + _restate(nm.group(1), file_min, slot_m) + text[nm.end(1):]
    out = text.encode("utf-8")
    # The edit is on text, so read the result back: a "fitted" workout is one
    # that parses and lasts the slot, or the file is served as written.
    try:
        ET.fromstring(out)
        served = zwo_parse.scan_zwo(_NamedBytes(out))
    except ET.ParseError:
        served = None
    if served is None or int(served["total_sec"]) != slot_s:
        return Fit(raw, "unfitted", file_min)
    return Fit(out, "fitted", file_min)


def _main_easy_power(segs, z1_top: float, z2_top: float) -> str:
    """The constant power the file holds longest in Z2 (its own attribute
    text), or 0.65 FTP when it holds none."""
    held: dict = {}
    for el in segs:
        legs = []
        if el.tag == "SteadyState":
            legs = [(el.get("Power"), el.get("Duration"), 1)]
        elif el.tag == "IntervalsT":
            reps = el.get("Repeat") or 1
            legs = [(el.get("OnPower"), el.get("OnDuration"), reps),
                    (el.get("OffPower"), el.get("OffDuration"), reps)]
        for p, d, n in legs:
            try:
                if p is not None and z1_top <= float(p) < z2_top:
                    held[p] = held.get(p, 0) + float(d or 0) * float(n)
            except ValueError:
                pass
    if not held:
        return _DEFAULT_EASY_POWER
    return min(held, key=lambda p: (-held[p], float(p)))


class _NamedBytes(io.BytesIO):
    """Bytes zwo_parse.scan_zwo can read as it reads a path (it names a file
    without a <name> after the path's stem)."""
    stem = "workout"


def served_fit(zwo_path, slot_min, session_type) -> Fit:
    """The library file at ``zwo_path`` fitted to the slot (no capacity cap)."""
    return fit_zwo_to_slot(Path(zwo_path).read_bytes(), slot_min, session_type)


def served_minutes(zwo_path, slot_min, session_type) -> "int | None":
    """The minutes a planned session's workout lasts as served: the slot's
    when the file is fitted to it, None when it is served as the library
    wrote it (the caller states the file's own length). What the dashboard
    states for a session's workout, so it says what the head unit gets."""
    path = Path(zwo_path)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    return _served_minutes(str(path), mtime, slot_min, session_type or "")


def served_zone_minutes(zwo_path, slot_min, session_type) -> "dict | None":
    """Minutes per zone of a planned session's workout as served (fitted to
    the slot, no capacity cap), by time in zone (zones.tiz_minutes, contract
    slim/9.3 amendment 2). None when the file cannot be read."""
    path = Path(zwo_path)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    m = _served_zone_minutes(str(path), mtime, slot_min, session_type or "")
    return dict(m) if m is not None else None


@functools.lru_cache(maxsize=2048)
def _served_zone_minutes(path: str, mtime: int, slot_min, session_type: str):
    try:
        return zones.tiz_minutes(served_fit(path, slot_min, session_type).data)
    except (ET.ParseError, OSError, ValueError, TypeError):
        return None


# ponytail: keyed on the file's mtime, unbounded by profile; per-profile
# invalidation if a library edit ever keeps its mtime.
@functools.lru_cache(maxsize=1024)
def _served_minutes(path: str, mtime: int, slot_min, session_type: str):
    if session_type not in _EASY_SLOTS:
        return None
    if served_fit(path, slot_min, session_type).status != "fitted":
        return None
    return int(slot_min)


def served_zwo_bytes(zwo_file, slot_min, session_type, pm, *, cap=None) -> bytes:
    """THE workout a planned session serves (S1): the library bytes fitted to
    the slot (``fit_zwo_to_slot``), then the measured-capacity cap exactly as
    before. The calendar push, the ZWO download and the FIT export all build
    from here. ``zwo_file`` is the resolved path of the library file;
    ``slot_min`` None (a library download, a library push) serves the file
    unfitted. ``cap``: None applies the profile's own cap rule
    (``_capacity_cap_active``); True/False is the caller's decision (a
    PROMPT APPROVE download, an hr FIT that is never capped)."""
    path = Path(zwo_file)
    fit = served_fit(path, slot_min, session_type)
    if fit.status == "unfitted":
        served_unfitted[path.name] += 1
        log.warning("served workout: %s cannot be fitted to a %s-min %s slot "
                    "(file %s min); served as written",
                    path.name, slot_min, session_type, fit.file_min)
    out = fit.data
    if cap is None:
        cap = pm is not None and _capacity_cap_active(pm)
    if cap and pm is not None:
        out = _cap_zwo_bytes(out, path.name, pm)
    return out


def _wrap_zwo_outdoor(xml_text: str, transit_min: int, spin_min: int) -> str:
    """G1 (v2.1) — frame a prescribed indoor block inside a real OUTDOOR ride:
    prepend a flat transit warmup and append a spin-home cooldown. The prescribed
    body is passed through UNCHANGED (string-level insert, not re-serialized, so
    the middle is byte-identical). Export-only: this is the download path, so it
    never touches the planner's accounted/weekly TSS — the transit + spin-home are
    OFF-PLAN additional easy minutes by construction."""
    t_s = max(0, int(transit_min)) * 60
    s_s = max(0, int(spin_min)) * 60
    out = xml_text
    if t_s and "<workout>" in out:
        out = out.replace(
            "<workout>",
            f'<workout>\n        <Warmup Duration="{t_s}" PowerLow="0.40" '
            f'PowerHigh="0.60"/>  <!-- G1 transit to climb (off-plan) -->', 1)
    if s_s and "</workout>" in out:
        out = out.replace(
            "</workout>",
            f'        <Cooldown Duration="{s_s}" PowerLow="0.50" PowerHigh="0.40"/>'
            f'  <!-- G1 spin home (off-plan) -->\n    </workout>', 1)
    return out

def _zwo_download_response(path, filename: str, outdoor: int,
                          transit_min: int, spin_min: int,
                          cap_active: bool = False,
                          slot_min=None, session_type: str = ""):
    """Shared ZWO download: plain FileResponse, or the served workout.

    The body is the served workout (``served_zwo_bytes``): fitted to
    ``slot_min`` for a planned easy session (slim/9.1), capped to the rider's
    measured-power envelope when ``cap_active`` (task #24, short reps only),
    and (G1) wrapped for an outdoor ride when ``outdoor``. A library download
    with none of these is the plain FileResponse, byte-identical to disk."""
    plain = FileResponse(
        path, filename=filename, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    if not (outdoor or slot_min or cap_active):
        return plain
    try:
        from profile_manager import ProfileManager
        body = served_zwo_bytes(path, slot_min, session_type,
                                ProfileManager.get() if cap_active else None,
                                cap=cap_active)
    except Exception:
        return plain
    served = Response(
        body, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    if not outdoor:
        return served
    try:
        wrapped = _wrap_zwo_outdoor(body.decode("utf-8"), transit_min, spin_min)
    except Exception:
        return served
    out_name = filename.rsplit(".", 1)[0] + "_outdoor.zwo"
    return Response(
        wrapped, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'})

def _cap_active_for_download(cap: int) -> bool:
    """task #24: resolve the ZWO/FIT serve cap for the active profile.
    ``cap=1`` (PROMPT APPROVE) forces the cap; otherwise the "on" toggle. Both
    still require pmax_is_set + power mode (``_capacity_cap_active``)."""
    try:
        from profile_manager import ProfileManager
        return _capacity_cap_active(ProfileManager.get(), force=bool(cap))
    except Exception:
        return False

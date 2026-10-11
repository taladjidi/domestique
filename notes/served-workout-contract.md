# Served-workout contract (slim/9.1, 2026-10-11)

Branch `slim/9.1-served-file` off production 433c6067. Owner's report: the
Domestique plan and the intervals.icu calendar do not match; "some Z2 rides
are under 3 hrs".

## What was measured (read-only, owner's live data, 2026-10-11)

`~/Documents/dq-audit-probes/plan-vs-icu/diff.py` and `slot_vs_file.py`:
the stored plan, `/api/calendar` and the intervals.icu events agree on every
day's type and file. They disagree on length and load. The week of 10-19:
slots 664 min / 535 TSS (exactly its target), the files pushed to
intervals.icu (and from there to the Bryton) 614 min / 488 TSS (-9 %).
Tue 10-14 180 vs 170, Mon 10-19 177 vs 160, Wed 10-21 175 vs 160,
Fri 10-23 177 vs 160.

Cause: contract 6.8 / R4a keep the slot's length and assume "the rider rides
an easy file on past its end". The push, the download and the FIT export
serve the library file verbatim (`icu_calendar_push._build_event` reads the
bytes; `download_lib._cap_zwo_bytes` is the one transform), so the head unit
ends the workout at the file's length. One fact (the session's length) has
two owners: the slot and the file.

## Assertions

**S1 One owner for the served workout.** One function,
`download_lib.served_zwo_bytes(zwo_file, slot_min, session_type, pm)`,
produces the workout a planned session serves: the library bytes, fitted to
the slot (S2), then the measured-capacity cap exactly as today. The calendar
push (power mode), the ZWO download of a planned session and the FIT export
(power and hr views) all build from it. A static test fails if any of them
reads a planned session's workout bytes another way.

**S2 Fitted to the slot, for easy slots.** For a session whose type is
recovery, z2, long_z2 or endurance (the slots R4a covers), when the file's
length differs from the slot by 2 min or more:
- the longest steady block whose power is in Z1-Z2 is lengthened or
  shortened so the workout's total equals the slot to the minute;
- a file with no such block and a slot longer than the file gets one steady
  block at the file's own main easy power (0.65 FTP when it has none)
  inserted before its cooldown, for the missing minutes;
- shortening never takes a block below 10 min and never removes or reshapes
  any other block; a file that cannot reach the slot that way is served as
  it is and reported (log line + a counter the evaluator can read).
Hard slots (tempo and above, ftp_test, races) are served exactly as today.

**S3 Every surface says what is served.** For a fitted session, the stored
`duration_min` equals the served length, `tss_estimate` is the served
workout's load (within the existing tolerance), and the minutes in the pushed
event's name and in the ZWO's own name/description equal the slot's.

**S4 Deterministic and idempotent.** The same session gives byte-identical
served bytes on every call; a second calendar reconcile after a push reports
0 changes.

**S5 Nothing else moves.** The characterization golden changes only where a
served file's length differs from its easy slot by 2 min or more, and only in
the served-length / load fields; hard slots, rest days, weekly targets and
the plan's structure are unchanged. Every changed case is listed in the
evaluation.

**S6 On the owner's data (sandbox copy).** Every intervals.icu event the push
would send in the 14-day window has a length equal to its stored slot
(+/- 1 min); the week of 10-19 serves 664 min.

**W1 Wellness: an empty reading never erases a stored one, in either store**
(port of platypus45/domestique#30's 326c05e5). One rule
(`ride_storage.wellness_value`) for the SQLite upsert and the day's JSON
file; `persist_wellness` merges over the stored day; 0 is empty for hrv,
restingHR, sleepSecs, sleepScore and the other readings that cannot be 0.
Production's existing manual-HRV guard in the SQLite upsert stays as it is.
Why now: intervals.icu has said it will likely stop passing Garmin data
through its API (platypus45/domestique#29), and the owner's HRV, resting HR
and sleep come from a Garmin band; today a sync with those fields empty
replaces 90 days of JSON wellness files.

**C1 Check, change nothing unless a rule is broken.** The 10-26 recovery week
serves `pyramid_1x2min_90pct_32min` (IF 0.69) and
`endurance_8x1min-1min_85pct_120min` (IF 0.68) in z2 slots and
`threshold_1x2min_81pct_46min_v3` (IF 0.62) in a recovery slot. Report each
against the slot's ceiling in `_within_slot` and against contract 6.2 / M3.

## Out of scope
The 14-day push horizon (by design); hard slots shorter or longer than their
file; the intervals.icu-moves read-back (#28); slim/10.

## Process
Implementer commits after every item, each with a test that fails without it
(mutation result in the message). Full gate (`UV_OFFLINE=1`, inside
`unshare -rn`) and characterization with every golden change read before
`--bless`. Then an independent evaluator on a sandbox copy of the owner's
data (S6, C1), then the owner's deploy decision.

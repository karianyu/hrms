# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import Case, Interval
from frappe.query_builder.terms import SubQuery
from frappe.utils import get_link_to_form, getdate

from hrms.hr.utils import validate_bulk_tool_fields


# ---------------------------------------------------------------------------
# Constants — all overridable per-engine-instance
# ---------------------------------------------------------------------------

# Maximum consecutive days an employee may work without a rest day.
# After this many days in a row they are forced to take at least one day off.
MAX_STREAK_DAYS      = 6

# Target number of working days per ISO week per employee.  The engine tries
# to reach this floor for everyone before handing surplus days to employees
# who already meet it.  Acts as the "healthy working days" target.
TARGET_DAYS_PER_WEEK = 5

MIN_REST_HOURS       = 11.0     # minimum hours of rest required between consecutive shifts
MAX_WEEKLY_HOURS     = 48.0     # healthy-hours ceiling per ISO week (EU WTD default)
MAX_SAME_SHIFT_DAYS  = 2        # max consecutive days on the same shift type before rotation


# ---------------------------------------------------------------------------
# Shift timing helpers
# ---------------------------------------------------------------------------

def _duration_hours(start_time, end_time) -> float:
    """
    Return the duration of a shift in fractional hours.

    ``start_time`` and ``end_time`` are ``datetime.timedelta`` objects as
    returned by Frappe (seconds since midnight).  Overnight shifts whose
    ``end_time < start_time`` are handled by adding 24 hours to ``end_time``
    before computing the difference.
    """
    start_s = start_time.total_seconds()
    end_s   = end_time.total_seconds()
    if end_s <= start_s:        # overnight shift crosses midnight
        end_s += 86_400
    return (end_s - start_s) / 3600


def _end_offset_hours(start_time, end_time) -> float:
    """
    Hours after midnight of the *start* day at which the shift ends.
    For an overnight shift this value exceeds 24.

    Example: start=22:00, end=06:00 → 30.0  (06:00 the following day)
    """
    start_s = start_time.total_seconds()
    end_s   = end_time.total_seconds()
    if end_s <= start_s:
        end_s += 86_400
    return end_s / 3600


def _start_offset_hours(start_time) -> float:
    """Hours after midnight at which the shift starts."""
    return start_time.total_seconds() / 3600


# ---------------------------------------------------------------------------
# ShiftProfile — pre-computed, immutable timing descriptor
# ---------------------------------------------------------------------------

class ShiftProfile:
    """
    Immutable timing metadata for one Shift Type, fetched once at engine
    construction and reused throughout the scheduling loop.

    Attributes
    ----------
    name          Shift Type name (database key).
    start_h       Start time in hours-after-midnight  (e.g. 6.0 for 06:00).
    end_h         End time in hours-after-midnight on the *start day's* clock
                  (e.g. 30.0 for an overnight shift ending at 06:00 next day).
    duration_h    Shift duration in fractional hours.
    is_overnight  True when the shift crosses midnight.
    """

    __slots__ = ("name", "start_h", "end_h", "duration_h", "is_overnight")

    def __init__(self, name: str, start_time, end_time):
        self.name        = name
        self.start_h     = _start_offset_hours(start_time)
        self.end_h       = _end_offset_hours(start_time, end_time)
        self.duration_h  = _duration_hours(start_time, end_time)
        self.is_overnight = end_time < start_time

    def rest_gap_hours(self, next_profile: "ShiftProfile") -> float:
        """
        Hours of rest between the end of *this* shift and the start of
        ``next_profile`` on the *following* calendar day.

        Both times are placed on the same 48-hour clock:
        - ``self.end_h``                       hours after this shift's start-day midnight
        - ``next_profile.start_h + 24``        hours after the same reference midnight
          (adding 24 because the next shift is one calendar day later)

        A positive return value means the employee has had that many hours of
        rest.  A value below ``min_rest_hours`` means they haven't recovered
        enough before the next assignment.
        """
        return (next_profile.start_h + 24.0) - self.end_h


# ---------------------------------------------------------------------------
# RosterEngine
# ---------------------------------------------------------------------------

class RosterEngine:
    """
    Builds an evenly-distributed, time-aware shift roster for a pool of
    employees over a date range, enforcing:

    Leave exclusion
        Employees on approved or pending leave are skipped for those dates.

    Double-booking prevention
        Existing active shift assignments are not duplicated (unless HR
        Settings enables multiple shifts *and* timings do not overlap).

    Minimum rest between shifts
        At least ``min_rest_hours`` (default 11 h, per EU Working Time
        Directive) must separate the end of an employee's last shift from
        the start of the next proposed shift.  This prevents scheduling
        a 07:00 Morning shift the morning after a 22:00–06:00 Night shift.

    Weekly hours budget
        Each employee's accumulated hours within an ISO calendar week must
        not exceed ``max_weekly_hours`` (default 48 h).

    Consecutive-day cap
        No employee works more than ``max_consecutive_days`` (default 6)
        days in a row.

    Shift rotation with forward-only phase transitions (new)
        The caller supplies a ``rotation_cycle`` — an ordered list of shift
        type names representing the intended rotation sequence, e.g.
        ``["Morning", "Evening", "Night"]``.

        The engine tracks every employee's *current phase* (position in the
        cycle) and enforces two rules:

        (a) **Same-shift streak cap** (``max_same_shift_days``, default 2):
            after this many consecutive days on the same shift type the
            employee must advance to the next phase on their next worked day.
            They are ineligible for their current phase until rotation occurs.

        (b) **Forward-only transitions** (hard block): an employee may only
            move *forward* in the cycle, or stay on their current phase within
            the streak limit.  A backward jump — e.g. Night → Morning —
            compresses the circadian rhythm and is treated as a hard block.
            The only way to "go backward" is through a rest day, which resets
            the phase pointer so any phase becomes eligible again.

        Rotation is applied as a *weighted preference* when multiple
        candidates are available: employees whose current phase is the target
        shift, or who are due to rotate into it, are ranked ahead of others.
        This distributes every shift type across the full workforce over time
        without sacrificing coverage when no perfectly-phased employee exists.

    Hours-weighted fair distribution
        Candidates are sorted by accumulated *hours* (not days) within each
        rotation-preference tier, so heavier shifts are fairly accounted for.

    Chronological shift ordering
        On any given day, shifts are filled in ascending start-time order so
        the rest-gap and rotation checks are evaluated consistently.
    """

    def __init__(
        self,
        employees: list[str],
        shift_types: list[str],
        start_date: date,
        end_date: date,
        company: str,
        min_coverage: Optional[dict[str, int]] = None,
        max_coverage: Optional[dict[str, Optional[int]]] = None,
        allow_multiple_shifts: bool = False,
        max_streak_days: int = MAX_STREAK_DAYS,
        target_days_per_week: int = TARGET_DAYS_PER_WEEK,
        min_rest_hours: float = MIN_REST_HOURS,
        max_weekly_hours: float = MAX_WEEKLY_HOURS,
        rotation_cycle: Optional[list[str]] = None,
        max_same_shift_days: int = MAX_SAME_SHIFT_DAYS,
    ):
        self.employees             = employees
        self.shift_types           = shift_types
        self.start_date            = start_date
        self.end_date              = end_date
        self.company               = company
        self.min_coverage          = min_coverage or {s: 1 for s in shift_types}
        self.max_coverage          = max_coverage or {}
        self.allow_multiple_shifts = allow_multiple_shifts
        self.max_streak_days       = max_streak_days
        self.target_days_per_week  = target_days_per_week
        self.min_rest_hours        = min_rest_hours
        self.max_weekly_hours      = max_weekly_hours

        # rotation_cycle: ordered list of shift type names representing the
        # intended rotation sequence.  Only shift types that appear in both
        # this list and self.shift_types participate in rotation enforcement.
        # If None or empty, rotation is derived from chronological start-time
        # order (same order used by _ordered_shifts).
        raw_cycle = rotation_cycle or []
        # Keep only names that are actually being scheduled
        self.rotation_cycle = [s for s in raw_cycle if s in shift_types]
        if not self.rotation_cycle and len(shift_types) > 1:
            # Fall back to chronological order — filled in after profiles load
            self._rotation_needs_init = True
        else:
            self._rotation_needs_init = False

        self.max_same_shift_days   = max_same_shift_days
        # Lookup: shift_type name → index in rotation_cycle (set after profile fetch)
        self._cycle_index: dict[str, int] = {}

        # ── Runtime state ─────────────────────────────────────────────────
        # employee → {date → set(shift_type)}
        # Single source of truth for all availability checks.  Holds the DB
        # lookback window and every day planned in this run.
        self._existing: dict[str, dict[date, set]] = defaultdict(lambda: defaultdict(set))

        # employee → set[date]
        self._on_leave: dict[str, set[date]] = defaultdict(set)

        # employee → accumulated hours in THIS run (hours-weighted fairness)
        self._hours: dict[str, float] = {e: 0.0 for e in employees}

        # employee → {iso_week_number → hours}
        # Tracks the weekly budget across both existing DB assignments and
        # new assignments created in this run.
        self._weekly_hours: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))

        # employee → {iso_week_number → days_worked}
        # Used to implement the target-days-per-week floor: employees below
        # the target get priority over those who have already met it.
        self._weekly_days: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))

        # employee → date of last actual assignment (needed for multi-day rest gap)
        self._last_worked_date: dict[str, Optional[date]] = {e: None for e in employees}

        # employee → the ShiftProfile of their most recently assigned shift.
        # Seeded from the DB lookback for employees mid-roster-cycle.
        self._last_shift: dict[str, Optional[ShiftProfile]] = {e: None for e in employees}

        # Reporting buckets — which employees were blocked and why
        self._forced_rest:    dict[str, set[date]] = defaultdict(set)  # consecutive-day cap
        self._rest_blocked:   dict[str, set[date]] = defaultdict(set)  # insufficient rest gap
        self._hours_capped:   dict[str, set[date]] = defaultdict(set)  # weekly hours exceeded
        self._rotation_block: dict[str, set[date]] = defaultdict(set)  # backward phase jump blocked

        # Rotation state per employee
        # employee → name of the shift type they most recently worked
        self._last_shift_type: dict[str, Optional[str]] = {e: None for e in employees}
        # employee → how many consecutive days they have been on _last_shift_type
        self._same_shift_streak: dict[str, int] = {e: 0 for e in employees}

        # Fetch shift timings first — everything else depends on them
        self._profiles: dict[str, ShiftProfile] = self._fetch_shift_profiles()

        # If rotation_cycle was not supplied, fall back to chronological order
        if self._rotation_needs_init:
            self.rotation_cycle = sorted(self.shift_types, key=lambda s: self._profiles[s].start_h)

        # Build cycle index for O(1) lookup
        self._cycle_index = {name: idx for idx, name in enumerate(self.rotation_cycle)}

        self._fetch_existing_assignments()
        self._fetch_leave_dates()

    # ------------------------------------------------------------------
    # Shift profile fetching
    # ------------------------------------------------------------------

    def _fetch_shift_profiles(self) -> dict[str, ShiftProfile]:
        """
        Fetch ``start_time`` / ``end_time`` for all requested shift types in
        a single query and build a ``ShiftProfile`` for each one.

        Raises a user-visible error if any shift type is missing or has no
        timing data configured — these are prerequisites for the engine.
        """
        ShiftType = frappe.qb.DocType("Shift Type")
        rows = (
            frappe.qb.from_(ShiftType)
            .select(ShiftType.name, ShiftType.start_time, ShiftType.end_time)
            .where(ShiftType.name.isin(self.shift_types))
            .run(as_dict=True)
        )

        found   = {r.name for r in rows}
        missing = [s for s in self.shift_types if s not in found]
        if missing:
            frappe.throw(
                _("The following Shift Types could not be found: {0}").format(", ".join(missing))
            )

        profiles = {}
        for row in rows:
            if not row.start_time or not row.end_time:
                frappe.throw(
                    _("Shift Type {0} is missing start_time or end_time.").format(row.name)
                )
            profiles[row.name] = ShiftProfile(row.name, row.start_time, row.end_time)

        return profiles

    # ------------------------------------------------------------------
    # Chronological shift ordering
    # ------------------------------------------------------------------

    def _ordered_shifts(self) -> list[str]:
        """
        Return shift types sorted by start_h ascending.  Filling shifts in
        chronological order within a day keeps ``_last_shift`` consistent —
        the engine always registers the day's *latest* ending shift, which is
        the one relevant to the rest-gap check for the following day.
        """
        return sorted(self.shift_types, key=lambda s: self._profiles[s].start_h)
    
    

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_existing_assignments(self):
        """
        Load all active submitted shift assignments overlapping the planning
        window, extended by a lookback of ``max_consecutive_days`` days for
        streak tracking.

        Side-effects:
        - Populates ``_existing`` for every day in the lookback + planning window.
        - Initialises ``_last_shift`` from the day immediately before
          ``start_date`` so the rest-gap check works from day one.
        - Pre-populates ``_weekly_hours`` from existing DB assignments that
          fall within the planning window, so the budget is accurate before
          the engine places any new assignment.
        """
        lookback_start = self.start_date - timedelta(days=self.max_streak_days)

        ShiftAssignment = frappe.qb.DocType("Shift Assignment")
        rows = (
            frappe.qb.from_(ShiftAssignment)
            .select(
                ShiftAssignment.employee,
                ShiftAssignment.shift_type,
                ShiftAssignment.start_date,
                ShiftAssignment.end_date,
            )
            .where(
                (ShiftAssignment.docstatus == 1)
                & (ShiftAssignment.status == "Active")
                & (ShiftAssignment.employee.isin(self.employees))
                & (
                    (ShiftAssignment.end_date >= lookback_start)
                    | ShiftAssignment.end_date.isnull()
                )
                & (ShiftAssignment.start_date <= self.end_date)
            )
            .run(as_dict=True)
        )

        for row in rows:
            s          = getdate(row.start_date)
            e          = getdate(row.end_date) if row.end_date else self.end_date
            emp        = row.employee
            shift_name = row.shift_type
            profile    = self._profiles.get(shift_name)

            cur = s
            while cur <= e:
                if lookback_start <= cur <= self.end_date:
                    self._existing[emp][cur].add(shift_name)

                    # Pre-fill weekly hours/days for days inside the planning window
                    if profile and self.start_date <= cur <= self.end_date:
                        week_key = cur.isocalendar()[1]
                        self._weekly_hours[emp][week_key] += profile.duration_h
                        self._weekly_days[emp][week_key]  += 1

                    # Track last worked date across the full lookback window
                    if lookback_start <= cur <= self.end_date:
                        prev_last = self._last_worked_date.get(emp)
                        if prev_last is None or cur > prev_last:
                            self._last_worked_date[emp] = cur

                    # Seed _last_shift and rotation state from the lookback window.
                    # Use the chronologically latest shift on the day immediately
                    # before start_date so both the rest-gap check and the
                    # rotation phase pointer are correct from the first planned day.
                    if profile and cur < self.start_date:
                        prev = self._last_shift.get(emp)
                        if prev is None or profile.start_h >= prev.start_h:
                            self._last_shift[emp] = profile

                    # Seed rotation state: count same-shift streak in lookback
                    if cur < self.start_date:
                        prev_type = self._last_shift_type.get(emp)
                        if prev_type == shift_name:
                            self._same_shift_streak[emp] += 1
                        elif prev_type is not None:
                            # Different shift type encountered — streak resets
                            # but we only care about the streak entering start_date,
                            # which is the last contiguous block, so reset here.
                            self._same_shift_streak[emp] = 1
                        self._last_shift_type[emp] = shift_name

                cur += timedelta(days=1)

    def _fetch_leave_dates(self):
        """
        Load approved and pending leave applications overlapping the planning
        window.  Employees on leave are excluded from scheduling that day.
        """
        LeaveApplication = frappe.qb.DocType("Leave Application")
        rows = (
            frappe.qb.from_(LeaveApplication)
            .select(
                LeaveApplication.employee,
                LeaveApplication.from_date,
                LeaveApplication.to_date,
            )
            .where(
                (LeaveApplication.employee.isin(self.employees))
                & (LeaveApplication.status.isin(["Approved", "Open"]))
                & (LeaveApplication.docstatus != 2)
                & (LeaveApplication.to_date >= self.start_date)
                & (LeaveApplication.from_date <= self.end_date)
            )
            .run(as_dict=True)
        )
        for row in rows:
            cur = getdate(row.from_date)
            end = getdate(row.to_date)
            while cur <= end:
                self._on_leave[row.employee].add(cur)
                cur += timedelta(days=1)

    # ------------------------------------------------------------------
    # Eligibility guards
    # ------------------------------------------------------------------

    def _consecutive_days(self, employee: str, up_to: date) -> int:
        """
        Count the unbroken worked-day streak ending on ``up_to - 1``.
        Reads ``_existing`` which covers the DB lookback window and all
        days planned so far in this run.
        """
        streak = 0
        check  = up_to - timedelta(days=1)
        while self._existing[employee].get(check):
            streak += 1
            check  -= timedelta(days=1)
        return streak

    def _has_enough_rest(self, employee: str, day: date, profile: ShiftProfile) -> bool:
        """
        Return True if the employee has accumulated at least ``min_rest_hours``
        between the end of their last worked shift and the start of ``profile``
        on ``day``.

        The calculation is date-aware:
        - If the candidate day is the *same* calendar day as the last worked day
          (same-day second shift), the gap is simply
          ``profile.start_h - last.end_h``.  The 24-hour wrap used for next-day
          checks would incorrectly add a full day of phantom rest.
        - If the candidate day is N calendar days after the last worked day,
          the employee has had at least N-1 full days of rest plus the tail of
          the last shift day and the head of the candidate day.  We compute the
          exact gap as ``profile.start_h + N*24 - last.end_h``.

        This fixes the cascading rest_blocked bug where Night shift employees
        were permanently blocked from Day shift even after 48+ hours of rest.
        """
        last_shift = self._last_shift.get(employee)
        last_date  = self._last_worked_date.get(employee)
        if last_shift is None or last_date is None:
            return True

        days_elapsed = (day - last_date).days
        if days_elapsed < 0:
            # Candidate is before last worked date — should not happen in a
            # forward-only build, but guard defensively.
            return False
        if days_elapsed == 0:
            # Same calendar day: second shift must start after this one ends
            gap = profile.start_h - last_shift.end_h
        else:
            # N days later: gap = start of candidate + N*24 - end of last shift
            # (all on the same 48-hour reference clock anchored at last_date midnight)
            gap = profile.start_h + days_elapsed * 24.0 - last_shift.end_h

        return gap >= self.min_rest_hours

    def _weekly_hours_ok(self, employee: str, day: date, profile: ShiftProfile) -> bool:
        """
        Return True if adding this shift would keep the employee within
        ``max_weekly_hours`` for the ISO week containing ``day``.
        """
        week_key = day.isocalendar()[1]
        current  = self._weekly_hours[employee].get(week_key, 0.0)
        return (current + profile.duration_h) <= self.max_weekly_hours

    def _rotation_phase_distance(self, employee: str, target_shift: str) -> int:
        """
        Return the forward distance from the employee's current phase to
        ``target_shift`` in the rotation cycle.

        - 0  → employee is already on this phase (or has no history)
        - 1  → one step forward (ideal next rotation)
        - N  → N steps forward
        - -1 → ``target_shift`` is not in the cycle (no rotation constraint)

        The cycle is treated as circular for distance calculation: Night → Morning
        is one step forward when the cycle wraps, but is only allowed after a
        rest day (the rest-gap check handles that independently).
        """
        if target_shift not in self._cycle_index:
            return -1   # shift not in rotation cycle, no constraint
        current_type = self._last_shift_type.get(employee)
        if current_type is None or current_type not in self._cycle_index:
            return 0    # no history or history outside cycle → freely assignable
        cur_idx  = self._cycle_index[current_type]
        tgt_idx  = self._cycle_index[target_shift]
        n        = len(self.rotation_cycle)
        return (tgt_idx - cur_idx) % n  # forward distance (0..n-1)

    def _rotation_allowed(self, employee: str, target_shift: str) -> bool:
        """
        Return True if assigning ``target_shift`` to this employee is permitted
        under the rotation rules.

        Rules
        -----
        1. If the target is not in the rotation cycle, there is no constraint.
        2. If the employee is on the same phase and has NOT yet exhausted
           ``max_same_shift_days``, staying is allowed.
        3. If the employee has exhausted ``max_same_shift_days`` on the current
           phase, the target must be the *next* phase in the cycle (distance == 1).
           Staying on the same phase is blocked.
        4. Any target that is *behind* the employee's current phase in the cycle
           (distance > 1 in the forward direction means we're skipping phases,
           but distance == 0 means same) is a backward jump and is hard-blocked.
           Specifically, any target with forward-distance > 1 while the employee
           is still within their streak is also blocked to prevent phase-skipping.
        """
        if target_shift not in self._cycle_index:
            return True   # not in cycle, no constraint

        dist         = self._rotation_phase_distance(employee, target_shift)
        same_streak  = self._same_shift_streak.get(employee, 0)
        streak_cap   = self.max_same_shift_days

        if dist == 0:
            # Same phase — only allowed if streak has not hit the cap
            return same_streak < streak_cap

        if dist == 1:
            # Next phase forward — always allowed (this is the ideal rotation)
            return True

        # dist >= 2: skipping phases is not allowed — the employee must pass
        # through intermediate shifts.  This prevents e.g. Morning jumping
        # directly to Night, which would be biologically equivalent to a
        # backward jump from the body clock's perspective.
        return False

    def _rotation_priority(self, employee: str, target_shift: str) -> int:
        """
        Return a sort key (lower = higher priority) expressing how well this
        employee fits ``target_shift`` from a rotation standpoint.

        0 — Employee is due to rotate into exactly this phase (dist == 1 and
            streak cap reached, or dist == 0 and streak fresh).
        1 — Employee is on a different phase but rotation is permitted.
        2 — Employee has no rotation history (fresh start).

        This is used as the *primary* sort key in ``_score_candidate``; hours
        accumulated is the secondary key so that within each priority tier
        the employee with less total time is preferred.
        """
        if target_shift not in self._cycle_index:
            return 2   # no rotation preference
        current_type = self._last_shift_type.get(employee)
        if current_type is None:
            return 2
        dist        = self._rotation_phase_distance(employee, target_shift)
        same_streak = self._same_shift_streak.get(employee, 0)
        if dist == 0 and same_streak < self.max_same_shift_days:
            return 0   # continuing current phase within cap — natural fit
        if dist == 1:
            return 0   # rotating into the next phase — also ideal
        return 1       # permitted but not the ideal next phase

    def _weekly_days_score(self, employee: str, day: date) -> int:
        """
        Return a sort key expressing how far below the weekly target this
        employee is.  Lower value = higher priority (employee needs more days).

        Employees below ``target_days_per_week`` for the current ISO week
        return a negative deficit (e.g. -3 if they have 2 days and target is 5).
        Employees at or above the target return 0.
        This is used as the *primary* sort key so under-scheduled employees are
        filled first before surplus days go to those already at the target.
        """
        week_key = day.isocalendar()[1]
        worked   = self._weekly_days[employee].get(week_key, 0)
        deficit  = worked - self.target_days_per_week   # negative = below target
        return min(deficit, 0)   # cap at 0; surplus doesn't further penalise

    def _score_candidate(self, employee: str, target_shift: str, day: date) -> tuple:
        """
        Composite sort key for ranking candidates for a slot.  Lower = better.

        (weekly_days_score, rotation_priority, accumulated_hours)

        1. weekly_days_score:   employees below their weekly target come first
        2. rotation_priority:   within that group, those due for this phase come first
        3. accumulated_hours:   within that group, those with fewer hours come first
        """
        return (
            self._weekly_days_score(employee, day),
            self._rotation_priority(employee, target_shift),
            self._hours[employee],
        )

    def _is_available(
        self,
        employee: str,
        day: date,
        profile: ShiftProfile,
        ignore_rotation: bool = False,
    ) -> bool:
        """
        Return True if ``employee`` can be assigned to ``profile`` on ``day``.

        Guards are applied cheapest-first:

        1. Not on leave.
        2. No existing shift that day (unless multiple shifts allowed).
        3. Not already on this exact shift type that day.
        4. Streak of consecutive worked days has not hit ``max_streak_days``.
        5. Sufficient rest since their last shift (≥ ``min_rest_hours``),
           accounting for the exact number of calendar days elapsed.
        6. Would not exceed the weekly hours budget (≤ ``max_weekly_hours``).
        7. Rotation: no backward phase jump AND same-shift streak within cap.
           This guard is skipped when ``ignore_rotation=True``, which the
           build loop uses when filling *minimum* coverage slots to ensure the
           floor is always reachable even if rotation would otherwise block it.
        """
        if day in self._on_leave.get(employee, set()):
            return False

        existing_shifts = self._existing[employee].get(day, set())

        if not self.allow_multiple_shifts and existing_shifts:
            return False

        if profile.name in existing_shifts:
            return False

        if self._consecutive_days(employee, day) >= self.max_streak_days:
            self._forced_rest[employee].add(day)
            return False

        if not self._has_enough_rest(employee, day, profile):
            self._rest_blocked[employee].add(day)
            return False

        if not self._weekly_hours_ok(employee, day, profile):
            self._hours_capped[employee].add(day)
            return False

        # Guard 7 — rotation (soft: skipped for minimum-coverage pass)
        if not ignore_rotation and self.rotation_cycle:
            if not self._rotation_allowed(employee, profile.name):
                self._rotation_block[employee].add(day)
                return False

        return True

    def _sorted_by_hours(self, candidates: list[str]) -> list[str]:
        """
        Sort candidates ascending by accumulated hours.  Using hours (not
        days) as the fairness metric gives lighter-shift workers appropriate
        priority over colleagues who have worked the same number of days but
        on shorter shifts.
        """
        return sorted(candidates, key=lambda e: self._hours[e])

    # ------------------------------------------------------------------
    # Core build loop
    # ------------------------------------------------------------------

    def build(self) -> dict[date, dict[str, list[str]]]:
        """
        Iterate every day in the planning window, filling each shift type in
        ascending start-time order.

        For each shift the engine uses a two-pass strategy:

        Pass 1 — Minimum guarantee
            Fill exactly ``min_coverage[shift_type]`` slots (the hard floor).
            Candidates are ranked by ``_score_candidate`` (rotation priority,
            then fewest accumulated hours).  If fewer than ``min_coverage``
            eligible employees exist the gap surfaces in the ``uncovered`` report.

        Pass 2 — Surplus distribution
            After the minimum is met, any remaining eligible employees who have
            not yet been assigned today are offered an additional slot on that
            shift, again ranked by ``_score_candidate``.  This keeps utilisation
            high when the pool is large while maintaining all health constraints
            (rest gap, weekly hours, consecutive-day cap, rotation rules) for
            every additional assignment.

            Surplus employees are only added when it does not deplete the minimum
            of *later* shifts in the same day.  Before each surplus pass the
            engine identifies employees who are the *sole* cover for a later
            shift's minimum and marks them as reserved so the current shift
            cannot absorb them.

        Returns
        -------
        dict of {date: {shift_type: [employee_id, ...]}}
        """
        roster: dict[date, dict[str, list[str]]] = {}
        ordered_shifts = self._ordered_shifts()
        cur = self.start_date

        while cur <= self.end_date:
            roster[cur] = {}
            assigned_today: set[str] = set()

            # Build initial reserved set: employees who are the only ones able
            # to cover the minimum of a later-starting shift are held back from
            # surplus slots on earlier shifts so the minimum is always reachable.
            def _compute_reserved() -> set[str]:
                # Reserve employees who are the only ones able to cover the
                # minimum of a later shift.  We check with ignore_rotation=True
                # so we see the true fallback pool — if an employee can cover
                # a later shift (even via fallback), they should be reserved.
                reserved: set[str] = set()
                for later_shift in reversed(ordered_shifts):
                    later_profile = self._profiles[later_shift]
                    later_min     = self.min_coverage.get(later_shift, 1)
                    eligible_later = [
                        e for e in self.employees
                        if self._is_available(e, cur, later_profile, ignore_rotation=True)
                        and (self.allow_multiple_shifts or e not in assigned_today)
                    ]
                    if len(eligible_later) <= later_min:
                        reserved.update(eligible_later)
                return reserved

            reserved = _compute_reserved()

            for shift_name in ordered_shifts:
                profile     = self._profiles[shift_name]
                min_needed  = self.min_coverage.get(shift_name, 1)
                max_allowed = self.max_coverage.get(shift_name)

                if max_allowed is not None:
                    min_needed = min(min_needed, max_allowed)

                # ── Pass 1: guarantee the minimum ─────────────────────────
                # Rotation is treated as a soft preference here — we relax it
                # (ignore_rotation=True) so the minimum is always reachable
                # even when rotation rules would otherwise block everyone.
                # Try with rotation first; fall back without it if short.
                min_candidates_with_rot = [
                    e for e in self.employees
                    if self._is_available(e, cur, profile, ignore_rotation=False)
                    and (self.allow_multiple_shifts or e not in assigned_today)
                ]
                ranked_with_rot = sorted(
                    min_candidates_with_rot,
                    key=lambda e: self._score_candidate(e, shift_name, cur),
                )
                print("Ranked With Rot -- ", ranked_with_rot)
                print("Self Emps ", self.employees)
                chosen = ranked_with_rot[:min_needed]

                # If rotation-aware pool is short of the minimum, fill the
                # remaining slots from employees who pass all other guards
                # but fail the rotation check (ignore_rotation=True).
                if len(chosen) < min_needed:
                    already_chosen = set(chosen)
                    fallback_candidates = [
                        e for e in self.employees
                        if self._is_available(e, cur, profile, ignore_rotation=True)
                        and (self.allow_multiple_shifts or e not in assigned_today)
                        and e not in already_chosen
                    ]
                    ranked_fallback = sorted(
                        fallback_candidates,
                        key=lambda e: self._score_candidate(e, shift_name, cur),
                    )
                    slots_left = min_needed - len(chosen)
                    chosen = chosen + ranked_fallback[:slots_left]

                # ── Pass 2: surplus — rotation IS enforced, max_allowed caps ─
                surplus_candidates = [
                    e for e in min_candidates_with_rot
                    if e not in set(chosen) and e not in reserved
                ]
                surplus_ranked = sorted(
                    surplus_candidates,
                    key=lambda e: self._score_candidate(e, shift_name, cur),
                )
                if max_allowed is not None:
                    remaining = max_allowed - len(chosen)
                    surplus   = surplus_ranked[:max(remaining, 0)]
                else:
                    surplus = surplus_ranked
                chosen = chosen + surplus

                roster[cur][shift_name] = chosen

                for emp in chosen:
                    week_key = cur.isocalendar()[1]
                    self._hours[emp]                  += profile.duration_h
                    self._weekly_hours[emp][week_key] += profile.duration_h
                    self._weekly_days[emp][week_key]  += 1
                    self._last_shift[emp]              = profile
                    self._last_worked_date[emp]        = cur
                    assigned_today.add(emp)
                    self._existing[emp][cur].add(shift_name)

                    if self._last_shift_type[emp] == shift_name:
                        self._same_shift_streak[emp] += 1
                    else:
                        self._same_shift_streak[emp] = 1
                    self._last_shift_type[emp] = shift_name

                reserved = _compute_reserved()

            cur += timedelta(days=1)

        return roster

    # ------------------------------------------------------------------
    # Summary helpers
    # ------------------------------------------------------------------

    def workload_summary(self) -> list[dict]:
        """
        Per-employee stats after ``build()`` has been called.
        Reports assigned days (within the planning window) and total hours.
        """
        day_counts: dict[str, int] = defaultdict(int)
        for emp, day_map in self._existing.items():
            if emp not in self._hours:
                continue
            for d, shifts in day_map.items():
                if d >= self.start_date and shifts:
                    day_counts[emp] += 1

        return [
            {
                "employee":      emp,
                "assigned_days": day_counts.get(emp, 0),
                "total_hours":   round(self._hours[emp], 1),
            }
            for emp in sorted(self.employees, key=lambda e: -self._hours[e])
        ]

    def shift_type_summary(self) -> list[dict]:
        """
        Per-shift-type timing metadata for the preview panel.
        Sorted by start time so the UI can render a chronological legend.
        """
        return [
            {
                "shift_type":   p.name,
                "duration_h":   round(p.duration_h, 1),
                "is_overnight": p.is_overnight,
                "start_h":      round(p.start_h, 2),
                "end_h":        round(p.end_h % 24, 2),   # normalise back to clock hour
            }
            for p in sorted(self._profiles.values(), key=lambda p: p.start_h)
        ]


# ---------------------------------------------------------------------------
# Frappe Document
# ---------------------------------------------------------------------------

class ShiftAssignmentTool(Document):

    # ------------------------------------------------------------------
    # Existing helpers (unchanged)
    # ------------------------------------------------------------------

    @frappe.whitelist()
    def get_employees(self, advanced_filters: list | None = None) -> list:
        if not advanced_filters:
            advanced_filters = []

        quick_filter_fields = [
            "company", "branch", "department",
            "designation", "grade", "employment_type",
        ]
        filters  = [[d, "=", self.get(d)] for d in quick_filter_fields if self.get(d)]
        filters += advanced_filters

        if self.action == "Process Shift Requests":
            return self.get_shift_requests(filters)
        return self.get_employees_for_assigning_shift(filters)

    def get_employees_for_assigning_shift(self, filters):
        Employee = frappe.qb.DocType("Employee")
        query = frappe.qb.get_query(
            Employee,
            fields=[
                Employee.employee, Employee.employee_name,
                Employee.branch, Employee.department, Employee.default_shift,
            ],
            filters=filters,
        ).where(
            (Employee.status == "Active")
            & (Employee.date_of_joining <= self.start_date)
            & ((Employee.relieving_date >= self.start_date) | (Employee.relieving_date.isnull()))
        )
        if self.end_date:
            query = query.where(
                (Employee.relieving_date >= self.end_date) | (Employee.relieving_date.isnull())
            )

        self.allow_multiple_shifts = frappe.db.get_single_value(
            "HR Settings", "allow_multiple_shift_assignments"
        )
        if self.action == "Assign Shift Schedule":
            query = query.where(
                Employee.employee.notin(SubQuery(self.get_query_for_employees_with_same_shift_schedule()))
            )
        elif self.status == "Active":
            query = query.where(
                Employee.employee.notin(SubQuery(self.get_query_for_employees_with_shifts()))
            )

        return query.run(as_dict=True)

    def get_shift_requests(self, filters):
        Employee     = frappe.qb.DocType("Employee")
        ShiftRequest = frappe.qb.DocType("Shift Request")
        query = (
            frappe.qb.get_query(
                Employee,
                fields=[Employee.employee, Employee.employee_name],
                filters=filters,
            )
            .inner_join(ShiftRequest)
            .on(ShiftRequest.employee == Employee.name)
            .select(
                ShiftRequest.name, ShiftRequest.shift_type,
                ShiftRequest.from_date, ShiftRequest.to_date,
            )
            .where(ShiftRequest.status == "Draft")
        )

        if self.shift_type_filter:
            query = query.where(ShiftRequest.shift_type == self.shift_type_filter)
        if self.approver:
            query = query.where(ShiftRequest.approver == self.approver)
        if self.from_date:
            query = query.where((ShiftRequest.to_date >= self.from_date) | (ShiftRequest.to_date.isnull()))
        if self.to_date:
            query = query.where(ShiftRequest.from_date <= self.to_date)

        data = query.run(as_dict=True)
        for d in data:
            d.employee_name  = d.employee + ": " + d.employee_name
            d.shift_request  = get_link_to_form("Shift Request", d.name)
        return data

    def get_query_for_employees_with_shifts(self):
        ShiftAssignment = frappe.qb.DocType("Shift Assignment")
        query = (
            frappe.qb.from_(ShiftAssignment)
            .select(ShiftAssignment.employee)
            .distinct()
            .where(
                (ShiftAssignment.status == "Active")
                & (ShiftAssignment.docstatus == 1)
                & ((ShiftAssignment.end_date >= self.start_date) | (ShiftAssignment.end_date.isnull()))
            )
        )
        if self.end_date:
            query = query.where(ShiftAssignment.start_date <= self.end_date)
        if self.allow_multiple_shifts:
            query = self.get_query_checking_overlapping_shift_timings(
                query, ShiftAssignment, self.shift_type
            )
        return query

    def get_query_for_employees_with_same_shift_schedule(self):
        days = frappe.get_all("Assignment Rule Day", {"parent": self.shift_schedule}, pluck="day")

        ShiftScheduleAssignment = frappe.qb.DocType("Shift Schedule Assignment")
        ShiftSchedule           = frappe.qb.DocType("Shift Schedule")
        Day                     = frappe.qb.DocType("Assignment Rule Day")

        query = (
            frappe.qb.from_(ShiftScheduleAssignment)
            .left_join(ShiftSchedule)
            .on(ShiftSchedule.name == ShiftScheduleAssignment.shift_schedule)
            .left_join(Day)
            .on(ShiftSchedule.name == Day.parent)
            .select(ShiftScheduleAssignment.employee)
            .distinct()
            .where((ShiftScheduleAssignment.enabled == 1) & (Day.day.isin(days)))
        )
        if self.allow_multiple_shifts:
            shift_type = frappe.db.get_value("Shift Schedule", self.shift_schedule, "shift_type")
            query = self.get_query_checking_overlapping_shift_timings(query, ShiftSchedule, shift_type)
        return query

    def get_query_checking_overlapping_shift_timings(self, query, doctype, shift_type):
        shift_start, shift_end = frappe.db.get_value(
            "Shift Type", shift_type, ["start_time", "end_time"]
        )
        if shift_end < shift_start:
            shift_end += timedelta(hours=24)

        ShiftType     = frappe.qb.DocType("Shift Type")
        end_time_case = (
            Case()
            .when(ShiftType.end_time < ShiftType.start_time, ShiftType.end_time + Interval(hours=24))
            .else_(ShiftType.end_time)
        )
        return (
            query.left_join(ShiftType)
            .on(doctype.shift_type == ShiftType.name)
            .where((end_time_case >= shift_start) & (ShiftType.start_time <= shift_end))
        )

    # ------------------------------------------------------------------
    # Manual bulk assign (unchanged)
    # ------------------------------------------------------------------

    @frappe.whitelist()
    def bulk_assign(self, employees: list):
        if self.action == "Assign Shift":
            mandatory_fields = ["shift_type"]
            doctype = "Shift Assignments"
        elif self.action == "Assign Shift Schedule":
            mandatory_fields = ["shift_schedule"]
            doctype = "Shift Schedule Assignments"
        else:
            frappe.throw(_("Invalid Action"))

        mandatory_fields.extend(["company", "start_date"])
        validate_bulk_tool_fields(self, mandatory_fields, employees, "start_date", "end_date")

        if self.action == "Assign Shift" and len(employees) <= 30:
            return self._bulk_assign(employees)

        frappe.enqueue(self._bulk_assign, timeout=3000, employees=employees)
        frappe.msgprint(
            _("Creation of {0} has been queued. It may take a few minutes.").format(doctype),
            alert=True,
            indicator="blue",
        )

    def _bulk_assign(self, employees: list):
        success, failure = [], []
        count     = 0
        savepoint = "before_assignment"

        if self.action == "Assign Shift":
            doctype = "Shift Assignment"
            event   = "completed_bulk_shift_assignment"
        else:
            doctype = "Shift Schedule Assignment"
            event   = "completed_bulk_shift_schedule_assignment"

        for d in employees:
            try:
                frappe.db.savepoint(savepoint)
                assignment = (
                    self.create_shift_schedule_assignment(d)
                    if self.action == "Assign Shift Schedule"
                    else create_shift_assignment(
                        d, self.company, self.shift_type,
                        self.start_date, self.end_date,
                        self.status, self.shift_location,
                    )
                )
                if self.action == "Assign Shift Schedule":
                    assignment.create_shifts(self.start_date, self.end_date)

            except Exception:
                frappe.db.rollback(save_point=savepoint)
                frappe.log_error(
                    f"Bulk Assignment - {doctype} failed for employee {d}.",
                    reference_doctype=doctype,
                )
                failure.append(d)
            else:
                success.append({"doc": get_link_to_form(doctype, assignment.name), "employee": d})

            count += 1
            frappe.publish_progress(
                count * 100 / len(employees), title=_("Creating {0}...").format(doctype)
            )

        frappe.clear_messages()
        frappe.publish_realtime(
            event,
            message={"success": success, "failure": failure},
            doctype="Shift Assignment Tool",
            after_commit=True,
        )

    # ------------------------------------------------------------------
    # Automated roster assignment
    # ------------------------------------------------------------------

    @frappe.whitelist()
    def auto_assign_roster(
        self,
        employees: list[str],
        shift_types: list[dict | str],
        min_coverage: dict | None = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Automatically build and (optionally) persist an evenly distributed,
        time-aware roster.

        Parameters
        ----------
        employees:
            Employee IDs to roster (output of ``get_employees()``).
        shift_types:
            Either a legacy flat list of shift type name strings::

                ["Day Shift", "Night Shift"]

            or the preferred structured format where each entry carries its
            own minimum headcount requirement::

                [
                    {"shift_type": "Day Shift",   "min_coverage": 3},
                    {"shift_type": "Night Shift",  "min_coverage": 1},
                ]

            When the structured format is used, the ``min_coverage`` parameter
            is ignored (per-shift values from the list take precedence).
        min_coverage:
            Legacy flat override ``{shift_type: headcount}`` per day.
            Only applied when ``shift_types`` is supplied as plain strings.
            Defaults to 1 per shift when absent.
        dry_run:
            Return the proposed plan without writing to the database.

        Returns
        -------
        ``roster``        ``{date_str: {shift_type: [employee_id]}}``
        ``workload``      Per-employee assigned_days + total_hours.
        ``shift_types``   Timing metadata for each shift type.
        ``uncovered``     ``{date_str: {shift_type: shortage}}``
        ``skipped_leave`` Employees excluded ≥1 day due to leave.
        ``forced_rest``   ``{employee: [date_str]}`` — streak-cap blocks.
        ``rest_blocked``  ``{employee: [date_str]}`` — rest-gap blocks.
        ``hours_capped``  ``{employee: [date_str]}`` — weekly-hours blocks.
        ``created``       (dry_run=False) list of created doc links.
        """
        # ── Parse the shift_types argument ────────────────────────────────────
        # Accept either:
        #   (a) [{"shift_type": "Day Shift", "min_coverage": 3}, ...]   ← new
        #   (b) ["Day Shift", "Night Shift"]                             ← legacy

        parsed_shift_names:  list[str] = []
        parsed_min_coverage: dict[str, int] = {}
        parsed_min_coverage: dict[str, int] = {}
        parsed_max_coverage: dict[str, Optional[int]] = {}

        for entry in shift_types or []:
            if isinstance(entry, dict):
                name = entry.get("shift_type") or entry.get("name", "")
                if not name:
                    continue
                parsed_shift_names.append(name)
                parsed_min_coverage[name] = int(entry.get("min_coverage", 1))
                max_c = entry.get("max_coverage")
                parsed_max_coverage[name] = int(max_c) if max_c is not None else None
            elif isinstance(entry, str):
                parsed_shift_names.append(entry)
                parsed_min_coverage[entry] = (min_coverage or {}).get(entry, 1)
                parsed_max_coverage[entry] = None

        # Deduplicate while preserving order
        seen: set[str] = set()
        shift_names: list[str] = []
        for n in parsed_shift_names:
            if n not in seen:
                seen.add(n)
                shift_names.append(n)

        # Honour any explicit legacy overrides that weren't in the structured list
        if min_coverage:
            for k, v in min_coverage.items():
                if k not in parsed_min_coverage:
                    parsed_min_coverage[k] = v
                    parsed_max_coverage[k] = None

        self._validate_auto_roster_prerequisites(employees, shift_names)

        allow_multiple_shifts = frappe.db.get_single_value(
            "HR Settings", "allow_multiple_shift_assignments"
        )

        engine = RosterEngine(
            employees=employees,
            shift_types=shift_names,
            start_date=getdate(self.start_date),
            end_date=getdate(self.end_date),
            company=self.company,
            min_coverage=parsed_min_coverage,
            max_coverage=parsed_max_coverage,
            allow_multiple_shifts=bool(allow_multiple_shifts),
            rotation_cycle=shift_names,
        )

        raw_roster = engine.build()

        # ── Coverage gaps ─────────────────────────────────────────────────
        uncovered = {}
        for day, shifts in raw_roster.items():
            day_gaps = {}
            for shift_type, assigned in shifts.items():
                needed = parsed_min_coverage.get(shift_type, 1)
                if len(assigned) < needed:
                    day_gaps[shift_type] = needed - len(assigned)
            if day_gaps:
                uncovered[str(day)] = day_gaps

        # ── Serialise exclusion-reason reports ────────────────────────────
        def _serialise(mapping: dict) -> dict:
            return {
                emp: sorted(str(d) for d in days)
                for emp, days in mapping.items() if days
            }

        skipped_leave   = [emp for emp, days in engine._on_leave.items() if days]
        forced_rest     = _serialise(engine._forced_rest)
        rest_blocked    = _serialise(engine._rest_blocked)
        hours_capped    = _serialise(engine._hours_capped)
        rotation_block  = _serialise(engine._rotation_block)

        # ── Serialise roster ──────────────────────────────────────────────
        roster_out = {
            str(day): dict(shifts)
            for day, shifts in raw_roster.items()
        }

        result = {
            "roster":               roster_out,
            "workload":             engine.workload_summary(),
            "shift_types":          engine.shift_type_summary(),
            "rotation_cycle":       engine.rotation_cycle,
            "min_coverage":         parsed_min_coverage,
            "uncovered":            uncovered,
            "skipped_leave":        skipped_leave,
            "forced_rest":          forced_rest,
            "rest_blocked":         rest_blocked,
            "hours_capped":         hours_capped,
            "rotation_block":       rotation_block,
            "max_streak_days":       engine.max_streak_days,
            "target_days_per_week":  engine.target_days_per_week,
            "min_rest_hours":       engine.min_rest_hours,
            "max_weekly_hours":     engine.max_weekly_hours,
            "max_same_shift_days":  engine.max_same_shift_days,
        }

        if dry_run:
            result["dry_run"] = True
            return result

        # ── Persist ───────────────────────────────────────────────────────
        created, failed = self._persist_roster(raw_roster)
        result["created"] = created
        result["failed"]  = failed

        frappe.publish_realtime(
            "completed_auto_roster_assignment",
            message={
                "success":      created,
                "failure":      failed,
                "uncovered":       uncovered,
                "forced_rest":     forced_rest,
                "rest_blocked":    rest_blocked,
                "hours_capped":    hours_capped,
                "rotation_block":  rotation_block,
            },
            doctype="Shift Assignment Tool",
            after_commit=True,
        )

        return result

    def _validate_auto_roster_prerequisites(self, employees: list, shift_names: list[str]):
        """
        Validate prerequisites for the auto-roster engine.

        ``shift_names`` must be the already-parsed flat list of shift type name
        strings (not the raw structured input).
        """
        if not employees:
            frappe.throw(_("Please select at least one employee for auto-roster."))
        if not shift_names:
            frappe.throw(_("Please provide at least one shift type for auto-roster."))
        if not self.start_date or not self.end_date:
            frappe.throw(_("Start Date and End Date are required for auto-roster."))
        if getdate(self.start_date) > getdate(self.end_date):
            frappe.throw(_("Start Date must be before or equal to End Date."))
        if not self.company:
            frappe.throw(_("Company is required for auto-roster."))

    def _persist_roster(self, roster: dict) -> tuple[list, list]:
        """Write one Shift Assignment per (day, shift, employee) triple.

        On success the assignment is created with status ``"Active"``.
        If the normal active creation raises an exception (e.g. a validation
        conflict), the assignment is retried with status ``"Inactive"`` so
        that it is still persisted and can be manually reviewed / activated,
        rather than being silently dropped via a savepoint rollback.
        """
        created, failed = [], []
        total = sum(len(emps) for shifts in roster.values() for emps in shifts.values())
        count = 0

        for day, shifts in sorted(roster.items()):
            for shift_type, employees in shifts.items():
                for employee in employees:
                    status_used = "Active"
                    try:
                        assignment = create_shift_assignment(
                            employee=employee,
                            company=self.company,
                            shift_type=shift_type,
                            start_date=str(day),
                            end_date=str(day),
                            status="Active",
                            shift_location=self.get("shift_location"),
                        )
                    except Exception:
                        # Active creation failed — fall back to Inactive so the
                        # record is still written and can be moved manually.
                        status_used = "Inactive"
                        try:
                            assignment = create_shift_assignment(
                                employee=employee,
                                company=self.company,
                                shift_type=shift_type,
                                start_date=str(day),
                                end_date=str(day),
                                status="Inactive",
                                shift_location=self.get("shift_location"),
                            )
                        except Exception:
                            frappe.log_error(
                                f"Auto Roster - Shift Assignment failed for "
                                f"employee {employee} on {day} ({shift_type}).",
                                reference_doctype="Shift Assignment",
                            )
                            failed.append({
                                "employee": employee, "shift_type": shift_type, "date": str(day),
                            })
                            count += 1
                            if total:
                                frappe.publish_progress(
                                    count * 100 / total, title=_("Building Roster...")
                                )
                            continue

                    created.append({
                        "employee":    employee,
                        "shift_type":  shift_type,
                        "date":        str(day),
                        "doc":         get_link_to_form("Shift Assignment", assignment.name),
                        "status":      status_used,
                    })

                    count += 1
                    if total:
                        frappe.publish_progress(
                            count * 100 / total, title=_("Building Roster...")
                        )

        frappe.clear_messages()
        return created, failed

    # ------------------------------------------------------------------
    # Preview endpoint
    # ------------------------------------------------------------------

    @frappe.whitelist()
    def preview_roster(
        self,
        employees: list[str],
        shift_types: list[dict | str],
        min_coverage: dict | None = None,
    ) -> dict:
        """Return a dry-run roster without writing anything to the database.

        Accepts the same ``shift_types`` argument as ``auto_assign_roster`` —
        either a flat list of strings or a structured list of
        ``{"shift_type": ..., "min_coverage": ...}`` dicts.
        """

        return self.auto_assign_roster(
            employees=employees,
            shift_types=shift_types,
            min_coverage=min_coverage,
            dry_run=True,
        )

    # ------------------------------------------------------------------
    # Shift request processing (unchanged)
    # ------------------------------------------------------------------

    @frappe.whitelist()
    def bulk_process_shift_requests(self, shift_requests: list, status: str):
        if not shift_requests:
            frappe.throw(
                _("Please select at least one Shift Request to perform this action."),
                title=_("No Shift Requests Selected"),
            )

        if len(shift_requests) <= 30:
            return self._bulk_process_shift_requests(shift_requests, status)

        frappe.enqueue(
            self._bulk_process_shift_requests,
            timeout=3000,
            shift_requests=shift_requests,
            status=status,
        )
        frappe.msgprint(
            _("Processing of Shift Requests has been queued. It may take a few minutes."),
            alert=True,
            indicator="blue",
        )

    def _bulk_process_shift_requests(self, shift_requests: list, status: str):
        success, failure = [], []
        count = 0

        for d in shift_requests:
            try:
                shift_request        = frappe.get_doc("Shift Request", d["shift_request"])
                shift_request.status = status
                shift_request.save()
                shift_request.submit()

            except Exception:
                frappe.log_error(
                    f"Bulk Processing - Processing failed for Shift Request {d['shift_request']}.",
                    reference_doctype="Shift Request",
                )
                failure.append(d["employee"])
            else:
                success.append({
                    "doc":      get_link_to_form("Shift Request", shift_request.name),
                    "employee": d["employee"],
                })

            count += 1
            frappe.publish_progress(
                count * 100 / len(shift_requests), title=_("Processing Requests...")
            )

        frappe.clear_messages()
        frappe.publish_realtime(
            "completed_bulk_shift_request_processing",
            message={"success": success, "failure": failure, "for_processing": True},
            doctype="Shift Assignment Tool",
            after_commit=True,
        )

    def create_shift_schedule_assignment(self, employee: str) -> str:
        assignment = frappe.new_doc("Shift Schedule Assignment")
        assignment.shift_schedule        = self.shift_schedule
        assignment.employee              = employee
        assignment.company               = self.company
        assignment.shift_status          = self.status
        assignment.shift_location        = self.shift_location
        assignment.enabled               = 0 if self.end_date else 1
        assignment.create_shifts_after   = self.start_date
        assignment.flags.ingore_validate = True
        assignment.save()
        return assignment


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def create_shift_assignment(
    employee: str,
    company: str,
    shift_type: str,
    start_date: str,
    end_date: str,
    status: str,
    shift_location: str | None = None,
    shift_schedule_assignment: str | None = None,
) -> str:
    assignment = frappe.new_doc("Shift Assignment")
    assignment.employee                  = employee
    assignment.company                   = company
    assignment.shift_type                = shift_type
    assignment.start_date                = start_date
    assignment.end_date                  = end_date
    assignment.status                    = status
    assignment.shift_location            = shift_location
    assignment.shift_schedule_assignment = shift_schedule_assignment
    assignment.save()
    assignment.submit()
    return assignment
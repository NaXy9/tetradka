# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Booking domain services: availability expansion and concurrency-safe booking.

Availability rules are stored as weekday + local times in the tutor's timezone;
expand_availability() is the single place where they are converted to concrete
UTC intervals, so all DST handling is concentrated (and tested) here.
"""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.catalog.models import Subject, TutorProfile
from apps.payments.services import reconcile_booking_payment
from apps.users.models import User

from .models import Booking, InvalidStatusTransition

UTC = dt.UTC

# A lesson cannot start sooner than this, so tutors are never surprised by a
# booking that begins in a few minutes.
MIN_BOOKING_LEAD = dt.timedelta(hours=2)

# Cancelling more than this far ahead of the lesson refunds the student in full;
# closer than that, the tutor's late-cancellation policy applies.
CANCELLATION_FULL_REFUND_WINDOW = dt.timedelta(hours=24)

# A booking holds its slot only briefly while awaiting payment; left unpaid past
# this window it is auto-cancelled so the slot returns to the pool.
PENDING_PAYMENT_TIMEOUT = dt.timedelta(minutes=15)


class SlotUnavailableError(Exception):
    """The requested interval cannot be booked (taken, outside availability, or too soon)."""


class BookingNotCancellableError(Exception):
    """The booking is in a status that has no cancellation edge (already cancelled/completed)."""


def _local_to_utc(day: dt.date, time_: dt.time, tz: ZoneInfo) -> dt.datetime:
    """Map a tutor's wall-clock time on a given local date to a UTC instant.

    Ambiguous times (the hour repeated when clocks fall back) resolve to their
    first occurrence. Nonexistent times (skipped when clocks spring forward)
    clamp to the transition instant, so a window edge inside the gap lands
    exactly where the clocks jumped and phantom intervals cannot appear.
    """
    local = dt.datetime.combine(day, time_, tzinfo=tz)
    utc_value = local.astimezone(UTC)
    if utc_value.astimezone(tz).replace(tzinfo=None) == local.replace(tzinfo=None):
        return utc_value
    # Nonexistent local time: binary-search the instant where the UTC offset
    # changes, between the two candidate mappings (fold=1 lands before the gap,
    # fold=0 after it). Offsets change on whole seconds, so this is exact.
    lo = int(local.replace(fold=1).astimezone(UTC).timestamp())
    hi = int(utc_value.timestamp())
    hi_offset = dt.datetime.fromtimestamp(hi, UTC).astimezone(tz).utcoffset()
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if dt.datetime.fromtimestamp(mid, UTC).astimezone(tz).utcoffset() == hi_offset:
            hi = mid
        else:
            lo = mid
    return dt.datetime.fromtimestamp(hi, UTC)


def expand_availability(
    tutor: TutorProfile, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Expand the tutor's weekly rules into concrete UTC intervals within [start, end).

    `start`/`end` must be aware UTC datetimes. Day-off exceptions remove all
    windows for their local date; a replacement exception substitutes the rule
    windows for that date. Around DST transitions the tutor's wall clock wins:
    a window crossing a "spring forward" gap shrinks by the skipped hour, one
    crossing "fall back" grows by the repeated hour, and a window that lies
    entirely inside the gap disappears (see _local_to_utc for edge mapping).
    Returns clipped, sorted (start_utc, end_utc) tuples.
    Raises ValueError if `start` or `end` is naive.
    """
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("start and end must be timezone-aware")

    tz = ZoneInfo(tutor.user.timezone)
    first_date = start.astimezone(tz).date()
    last_date = end.astimezone(tz).date()

    rules_by_weekday: dict[int, list] = {}
    for rule in tutor.availability_rules.all():
        rules_by_weekday.setdefault(rule.weekday, []).append(rule)
    exceptions = {
        exc.date: exc
        for exc in tutor.availability_exceptions.filter(date__range=(first_date, last_date))
    }

    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    day = first_date
    while day <= last_date:
        exc = exceptions.get(day)
        if exc and exc.is_day_off:
            windows = []
        elif exc:
            windows = [(exc.start_time, exc.end_time)]
        else:
            windows = [
                (rule.start_time, rule.end_time) for rule in rules_by_weekday.get(day.weekday(), [])
            ]
        for start_time, end_time in windows:
            window_start = _local_to_utc(day, start_time, tz)
            window_end = _local_to_utc(day, end_time, tz)
            if window_end <= window_start:  # window fully swallowed by a DST gap
                continue
            clipped_start = max(window_start, start)
            clipped_end = min(window_end, end)
            if clipped_start < clipped_end:
                intervals.append((clipped_start, clipped_end))
        day += dt.timedelta(days=1)

    return sorted(intervals)


def free_slots(
    tutor: TutorProfile, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Return the tutor's bookable UTC intervals within [start, end).

    Availability (see expand_availability) minus the time already taken by active
    (pending/confirmed) bookings: each availability interval has the overlapping
    bookings cut out of it, leaving the remaining free sub-intervals. Cancelled
    and completed bookings free their time up again. `start`/`end` must be aware.
    Returns sorted, non-overlapping (start_utc, end_utc) tuples.
    Raises ValueError if `start` or `end` is naive (propagated from expand_availability).
    """
    intervals = expand_availability(tutor, start, end)
    if not intervals:
        return []

    # Only active bookings block a slot; ordered by start so a single forward
    # pass can subtract them from each availability interval.
    busy = list(
        Booking.objects.filter(
            tutor=tutor,
            status__in=(Booking.Status.PENDING, Booking.Status.CONFIRMED),
            starts_at__lt=end,
            ends_at__gt=start,
        )
        .order_by("starts_at")
        .values_list("starts_at", "ends_at")
    )
    if not busy:
        return intervals

    free: list[tuple[dt.datetime, dt.datetime]] = []
    for interval_start, interval_end in intervals:
        cursor = interval_start
        for booking_start, booking_end in busy:
            if booking_end <= cursor or booking_start >= interval_end:
                continue  # booking does not touch the unconsumed part of this interval
            if booking_start > cursor:
                free.append((cursor, booking_start))
            cursor = max(cursor, booking_end)
            if cursor >= interval_end:
                break
        if cursor < interval_end:
            free.append((cursor, interval_end))
    return free


def _covers(tutor: TutorProfile, starts_at: dt.datetime, ends_at: dt.datetime) -> bool:
    """Whether [starts_at, ends_at) lies entirely within the tutor's availability."""
    covered_until = starts_at
    for interval_start, interval_end in expand_availability(tutor, starts_at, ends_at):
        if interval_start > covered_until:
            return False
        covered_until = max(covered_until, interval_end)
        if covered_until >= ends_at:
            return True
    return covered_until >= ends_at


def create_booking(
    *,
    student: User,
    tutor: TutorProfile,
    subject: Subject,
    starts_at: dt.datetime,
    ends_at: dt.datetime,
) -> Booking:
    """Create a pending booking, guaranteeing the slot is valid and not taken twice.

    A slot is bookable when it starts at least MIN_BOOKING_LEAD from now, lies
    entirely within the tutor's availability (rules minus exceptions), and does
    not overlap an active (pending/confirmed) booking. Concurrency safety is
    layered: the tutor row is locked with select_for_update() so concurrent
    requests for the same tutor serialize and the overlap check sees committed
    state; the GiST exclusion constraint in PostgreSQL remains the last line of
    defense should any code path bypass this function.

    Returns the created pending Booking with the price derived from the tutor's
    current hourly rate. Raises SlotUnavailableError when the slot cannot be
    booked and ValueError on naive or inverted datetimes.
    """
    if starts_at.utcoffset() is None or ends_at.utcoffset() is None:
        raise ValueError("starts_at and ends_at must be timezone-aware")
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")
    if starts_at <= timezone.now() + MIN_BOOKING_LEAD:
        raise SlotUnavailableError("the slot starts too soon")

    with transaction.atomic():
        # Re-fetch under the lock: the price must come from the committed row,
        # not from a possibly stale in-memory object.
        locked_tutor = TutorProfile.objects.select_for_update().get(pk=tutor.pk)
        if not _covers(locked_tutor, starts_at, ends_at):
            raise SlotUnavailableError("the slot is outside the tutor's availability")
        overlap = Booking.objects.filter(
            tutor=locked_tutor,
            status__in=(Booking.Status.PENDING, Booking.Status.CONFIRMED),
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        ).exists()
        if overlap:
            raise SlotUnavailableError("the requested slot is already booked")

        duration = ends_at - starts_at
        duration_hours = Decimal(duration.days * 86400 + duration.seconds) / Decimal(3600)
        price = (locked_tutor.hourly_rate * duration_hours).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return Booking.objects.create(
            student=student,
            tutor=locked_tutor,
            subject=subject,
            starts_at=starts_at,
            ends_at=ends_at,
            price=price,
        )


def _refund_amount(booking: Booking, target_status: str, now: dt.datetime) -> Decimal:
    """Refund owed to the student for a cancellation, following the refund policy.

    A tutor-initiated cancellation always refunds the full price. A student who
    cancels earlier than CANCELLATION_FULL_REFUND_WINDOW before the lesson is
    also refunded in full; closer than that, only the tutor's configured
    late-cancellation percentage is returned.
    """
    if target_status == Booking.Status.CANCELLED_BY_TUTOR:
        return booking.price
    if booking.starts_at - now > CANCELLATION_FULL_REFUND_WINDOW:
        return booking.price
    percent = Decimal(booking.tutor.late_cancellation_refund_percent)
    return (booking.price * percent / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def cancel_booking(*, booking: Booking, actor: User, reason: str = "") -> Decimal:
    """Cancel a booking on behalf of one of its parties and return the refund owed.

    The target status is chosen from the actor's side of the booking (student →
    cancelled_by_student, tutor → cancelled_by_tutor), and the move goes through
    the status machine, so the row is locked, the edge is validated against
    committed state, and the transition is audit-logged. Concurrent cancellations
    therefore serialize: the loser sees an already-cancelled booking and fails.

    The cancellation and the payment settlement happen in one transaction, so the
    booking can never end up cancelled with its hold left dangling: reconcile_booking_payment
    releases a full-refund hold (or fails an unconfirmed one) under the same lock,
    booking-first then payment. A late (partial-refund) student cancellation is the one
    case whose money is deferred to the capture flow — see reconcile_booking_payment.

    Returns the refund amount owed to the student. Raises BookingNotCancellableError
    when the actor is not a party to the booking or the booking's current status
    has no cancellation edge.
    """
    if actor.id == booking.student_id:
        target = Booking.Status.CANCELLED_BY_STUDENT
    elif actor.id == booking.tutor.user_id:
        target = Booking.Status.CANCELLED_BY_TUTOR
    else:
        raise BookingNotCancellableError("only a party to the booking may cancel it")

    try:
        with transaction.atomic():
            booking.transition_to(target, actor=actor, reason=reason)
            refund = _refund_amount(booking, target, timezone.now())
            reconcile_booking_payment(booking=booking, refund_amount=refund, actor=actor)
    except InvalidStatusTransition as exc:
        raise BookingNotCancellableError(str(exc)) from exc

    return refund


def expire_pending_bookings(*, now: dt.datetime | None = None) -> int:
    """Cancel bookings left unpaid past PENDING_PAYMENT_TIMEOUT and free their slots.

    A booking created more than PENDING_PAYMENT_TIMEOUT ago and still awaiting
    payment is treated as abandoned: it moves to cancelled_by_student (the
    pending→timeout edge of the status machine), which releases the slot for
    rebooking. Run periodically by Celery beat.

    Each candidate is re-checked as still pending under a row lock before the
    transition. This guards the race with a payment that confirms the booking
    between the scan and the sweep: confirmed→cancelled_by_student is itself a
    legal edge, so the status machine alone would happily cancel a just-paid
    lesson; the pending re-check under the lock is what prevents that.

    A pending booking's only possible live payment is an unconfirmed ``created`` hold
    (a confirmed hold would have moved the booking to confirmed), so reconciling it in
    the same transaction fails that payment and frees any hold opened at the PSP. The
    full ``booking.price`` is passed as the refund because nothing was captured.

    Returns the number of bookings cancelled.
    """
    now = now or timezone.now()
    cutoff = now - PENDING_PAYMENT_TIMEOUT
    stale_ids = list(
        Booking.objects.filter(status=Booking.Status.PENDING, created_at__lt=cutoff).values_list(
            "pk", flat=True
        )
    )

    cancelled = 0
    for pk in stale_ids:
        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=pk)
            if booking.status != Booking.Status.PENDING:
                continue  # confirmed or cancelled concurrently; leave it alone
            booking.transition_to(
                Booking.Status.CANCELLED_BY_STUDENT,
                actor=None,  # system action; no human actor
                reason="pending payment timeout",
            )
            reconcile_booking_payment(booking=booking, refund_amount=booking.price, actor=None)
            cancelled += 1
    return cancelled

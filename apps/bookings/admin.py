from django.contrib import admin

from .models import (
    AvailabilityException,
    AvailabilityRule,
    Booking,
    BookingStatusTransition,
    Review,
)


@admin.register(AvailabilityRule)
class AvailabilityRuleAdmin(admin.ModelAdmin):
    list_display = ("tutor", "weekday", "start_time", "end_time")
    list_filter = ("weekday",)


@admin.register(AvailabilityException)
class AvailabilityExceptionAdmin(admin.ModelAdmin):
    list_display = ("tutor", "date", "is_day_off", "start_time", "end_time")
    list_filter = ("is_day_off",)


class BookingStatusTransitionInline(admin.TabularInline):
    model = BookingStatusTransition
    extra = 0
    readonly_fields = ("from_status", "to_status", "actor", "reason", "created_at")
    can_delete = False


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("id", "student", "tutor", "subject", "starts_at", "ends_at", "status", "price")
    list_filter = ("status",)
    date_hierarchy = "starts_at"
    search_fields = ("student__email", "tutor__user__email")
    inlines = [BookingStatusTransitionInline]


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("booking", "rating", "created_at")
    list_filter = ("rating",)

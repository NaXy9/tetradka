from django.contrib import admin

from .models import Lesson, Recording


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "booking",
        "livekit_room",
        "started_at",
        "ended_at",
        "consent_student",
        "consent_tutor",
    )
    search_fields = ("livekit_room",)


@admin.register(Recording)
class RecordingAdmin(admin.ModelAdmin):
    list_display = ("id", "lesson", "file_key", "duration_seconds", "status", "expires_at")
    list_filter = ("status",)

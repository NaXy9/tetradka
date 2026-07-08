from django.contrib import admin

from .models import Summary, Transcript


@admin.register(Transcript)
class TranscriptAdmin(admin.ModelAdmin):
    list_display = ("id", "recording", "created_at")


@admin.register(Summary)
class SummaryAdmin(admin.ModelAdmin):
    list_display = ("id", "recording", "status", "created_at")
    list_filter = ("status",)

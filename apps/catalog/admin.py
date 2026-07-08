from django.contrib import admin

from .models import Subject, TutorProfile, TutorSubject


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


class TutorSubjectInline(admin.TabularInline):
    model = TutorSubject
    extra = 0


@admin.register(TutorProfile)
class TutorProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "hourly_rate", "is_verified", "rating", "lessons_count", "balance")
    list_filter = ("is_verified",)
    search_fields = ("user__email",)
    inlines = [TutorSubjectInline]

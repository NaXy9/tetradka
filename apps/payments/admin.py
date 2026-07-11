from django.contrib import admin

from .models import Payment, Payout, PayoutAccount, ProcessedWebhookEvent


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "booking", "provider", "provider_id", "amount", "commission", "status")
    list_filter = ("provider", "status")
    search_fields = ("provider_id",)


@admin.register(PayoutAccount)
class PayoutAccountAdmin(admin.ModelAdmin):
    list_display = ("tutor",)


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ("id", "tutor", "amount", "period_start", "period_end", "status")
    list_filter = ("status",)


@admin.register(ProcessedWebhookEvent)
class ProcessedWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("id", "provider", "event_id", "event_type", "payment", "created_at")
    list_filter = ("provider", "event_type")
    search_fields = ("event_id",)

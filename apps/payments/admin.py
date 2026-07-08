from django.contrib import admin

from .models import Payment, Payout, PayoutAccount


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

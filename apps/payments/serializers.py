# Tetradka — Copyright (c) 2026 Igor Pryanikov
# Licensed under PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""Serializers for the payment API."""

from rest_framework import serializers

from .models import Payment


class PaymentSerializer(serializers.ModelSerializer):
    """Read representation of a payment. The hold is still pending while status is
    ``created``; it becomes ``held`` once the provider confirms the authorization."""

    class Meta:
        model = Payment
        fields = ["id", "booking", "provider", "status", "amount", "created_at"]
        read_only_fields = fields

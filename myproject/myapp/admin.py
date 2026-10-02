from django.contrib import admin
from django.urls import path
from django.shortcuts import redirect
from django.contrib import messages

from .models import (
    SystemPaymentMethod,
    DepositRequest,
    UserProfileBalance,
    WithdrawalRequest,
    SystemSetting,
)


# ==============================================================================
# PAGE 1: TRANSACTION VERIFICATION BOARD
# ==============================================================================

@admin.register(DepositRequest)
class DepositRequestAdmin(admin.ModelAdmin):

    list_display = (
        'device_token',
        'amount',
        'payment_method',
        'sender_name',
        'status',
        'created_at'
    )

    list_filter = (
        'status',
        'payment_method'
    )

    search_fields = (
        'device_token',
        'sender_name'
    )

    actions = [
        'approve_deposits',
        'reject_deposits'
    ]

    def approve_deposits(self, request, queryset):

        count = 0

        for deposit in queryset.filter(status='PENDING'):

            profile, _ = UserProfileBalance.objects.get_or_create(
                device_token=deposit.device_token
            )

            profile.coins += deposit.amount
            profile.save()

            deposit.status = 'APPROVED'
            deposit.save()

            count += 1

        self.message_user(
            request,
            f"Successfully approved {count} deposit receipts and updated player balances!"
        )

    def reject_deposits(self, request, queryset):

        updated = queryset.filter(status='PENDING').update(status='REJECTED')

        self.message_user(
            request,
            f"Marked {updated} pending deposits as rejected."
        )

    approve_deposits.short_description = "✅ Approve selected deposit receipts"
    reject_deposits.short_description = "❌ Reject selected deposit receipts"


# ==============================================================================
# PAGE 2: ACCOUNT NUMBERS CONFIGURATION
# ==============================================================================

@admin.register(SystemPaymentMethod)
class SystemPaymentMethodAdmin(admin.ModelAdmin):

    list_display = (
        'method_type',
        'account_name',
        'account_number',
        'is_active'
    )

    list_editable = (
        'account_name',
        'account_number',
        'is_active'
    )


# ==============================================================================
# USER PROFILE BALANCE
# ==============================================================================

@admin.register(UserProfileBalance)
class UserProfileBalanceAdmin(admin.ModelAdmin):

    list_display = (
        'device_token',
        'coins'
    )

    search_fields = (
        'device_token',
    )


# ==============================================================================
# WITHDRAWAL REQUESTS
# ==============================================================================

@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):

    list_display = (
        'device_token',
        'amount',
        'method',
        'account_title',
        'account_number',
        'status',
        'created_at'
    )

    list_filter = (
        'status',
        'method'
    )

    readonly_fields = (
        'status',
        'created_at',
        'updated_at'
    )

    def get_urls(self):

        urls = super().get_urls()

        custom_urls = [
            path(
                '<int:object_id>/actions/approve/',
                self.admin_site.admin_view(self.approve_request),
                name='withdraw-approve'
            ),
            path(
                '<int:object_id>/actions/reject/',
                self.admin_site.admin_view(self.reject_request),
                name='withdraw-reject'
            ),
        ]

        return custom_urls + urls

    def approve_request(self, request, object_id):

        obj = WithdrawalRequest.objects.get(pk=object_id)

        if obj.status == 'PENDING':

            profile, _ = UserProfileBalance.objects.get_or_create(
                device_token=obj.device_token
            )

            if profile.coins >= obj.amount:

                profile.coins -= obj.amount
                profile.save()

                obj.status = 'APPROVED'
                obj.save()

                self.message_user(
                    request,
                    "Withdrawal approved successfully!",
                    messages.SUCCESS
                )

            else:

                self.message_user(
                    request,
                    "Error: User does not have sufficient balance!",
                    messages.ERROR
                )

        return redirect(f'../../')

    def reject_request(self, request, object_id):

        obj = WithdrawalRequest.objects.get(pk=object_id)

        if obj.status == 'PENDING':

            obj.status = 'REJECTED'
            obj.save()

            self.message_user(
                request,
                "Withdrawal request rejected.",
                messages.WARNING
            )

        return redirect(f'../../')


# ==============================================================================
# SYSTEM SETTINGS  (App config — support contacts, terms, etc.)
# ==============================================================================

@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):

    list_display = (
        'key',
        'value',
        'updated_at',
    )

    search_fields = (
        'key',
        'value',
    )

    list_editable = (
        'value',
    )

    ordering = (
        'key',
    )

    help_text = (
        "Common keys: support_whatsapp, support_telegram, "
        "support_email, terms_text, privacy_text, app_version, "
        "gift_enabled, paid_spin_cost"
    )
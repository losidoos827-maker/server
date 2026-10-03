import json

from django.contrib import messages
from django.contrib.auth.hashers import check_password
from django.http import JsonResponse

from django.shortcuts import (
    get_object_or_404,
    redirect,
    render,
)

from django.views.decorators.csrf import (
    csrf_exempt,
)

from django.contrib.admin.views.decorators import (
    staff_member_required,
)

from django.db import transaction

from ..models import (
    WithdrawalRequest,
    UserProfileBalance,
    SystemTransactionLog,
)

from ..services.wager_service import (
    process_withdrawal_approval,
)


# ==========================================
# MOBILE WITHDRAWAL
#
# Coins are DEDUCTED immediately at request time.
# If approved → coins stay deducted.
# If rejected → coins are REFUNDED back.
# ==========================================

@csrf_exempt
def submit_withdrawal_request(request):

    if request.method != "POST":

        return JsonResponse(
            {"error": "POST required"},
            status=405,
        )

    try:

        data = json.loads(request.body)

        device_token = data.get(
            "device_token"
        )

        amount = int(
            data.get("amount", 0)
        )

        method = data.get("method")

        account_title = data.get(
            "account_title"
        )

        account_number = data.get(
            "account_number"
        )

        pin = str(
            data.get("pin", "")
        ).strip()

        if not all([
            device_token,
            amount > 0,
            method,
            account_title,
            account_number,
        ]):

            return JsonResponse(
                {
                    "error": (
                        "Invalid fields "
                        "verification failure"
                    )
                },
                status=400,
            )

        if not pin:

            return JsonResponse(
                {
                    "error":
                        "Withdrawal PIN is required.",
                },
                status=400,
            )

        with transaction.atomic():

            profile = (
                UserProfileBalance.objects
                .select_for_update()
                .filter(device_token=device_token)
                .first()
            )

            if not profile:

                return JsonResponse(
                    {
                        "error":
                            "User wallet profile not found.",
                    },
                    status=404,
                )

            # ----------------------------------------------
            # PIN must be set
            # ----------------------------------------------

            if not profile.has_withdrawal_pin:

                return JsonResponse(
                    {
                        "error": (
                            "Withdrawal PIN not set. "
                            "Set your PIN first."
                        ),
                        "code": "PIN_NOT_SET",
                    },
                    status=400,
                )

            # ----------------------------------------------
            # Verify PIN
            # ----------------------------------------------

            if not check_password(
                pin,
                profile.withdrawal_pin_hash,
            ):

                return JsonResponse(
                    {
                        "error": "Incorrect PIN.",
                        "code": "PIN_INCORRECT",
                    },
                    status=400,
                )

            # ----------------------------------------------
            # Balance check
            # ----------------------------------------------

            if profile.coins < amount:

                return JsonResponse(
                    {
                        "error": (
                            "Insufficient wallet balance "
                            "to request withdrawal"
                        )
                    },
                    status=400,
                )

            # ==============================================
            # DEDUCT COINS IMMEDIATELY
            # ==============================================

            profile.coins -= amount
            profile.save(update_fields=["coins"])

            # ==============================================
            # Ledger entry
            # ==============================================

            withdrawal = WithdrawalRequest.objects.create(
                device_token=device_token,
                amount=amount,
                method=method,
                account_title=account_title,
                account_number=account_number,
                status="PENDING",
            )

            SystemTransactionLog.objects.create(
                user_profile=profile,
                amount=-amount,
                log_type="WITHDRAWAL",
                reference_id=f"REQ_{withdrawal.id}",
            )

            print(
                f"💸 WITHDRAWAL REQUEST | "
                f"device={device_token} | "
                f"amount={amount} | "
                f"new_balance={profile.coins}"
            )

        return JsonResponse({
            "status": "success",
            "message": (
                "Withdrawal request submitted. "
                "Coins deducted from wallet."
            ),
            "new_balance": profile.coins,
        })

    except (
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ):

        return JsonResponse(
            {"error": "Invalid request payload."},
            status=400,
        )

    except Exception as exc:

        return JsonResponse(
            {"error": str(exc)},
            status=400,
        )


# ==========================================
# ADMIN WITHDRAWAL DASHBOARD
# ==========================================

@staff_member_required
def custom_withdrawal_dashboard(request):

    withdrawals = (
        WithdrawalRequest.objects
        .all()
        .order_by("-created_at")
    )

    withdrawal_items = []

    for withdrawal in withdrawals:

        profile, _ = (
            UserProfileBalance.objects
            .get_or_create(
                device_token=(
                    withdrawal.device_token
                )
            )
        )

        withdrawal_items.append({
            "request":
                withdrawal,

            "current_balance":
                profile.coins,

            # Coins already deducted at request time
            # so this is just informational
            "has_enough":
                True,
        })

    context = {
        "withdrawal_items":
            withdrawal_items
    }

    if (
        request.method == "GET"
        and request.headers.get(
            "x-requested-with"
        ) == "XMLHttpRequest"
    ):

        return render(
            request,
            "withdrawal_table_partial.html",
            context,
        )

    return render(
        request,
        "withdrawal_change_form.html",
        context,
    )


# ==========================================
# APPROVE WITHDRAWAL
#
# Coins were already deducted at request time.
# Just change status to APPROVED.
# ==========================================

@staff_member_required
def approve_withdrawal_custom(
    request,
    withdraw_id,
):

    withdrawal = get_object_or_404(
        WithdrawalRequest,
        id=withdraw_id,
        status="PENDING",
    )

    result = process_withdrawal_approval(
        withdrawal.id
    )

    if (
        request.headers.get(
            "x-requested-with"
        ) == "XMLHttpRequest"
    ):

        return JsonResponse(
            result,
            status=(
                200
                if result["status"] == "success"
                else 400
            ),
        )

    if result["status"] == "success":

        messages.success(
            request,
            result["message"],
        )

    else:

        messages.error(
            request,
            result["message"],
        )

    return redirect(
        "custom_withdrawal_dashboard"
    )


# ==========================================
# REJECT WITHDRAWAL
#
# Coins are REFUNDED back to the user's wallet.
# ==========================================

@staff_member_required
def reject_withdrawal_custom(
    request,
    withdraw_id,
):

    with transaction.atomic():

        withdrawal = get_object_or_404(
            WithdrawalRequest,
            id=withdraw_id,
            status="PENDING",
        )

        # ----------------------------------------------
        # Refund coins to user
        # ----------------------------------------------

        profile = (
            UserProfileBalance.objects
            .select_for_update()
            .filter(
                device_token=withdrawal.device_token
            )
            .first()
        )

        if profile:

            profile.coins += withdrawal.amount
            profile.save(update_fields=["coins"])

            # ------------------------------------------
            # Refund ledger
            # ------------------------------------------

            SystemTransactionLog.objects.create(
                user_profile=profile,
                amount=withdrawal.amount,
                log_type="WAGER_REFUND",
                reference_id=f"WD_REJECT_{withdrawal.id}",
            )

            print(
                f"↩️ WITHDRAWAL REJECTED | "
                f"device={withdrawal.device_token} | "
                f"amount={withdrawal.amount} | "
                f"refunded_to={profile.coins}"
            )

        withdrawal.status = "REJECTED"
        withdrawal.save(update_fields=["status"])

    if (
        request.headers.get(
            "x-requested-with"
        ) == "XMLHttpRequest"
    ):

        return JsonResponse({
            "status": "success",
            "message": (
                "Withdrawal rejected. "
                "Coins refunded to user."
            ),
            "refunded_amount": withdrawal.amount,
        })

    messages.warning(
        request,
        (
            f"Withdrawal rejected. "
            f"{withdrawal.amount} coins refunded "
            f"to {withdrawal.device_token}."
        ),
    )

    return redirect(
        "custom_withdrawal_dashboard"
    )
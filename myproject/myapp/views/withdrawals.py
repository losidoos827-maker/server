import json

from django.contrib import messages
from django.contrib.auth.hashers import check_password
from django.http import JsonResponse
from django.utils import timezone

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

from datetime import timedelta

from ..models import (
    WithdrawalRequest,
    UserProfileBalance,
    SystemTransactionLog,
)

from ..services.wager_service import (
    process_withdrawal_approval,
)


# ==========================================================
# CONSTANTS
# ==========================================================

# After this many approved withdrawals, user must provide
# FRESH location on every subsequent withdrawal.
LOCATION_REQUIRED_AFTER = 8

# Location must have been updated within this many minutes
# to be considered "fresh" for a withdrawal.
LOCATION_FRESHNESS_MINUTES = 5


# ==========================================================
# HELPER — CHECK LOCATION REQUIREMENT
# ==========================================================

def _check_location_requirement(profile):

    # Count approved withdrawals
    approved_count = (
        WithdrawalRequest.objects
        .filter(
            device_token=profile.device_token,
            status="APPROVED",
        )
        .count()
    )

    # Location becomes mandatory after 3 approvals
    requires_location = (
        approved_count >= LOCATION_REQUIRED_AFTER
    )

    # Check if we have a RECENT location on file
    has_fresh_location = False

    if (
        profile.latitude is not None
        and profile.longitude is not None
        and profile.location_updated_at is not None
    ):

        age = (
            timezone.now()
            - profile.location_updated_at
        )

        has_fresh_location = (
            age < timedelta(
                minutes=LOCATION_FRESHNESS_MINUTES
            )
        )

    return (
        approved_count,
        requires_location,
        has_fresh_location,
    )


# ==========================================================
# WITHDRAWAL STATUS  (Android checks before withdraw)
# ==========================================================

@csrf_exempt
def get_withdrawal_status(request, device_token):

    if not device_token:

        return JsonResponse(
            {"status": "error", "message": "device_token required"},
            status=400,
        )

    profile = (
        UserProfileBalance.objects
        .filter(device_token=device_token)
        .first()
    )

    if not profile:

        return JsonResponse(
            {"status": "error", "message": "Profile not found."},
            status=404,
        )

    (
        approved_count,
        requires_location,
        has_fresh_location,
    ) = _check_location_requirement(profile)

    return JsonResponse({
        "status": "success",
        "approved_withdrawal_count": approved_count,
        "requires_location": requires_location,
        "has_location": has_fresh_location,
        "location_freshness_minutes": LOCATION_FRESHNESS_MINUTES,
        "location_required_and_missing": (
            requires_location and not has_fresh_location
        ),
    })


# ==========================================================
# MOBILE WITHDRAWAL
# ==========================================================

@csrf_exempt
def submit_withdrawal_request(request):

    if request.method != "POST":

        return JsonResponse(
            {"error": "POST required"},
            status=405,
        )

    try:

        data = json.loads(request.body)

        device_token = data.get("device_token")
        amount = int(data.get("amount", 0))
        method = data.get("method")
        account_title = data.get("account_title")
        account_number = data.get("account_number")
        pin = str(data.get("pin", "")).strip()

        if not all([
            device_token,
            amount > 0,
            method,
            account_title,
            account_number,
        ]):

            return JsonResponse(
                {"error": "Invalid fields verification failure"},
                status=400,
            )

        if not pin:

            return JsonResponse(
                {"error": "Withdrawal PIN is required."},
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
                    {"error": "User wallet profile not found."},
                    status=404,
                )

            # ==================================================
            # LOCATION CHECK — fresh location required
            # ==================================================

            (
                approved_count,
                requires_location,
                has_fresh_location,
            ) = _check_location_requirement(profile)

            if requires_location and not has_fresh_location:

                return JsonResponse(
                    {
                        "error": (
                            "Fresh location required. "
                            "Please grant location permission "
                            "again to continue withdrawing."
                        ),
                        "code": "LOCATION_REQUIRED",
                    },
                    status=400,
                )

            # ==================================================
            # PIN CHECK
            # ==================================================

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

            # ==================================================
            # BALANCE CHECK
            # ==================================================

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

            # ==================================================
            # DEDUCT + CREATE REQUEST
            # ==================================================

            profile.coins -= amount
            profile.save(update_fields=["coins"])

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

            # ==================================================
            # CONSUME LOCATION — force fresh next time
            #
            # After a successful withdrawal submission,
            # we clear the location so the user must
            # provide it fresh for the next one.
            # ==================================================

            if requires_location:

                profile.location_updated_at = None

                profile.save(
                    update_fields=["location_updated_at"]
                )

                print(
                    f"📍 LOCATION CONSUMED | "
                    f"device={device_token} — "
                    f"fresh location required next time"
                )

            print(
                f"💸 WITHDRAWAL REQUEST | "
                f"device={device_token} | "
                f"amount={amount} | "
                f"new_balance={profile.coins} | "
                f"approved_count={approved_count}"
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


# ==========================================================
# ADMIN WITHDRAWAL DASHBOARD
# ==========================================================

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
                device_token=withdrawal.device_token
            )
        )

        withdrawal_items.append({
            "request": withdrawal,
            "current_balance": profile.coins,
            "has_enough": True,
        })

    context = {
        "withdrawal_items": withdrawal_items
    }

    if (
        request.method == "GET"
        and request.headers.get("x-requested-with")
        == "XMLHttpRequest"
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


# ==========================================================
# APPROVE WITHDRAWAL
# ==========================================================

@staff_member_required
def approve_withdrawal_custom(request, withdraw_id):

    withdrawal = get_object_or_404(
        WithdrawalRequest,
        id=withdraw_id,
        status="PENDING",
    )

    result = process_withdrawal_approval(withdrawal.id)

    if (
        request.headers.get("x-requested-with")
        == "XMLHttpRequest"
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

        messages.success(request, result["message"])

    else:

        messages.error(request, result["message"])

    return redirect("custom_withdrawal_dashboard")


# ==========================================================
# REJECT WITHDRAWAL
# ==========================================================

@staff_member_required
def reject_withdrawal_custom(request, withdraw_id):

    with transaction.atomic():

        withdrawal = get_object_or_404(
            WithdrawalRequest,
            id=withdraw_id,
            status="PENDING",
        )

        profile = (
            UserProfileBalance.objects
            .select_for_update()
            .filter(device_token=withdrawal.device_token)
            .first()
        )

        if profile:

            profile.coins += withdrawal.amount
            profile.save(update_fields=["coins"])

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
        request.headers.get("x-requested-with")
        == "XMLHttpRequest"
    ):

        return JsonResponse({
            "status": "success",
            "message": (
                "Withdrawal rejected. Coins refunded to user."
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

    return redirect("custom_withdrawal_dashboard")
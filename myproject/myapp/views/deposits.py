import json

from django.contrib import messages
from django.db import transaction
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

from ..models import (
    DepositRequest,
    UserProfileBalance,
    SystemTransactionLog,
    ReferralSystem,
)


# ==========================================
# MOBILE DEPOSIT
# ==========================================

@csrf_exempt
def submit_deposit_request(request):

    if request.method != "POST":

        return JsonResponse(
            {"error": "POST required"},
            status=405,
        )

    try:

        data = json.loads(request.body)

        device_token = data.get("device_token")

        amount = int(data.get("amount", 0))

        method = data.get("payment_method")

        sender_name = data.get("sender_name", "")

        if (
            not device_token
            or amount <= 0
            or not method
        ):

            return JsonResponse(
                {
                    "error": (
                        "Invalid payload values "
                        "validation failure"
                    )
                },
                status=400,
            )

        DepositRequest.objects.create(
            device_token=device_token,
            amount=amount,
            payment_method=method,
            sender_name=sender_name,
            status="PENDING",
        )

        return JsonResponse({
            "status": "success",
            "message": "Verification submitted successfully!",
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
# ADMIN DEPOSIT DASHBOARD
# ==========================================

@staff_member_required
def custom_deposit_dashboard(request):

    deposits = (
        DepositRequest.objects
        .all()
        .order_by("-created_at")
    )

    context = {
        "deposit_items": deposits
    }

    if (
        request.method == "GET"
        and request.headers.get(
            "x-requested-with"
        ) == "XMLHttpRequest"
    ):

        return render(
            request,
            "deposit_table_partial.html",
            context,
        )

    return render(
        request,
        "deposit_change_form.html",
        context,
    )


# ==========================================
# APPROVE DEPOSIT  (staff only)
#
# On first approved deposit for a user:
#   - Mark has_made_first_deposit = True
#   - Trigger referrer reward if applicable
# ==========================================

@staff_member_required
def approve_deposit_custom(
    request,
    deposit_id,
):

    referrer_reward_credited = 0

    with transaction.atomic():

        deposit = get_object_or_404(
            DepositRequest,
            id=deposit_id,
            status="PENDING",
        )

        profile, _ = (
            UserProfileBalance.objects
            .select_for_update()
            .get_or_create(
                device_token=deposit.device_token
            )
        )

        # ----------------------------------------------
        # Credit user with deposit amount
        # ----------------------------------------------

        profile.coins += deposit.amount

        # ----------------------------------------------
        # First deposit check
        # ----------------------------------------------

        is_first_deposit = not profile.has_made_first_deposit

        if is_first_deposit:
            profile.has_made_first_deposit = True

        profile.save(
            update_fields=[
                "coins",
                "has_made_first_deposit",
            ]
        )

        deposit.status = "APPROVED"
        deposit.save(update_fields=["status"])

        # ----------------------------------------------
        # Deposit ledger
        # ----------------------------------------------

        SystemTransactionLog.objects.create(
            user_profile=profile,
            amount=deposit.amount,
            log_type="DEPOSIT",
            reference_id=str(deposit.id),
        )

        # ==============================================
        # REFERRAL REWARD (only on FIRST deposit)
        # ==============================================

        if is_first_deposit:

            print(
                f"🎉 FIRST DEPOSIT | "
                f"user={profile.device_token} | "
                f"amount={deposit.amount}"
            )

            # Find if this user was referred
            referral = (
                ReferralSystem.objects
                .select_for_update()
                .filter(
                    referred_user=profile,
                    referrer_reward_paid=False,
                )
                .first()
            )

            if referral and referral.referrer:

                # Lock referrer profile
                referrer = (
                    UserProfileBalance.objects
                    .select_for_update()
                    .filter(id=referral.referrer.id)
                    .first()
                )

                if referrer:

                    reward_amount = (
                        referral.referrer_reward_amount
                        or 50
                    )

                    # Credit referrer
                    referrer.coins += reward_amount
                    referrer.save(update_fields=["coins"])

                    # Mark referral as paid
                    referral.referrer_reward_paid = True
                    referral.total_commission_earned = (
                        referral.total_commission_earned
                        + reward_amount
                    )
                    referral.save(
                        update_fields=[
                            "referrer_reward_paid",
                            "total_commission_earned",
                        ]
                    )

                    # Ledger entry
                    SystemTransactionLog.objects.create(
                        user_profile=referrer,
                        amount=reward_amount,
                        log_type="REFERRAL_INVITE_REWARD",
                        reference_id=(
                            f"INVITE_"
                            f"{profile.referral_code}_"
                            f"{deposit.id}"
                        ),
                    )

                    referrer_reward_credited = reward_amount

                    print(
                        f"💰 REFERRER REWARD PAID | "
                        f"referrer={referrer.device_token} | "
                        f"amount={reward_amount}"
                    )

    # ==============================================
    # RESPONSE
    # ==============================================

    if (
        request.headers.get(
            "x-requested-with"
        ) == "XMLHttpRequest"
    ):

        return JsonResponse({
            "status": "success",
            "message": (
                f"Approved {deposit.amount} "
                "coins successfully!"
                + (
                    f" Referrer rewarded "
                    f"{referrer_reward_credited} coins."
                    if referrer_reward_credited
                    else ""
                )
            ),
            "referrer_reward": referrer_reward_credited,
        })

    messages.success(
        request,
        (
            f"Approved {deposit.amount} coins "
            f"for {deposit.device_token} "
            f"successfully!"
            + (
                f" Referrer got "
                f"{referrer_reward_credited} coins."
                if referrer_reward_credited
                else ""
            )
        ),
    )

    return redirect("custom_deposit_dashboard")


# ==========================================
# REJECT DEPOSIT  (staff only)
# ==========================================

@staff_member_required
def reject_deposit_custom(
    request,
    deposit_id,
):

    deposit = get_object_or_404(
        DepositRequest,
        id=deposit_id,
        status="PENDING",
    )

    deposit.status = "REJECTED"
    deposit.save(update_fields=["status"])

    if (
        request.headers.get(
            "x-requested-with"
        ) == "XMLHttpRequest"
    ):

        return JsonResponse({
            "status": "success",
            "message": "Deposit request rejected.",
        })

    messages.error(
        request,
        (
            f"Rejected deposit request from "
            f"{deposit.device_token}."
        ),
    )

    return redirect("custom_deposit_dashboard")
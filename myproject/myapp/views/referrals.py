# views/referrals.py

import json

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from ..models import (
    UserProfileBalance,
    ReferralSystem,
    SystemTransactionLog,
    SystemSetting,
)


# ==========================================================
# HELPERS
# ==========================================================

def _get_signup_bonus():
    """
    Bonus given to the NEW user who signs up with a code.
    """
    try:
        return int(
            SystemSetting.get_value(
                "referral_signup_bonus",
                "50"
            )
        )
    except (ValueError, TypeError):
        return 50


def _get_invite_reward():
    """
    Reward given to the REFERRER when the invited
    user makes their first deposit.
    """
    try:
        return int(
            SystemSetting.get_value(
                "referral_invite_reward",
                "50"
            )
        )
    except (ValueError, TypeError):
        return 50


# ==========================================================
# GET USER REFERRAL CODE
# ==========================================================

def get_user_referral_code(
    request,
    device_token,
):

    try:

        profile, _ = (
            UserProfileBalance.objects
            .get_or_create(
                device_token=device_token
            )
        )

        if not profile.referral_code:
            profile.save()
            profile.refresh_from_db()

        return JsonResponse({
            "status": "success",
            "referral_code": profile.referral_code,
        })

    except Exception as exc:

        return JsonResponse(
            {"status": "error", "message": str(exc)},
            status=500,
        )


# ==========================================================
# APPLY REFERRAL
#
# New user gets signup bonus immediately.
# Referrer gets reward later — when the new user
# makes their FIRST approved deposit.
# ==========================================================

@csrf_exempt
def verify_and_apply_referral(request):

    if request.method != "POST":

        return JsonResponse(
            {"status": "error", "message": "POST required."},
            status=405,
        )

    try:

        data = json.loads(request.body)

        device_token = data.get("device_token")

        referral_code = (
            data.get("referral_code", "")
            .strip()
            .upper()
        )

    except (
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):

        return JsonResponse(
            {
                "status": "error",
                "message": "Malformed JSON payload parameters",
            },
            status=400,
        )

    if not device_token or not referral_code:

        return JsonResponse(
            {
                "status": "error",
                "message": (
                    "Field verification failure! "
                    "device_token and referral_code are required."
                ),
            },
            status=400,
        )

    try:

        user_profile, _ = (
            UserProfileBalance.objects
            .get_or_create(device_token=device_token)
        )

        if (
            ReferralSystem.objects
            .filter(referred_user=user_profile)
            .exists()
        ):

            return JsonResponse(
                {
                    "status": "error",
                    "message": (
                        "Referral milestone already claimed "
                        "on this device."
                    ),
                },
                status=400,
            )

        if user_profile.referral_code == referral_code:

            return JsonResponse(
                {
                    "status": "error",
                    "message": "Self-referral operation is invalid.",
                },
                status=400,
            )

        referrer = (
            UserProfileBalance.objects
            .filter(referral_code=referral_code)
            .first()
        )

        if not referrer:

            return JsonResponse(
                {
                    "status": "error",
                    "message": (
                        f'The referral code "{referral_code}" '
                        "does not exist in the database."
                    ),
                },
                status=400,
            )

        with transaction.atomic():

            signup_bonus = _get_signup_bonus()
            invite_reward = _get_invite_reward()

            user_profile = (
                UserProfileBalance.objects
                .select_for_update()
                .get(id=user_profile.id)
            )

            # ----------------------------------------------
            # Give signup bonus to the NEW user only
            # ----------------------------------------------

            user_profile.coins += signup_bonus
            user_profile.save(update_fields=["coins"])

            SystemTransactionLog.objects.create(
                user_profile=user_profile,
                amount=signup_bonus,
                log_type="REFERRAL_BONUS",
                reference_id=(
                    f"REFERRED_BY_{referrer.referral_code}"
                ),
            )

            # ----------------------------------------------
            # Create referral record
            #
            # Referrer reward is NOT paid yet.
            # It will be paid when user_profile makes
            # their first deposit (see approve_deposit).
            # ----------------------------------------------

            ReferralSystem.objects.create(
                referrer=referrer,
                referred_user=user_profile,
                total_commission_earned=0,
                referrer_reward_paid=False,
                referrer_reward_amount=invite_reward,
            )

            print(
                f"🎁 REFERRAL APPLIED | "
                f"new_user={user_profile.device_token} | "
                f"referrer={referrer.device_token} | "
                f"signup_bonus={signup_bonus} | "
                f"invite_reward_pending={invite_reward}"
            )

        return JsonResponse({
            "status": "success",
            "message": "Referral applied successfully.",
            "signup_bonus": signup_bonus,
        })

    except Exception as exc:

        return JsonResponse(
            {
                "status": "error",
                "message": (
                    "Internal Ledger processing failure: "
                    f"{exc}"
                ),
            },
            status=500,
        )


# ==========================================================
# REFERRAL DASHBOARD
# ==========================================================

def get_referral_dashboard(
    request,
    device_token,
):

    print(f"🔍 REFERRAL DASHBOARD HIT | device_token={device_token}")

    profile = (
        UserProfileBalance.objects
        .filter(device_token=device_token)
        .first()
    )

    if not profile:

        print(f"❌ REFERRAL DASHBOARD | Profile not found")

        return JsonResponse(
            {"status": "error", "message": "Profile not found."},
            status=404,
        )

    if not profile.referral_code:

        print(f"⚠️ SELF-HEAL | Generating new referral_code...")

        profile.save()
        profile.refresh_from_db()

    referrals = (
        ReferralSystem.objects
        .filter(referrer=profile)
        .select_related("referred_user")
        .order_by("-created_at")
    )

    total_referred = referrals.count()

    total_commission = (
        sum(
            r.total_commission_earned or 0
            for r in referrals
        )
    )

    # Pending rewards (not yet triggered by first deposit)
    pending_rewards = referrals.filter(
        referrer_reward_paid=False
    ).count()

    referred_list = []

    for r in referrals[:50]:

        referred = r.referred_user

        name = (
            referred.nickname
            or "Player"
        )

        pic_url = None

        if referred.profile_pic:
            try:
                pic_url = referred.profile_pic.url
            except Exception:
                pic_url = None

        referred_list.append({
            "name": name,
            "profile_pic": pic_url,
            "joined_at": r.created_at.strftime("%d %b %Y"),
            "commission_earned": (
                r.total_commission_earned or 0
            ),
            "reward_paid": r.referrer_reward_paid,
            "reward_amount": r.referrer_reward_amount or 0,
        })

    response_data = {
        "status": "success",
        "referral_code": profile.referral_code,
        "total_referred": total_referred,
        "total_commission_earned": total_commission,
        "pending_rewards": pending_rewards,
        "referred_users": referred_list,
    }

    print(f"📤 REFERRAL DASHBOARD RESPONSE | {response_data}")

    return JsonResponse(response_data)
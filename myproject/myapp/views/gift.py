# views/gift.py

import json
import random
import time

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from ..models import (
    UserProfileBalance,
    SystemTransactionLog,
    SystemSetting,
    SpinHistory,
)


# ==========================================================
# GIFT POOL
# ==========================================================

GIFTS = [
    {"name": "50 Coins", "emoji": "🪙", "coin_value": 50},
    {"name": "10 Coins", "emoji": "🪙", "coin_value": 10},
    {"name": "20 Coins", "emoji": "💰", "coin_value": 20},
    {"name": "Rose", "emoji": "🌹", "coin_value": 0},
    {"name": "30 Diamond", "emoji": "💎", "coin_value": 30},
    {"name": "Try Again", "emoji": "✨", "coin_value": 0},
    {"name": "40 Coins", "emoji": "🪙", "coin_value": 40},
    {"name": "Crown", "emoji": "👑", "coin_value": 0},
]

FREE_SPIN_COOLDOWN = 24 * 60 * 60
DAILY_BONUS_COOLDOWN = 24 * 60 * 60


# ==========================================================
# HELPERS
# ==========================================================

def get_paid_cost():
    return int(
        SystemSetting.get_value("paid_spin_cost", "40")
    )


def is_gift_enabled():
    return (
        SystemSetting.get_value("gift_enabled", "1") == "1"
    )


def _get_bonus_amount():
    try:
        return int(
            SystemSetting.get_value(
                "daily_bonus_amount",
                "100"
            )
        )
    except (ValueError, TypeError):
        return 100


# ==========================================================
# SPIN STATUS
# ==========================================================

@csrf_exempt
def get_spin_status(request):

    if request.method != "POST":
        return JsonResponse(
            {"error": "POST required"},
            status=405,
        )

    try:
        data = json.loads(request.body)
        device_token = data.get("device_token")

        if not device_token:
            return JsonResponse(
                {"error": "device_token required"},
                status=400,
            )

        if not is_gift_enabled():
            return JsonResponse(
                {"error": "Gift system disabled"},
                status=403,
            )

        profile, _ = (
            UserProfileBalance.objects
            .get_or_create(device_token=device_token)
        )

        last_spin = (
            getattr(profile, "last_free_spin", 0) or 0
        )

        now = int(time.time())

        can_free = (now - last_spin) > FREE_SPIN_COOLDOWN

        time_left = (
            0 if can_free
            else FREE_SPIN_COOLDOWN - (now - last_spin)
        )

        return JsonResponse({
            "status": "success",
            "can_use_free_spin": can_free,
            "time_left": time_left,
            "coins": profile.coins,
            "paid_spin_cost": get_paid_cost(),
            "gifts": GIFTS,
        })

    except Exception as exc:
        return JsonResponse(
            {"error": str(exc)},
            status=400,
        )


# ==========================================================
# SUBMIT SPIN
# ==========================================================

@csrf_exempt
def submit_spin(request):

    if request.method != "POST":
        return JsonResponse(
            {"error": "POST required"},
            status=405,
        )

    try:
        data = json.loads(request.body)
        device_token = data.get("device_token")

        if not device_token:
            return JsonResponse(
                {"error": "device_token required"},
                status=400,
            )

        if not is_gift_enabled():
            return JsonResponse(
                {"error": "Gift system disabled"},
                status=403,
            )

        with transaction.atomic():

            profile, _ = (
                UserProfileBalance.objects
                .select_for_update()
                .get_or_create(
                    device_token=device_token
                )
            )

            last_spin = (
                getattr(profile, "last_free_spin", 0) or 0
            )

            now = int(time.time())

            is_free = (
                now - last_spin
            ) > FREE_SPIN_COOLDOWN

            paid_cost = get_paid_cost()

            cost = 0 if is_free else paid_cost

            if profile.coins < cost:
                return JsonResponse(
                    {"error": "Not enough coins"},
                    status=400,
                )

            if cost > 0:
                profile.coins -= cost

                SystemTransactionLog.objects.create(
                    user_profile=profile,
                    amount=-cost,
                    log_type="SPIN_COST",
                    reference_id=f"spin_{now}",
                )

            won_index = random.randint(
                0, len(GIFTS) - 1
            )

            won_gift = GIFTS[won_index]

            if won_gift["coin_value"] > 0:
                profile.coins += won_gift["coin_value"]

                SystemTransactionLog.objects.create(
                    user_profile=profile,
                    amount=won_gift["coin_value"],
                    log_type="SPIN_REWARD",
                    reference_id=f"spin_{now}",
                )

            if is_free:
                profile.last_free_spin = now

            profile.save()

            # Record spin history
            SpinHistory.objects.create(
                user_profile=profile,
                gift_name=won_gift["name"],
                gift_emoji=won_gift["emoji"],
                coin_value=won_gift["coin_value"],
                was_free=is_free,
                cost_paid=cost,
            )

        return JsonResponse({
            "status": "success",
            "won_index": won_index,
            "won_gift": won_gift,
            "coins": profile.coins,
            "was_free": is_free,
        })

    except Exception as exc:
        return JsonResponse(
            {"error": str(exc)},
            status=400,
        )


# ==========================================================
# GIFT CONFIG
# ==========================================================

@csrf_exempt
def gift_config_api(request):
    return JsonResponse({
        "gift_enabled": "1" if is_gift_enabled() else "0",
        "paid_spin_cost": get_paid_cost(),
    })


# ==========================================================
# GIFT CLAIM (alias for submit_spin)
# ==========================================================

@csrf_exempt
def gift_claim_api(request):
    return submit_spin(request)


# ==========================================================
# SPIN HISTORY
# ==========================================================

def get_spin_history(request, device_token):

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

    try:
        limit = int(request.GET.get("limit", 20))
    except (ValueError, TypeError):
        limit = 20

    limit = max(1, min(limit, 100))

    entries = (
        SpinHistory.objects
        .filter(user_profile=profile)
        .order_by("-spun_at")[:limit]
    )

    payload = []

    for e in entries:

        payload.append({
            "gift_name": e.gift_name,
            "gift_emoji": e.gift_emoji,
            "coin_value": e.coin_value,
            "was_free": e.was_free,
            "cost_paid": e.cost_paid,
            "spun_at": e.spun_at.strftime("%d %b %Y, %I:%M %p"),
        })

    return JsonResponse({
        "status": "success",
        "history": payload,
    })


# ==========================================================
# DAILY BONUS — STATUS
# ==========================================================

@csrf_exempt
def get_daily_bonus_status(request, device_token):

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

    now = int(time.time())
    last = int(profile.last_daily_bonus or 0)
    elapsed = now - last

    can_claim = elapsed >= DAILY_BONUS_COOLDOWN

    time_left = (
        0 if can_claim
        else DAILY_BONUS_COOLDOWN - elapsed
    )

    return JsonResponse({
        "status": "success",
        "can_claim": can_claim,
        "time_left": time_left,
        "bonus_amount": _get_bonus_amount(),
        "coins": profile.coins,
    })


# ==========================================================
# DAILY BONUS — CLAIM
# ==========================================================

@csrf_exempt
def claim_daily_bonus(request):

    if request.method != "POST":
        return JsonResponse(
            {"status": "error", "message": "POST required."},
            status=405,
        )

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse(
            {"status": "error", "message": "Invalid JSON."},
            status=400,
        )

    device_token = str(data.get("device_token", "")).strip()

    if not device_token:
        return JsonResponse(
            {"status": "error", "message": "device_token is required."},
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
                {"status": "error", "message": "Profile not found."},
                status=404,
            )

        now = int(time.time())
        last = int(profile.last_daily_bonus or 0)
        elapsed = now - last

        if elapsed < DAILY_BONUS_COOLDOWN:
            time_left = DAILY_BONUS_COOLDOWN - elapsed
            return JsonResponse({
                "status": "error",
                "message": "Already claimed. Try again later.",
                "time_left": time_left,
            }, status=400)

        bonus_amount = _get_bonus_amount()

        profile.coins += bonus_amount
        profile.last_daily_bonus = now

        profile.save(
            update_fields=["coins", "last_daily_bonus"]
        )

        SystemTransactionLog.objects.create(
            user_profile=profile,
            amount=bonus_amount,
            log_type="DEPOSIT",
            reference_id=f"DAILY_{now}",
        )

        print(
            f"🎁 DAILY BONUS | "
            f"device={device_token} | "
            f"amount={bonus_amount} | "
            f"new_balance={profile.coins}"
        )

        return JsonResponse({
            "status": "success",
            "message": f"Daily bonus claimed! +{bonus_amount} coins",
            "bonus_amount": bonus_amount,
            "coins": profile.coins,
        })
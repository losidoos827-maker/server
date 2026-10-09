from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone

import json

from ..models import (
    DepositRequest,
    WithdrawalRequest,
    SystemPaymentMethod,
    UserProfileBalance,
)


# ==========================================================
# CLIENT IP HELPER
# ==========================================================

def _get_client_ip(request):

    xff = request.META.get("HTTP_X_FORWARDED_FOR")

    if xff:
        return xff.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR", "")


# ==========================================================
# TRANSACTION HISTORY
# ==========================================================

def get_transaction_history(
    request,
    device_token,
):

    transactions = []

    deposits = (
        DepositRequest.objects
        .filter(device_token=device_token)
        .order_by("-created_at")
    )

    for deposit in deposits:

        transactions.append({
            "type": "DEPOSIT",
            "amount": deposit.amount,
            "status": deposit.status,
            "date": deposit.created_at,
        })

    withdrawals = (
        WithdrawalRequest.objects
        .filter(device_token=device_token)
        .order_by("-created_at")
    )

    for withdrawal in withdrawals:

        transactions.append({
            "type": "WITHDRAWAL",
            "amount": withdrawal.amount,
            "status": withdrawal.status,
            "date": withdrawal.created_at,
        })

    transactions.sort(
        key=lambda item: item["date"],
        reverse=True,
    )

    for item in transactions:

        item["date"] = item["date"].strftime(
            "%d %b %Y, %I:%M %p"
        )

    return JsonResponse({
        "status": "success",
        "transactions": transactions,
    })


# ==========================================================
# ACTIVE PAYMENT DETAILS
# ==========================================================

def get_active_payment_details(request):

    methods = (
        SystemPaymentMethod.objects
        .filter(is_active=True)
    )

    data = {
        method.method_type: {
            "name": method.account_name,
            "number": method.account_number,
        }
        for method in methods
    }

    return JsonResponse({
        "status": "success",
        "methods": data,
    })


# ==========================================================
# USER BALANCE  (also captures IP)
# ==========================================================

def get_user_balance(
    request,
    device_token,
):

    profile, _ = (
        UserProfileBalance.objects
        .get_or_create(
            device_token=device_token
        )
    )

    client_ip = _get_client_ip(request)

    if client_ip and profile.ip_address != client_ip:

        profile.ip_address = client_ip
        profile.save(update_fields=["ip_address"])

    return JsonResponse({
        "status": "success",
        "coins": profile.coins,
        "locked_coins": profile.locked_coins,
    })


# ==========================================================
# USER LOCATION  (GPS)
# ==========================================================

@csrf_exempt
def update_user_location(request):

    if request.method != "POST":

        return JsonResponse(
            {"error": "POST required"},
            status=405,
        )

    try:

        data = json.loads(request.body)

        device_token = data.get("device_token")
        latitude = data.get("latitude")
        longitude = data.get("longitude")

        if not device_token:

            return JsonResponse(
                {"error": "device_token required"},
                status=400,
            )

        if latitude is None or longitude is None:

            return JsonResponse(
                {"error": "latitude and longitude required"},
                status=400,
            )

        try:

            latitude = float(latitude)
            longitude = float(longitude)

        except (ValueError, TypeError):

            return JsonResponse(
                {"error": "invalid coordinates"},
                status=400,
            )

        if not (-90 <= latitude <= 90):

            return JsonResponse(
                {"error": "latitude out of range"},
                status=400,
            )

        if not (-180 <= longitude <= 180):

            return JsonResponse(
                {"error": "longitude out of range"},
                status=400,
            )

        profile, _ = (
            UserProfileBalance.objects
            .get_or_create(
                device_token=device_token
            )
        )

        profile.latitude = latitude
        profile.longitude = longitude
        profile.location_updated_at = timezone.now()

        client_ip = _get_client_ip(request)

        if client_ip and not profile.ip_address:

            profile.ip_address = client_ip

        profile.save(
            update_fields=[
                "latitude",
                "longitude",
                "location_updated_at",
                "ip_address",
            ]
        )

        print(
            f"📍 LOCATION UPDATE | "
            f"device={device_token} | "
            f"lat={latitude} | "
            f"lng={longitude}"
        )

        return JsonResponse({
            "status": "success",
            "message": "Location updated.",
        })

    except json.JSONDecodeError:

        return JsonResponse(
            {"error": "invalid JSON"},
            status=400,
        )

    except Exception as exc:

        return JsonResponse(
            {"error": str(exc)},
            status=500,
        )
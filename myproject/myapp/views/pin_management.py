# views/pin_management.py

import json
import secrets
import time

from django.contrib.auth.hashers import (
    make_password,
    check_password,
)

from django.core.cache import cache
from django.core.mail import send_mail
from django.http import JsonResponse

from django.views.decorators.csrf import (
    csrf_exempt,
)

from ..models import UserProfileBalance


# ==========================================================
# CONSTANTS
# ==========================================================

PIN_LENGTH = 4

PIN_RESET_OTP_TTL = 300          # 5 minutes

PIN_RESET_SEND_LIMIT = 3         # per 10 minutes

PIN_RESET_VERIFY_LIMIT = 5       # per 10 minutes


# ==========================================================
# HELPERS
# ==========================================================

def _is_valid_pin(pin):

    if not isinstance(pin, str):
        return False

    if len(pin) != PIN_LENGTH:
        return False

    return pin.isdigit()


def _get_profile(device_token):

    return (
        UserProfileBalance.objects
        .filter(device_token=device_token)
        .first()
    )


# ==========================================================
# PIN STATUS
# ==========================================================

@csrf_exempt
def get_pin_status(request):

    if request.method != "POST":

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "POST required.",
            },
            status=405,
        )

    try:

        data = json.loads(request.body)

    except json.JSONDecodeError:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Invalid JSON.",
            },
            status=400,
        )

    device_token = data.get(
        "device_token"
    )

    if not device_token:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "device_token is required.",
            },
            status=400,
        )

    profile = _get_profile(device_token)

    if not profile:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Profile not found.",
            },
            status=404,
        )

    return JsonResponse({
        "status":
            "success",

        "has_withdrawal_pin":
            profile.has_withdrawal_pin,

        "has_email":
            bool(profile.email),
    })


# ==========================================================
# SET PIN  (first time)
# ==========================================================

@csrf_exempt
def set_withdrawal_pin(request):

    if request.method != "POST":

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "POST required.",
            },
            status=405,
        )

    try:

        data = json.loads(request.body)

    except json.JSONDecodeError:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Invalid JSON.",
            },
            status=400,
        )

    device_token = data.get(
        "device_token"
    )

    pin = str(data.get("pin", "")).strip()

    confirm_pin = str(
        data.get("confirm_pin", "")
    ).strip()

    if not device_token:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "device_token is required.",
            },
            status=400,
        )

    if not _is_valid_pin(pin):

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    (
                        f"PIN must be exactly "
                        f"{PIN_LENGTH} digits."
                    ),
            },
            status=400,
        )

    if pin != confirm_pin:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "PINs do not match.",
            },
            status=400,
        )

    profile = _get_profile(device_token)

    if not profile:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Profile not found.",
            },
            status=404,
        )

    if profile.has_withdrawal_pin:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    (
                        "PIN already set. "
                        "Use change PIN instead."
                    ),
            },
            status=400,
        )

    profile.withdrawal_pin_hash = (
        make_password(pin)
    )

    profile.has_withdrawal_pin = True

    profile.save(
        update_fields=[
            "withdrawal_pin_hash",
            "has_withdrawal_pin",
        ]
    )

    return JsonResponse({
        "status":
            "success",

        "message":
            "Withdrawal PIN set successfully.",
    })


# ==========================================================
# CHANGE PIN  (needs old PIN)
# ==========================================================

@csrf_exempt
def change_withdrawal_pin(request):

    if request.method != "POST":

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "POST required.",
            },
            status=405,
        )

    try:

        data = json.loads(request.body)

    except json.JSONDecodeError:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Invalid JSON.",
            },
            status=400,
        )

    device_token = data.get(
        "device_token"
    )

    old_pin = str(
        data.get("old_pin", "")
    ).strip()

    new_pin = str(
        data.get("new_pin", "")
    ).strip()

    confirm_pin = str(
        data.get("confirm_pin", "")
    ).strip()

    if not device_token:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "device_token is required.",
            },
            status=400,
        )

    if not _is_valid_pin(old_pin):

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Invalid old PIN format.",
            },
            status=400,
        )

    if not _is_valid_pin(new_pin):

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    (
                        f"New PIN must be exactly "
                        f"{PIN_LENGTH} digits."
                    ),
            },
            status=400,
        )

    if new_pin != confirm_pin:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "New PINs do not match.",
            },
            status=400,
        )

    profile = _get_profile(device_token)

    if not profile:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Profile not found.",
            },
            status=404,
        )

    if not profile.has_withdrawal_pin:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    (
                        "No PIN set yet. "
                        "Use set PIN first."
                    ),
            },
            status=400,
        )

    if not check_password(
        old_pin,
        profile.withdrawal_pin_hash,
    ):

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Old PIN is incorrect.",
            },
            status=400,
        )

    profile.withdrawal_pin_hash = (
        make_password(new_pin)
    )

    profile.save(
        update_fields=[
            "withdrawal_pin_hash",
        ]
    )

    return JsonResponse({
        "status":
            "success",

        "message":
            "PIN changed successfully.",
    })


# ==========================================================
# RESET PIN — STEP 1: SEND OTP
# ==========================================================

@csrf_exempt
def reset_pin_send_otp(request):

    if request.method != "POST":

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "POST required.",
            },
            status=405,
        )

    try:

        data = json.loads(request.body)

    except json.JSONDecodeError:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Invalid JSON.",
            },
            status=400,
        )

    device_token = data.get(
        "device_token"
    )

    if not device_token:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "device_token is required.",
            },
            status=400,
        )

    profile = _get_profile(device_token)

    if not profile:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Profile not found.",
            },
            status=404,
        )

    if not profile.email:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    (
                        "No email on file. "
                        "Contact support to reset PIN."
                    ),
            },
            status=400,
        )

    # ----------------------------------------------
    # Rate limit
    # ----------------------------------------------

    send_key = f"pin_reset_send:{device_token}"

    sends = cache.get(send_key, 0)

    if sends >= PIN_RESET_SEND_LIMIT:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    (
                        "Too many reset requests. "
                        "Try again in 10 minutes."
                    ),
            },
            status=429,
        )

    cache.set(
        send_key,
        sends + 1,
        timeout=600,
    )

    # ----------------------------------------------
    # Generate OTP
    # ----------------------------------------------

    otp_code = (
        f"{secrets.randbelow(1_000_000):06d}"
    )

    cache.set(
        f"pin_reset_otp:{device_token}",
        otp_code,
        timeout=PIN_RESET_OTP_TTL,
    )

    cache.delete(
        f"pin_reset_verify:{device_token}"
    )

    # ----------------------------------------------
    # Send email
    # ----------------------------------------------

    try:

        send_mail(
            subject="Rocks Games — PIN Reset Code",
            message=(
                f"Your PIN reset code is: "
                f"{otp_code}\n\n"
                f"Valid for 5 minutes. "
                f"If you did not request this, "
                f"ignore this email."
            ),
            from_email=(
                "bedrockentertainent@gmail.com"
            ),
            recipient_list=[profile.email],
            fail_silently=False,
        )

    except Exception as mail_err:

        print(
            f"[PIN_RESET_MAIL_FAIL] {mail_err}"
        )

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Failed to send reset email.",
            },
            status=500,
        )

    # ----------------------------------------------
    # Mask email for client display
    # ----------------------------------------------

    email = profile.email

    if "@" in email:

        local, domain = email.split(
            "@",
            1,
        )

        if len(local) > 2:

            masked_local = (
                local[0]
                + "*" * (len(local) - 2)
                + local[-1]
            )

        else:

            masked_local = local[0] + "*"

        masked_email = (
            f"{masked_local}@{domain}"
        )

    else:

        masked_email = email

    return JsonResponse({
        "status":
            "success",

        "message":
            f"Reset code sent to {masked_email}.",

        "masked_email":
            masked_email,
    })


# ==========================================================
# RESET PIN — STEP 2: VERIFY OTP + SET NEW PIN
# ==========================================================

@csrf_exempt
def reset_pin_verify_otp(request):

    if request.method != "POST":

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "POST required.",
            },
            status=405,
        )

    try:

        data = json.loads(request.body)

    except json.JSONDecodeError:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Invalid JSON.",
            },
            status=400,
        )

    device_token = data.get(
        "device_token"
    )

    otp_code = str(
        data.get("otp_code", "")
    ).strip()

    new_pin = str(
        data.get("new_pin", "")
    ).strip()

    confirm_pin = str(
        data.get("confirm_pin", "")
    ).strip()

    if not device_token:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "device_token is required.",
            },
            status=400,
        )

    if not otp_code:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "OTP code is required.",
            },
            status=400,
        )

    if not _is_valid_pin(new_pin):

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    (
                        f"PIN must be exactly "
                        f"{PIN_LENGTH} digits."
                    ),
            },
            status=400,
        )

    if new_pin != confirm_pin:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "PINs do not match.",
            },
            status=400,
        )

    # ----------------------------------------------
    # Rate limit verify
    # ----------------------------------------------

    verify_key = (
        f"pin_reset_verify:{device_token}"
    )

    attempts = cache.get(verify_key, 0)

    if attempts >= PIN_RESET_VERIFY_LIMIT:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    (
                        "Too many attempts. "
                        "Request a new code."
                    ),
            },
            status=429,
        )

    # ----------------------------------------------
    # Check OTP
    # ----------------------------------------------

    stored_otp = cache.get(
        f"pin_reset_otp:{device_token}"
    )

    if not stored_otp:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "OTP expired or not requested.",
            },
            status=400,
        )

    if stored_otp != otp_code:

        cache.set(
            verify_key,
            attempts + 1,
            timeout=600,
        )

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Invalid OTP code.",
            },
            status=400,
        )

    # ----------------------------------------------
    # Consume OTP
    # ----------------------------------------------

    cache.delete(
        f"pin_reset_otp:{device_token}"
    )

    cache.delete(verify_key)

    # ----------------------------------------------
    # Update PIN
    # ----------------------------------------------

    profile = _get_profile(device_token)

    if not profile:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Profile not found.",
            },
            status=404,
        )

    profile.withdrawal_pin_hash = (
        make_password(new_pin)
    )

    profile.has_withdrawal_pin = True

    profile.save(
        update_fields=[
            "withdrawal_pin_hash",
            "has_withdrawal_pin",
        ]
    )

    return JsonResponse({
        "status":
            "success",

        "message":
            "PIN reset successfully.",
    })
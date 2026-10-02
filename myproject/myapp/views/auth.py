# views/auth.py

import json
import hashlib
import secrets
import time

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.http import JsonResponse

from django.views.decorators.csrf import (
    csrf_exempt,
)

from ..models import (
    UserProfileBalance,
    GameUser,
)


OTP_TTL_SECONDS = 300

OTP_SEND_LIMIT_PER_IP = 5

OTP_VERIFY_LIMIT_PER_EMAIL = 5


# ==========================================================
# HELPERS
# ==========================================================

def generate_secure_token(
    email,
    device_id,
    ip,
):

    raw = (
        f"{email}:{device_id}:{ip}:"
        f"{time.time()}:"
        f"{secrets.token_hex(8)}"
    )

    return hashlib.sha256(
        raw.encode()
    ).hexdigest()


def _client_ip(request):

    xff = request.META.get(
        "HTTP_X_FORWARDED_FOR"
    )

    if xff:

        return xff.split(",")[0].strip()

    return request.META.get(
        "REMOTE_ADDR",
        "unknown",
    )


# ==========================================================
# SEND OTP  (server-generated code)
# ==========================================================

@csrf_exempt
def send_otp(request):

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

        email = str(
            data.get("email", "")
        ).strip().lower()

        if not email:

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        "email is required.",
                },
                status=400,
            )

        # ----------------------------------------------
        # Rate limit per IP
        # ----------------------------------------------

        ip = _client_ip(request)

        send_key = f"otp_send:{ip}"

        sends = cache.get(
            send_key,
            0,
        )

        if sends >= OTP_SEND_LIMIT_PER_IP:

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        (
                            "Too many OTP requests. "
                            "Try again later."
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
        # Generate code server-side
        # ----------------------------------------------

        code = (
            f"{secrets.randbelow(1_000_000):06d}"
        )

        cache.set(
            f"otp:{email}",
            code,
            timeout=OTP_TTL_SECONDS,
        )

        cache.delete(
            f"otp_verify:{email}"
        )

        # ----------------------------------------------
        # Send mail
        # ----------------------------------------------

        try:

            send_mail(
                subject=(
                    "Rocks Games Verification"
                ),
                message=(
                    f"Your verification code is: "
                    f"{code}\n\n"
                    f"Valid for 5 minutes."
                ),
                from_email=getattr(
                    settings,
                    "DEFAULT_FROM_EMAIL",
                    (
                        "bedrockentertainent"
                        "@gmail.com"
                    ),
                ),
                recipient_list=[email],
                fail_silently=False,
            )

        except Exception as mail_err:

            print(
                f"[OTP_MAIL_FAIL] {mail_err}"
            )

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        "Failed to send OTP email.",
                },
                status=500,
            )

        return JsonResponse(
            {
                "status":
                    "success",

                "message":
                    "OTP sent.",
            },
            status=200,
        )

    except json.JSONDecodeError:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Invalid JSON body.",
            },
            status=400,
        )

    except Exception as e:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    str(e),
            },
            status=500,
        )


# ==========================================================
# VERIFY EMAIL LOGIN  (OTP-based)
# ==========================================================

@csrf_exempt
def verify_email_login(request):

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

        email = str(
            data.get("email", "")
        ).strip().lower()

        username = str(
            data.get("username", "")
        ).strip()

        device_id = str(
            data.get("device_id", "")
        ).strip()

        code = str(
            data.get("code", "")
        ).strip()

        if not email:

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        "email is required.",
                },
                status=400,
            )

        if not username:

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        "username is required.",
                },
                status=400,
            )

        if not device_id:

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        "device_id is required.",
                },
                status=400,
            )

        if not code:

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        (
                            "Verification code "
                            "is required."
                        ),
                },
                status=400,
            )

        # ----------------------------------------------
        # Rate limit verify
        # ----------------------------------------------

        verify_key = f"otp_verify:{email}"

        attempts = cache.get(
            verify_key,
            0,
        )

        if (
            attempts
            >= OTP_VERIFY_LIMIT_PER_EMAIL
        ):

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        (
                            "Too many verification "
                            "attempts."
                        ),
                },
                status=429,
            )

        # ----------------------------------------------
        # Check code
        # ----------------------------------------------

        stored_code = cache.get(
            f"otp:{email}"
        )

        if not stored_code:

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        (
                            "OTP expired or "
                            "not requested."
                        ),
                },
                status=400,
            )

        if stored_code != code:

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
                        (
                            "Invalid verification "
                            "code."
                        ),
                },
                status=400,
            )

        # ----------------------------------------------
        # Consume OTP
        # ----------------------------------------------

        cache.delete(f"otp:{email}")

        cache.delete(verify_key)

        ip_address = _client_ip(request)

        auth_token = generate_secure_token(
            email,
            device_id,
            ip_address,
        )

        existing_email = (
            GameUser.objects
            .filter(email=email)
            .first()
        )

        existing_device = (
            GameUser.objects
            .filter(device_id=device_id)
            .first()
        )

        if existing_email and (
            not existing_device
            or existing_email.id
            != existing_device.id
        ):

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        (
                            "This email is already "
                            "registered with another "
                            "device."
                        ),
                },
                status=409,
            )

        if (
            existing_device
            and existing_device.email != email
        ):

            return JsonResponse(
                {
                    "status":
                        "error",

                    "message":
                        (
                            "This device is already "
                            "registered with another "
                            "email."
                        ),
                },
                status=409,
            )

        with transaction.atomic():

            user, created = (
                GameUser.objects
                .update_or_create(
                    email=email,
                    defaults={
                        "username":
                            username,

                        "device_id":
                            device_id,

                        "ip_address":
                            ip_address,

                        "auth_token":
                            auth_token,
                    },
                )
            )

        return JsonResponse(
            {
                "status":
                    "success",

                "created":
                    created,

                "user": {
                    "user_id":
                        user.id,

                    "username":
                        user.username,

                    "email":
                        user.email,

                    "device_id":
                        user.device_id,

                    "auth_token":
                        user.auth_token,
                },
            },
            status=201 if created else 200,
        )

    except json.JSONDecodeError:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    "Invalid JSON body.",
            },
            status=400,
        )

    except Exception as e:

        return JsonResponse(
            {
                "status":
                    "error",

                "message":
                    str(e),
            },
            status=500,
        )
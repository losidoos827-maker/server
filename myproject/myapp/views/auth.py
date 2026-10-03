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
from django.views.decorators.csrf import csrf_exempt

from ..models import UserProfileBalance, GameUser


OTP_TTL_SECONDS = 300
OTP_SEND_LIMIT_PER_IP = 5
OTP_VERIFY_LIMIT_PER_EMAIL = 5


def generate_secure_token(email, device_id, ip):
    raw = f"{email}:{device_id}:{ip}:{time.time()}:{secrets.token_hex(8)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


# ==========================================================
# SEND OTP
# ==========================================================

@csrf_exempt
def send_otp(request):
    if request.method != "POST":
        return JsonResponse(
            {"status": "error", "message": "POST required."},
            status=405,
        )

    try:
        data = json.loads(request.body)
        email = str(data.get("email", "")).strip().lower()

        if not email:
            return JsonResponse(
                {"status": "error", "message": "email is required."},
                status=400,
            )

        ip = _client_ip(request)
        send_key = f"otp_send:{ip}"
        sends = cache.get(send_key, 0)

        if sends >= OTP_SEND_LIMIT_PER_IP:
            return JsonResponse(
                {"status": "error", "message": "Too many OTP requests. Try again later."},
                status=429,
            )

        cache.set(send_key, sends + 1, timeout=600)

        code = f"{secrets.randbelow(1_000_000):06d}"

        cache.set(f"otp:{email}", code, timeout=OTP_TTL_SECONDS)
        cache.delete(f"otp_verify:{email}")

        try:
            send_mail(
                subject="Rocks Games Verification",
                message=f"Your verification code is: {code}\n\nValid for 5 minutes.",
                from_email=getattr(
                    settings,
                    "DEFAULT_FROM_EMAIL",
                    "bedrockentertainent@gmail.com",
                ),
                recipient_list=[email],
                fail_silently=False,
            )
        except Exception as mail_err:
            print(f"[OTP_MAIL_FAIL] {mail_err}")
            return JsonResponse(
                {"status": "error", "message": "Failed to send OTP email."},
                status=500,
            )

        return JsonResponse(
            {"status": "success", "message": "OTP sent."},
            status=200,
        )

    except json.JSONDecodeError:
        return JsonResponse(
            {"status": "error", "message": "Invalid JSON body."},
            status=400,
        )
    except Exception as e:
        return JsonResponse(
            {"status": "error", "message": str(e)},
            status=500,
        )


# ==========================================================
# VERIFY EMAIL LOGIN  (with device lock + wallet transfer)
# ==========================================================

@csrf_exempt
def verify_email_login(request):
    if request.method != "POST":
        return JsonResponse(
            {"status": "error", "message": "POST required."},
            status=405,
        )

    try:
        data = json.loads(request.body)

        email = str(data.get("email", "")).strip().lower()
        username = str(data.get("username", "")).strip()
        device_id = str(data.get("device_id", "")).strip()
        code = str(data.get("code", "")).strip()

        if not email:
            return JsonResponse(
                {"status": "error", "message": "email is required."},
                status=400,
            )
        if not username:
            return JsonResponse(
                {"status": "error", "message": "username is required."},
                status=400,
            )
        if not device_id:
            return JsonResponse(
                {"status": "error", "message": "device_id is required."},
                status=400,
            )
        if not code:
            return JsonResponse(
                {"status": "error", "message": "Verification code is required."},
                status=400,
            )

        # ---- Rate limit verify ----
        verify_key = f"otp_verify:{email}"
        attempts = cache.get(verify_key, 0)

        if attempts >= OTP_VERIFY_LIMIT_PER_EMAIL:
            return JsonResponse(
                {"status": "error", "message": "Too many verification attempts."},
                status=429,
            )

        # ---- Check code ----
        stored_code = cache.get(f"otp:{email}")

        if not stored_code:
            return JsonResponse(
                {"status": "error", "message": "OTP expired or not requested."},
                status=400,
            )

        if stored_code != code:
            cache.set(verify_key, attempts + 1, timeout=600)
            return JsonResponse(
                {"status": "error", "message": "Invalid verification code."},
                status=400,
            )

        # ---- Consume OTP ----
        cache.delete(f"otp:{email}")
        cache.delete(verify_key)

        ip_address = _client_ip(request)
        auth_token = generate_secure_token(email, device_id, ip_address)

        # ==================================================
        # WALLET TRANSFER (reinstall / new device)
        #
        # Wallet (UserProfileBalance) is keyed by device_token,
        # but the "owner" is the email. When the user logs in
        # with an email that already has a wallet linked, we
        # move that wallet to the current device_token.
        #
        # This handles:
        #   - App reinstall → new device_token
        #   - Login on a different device (after logout)
        # ==================================================

        profile_by_email = (
            UserProfileBalance.objects
            .filter(email=email)
            .first()
        )

        profile_by_device = (
            UserProfileBalance.objects
            .filter(device_token=device_id)
            .first()
        )

        with transaction.atomic():

            if profile_by_email:

                # ------------------------------------------------
                # Another wallet is sitting at this device_token?
                # Move it away (orphan) so it doesn't conflict.
                # ------------------------------------------------

                if (
                    profile_by_device
                    and profile_by_device.id != profile_by_email.id
                ):

                    profile_by_device.device_token = (
                        f"orphan_{secrets.token_hex(8)}"
                    )

                    profile_by_device.save(
                        update_fields=["device_token"]
                    )

                # ------------------------------------------------
                # Move the email-linked wallet to this device.
                # ------------------------------------------------

                if profile_by_email.device_token != device_id:

                    profile_by_email.device_token = device_id

                    profile_by_email.save(
                        update_fields=["device_token"]
                    )

                    print(
                        f"💰 WALLET TRANSFER | "
                        f"email={email} | "
                        f"new_device={device_id} | "
                        f"coins={profile_by_email.coins}"
                    )

            elif profile_by_device:

                # ------------------------------------------------
                # No wallet for this email, but device already has one.
                #
                #   - If it has no email  → link it (legacy migration).
                #   - If it has a DIFFERENT email → orphan it (new user).
                # ------------------------------------------------

                if (
                    not profile_by_device.email
                    or profile_by_device.email == ""
                ):

                    profile_by_device.email = email

                    profile_by_device.save(
                        update_fields=["email"]
                    )

                    print(
                        f"🔗 WALLET LINKED | "
                        f"email={email} | "
                        f"device={device_id} | "
                        f"coins={profile_by_device.coins}"
                    )

                else:

                    profile_by_device.device_token = (
                        f"orphan_{secrets.token_hex(8)}"
                    )

                    profile_by_device.save(
                        update_fields=["device_token"]
                    )

                    print(
                        f"🚪 WALLET ORPHANED | "
                        f"old_email={profile_by_device.email} | "
                        f"new_email={email} | "
                        f"device={device_id}"
                    )

            # else: no wallet yet — will be created on demand.


        # ==================================================
        # DEVICE LOCK CHECK (relaxed)
        #
        # Ek email ko sirf ek device pe active rakho,
        # lekin reinstall ke case ko block na karo.
        #
        # Rule:
        #   - Agar device kisi DOOSRE email se juda hai → block
        #   - Agar email kisi purane device pe tha → allow takeover
        #     (purana device stale ho jayega)
        # ==================================================

        existing_email = GameUser.objects.filter(email=email).first()
        existing_device = GameUser.objects.filter(device_id=device_id).first()

        # ---- This device is bound to a DIFFERENT email? Block ----
        if (
            existing_device
            and existing_device.email
            and existing_device.email != email
        ):
            return JsonResponse(
                {
                    "status": "error",
                    "message": (
                        "This device is already registered "
                        "with another email. Please log out "
                        "from that account first."
                    ),
                },
                status=409,
            )

        # ---- Allow login / re-login (with takeover) ----
        with transaction.atomic():
            user, created = GameUser.objects.update_or_create(
                email=email,
                defaults={
                    "username": username,
                    "device_id": device_id,
                    "ip_address": ip_address,
                    "auth_token": auth_token,
                },
            )

        return JsonResponse(
            {
                "status": "success",
                "created": created,
                "user": {
                    "user_id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "device_id": user.device_id,
                    "auth_token": user.auth_token,
                },
            },
            status=201 if created else 200,
        )

    except json.JSONDecodeError:
        return JsonResponse(
            {"status": "error", "message": "Invalid JSON body."},
            status=400,
        )
    except Exception as e:
        return JsonResponse(
            {"status": "error", "message": str(e)},
            status=500,
        )


# ==========================================================
# LOGOUT  (frees the device lock)
# ==========================================================

@csrf_exempt
def logout_user(request):
    if request.method != "POST":
        return JsonResponse(
            {"status": "error", "message": "POST required."},
            status=405,
        )

    try:
        data = json.loads(request.body)

        device_id = str(data.get("device_id", "")).strip()

        if not device_id:
            return JsonResponse(
                {"status": "error", "message": "device_id is required."},
                status=400,
            )

        # ---- Clear device binding for this device ----
        updated = GameUser.objects.filter(
            device_id=device_id
        ).update(device_id="")

        print(
            f"🚪 LOGOUT | device={device_id} | "
            f"cleared={updated}"
        )

        return JsonResponse({
            "status": "success",
            "message": "Logged out successfully.",
            "cleared": updated,
        })

    except json.JSONDecodeError:
        return JsonResponse(
            {"status": "error", "message": "Invalid JSON body."},
            status=400,
        )
    except Exception as e:
        return JsonResponse(
            {"status": "error", "message": str(e)},
            status=500,
        )
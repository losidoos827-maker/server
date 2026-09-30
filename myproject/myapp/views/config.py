# myapp/views/config.py

from django.http import JsonResponse

from ..models import SystemSetting


# ==========================================================
# APP CONFIG
#
# Returns runtime configurable values that the mobile
# app should fetch at startup / settings screen.
# ==========================================================

def get_app_config(request):

    support_whatsapp = SystemSetting.get_value(
        "support_whatsapp",
        "+923001234567",
    )

    support_telegram = SystemSetting.get_value(
        "support_telegram",
        "yourusername",
    )

    support_email = SystemSetting.get_value(
        "support_email",
        "bedrockentertainent@gmail.com",
    )

    terms_text = SystemSetting.get_value(
        "terms_text",
        (
            "1. App ka misuse mat karo\n"
            "2. Apna account kisi ko share mat karo\n"
            "3. Hamara data copy karna mana hai\n"
            "4. Support se tameez se baat karo\n"
            "5. Rules torney pe account block ho sakta hai"
        ),
    )

    privacy_text = SystemSetting.get_value(
        "privacy_text",
        (
            "Hum tumhara data securely store karte hain. "
            "Kisi third party ko share nahi karte. "
            "Account delete karna ho to support se "
            "rabta karo."
        ),
    )

    app_version = SystemSetting.get_value(
        "app_version",
        "1.0.0",
    )

    return JsonResponse({
        "status":
            "success",

        "support_whatsapp":
            support_whatsapp,

        "support_telegram":
            support_telegram,

        "support_email":
            support_email,

        "terms_text":
            terms_text,

        "privacy_text":
            privacy_text,

        "app_version":
            app_version,
    })
# views/admin_analytics.py

from datetime import timedelta

from django.contrib.admin.views.decorators import (
    staff_member_required,
)
from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from ..models import (
    GameRoom,
    SystemTransactionLog,
    UserProfileBalance,
)


# ==========================================================
# HELPERS
# ==========================================================

def _sum_ledger(log_types, since, sign_negative=False):
    """
    Sum amounts from ledger between `since` and now.
    If sign_negative=True, returns absolute value of
    negative sums (used for withdrawals).
    """

    qs = SystemTransactionLog.objects.filter(
        log_type__in=log_types,
        timestamp__gte=since,
    )

    total = (
        qs.aggregate(total=Sum("amount"))["total"]
        or 0
    )

    if sign_negative:
        return abs(total)

    return total


# ==========================================================
# ANALYTICS DASHBOARD
# ==========================================================

@staff_member_required
def analytics_dashboard(request):

    now = timezone.now()

    # ==============================================
    # PERIOD
    # ==============================================

    period = request.GET.get("period", "24h")

    if period == "7d":
        since = now - timedelta(days=7)
        period_label = "Last 7 Days"
    elif period == "30d":
        since = now - timedelta(days=30)
        period_label = "Last 30 Days"
    else:
        period = "24h"
        since = now - timedelta(hours=24)
        period_label = "Last 24 Hours"

    # ==============================================
    # CASH FLOW — IN
    # ==============================================

    total_in = _sum_ledger(
        ["DEPOSIT"],
        since,
    )

    # ==============================================
    # CASH FLOW — OUT
    # ==============================================

    total_out = _sum_ledger(
        ["WITHDRAWAL"],
        since,
        sign_negative=True,
    )

    # ==============================================
    # PLATFORM EARNINGS
    # ==============================================

    # 1) Wager service fees (from completed rooms)
    wager_fees = (
        GameRoom.objects
        .filter(
            game_status="COMPLETED",
            updated_at__gte=since,
        )
        .aggregate(total=Sum("service_fee_cut"))["total"]
        or 0
    )

    # 2) Spin paid costs
    spin_costs = _sum_ledger(
        ["SPIN_COST"],
        since,
        sign_negative=True,
    )

    platform_earnings = wager_fees + spin_costs

    # ==============================================
    # WAGER VOLUME
    # ==============================================

    games_count = (
        GameRoom.objects
        .filter(
            game_status="COMPLETED",
            updated_at__gte=since,
        )
        .count()
    )

    wager_volume = (
        GameRoom.objects
        .filter(
            game_status="COMPLETED",
            updated_at__gte=since,
        )
        .aggregate(total=Sum("total_pool_escrow"))["total"]
        or 0
    )

    # ==============================================
    # GAME ACTIVITY
    # ==============================================

    total_payout = _sum_ledger(
        ["WAGER_PAYOUT"],
        since,
    )

    total_refunds = _sum_ledger(
        ["WAGER_REFUND"],
        since,
    )

    total_referral_bonus = _sum_ledger(
        ["REFERRAL_BONUS"],
        since,
    )

    total_referral_commission = _sum_ledger(
        ["REFERRAL_COMMISSION"],
        since,
    )

    total_spin_rewards = _sum_ledger(
        ["SPIN_REWARD"],
        since,
    )

    # ==============================================
    # HOURLY BREAKDOWN — last 24 hours only
    # ==============================================

    hourly_data = []

    if period == "24h":

        for i in range(23, -1, -1):

            bucket_start = now - timedelta(hours=i + 1)
            bucket_end = now - timedelta(hours=i)

            bucket_in = (
                SystemTransactionLog.objects
                .filter(
                    log_type="DEPOSIT",
                    timestamp__gte=bucket_start,
                    timestamp__lt=bucket_end,
                )
                .aggregate(total=Sum("amount"))["total"]
                or 0
            )

            bucket_out = abs(
                SystemTransactionLog.objects
                .filter(
                    log_type="WITHDRAWAL",
                    timestamp__gte=bucket_start,
                    timestamp__lt=bucket_end,
                )
                .aggregate(total=Sum("amount"))["total"]
                or 0
            )

            games_in_hour = (
                GameRoom.objects
                .filter(
                    game_status="COMPLETED",
                    updated_at__gte=bucket_start,
                    updated_at__lt=bucket_end,
                )
                .count()
            )

            hourly_data.append({
                "hour": bucket_start.strftime("%H:00"),
                "in": bucket_in,
                "out": bucket_out,
                "games": games_in_hour,
            })

    # ==============================================
    # RECENT TRANSACTIONS (last 30)
    # ==============================================

    recent_logs = (
        SystemTransactionLog.objects
        .filter(timestamp__gte=since)
        .select_related("user_profile")
        .order_by("-timestamp")[:30]
    )

    recent_transactions = []

    for log in recent_logs:

        recent_transactions.append({
            "device_token": log.user_profile.device_token,
            "nickname": (
                log.user_profile.nickname
                or "—"
            ),
            "log_type": log.log_type,
            "amount": log.amount,
            "reference_id": log.reference_id,
            "time": log.timestamp.strftime(
                "%d %b, %I:%M %p"
            ),
        })

    # ==============================================
    # CONTEXT
    # ==============================================

    context = {

        "period": period,
        "period_label": period_label,
        "since": since.strftime("%d %b %Y, %I:%M %p"),
        "now": now.strftime("%d %b %Y, %I:%M %p"),

        "summary": {
            "total_in": total_in,
            "total_out": total_out,
            "net_flow": total_in - total_out,
            "platform_earnings": platform_earnings,

            "wager_fees": wager_fees,
            "spin_costs": spin_costs,

            "games_count": games_count,
            "wager_volume": wager_volume,
            "total_payout": total_payout,
            "total_refunds": total_refunds,
            "total_referral_bonus": total_referral_bonus,
            "total_referral_commission": total_referral_commission,
            "total_spin_rewards": total_spin_rewards,
        },

        "hourly_data": hourly_data,
        "recent_transactions": recent_transactions,
    }

    return render(
        request,
        "admin_analytics.html",
        context,
    )
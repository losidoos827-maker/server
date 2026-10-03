# views/admin_rewards.py

import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.admin.views.decorators import (
    staff_member_required,
)
from django.db import transaction
from django.db.models import (
    Count,
    Q,
    Sum,
)
from django.shortcuts import redirect, render
from django.utils import timezone

from ..models import (
    GameHistory,
    LeaderboardReward,
    SystemSetting,
    SystemTransactionLog,
    UserProfileBalance,
)


# ==========================================================
# HELPERS
# ==========================================================

def _get_reward_amount(rank, period_type):
    """
    Fetch reward amount for a given rank and period
    from SystemSetting. Falls back to defaults.
    """

    if period_type == "weekly":
        defaults = {1: 5000, 2: 3000, 3: 1000}
    else:
        defaults = {1: 20000, 2: 10000, 3: 5000}

    key = f"{period_type}_reward_{rank}"

    try:
        return int(
            SystemSetting.get_value(
                key,
                str(defaults[rank]),
            )
        )
    except (ValueError, TypeError):
        return defaults[rank]


def _get_current_period(period_type):
    """
    Returns (period_key, period_start, period_end)
    for the CURRENT period.
    """

    now = timezone.now()

    if period_type == "weekly":

        iso = now.isocalendar()

        period_key = f"{iso.year}-W{iso.week:02d}"

        # Monday of current week
        period_start = (
            now - timedelta(days=now.weekday())
        ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        period_end = now

    else:  # monthly

        period_key = f"{now.year}-{now.month:02d}"

        period_start = now.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        period_end = now

    return period_key, period_start, period_end


def _get_top_players(period_start, period_end, limit=3):
    """
    Return top N players in the given date range.
    """

    qs = GameHistory.objects.filter(
        played_at__gte=period_start,
        played_at__lte=period_end,
    )

    agg = (
        qs.values("user_profile__device_token")
        .annotate(
            games=Count("id"),
            wins=Count(
                "id",
                filter=Q(result="WON"),
            ),
            earnings=Sum("coins_change"),
        )
        .filter(games__gt=0)
        .order_by("-wins", "-earnings")[:limit]
    )

    # Fetch profiles
    tokens = [
        row["user_profile__device_token"]
        for row in agg
    ]

    profiles = {
        p.device_token: p
        for p in UserProfileBalance.objects.filter(
            device_token__in=tokens
        )
    }

    results = []

    for idx, row in enumerate(agg):

        token = row["user_profile__device_token"]

        profile = profiles.get(token)

        results.append({
            "rank": idx + 1,
            "device_token": token,
            "name": (
                profile.nickname
                if profile and profile.nickname
                else "Player"
            ),
            "wins": row["wins"] or 0,
            "games": row["games"] or 0,
            "earnings": row["earnings"] or 0,
            "profile_obj": profile,
        })

    return results


def _already_distributed(period_key):
    return (
        LeaderboardReward.objects
        .filter(period_key=period_key)
        .exists()
    )


# ==========================================================
# DASHBOARD
# ==========================================================

@staff_member_required
def leaderboard_rewards_dashboard(request):

    # ----------------------------------------------
    # Weekly preview
    # ----------------------------------------------

    week_key, week_start, week_end = (
        _get_current_period("weekly")
    )

    weekly_top = _get_top_players(
        week_start,
        week_end,
    )

    weekly_prizes = {
        1: _get_reward_amount(1, "weekly"),
        2: _get_reward_amount(2, "weekly"),
        3: _get_reward_amount(3, "weekly"),
    }

    weekly_total = sum(weekly_prizes.values())

    weekly_distributed = _already_distributed(
        week_key
    )

    # ----------------------------------------------
    # Monthly preview
    # ----------------------------------------------

    month_key, month_start, month_end = (
        _get_current_period("monthly")
    )

    monthly_top = _get_top_players(
        month_start,
        month_end,
    )

    monthly_prizes = {
        1: _get_reward_amount(1, "monthly"),
        2: _get_reward_amount(2, "monthly"),
        3: _get_reward_amount(3, "monthly"),
    }

    monthly_total = sum(monthly_prizes.values())

    monthly_distributed = _already_distributed(
        month_key
    )

    # ----------------------------------------------
    # Distribution history
    # ----------------------------------------------

    history = (
        LeaderboardReward.objects
        .all()
        .order_by("-distributed_at")[:20]
    )

    history_list = []

    for h in history:

        winners = []

        try:
            winners = json.loads(
                h.winners_json or "[]"
            )
        except Exception:
            pass

        history_list.append({
            "period_type": h.period_type,
            "period_key": h.period_key,
            "distributed_at": h.distributed_at.strftime(
                "%d %b %Y, %I:%M %p"
            ),
            "total_amount": h.total_amount,
            "winners": winners,
        })

    context = {

        "weekly": {
            "period_key": week_key,
            "period_start": week_start.strftime("%d %b %Y"),
            "period_end": week_end.strftime("%d %b %Y"),
            "top_players": weekly_top,
            "prizes": weekly_prizes,
            "total": weekly_total,
            "already_distributed": weekly_distributed,
        },

        "monthly": {
            "period_key": month_key,
            "period_start": month_start.strftime("%d %b %Y"),
            "period_end": month_end.strftime("%d %b %Y"),
            "top_players": monthly_top,
            "prizes": monthly_prizes,
            "total": monthly_total,
            "already_distributed": monthly_distributed,
        },

        "history": history_list,
    }

    return render(
        request,
        "admin_rewards.html",
        context,
    )


# ==========================================================
# DISTRIBUTE — WEEKLY
# ==========================================================

@staff_member_required
def distribute_weekly_rewards(request):

    return _distribute(request, "weekly")


# ==========================================================
# DISTRIBUTE — MONTHLY
# ==========================================================

@staff_member_required
def distribute_monthly_rewards(request):

    return _distribute(request, "monthly")


# ==========================================================
# CORE DISTRIBUTION
# ==========================================================

def _distribute(request, period_type):

    if request.method != "POST":

        return redirect(
            "leaderboard_rewards_dashboard"
        )

    period_key, period_start, period_end = (
        _get_current_period(period_type)
    )

    if _already_distributed(period_key):

        messages.warning(
            request,
            (
                f"Rewards for {period_key} "
                f"have already been distributed."
            ),
        )

        return redirect(
            "leaderboard_rewards_dashboard"
        )

    top_players = _get_top_players(
        period_start,
        period_end,
        limit=3,
    )

    if not top_players:

        messages.error(
            request,
            (
                f"No players found for "
                f"{period_key}. Nothing to distribute."
            ),
        )

        return redirect(
            "leaderboard_rewards_dashboard"
        )

    # ----------------------------------------------
    # Compute prizes
    # ----------------------------------------------

    prizes = {
        1: _get_reward_amount(1, period_type),
        2: _get_reward_amount(2, period_type),
        3: _get_reward_amount(3, period_type),
    }

    total_distributed = 0

    winners_snapshot = []

    with transaction.atomic():

        for player in top_players:

            rank = player["rank"]

            reward = prizes.get(rank, 0)

            if reward <= 0:
                continue

            profile = (
                UserProfileBalance.objects
                .select_for_update()
                .filter(
                    id=player["profile_obj"].id
                )
                .first()
            )

            if not profile:
                continue

            # Credit coins
            profile.coins += reward
            profile.save(update_fields=["coins"])

            # Ledger entry
            SystemTransactionLog.objects.create(
                user_profile=profile,
                amount=reward,
                log_type="LEADERBOARD_REWARD",
                reference_id=(
                    f"{period_type.upper()}_"
                    f"{period_key}_"
                    f"RANK_{rank}"
                ),
            )

            total_distributed += reward

            winners_snapshot.append({
                "rank": rank,
                "device_token": profile.device_token,
                "name": player["name"],
                "wins": player["wins"],
                "reward": reward,
            })

            print(
                f"🏆 REWARD PAID | "
                f"{period_type} {period_key} | "
                f"Rank #{rank} | "
                f"{profile.device_token} | "
                f"+{reward}"
            )

        # ----------------------------------------------
        # Save distribution record
        # ----------------------------------------------

        LeaderboardReward.objects.create(
            period_type=period_type,
            period_key=period_key,
            period_start=period_start,
            period_end=period_end,
            distributed_by=request.user.username,
            total_amount=total_distributed,
            winners_json=json.dumps(
                winners_snapshot
            ),
        )

    messages.success(
        request,
        (
            f"✅ {period_type.title()} rewards "
            f"distributed for {period_key}. "
            f"Total: {total_distributed} coins "
            f"to {len(winners_snapshot)} players."
        ),
    )

    return redirect(
        "leaderboard_rewards_dashboard"
    )
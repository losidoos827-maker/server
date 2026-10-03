# views/leaderboard.py

from datetime import timedelta

from django.db.models import (
    Count,
    Q,
    Sum,
)
from django.http import JsonResponse
from django.utils import timezone

from ..models import GameHistory, UserProfileBalance


def _get_period_start(period):

    now = timezone.now()

    if period == "weekly":
        return now - timedelta(days=7)

    if period == "monthly":
        return now - timedelta(days=30)

    return None


def get_leaderboard(request):

    period = str(
        request.GET.get("period", "alltime")
    ).lower()

    if period not in ("weekly", "monthly", "alltime"):
        period = "alltime"

    try:
        limit = int(request.GET.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50

    limit = max(1, min(limit, 100))

    my_device_token = str(
        request.GET.get("device_token", "")
    ).strip()

    qs = GameHistory.objects.all()

    start_date = _get_period_start(period)

    if start_date:
        qs = qs.filter(played_at__gte=start_date)

    # ----------------------------------------------
    # Top players
    # ----------------------------------------------

    agg = (
        qs.values("user_profile__device_token")
        .annotate(
            games=Count("id"),
            wins=Count("id", filter=Q(result="WON")),
            losses=Count("id", filter=Q(result="LOST")),
            earnings=Sum("coins_change"),
        )
        .filter(games__gt=0)
    )

    agg_list = list(
        agg.order_by("-wins", "-earnings")[:limit]
    )

    tokens = [
        row["user_profile__device_token"]
        for row in agg_list
    ]

    profiles = {
        p.device_token: p
        for p in UserProfileBalance.objects.filter(
            device_token__in=tokens
        )
    }

    top_players = []

    for idx, row in enumerate(agg_list):

        token = row["user_profile__device_token"]
        profile = profiles.get(token)

        display_name = (
            profile.nickname
            if profile and profile.nickname
            else "Player"
        )

        pic_url = None
        if profile and profile.profile_pic:
            try:
                pic_url = profile.profile_pic.url
            except Exception:
                pic_url = None

        wins = row["wins"] or 0
        games = row["games"] or 0

        win_rate = (
            round((wins / games) * 100, 1)
            if games > 0
            else 0.0
        )

        top_players.append({
            "rank": idx + 1,
            "device_token": token,
            "display_name": display_name,
            "profile_pic": pic_url,
            "wins": wins,
            "games": games,
            "losses": row["losses"] or 0,
            "earnings": row["earnings"] or 0,
            "win_rate": win_rate,
        })

    # ----------------------------------------------
    # My rank
    #
    # FIX: must use .order_by() before .first()
    #      because we're performing aggregation.
    # ----------------------------------------------

    my_rank_data = None

    if my_device_token:

        my_row = (
            qs.values("user_profile__device_token")
            .annotate(
                games=Count("id"),
                wins=Count("id", filter=Q(result="WON")),
                earnings=Sum("coins_change"),
            )
            .filter(
                user_profile__device_token=my_device_token,
                games__gt=0,
            )
            .order_by("-wins", "-earnings")   # ← FIX
            .first()
        )

        if my_row:

            my_wins = my_row["wins"] or 0
            my_games = my_row["games"] or 0
            my_earnings = my_row["earnings"] or 0

            better_count = (
                qs.values("user_profile__device_token")
                .annotate(
                    games=Count("id"),
                    wins=Count("id", filter=Q(result="WON")),
                    earnings=Sum("coins_change"),
                )
                .filter(games__gt=0)
                .filter(
                    Q(wins__gt=my_wins)
                    | (
                        Q(wins=my_wins)
                        & Q(earnings__gt=my_earnings)
                    )
                )
                .count()
            )

            my_rank = better_count + 1

            my_win_rate = (
                round((my_wins / my_games) * 100, 1)
                if my_games > 0
                else 0.0
            )

            my_rank_data = {
                "rank": my_rank,
                "device_token": my_device_token,
                "wins": my_wins,
                "games": my_games,
                "earnings": my_earnings,
                "win_rate": my_win_rate,
            }

    return JsonResponse({
        "status": "success",
        "period": period,
        "top_players": top_players,
        "my_rank": my_rank_data,
    })
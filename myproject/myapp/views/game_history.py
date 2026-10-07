# views/game_history.py

from django.http import JsonResponse

from ..models import GameHistory, UserProfileBalance


def get_game_history(request, device_token):

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
        limit = int(request.GET.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50

    limit = max(1, min(limit, 200))

    entries = (
        GameHistory.objects
        .filter(user_profile=profile)
        .order_by("-played_at")[:limit]
    )

    total_games = GameHistory.objects.filter(user_profile=profile).count()
    total_wins = GameHistory.objects.filter(user_profile=profile, result="WON").count()
    total_losses = GameHistory.objects.filter(user_profile=profile, result="LOST").count()
    total_cancelled = GameHistory.objects.filter(user_profile=profile, result="CANCELLED").count()

    win_rate = (
        round((total_wins / total_games) * 100, 1)
        if total_games > 0
        else 0.0
    )

    history_payload = []

    for entry in entries:

        history_payload.append({
            "game_id": entry.game_id,
            "bet_amount": entry.bet_amount,
            "result": entry.result,
            "coins_change": entry.coins_change,
            "game_mode": entry.game_mode,
            "opponent_count": entry.opponent_count,
            "played_at": entry.played_at.strftime("%d %b %Y, %I:%M %p"),
        })

    return JsonResponse({
        "status": "success",
        "stats": {
            "total_games": total_games,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "total_cancelled": total_cancelled,
            "win_rate": win_rate,
        },
        "history": history_payload,
    })
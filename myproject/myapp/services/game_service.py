# myapp/services/game_service.py

import uuid
import time

from django.core.cache import cache

from ..models import SystemSetting


# ==========================================
# GIFT SYSTEM RULES
# ==========================================

def is_gift_enabled():

    try:

        s = SystemSetting.objects.get(
            key="gift_enabled"
        )

        return s.value == "1"

    except SystemSetting.DoesNotExist:

        return True


def get_spin_cost():

    try:

        s = SystemSetting.objects.get(
            key="paid_spin_cost"
        )

        return int(s.value)

    except (
        SystemSetting.DoesNotExist,
        ValueError,
    ):

        return 40


def can_user_spin(profile):

    if not is_gift_enabled():

        return (
            False,
            "Gift system disabled by admin",
        )

    return True, "allowed"


# ==========================================================
# GAME STATE IMPORTS
# ==========================================================

from .game_state import (
    get_or_create_game_state,
    assign_player_color,
    remove_game_state,
)


# ==========================================================
# STALE LOBBY CLEANUP HELPER
#
# Refunds locked coins and removes in-memory state.
# ==========================================================

def _cleanup_stale_lobby(game_id):

    from ..consumers import ACTIVE_GAMES

    from .wager_service import cancel_wager

    try:

        cancel_wager(str(game_id))

    except Exception as exc:

        print(
            f"⚠️ STALE LOBBY refund failed | "
            f"Game={game_id} | Error={exc}"
        )

    ACTIVE_GAMES.pop(str(game_id), None)

    remove_game_state(str(game_id))

    print(
        f"🧹 STALE LOBBY cleaned + refunded | "
        f"Game={game_id}"
    )


# ==========================================================
# FIND WAITING ROOM
# ==========================================================

def find_waiting_room(
    is_two_player=True,
    player_token=None,
):

    from ..consumers import ACTIVE_GAMES

    now = time.time()

    required_players = (
        2 if is_two_player else 4
    )

    for game_id, state in list(
        ACTIVE_GAMES.items()
    ):

        if state.get("is_private"):

            continue

        if (
            state.get("game_status")
            == "LOBBY"
            and (
                now
                - state.get(
                    "created_at",
                    now
                )
                > 120
            )
        ):

            _cleanup_stale_lobby(game_id)

            continue

        if state.get(
            "is_two_player_mode"
        ) != is_two_player:

            continue

        if state.get("game_status") in (
            "COMPLETED",
            "CANCELLED",
            "ACTIVE",
        ):

            continue

        # ----------------------------------------------
        # Skip rooms where THIS player is already in
        # (fixes "already joined" bug)
        # ----------------------------------------------

        if player_token:

            assignments = state.get(
                "player_assignments",
                {}
            )

            if player_token in assignments:

                print(
                    f"⏭️ SKIP room {game_id} — "
                    f"player already in"
                )

                continue

        player_count = len(
            state.get(
                "player_assignments",
                {}
            )
        )

        if player_count < required_players:

            return str(game_id)

    return None


# ==========================================================
# CREATE ROOM
# ==========================================================

def create_room(
    is_two_player=True
):

    game_id = str(uuid.uuid4())

    state = get_or_create_game_state(
        game_id=game_id,
        is_two_player=is_two_player,
    )

    return game_id, state


# ==========================================================
# JOIN MATCHMAKING
# ==========================================================

def initialize_game(
    player_token,
    player_name="Player",
    is_two_player=True,
):

    if not player_token:

        return {
            "status":
                "error",

            "message":
                "Player token is required.",
        }

    if not player_name:

        player_name = "Player"

    # ----------------------------------------------
    # REMOVE player from any stale LOBBY room
    # they are already sitting in.
    #
    # This fixes the "already joined" bug when
    # user taps Play, backs out, and taps Play
    # again quickly.
    #
    # If the room is now empty, refund coins.
    # ----------------------------------------------

    from ..consumers import ACTIVE_GAMES

    for existing_id, existing_state in list(
        ACTIVE_GAMES.items()
    ):

        if existing_state.get(
            "game_status"
        ) != "LOBBY":

            continue

        assignments = existing_state.get(
            "player_assignments",
            {}
        )

        if player_token in assignments:

            # ------------------------------------------
            # Remove this player from the stale room
            # ------------------------------------------

            del assignments[player_token]

            # Also remove from player_names
            names = existing_state.get(
                "player_names",
                {}
            )

            if player_token in names:

                del names[player_token]

            print(
                f"🧹 CLEANED stale LOBBY | "
                f"Game={existing_id} | "
                f"Player={player_token} | "
                f"Remaining="
                f"{len(assignments)}"
            )

            # ------------------------------------------
            # If room is now empty, refund + delete
            # ------------------------------------------

            if not assignments:

                _cleanup_stale_lobby(existing_id)

    # ----------------------------------------------
    # Now find a fresh waiting room
    # ----------------------------------------------

    game_id = find_waiting_room(
        is_two_player=is_two_player,
        player_token=player_token,
    )

    if game_id is None:

        game_id, state = create_room(
            is_two_player=is_two_player
        )

        print(
            f"🆕 ROOM CREATED | "
            f"Game={game_id} | "
            f"Mode="
            f"{'2P' if is_two_player else '4P'}"
        )

    else:

        state = get_or_create_game_state(
            game_id=game_id,
            is_two_player=is_two_player,
        )

        print(
            f"🔎 ROOM FOUND | "
            f"Game={game_id} | "
            f"assignments="
            f"{state.get('player_assignments')} | "
            f"turn_order="
            f"{state.get('player_turn_order')}"
        )

    assigned_color = assign_player_color(
        state,
        player_token,
    )

    if assigned_color is None:

        return {
            "status":
                "full",

            "game_id":
                game_id,

            "message":
                "Room is full.",
        }

    if "player_names" not in state:

        state["player_names"] = {}

    state["player_names"][
        player_token
    ] = player_name

    player_count = len(
        state.get(
            "player_assignments",
            {}
        )
    )

    required_players = (
        2 if is_two_player else 4
    )

    if player_count >= required_players:

        state["game_status"] = "ACTIVE"

        state["status_text"] = "Game started!"

        print(
            f"🎮 GAME STARTED | "
            f"Game={game_id} | "
            f"Players={player_count}"
        )

    else:

        state["game_status"] = "LOBBY"

        state["status_text"] = (
            "Waiting for opponents..."
        )

    return {
        "status":
            "success",

        "game_id":
            game_id,

        "color":
            assigned_color,

        "game_status":
            state["game_status"],

        "player_count":
            player_count,

        "required_players":
            required_players,

        "player_names":
            state.get("player_names", {}),
    }


# ==========================================================
# DELETE ROOM
# ==========================================================

def delete_game_room(game_id):

    removed = remove_game_state(game_id)

    if removed:

        print(
            f"🗑️ ROOM DELETED | "
            f"Game={game_id}"
        )

    else:

        print(
            f"⚠️ ROOM DELETE REQUEST | "
            f"Game={game_id} | "
            f"Room not found"
        )

    return removed
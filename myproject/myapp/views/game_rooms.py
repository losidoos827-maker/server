# views/game_rooms.py

import json
import secrets
import string
import uuid

from django.db import transaction
from django.http import JsonResponse

from django.views.decorators.csrf import (
    csrf_exempt,
)

from ..models import (
    GameRoom,
    UserProfileBalance,
)

from ..services.game_service import (
    get_or_create_game_state,
    assign_player_color,
    remove_game_state,
)

from ..services.wager_service import (
    join_wager,
    cancel_wager,
)

from ..consumers import ACTIVE_GAMES


ROOM_CODE_ALPHABET = (
    string.ascii_uppercase + string.digits
)

ROOM_CODE_LENGTH = 6


# ==========================================================
# HELPERS
# ==========================================================

def _generate_unique_room_code():

    for _ in range(20):

        code = "".join(
            secrets.choice(
                ROOM_CODE_ALPHABET
            )
            for _ in range(ROOM_CODE_LENGTH)
        )

        if not (
            GameRoom.objects
            .filter(room_code=code)
            .exists()
        ):

            return code

    return (
        "RM"
        + secrets.token_hex(3).upper()
    )


def _err(
    message,
    status=400,
):

    return JsonResponse(
        {
            "status":
                "error",

            "message":
                message,
        },
        status=status,
    )


# ==========================================================
# CREATE PRIVATE ROOM
# ==========================================================

@csrf_exempt
def create_private_room(request):

    if request.method != "POST":

        return _err(
            "POST required.",
            status=405,
        )

    try:

        data = json.loads(request.body)

    except json.JSONDecodeError:

        return _err(
            "Malformed JSON payload."
        )

    player_token = data.get(
        "player_token"
    )

    player_name = (
        data.get("player_name")
        or "Player"
    ).strip()

    is_two_player = bool(
        data.get(
            "is_two_player_mode",
            True,
        )
    )

    # ------------------------------------------------
    # FIX 1: Compute max_players here
    # ------------------------------------------------

    max_players = 2 if is_two_player else 4

    try:

        bet_amount = int(
            data.get("bet_amount", 0)
        )

    except (ValueError, TypeError):

        return _err(
            "Invalid bet amount."
        )

    if not player_token:

        return _err(
            "player_token is required."
        )

    if bet_amount <= 0:

        return _err(
            (
                "bet_amount must be "
                "greater than zero."
            )
        )

    # ------------------------------------------------
    # Balance pre-check
    # ------------------------------------------------

    profile = (
        UserProfileBalance.objects
        .filter(device_token=player_token)
        .first()
    )

    if not profile:

        return _err(
            "User wallet profile not found."
        )

    if profile.coins < bet_amount:

        return _err(
            (
                f"Insufficient funds. "
                f"Required: {bet_amount}, "
                f"Available: {profile.coins}"
            )
        )

    # ------------------------------------------------
    # Identifiers
    # ------------------------------------------------

    game_id = str(uuid.uuid4())

    room_code = _generate_unique_room_code()

    print(
        f"🆕 CREATE-ROOM | game_id={game_id} "
        f"room_code={room_code} "
        f"is_two_player={is_two_player} "
        f"max_players={max_players} "
        f"bet={bet_amount}"
    )

    # ------------------------------------------------
    # In-memory state
    # ------------------------------------------------

    state = get_or_create_game_state(
        game_id=game_id,
        is_two_player=is_two_player,
    )

    state["bet_amount"] = bet_amount

    state["is_private"] = True

    state["room_code"] = room_code

    color = assign_player_color(
        state,
        player_token,
    )

    if color is None:

        remove_game_state(game_id)

        return _err(
            (
                "Failed to assign host color. "
                "Room is full."
            )
        )

    if "player_names" not in state:

        state["player_names"] = {}

    state["player_names"][
        player_token
    ] = player_name

    # ------------------------------------------------
    # Create DB GameRoom
    # FIX: include max_players
    # ------------------------------------------------

    try:

        GameRoom.objects.create(
            game_id=game_id,
            bet_amount=bet_amount,
            game_status="LOBBY",
            total_pool_escrow=0,
            is_private=True,
            room_code=room_code,
            max_players=max_players,
        )

    except Exception as exc:

        remove_game_state(game_id)

        return _err(
            f"Failed to create room: {exc}",
            status=500,
        )

    # ------------------------------------------------
    # Lock bet
    # ------------------------------------------------

    result = join_wager(
        device_token=player_token,
        game_id=game_id,
        bet_amount=bet_amount,
    )

    if result.get("status") != "success":

        remove_game_state(game_id)

        (
            GameRoom.objects
            .filter(game_id=game_id)
            .delete()
        )

        return _err(
            result.get(
                "message",
                "Failed to lock bet.",
            )
        )

    return JsonResponse({
        "status":
            "success",

        "game_id":
            game_id,

        "room_code":
            room_code,

        "color":
            color,

        "bet_amount":
            bet_amount,

        "game_status":
            result.get(
                "game_status",
                "LOBBY",
            ),

        "coins":
            result.get(
                "coins",
                profile.coins,
            ),

        "locked_coins":
            result.get(
                "locked_coins",
                profile.locked_coins,
            ),

        "is_two_player_mode":
            is_two_player,

        "is_private":
            True,

        "max_players":
            max_players,
    })


# ==========================================================
# JOIN PRIVATE ROOM
# ==========================================================

@csrf_exempt
def join_private_room(request):

    if request.method != "POST":

        return _err(
            "POST required.",
            status=405,
        )

    try:

        data = json.loads(request.body)

    except json.JSONDecodeError:

        return _err(
            "Malformed JSON payload."
        )

    room_code = (
        data.get("room_code") or ""
    ).strip().upper()

    player_token = data.get(
        "player_token"
    )

    player_name = (
        data.get("player_name")
        or "Player"
    ).strip()

    if not room_code:

        return _err(
            "room_code is required."
        )

    if not player_token:

        return _err(
            "player_token is required."
        )

    game = (
        GameRoom.objects
        .filter(
            room_code=room_code,
            is_private=True,
        )
        .first()
    )

    if not game:

        return _err(
            "Room not found. Check the code."
        )

    # ------------------------------------------------
    # DEBUG
    # ------------------------------------------------

    print(
        f"🔍 JOIN-ROOM | code={room_code} "
        f"player={player_token} "
        f"status={game.game_status} "
        f"max_players={game.max_players} "
        f"player_count={game.players.count()}"
    )

    if game.game_status != "LOBBY":

        return _err(
            (
                f"Room is no longer accepting "
                f"players. Status: "
                f"{game.game_status}"
            )
        )

    # ------------------------------------------------
    # Already joined? Return current info
    # ------------------------------------------------

    if game.players.filter(
        device_token=player_token
    ).exists():

        state = ACTIVE_GAMES.get(
            game.game_id,
            {},
        )

        return JsonResponse({
            "status":
                "success",

            "game_id":
                game.game_id,

            "room_code":
                game.room_code,

            "color":
                state.get(
                    "player_assignments",
                    {},
                ).get(player_token),

            "bet_amount":
                game.bet_amount,

            "game_status":
                game.game_status,

            "max_players":
                game.max_players,

            "already_joined":
                True,
        })

    # ------------------------------------------------
    # FIX 2: Use game.max_players instead of 2
    # ------------------------------------------------

    if (
        game.players.count()
        >= game.max_players
    ):

        print(
            f"🔍 ROOM FULL | "
            f"{game.players.count()}/"
            f"{game.max_players}"
        )

        return _err("Room is full.")

    # ------------------------------------------------
    # Balance check
    # ------------------------------------------------

    profile = (
        UserProfileBalance.objects
        .filter(device_token=player_token)
        .first()
    )

    if not profile:

        return _err(
            "User wallet profile not found."
        )

    if profile.coins < game.bet_amount:

        return _err(
            (
                f"Insufficient funds. "
                f"Required: {game.bet_amount}, "
                f"Available: {profile.coins}"
            )
        )

    # ------------------------------------------------
    # In-memory state
    # ------------------------------------------------

    state = ACTIVE_GAMES.get(game.game_id)

    if not state:

        # ------------------------------------------------
        # FIX 3: Use game.max_players to decide mode
        # ------------------------------------------------

        is_two_player = (
            game.max_players == 2
        )

        state = get_or_create_game_state(
            game_id=game.game_id,
            is_two_player=is_two_player,
        )

        state["bet_amount"] = game.bet_amount

        state["is_private"] = True

        state["room_code"] = game.room_code

    color = assign_player_color(
        state,
        player_token,
    )

    if color is None:

        return _err("Room is full.")

    print(
        f"👤 JOIN-ROOM ASSIGNED | "
        f"color={color} "
        f"state_is_2p="
        f"{state.get('is_two_player_mode')} "
        f"assignments="
        f"{state.get('player_assignments')}"
    )

    if "player_names" not in state:

        state["player_names"] = {}

    state["player_names"][
        player_token
    ] = player_name

    # ------------------------------------------------
    # Lock bet
    # ------------------------------------------------

    result = join_wager(
        device_token=player_token,
        game_id=game.game_id,
        bet_amount=game.bet_amount,
    )

    if result.get("status") != "success":

        print(
            f"🔍 JOIN-WAGER FAILED | "
            f"{result.get('message')}"
        )

        return _err(
            result.get(
                "message",
                "Failed to lock bet.",
            )
        )

    # ------------------------------------------------
    # Sync in-memory with DB
    # ------------------------------------------------

    game.refresh_from_db()

    if game.game_status == "ACTIVE":

        state["game_status"] = "ACTIVE"

        state["status_text"] = "Game started!"

    return JsonResponse({
        "status":
            "success",

        "game_id":
            game.game_id,

        "room_code":
            game.room_code,

        "color":
            color,

        "bet_amount":
            game.bet_amount,

        "game_status":
            game.game_status,

        "coins":
            result.get(
                "coins",
                profile.coins,
            ),

        "locked_coins":
            result.get(
                "locked_coins",
                profile.locked_coins,
            ),

        # ------------------------------------------------
        # FIX 4: Use game.max_players, not state default
        # ------------------------------------------------

        "is_two_player_mode":
            game.max_players == 2,

        "is_private":
            True,

        "max_players":
            game.max_players,
    })


# ==========================================================
# CANCEL PRIVATE ROOM
# ==========================================================

@csrf_exempt
def cancel_private_room(request):

    if request.method != "POST":

        return _err(
            "POST required.",
            status=405,
        )

    try:

        data = json.loads(request.body)

    except json.JSONDecodeError:

        return _err(
            "Malformed JSON payload."
        )

    player_token = data.get(
        "player_token"
    )

    game_id = data.get("game_id")

    if not player_token or not game_id:

        return _err(
            (
                "player_token and game_id "
                "are required."
            )
        )

    game = (
        GameRoom.objects
        .filter(game_id=game_id)
        .first()
    )

    if not game:

        return _err(
            "Room not found.",
            status=404,
        )

    if not game.players.filter(
        device_token=player_token
    ).exists():

        return _err(
            (
                "You are not a participant "
                "of this room."
            ),
            status=403,
        )

    if game.game_status in (
        "COMPLETED",
        "CANCELLED",
    ):

        return _err(
            f"Room already {game.game_status}."
        )

    result = cancel_wager(game_id)

    remove_game_state(game_id)

    if result.get("status") != "success":

        return _err(
            result.get(
                "message",
                "Cancellation failed.",
            )
        )

    return JsonResponse({
        "status":
            "success",

        "message":
            (
                "Room cancelled and "
                "refunds issued."
            ),

        "game_id":
            game_id,

        "players_refunded":
            result.get(
                "players_refunded",
                0,
            ),
    })


# ==========================================================
# ROOM STATUS  (polling)
# ==========================================================

def get_private_room_status(
    request,
    game_id,
):

    game = (
        GameRoom.objects
        .filter(game_id=game_id)
        .first()
    )

    if not game:

        return _err(
            "Room not found.",
            status=404,
        )

    state = ACTIVE_GAMES.get(
        game_id,
        {},
    )

    assignments = state.get(
        "player_assignments",
        {},
    )

    player_names = state.get(
        "player_names",
        {},
    )

    players_payload = []

    for device_token, color in (
        assignments.items()
    ):

        players_payload.append({
            "device_token":
                device_token,

            "color":
                color,

            "name":
                player_names.get(
                    device_token,
                    "Player",
                ),
        })

    return JsonResponse({
        "status":
            "success",

        "game_id":
            game.game_id,

        "room_code":
            game.room_code,

        "bet_amount":
            game.bet_amount,

        "is_private":
            game.is_private,

        "game_status":
            game.game_status,

        "player_count":
            len(players_payload),

        # ------------------------------------------------
        # FIX: use game.max_players (accurate)
        # ------------------------------------------------

        "required_players":
            game.max_players,

        "players":
            players_payload,

        "total_pool_escrow":
            game.total_pool_escrow,
    })
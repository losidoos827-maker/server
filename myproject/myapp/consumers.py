import json
import random
import asyncio

from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import (
    AsyncWebsocketConsumer,
)

from .services.wager_service import (
    finalize_wager_game,
    cancel_wager,
)


# ==========================================================
# ACTIVE GAME MEMORY
# ==========================================================

ACTIVE_GAMES = {}


# ==========================================================
# PENDING AUTO-SURRENDER TASKS
#
# Key:   "{game_id}:{player_token}"
# Value: asyncio.Task
# ==========================================================

PENDING_SURRENDER_TASKS = {}


# ==========================================================
# AUTO-SURRENDER GRACE PERIOD
#
# Player disconnects → wait 30s → if not back, surrender.
# ==========================================================

AUTO_SURRENDER_GRACE_SECONDS = 30


# ==========================================================
# LUDO BOARD CONFIGURATION
# ==========================================================

START_OFFSETS = {

    "BLUE":
        0,

    "RED":
        13,

    "GREEN":
        26,

    "YELLOW":
        39,
}

SAFE_GLOBAL_CELLS = {
    0,
    8,
    13,
    21,
    26,
    34,
    39,
    47,
}


# ==========================================================
# GLOBAL CELL
# ==========================================================

def get_global_cell_index(
    color,
    position,
):

    if position == -1 or position >= 51:

        return None

    return (
        START_OFFSETS[color] + position
    ) % 52


# ==========================================================
# DB HELPERS (module-level, usable from tasks)
# ==========================================================

@database_sync_to_async
def _ensure_game_room_exists(
    game_id,
    state,
):

    from .models import (
        GameRoom,
        UserProfileBalance,
    )

    game = (
        GameRoom.objects
        .filter(game_id=game_id)
        .first()
    )

    if game:

        return game

    assignments = state.get(
        "player_assignments",
        {}
    )

    bet_amount = int(
        state.get("bet_amount", 0)
    )

    game = GameRoom.objects.create(
        game_id=game_id,
        bet_amount=bet_amount,
        game_status="ACTIVE",
        total_pool_escrow=(
            bet_amount
            * len(assignments)
        ),
    )

    for device_token in assignments.keys():

        profile = (
            UserProfileBalance.objects
            .filter(device_token=device_token)
            .first()
        )

        if profile:

            game.players.add(profile)

    return game


# ==========================================================
# AUTO-SURRENDER TASK
#
# Runs 30s after a player disconnects. If the player hasn't
# reconnected by then, this function:
#
#   - If game is ACTIVE  → opponent wins + payout
#   - If game is LOBBY   → cancel + refund
# ==========================================================

async def _auto_surrender_task(
    game_id,
    leaver_token,
    channel_layer,
):

    try:

        await asyncio.sleep(
            AUTO_SURRENDER_GRACE_SECONDS
        )

    except asyncio.CancelledError:

        print(
            f"✋ Auto-surrender cancelled "
            f"(player reconnected) | "
            f"Game={game_id} | "
            f"Player={leaver_token}"
        )

        return

    state = ACTIVE_GAMES.get(game_id)

    if not state:

        return

    status = state.get("game_status")

    if status in (
        "COMPLETED",
        "CANCELLED",
        "PAYOUT_ERROR",
    ):

        return

    assignments = state.get(
        "player_assignments",
        {}
    )

    if leaver_token not in assignments:

        return

    # ==================================================
    # LOBBY — cancel + refund
    # ==================================================

    if status == "LOBBY":

        print(
            f"⏱️ AUTO-CANCEL (lobby abandon) | "
            f"Game={game_id} | "
            f"Leaver={leaver_token}"
        )

        await database_sync_to_async(
            cancel_wager
        )(game_id)

        state["game_status"] = "CANCELLED"

        state["status_text"] = (
            "Player abandoned room. "
            "Game cancelled."
        )

        ACTIVE_GAMES.pop(game_id, None)

        await channel_layer.group_send(
            f"ludo_match_{game_id}",
            {
                "type":
                    "send_state_payload",

                "payload":
                    state,
            },
        )

        return

    # ==================================================
    # ACTIVE — opponent wins
    # ==================================================

    if status == "ACTIVE":

        opponent_color = None
        opponent_token = None

        for token, color in assignments.items():

            if token != leaver_token:

                opponent_color = color
                opponent_token = token

                break

        if not opponent_color:

            return

        leaver_color = assignments.get(
            leaver_token
        )

        print(
            f"⏱️ AUTO-SURRENDER (disconnect) | "
            f"Game={game_id} | "
            f"Leaver={leaver_token} "
            f"({leaver_color}) | "
            f"Winner={opponent_token} "
            f"({opponent_color})"
        )

        state["status_text"] = (
            f"{leaver_color} disconnected! "
            f"{opponent_color} wins."
        )

        await _ensure_game_room_exists(
            game_id,
            state,
        )

        result = await database_sync_to_async(
            finalize_wager_game
        )(
            game_id=game_id,
            winning_device_token=(
                opponent_token
            ),
        )

        if (
            not isinstance(result, dict)
            or result.get("status") != "success"
        ):

            error_message = (
                result.get(
                    "message",
                    "Unknown",
                )
                if isinstance(result, dict)
                else "Invalid response"
            )

            state["game_status"] = "PAYOUT_ERROR"

            state["status_text"] = (
                f"Payout failed: {error_message}"
            )

            print(
                f"❌ AUTO-PAYOUT FAILED | "
                f"{error_message}"
            )

        else:

            state["winner"] = opponent_color

            state[
                "winner_device_token"
            ] = opponent_token

            state["game_status"] = "COMPLETED"

            state["winner_payout"] = int(
                result.get(
                    "winner_payout",
                    0,
                )
            )

            state["platform_fee"] = int(
                result.get(
                    "service_fee",
                    0,
                )
            )

            state["total_pool"] = int(
                result.get(
                    "total_pool",
                    0,
                )
            )

            state["status_text"] = (
                f"{opponent_color} WON! "
                f"{state['winner_payout']} "
                f"coins awarded."
            )

            print(
                f"🏆 AUTO-PAYOUT DONE | "
                f"Winner={opponent_color} | "
                f"Payout={state['winner_payout']}"
            )

        await channel_layer.group_send(
            f"ludo_match_{game_id}",
            {
                "type":
                    "send_state_payload",

                "payload":
                    state,
            },
        )


# ==========================================================
# LUDO WEBSOCKET CONSUMER
# ==========================================================

class LudoGameConsumer(
    AsyncWebsocketConsumer
):

    # ======================================================
    # CONNECT
    # ======================================================

    async def connect(self):

        self.game_id = str(
            self.scope[
                "url_route"
            ]["kwargs"]["game_id"]
        )

        self.room_group_name = (
            f"ludo_match_{self.game_id}"
        )

        # ----------------------------------------------
        # Read player token
        # ----------------------------------------------

        query_string = (
            self.scope
            .get(
                "query_string",
                b""
            )
            .decode("utf-8")
        )

        parsed_params = parse_qs(
            query_string
        )

        token_list = parsed_params.get(
            "player_token",
            []
        )

        self.player_token = (
            token_list[0]
            if token_list
            else None
        )

        # ----------------------------------------------
        # Authenticate
        # ----------------------------------------------

        if not self.player_token:

            print(
                f"❌ WS REJECTED | "
                f"No token | "
                f"Game={self.game_id}"
            )

            await self.close(code=4001)

            return

        if not await self._validate_player_token(
            self.player_token
        ):

            print(
                f"❌ WS REJECTED | "
                f"Unknown token="
                f"{self.player_token} | "
                f"Game={self.game_id}"
            )

            await self.close(code=4001)

            return

        if not await self._player_in_game(
            self.game_id,
            self.player_token,
        ):

            print(
                f"❌ WS REJECTED | "
                f"Player not in game | "
                f"Game={self.game_id} "
                f"Player={self.player_token}"
            )

            await self.close(code=4003)

            return

        # ----------------------------------------------
        # Cancel any pending auto-surrender for this
        # player (they reconnected in time).
        # ----------------------------------------------

        task_key = (
            f"{self.game_id}:"
            f"{self.player_token}"
        )

        existing = PENDING_SURRENDER_TASKS.pop(
            task_key,
            None,
        )

        if existing:

            existing.cancel()

            print(
                f"✋ Cancelled pending auto-surrender "
                f"(reconnected) | "
                f"Game={self.game_id} | "
                f"Player={self.player_token}"
            )

        # ----------------------------------------------
        # Join group
        # ----------------------------------------------

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )

        await self.accept()

        print(
            f"▼ WS CONNECTED | "
            f"Game={self.game_id} | "
            f"Player={self.player_token}"
        )

        await self.broadcast_current_state()

    # ======================================================
    # DB HELPERS
    # ======================================================

    @database_sync_to_async
    def _validate_player_token(
        self,
        token,
    ):

        from .models import (
            UserProfileBalance
        )

        return (
            UserProfileBalance.objects
            .filter(device_token=token)
            .exists()
        )

    @database_sync_to_async
    def _player_in_game(
        self,
        game_id,
        token,
    ):

        from .models import GameRoom

        return (
            GameRoom.objects
            .filter(
                game_id=game_id,
                players__device_token=token,
            )
            .exists()
        )

    # ======================================================
    # DISCONNECT
    # ======================================================

    async def disconnect(
        self,
        close_code
    ):

        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name,
        )

        print(
            f"▲ WS DISCONNECTED | "
            f"Game={self.game_id} | "
            f"Player={self.player_token}"
        )

        # ----------------------------------------------
        # Schedule auto-surrender if game is ongoing
        # ----------------------------------------------

        if not self.player_token:

            return

        state = ACTIVE_GAMES.get(
            self.game_id
        )

        if not state:

            return

        status = state.get("game_status")

        if status not in (
            "LOBBY",
            "ACTIVE",
        ):

            return

        assignments = state.get(
            "player_assignments",
            {}
        )

        if self.player_token not in assignments:

            return

        task_key = (
            f"{self.game_id}:"
            f"{self.player_token}"
        )

        existing = PENDING_SURRENDER_TASKS.pop(
            task_key,
            None,
        )

        if existing:

            existing.cancel()

        task = asyncio.create_task(
            _auto_surrender_task(
                self.game_id,
                self.player_token,
                self.channel_layer,
            )
        )

        PENDING_SURRENDER_TASKS[
            task_key
        ] = task

        print(
            f"⏱️ Scheduled auto-surrender in "
            f"{AUTO_SURRENDER_GRACE_SECONDS}s | "
            f"Game={self.game_id} | "
            f"Player={self.player_token}"
        )

    # ======================================================
    # GET ACTIVE PLAYER COLORS
    # ======================================================

    def get_active_player_colors(
        self,
        state
    ):

        assignments = state.get(
            "player_assignments",
            {}
        )

        colors = []

        for color in assignments.values():

            if color not in colors:

                colors.append(color)

        return colors

    # ======================================================
    # NORMALIZE TURN ORDER
    # ======================================================

    def normalize_turn_order(
        self,
        state
    ):

        if state.get(
            "game_status"
        ) != "ACTIVE":

            return

        active_colors = (
            self.get_active_player_colors(
                state
            )
        )

        if not active_colors:

            return

        existing_order = state.get(
            "player_turn_order",
            []
        )

        new_order = []

        for color in existing_order:

            if (
                color in active_colors
                and color not in new_order
            ):

                new_order.append(color)

        for color in active_colors:

            if color not in new_order:

                new_order.append(color)

        state[
            "player_turn_order"
        ] = new_order

        if not new_order:

            state["turn_index"] = 0

        else:

            state["turn_index"] = (
                state.get(
                    "turn_index",
                    0
                )
                % len(new_order)
            )

    # ======================================================
    # TURN CHECK
    # ======================================================

    def is_current_players_turn(
        self,
        state
    ):

        self.normalize_turn_order(
            state
        )

        order = state.get(
            "player_turn_order",
            []
        )

        if not order:

            return False

        current_color = (
            order[state["turn_index"]]
        )

        assigned_color = (
            state[
                "player_assignments"
            ].get(self.player_token)
        )

        return (
            current_color
            == assigned_color
        )

    # ======================================================
    # FIND DEVICE BY COLOR
    # ======================================================

    def get_device_for_color(
        self,
        state,
        color
    ):

        for (
            device_token,
            assigned_color
        ) in state[
            "player_assignments"
        ].items():

            if assigned_color == color:

                return device_token

        return None

    # ======================================================
    # WIN CONDITION
    # ======================================================

    def has_player_won(
        self,
        state,
        color
    ):

        player_tokens = [
            token
            for token in state["tokens"]
            if token["color"] == color
        ]

        return (
            len(player_tokens) == 4
            and all(
                token["position"] == 56
                for token in player_tokens
            )
        )

    # ======================================================
    # DATABASE PAYOUT
    # ======================================================

    @database_sync_to_async
    def payout_winner(
        self,
        game_id,
        winning_device_token
    ):

        return finalize_wager_game(
            game_id=game_id,
            winning_device_token=(
                winning_device_token
            ),
        )

    # ======================================================
    # HANDLE WINNER
    # ======================================================

    async def handle_game_winner(
        self,
        state,
        winning_color
    ):

        winning_device_token = (
            self.get_device_for_color(
                state,
                winning_color
            )
        )

        if not winning_device_token:

            state[
                "game_status"
            ] = "PAYOUT_ERROR"

            state[
                "status_text"
            ] = (
                "Winner detected, but "
                "player identity could "
                "not be found."
            )

            print(
                f"❌ PAYOUT ERROR | "
                f"Game={self.game_id} | "
                f"Color={winning_color} | "
                f"Device not found"
            )

            return

        await _ensure_game_room_exists(
            self.game_id,
            state,
        )

        try:

            result = await self.payout_winner(
                self.game_id,
                winning_device_token
            )

        except Exception as e:

            state[
                "game_status"
            ] = "PAYOUT_ERROR"

            state[
                "status_text"
            ] = (
                "Winner detected, but an "
                "internal payout error "
                "occurred."
            )

            print(
                f"❌ PAYOUT EXCEPTION | "
                f"Game={self.game_id} | "
                f"Winner="
                f"{winning_device_token} | "
                f"Error={e}"
            )

            return

        payout_success = (
            isinstance(result, dict)
            and result.get("status")
            == "success"
        )

        if not payout_success:

            state[
                "game_status"
            ] = "PAYOUT_ERROR"

            error_message = (
                result.get(
                    "message",
                    "Unknown payout error."
                )
                if isinstance(result, dict)
                else "Invalid payout response."
            )

            state[
                "status_text"
            ] = (
                f"{winning_color} won, "
                f"but payout failed: "
                f"{error_message}"
            )

            print(
                f"❌ PAYOUT FAILED | "
                f"Game={self.game_id} | "
                f"Winner="
                f"{winning_device_token} | "
                f"Reason={error_message}"
            )

            return

        state["winner"] = winning_color

        state[
            "winner_device_token"
        ] = winning_device_token

        state["game_status"] = "COMPLETED"

        state[
            "winner_payout"
        ] = int(
            result.get(
                "winner_payout",
                0
            )
        )

        state[
            "platform_fee"
        ] = int(
            result.get(
                "service_fee",
                0
            )
        )

        state[
            "total_pool"
        ] = int(
            result.get(
                "total_pool",
                0
            )
        )

        state[
            "status_text"
        ] = (
            f"{winning_color} WON! "
            f"{state['winner_payout']} "
            f"coins awarded."
        )

        print(
            f"🏆 GAME WON | "
            f"Game={self.game_id} | "
            f"Winner={winning_color} | "
            f"Device="
            f"{winning_device_token} | "
            f"Payout="
            f"{state['winner_payout']} | "
            f"Fee={state['platform_fee']}"
        )

    # ======================================================
    # HANDLE SURRENDER
    # ======================================================

    async def handle_surrender(
        self,
        state
    ):

        current_status = state.get(
            "game_status"
        )

        # ==================================================
        # CASE 1 — GAME ACTIVE
        # ==================================================

        if current_status == "ACTIVE":

            opponent_color = None
            opponent_token = None

            for (
                token,
                color
            ) in state[
                "player_assignments"
            ].items():

                if token != self.player_token:

                    opponent_color = color
                    opponent_token = token

                    break

            if not opponent_color:

                state[
                    "game_status"
                ] = "CANCELLED"

                state[
                    "status_text"
                ] = (
                    "Player left before "
                    "game started."
                )

                await self.broadcast_current_state()

                return

            leaver_color = (
                state[
                    "player_assignments"
                ].get(self.player_token)
            )

            state[
                "status_text"
            ] = (
                f"{leaver_color} surrendered! "
                f"{opponent_color} wins."
            )

            print(
                f"🏳️ SURRENDER | "
                f"Game={self.game_id} | "
                f"Leaver={self.player_token} "
                f"({leaver_color}) | "
                f"Winner={opponent_token} "
                f"({opponent_color})"
            )

            await self.handle_game_winner(
                state,
                opponent_color
            )

            await self.broadcast_current_state()

            return

        # ==================================================
        # CASE 2 — GAME IN LOBBY
        # ==================================================

        if current_status == "LOBBY":

            state[
                "game_status"
            ] = "CANCELLED"

            state[
                "status_text"
            ] = "Player cancelled the match."

            print(
                f"🚪 LOBBY CANCELLED | "
                f"Game={self.game_id} | "
                f"Player={self.player_token}"
            )

            await self.broadcast_current_state()

            return

        # ==================================================
        # CASE 3 — Already COMPLETED
        # ==================================================

        await self.broadcast_current_state()

    # ======================================================
    # RECEIVE
    # ======================================================

    async def receive(
        self,
        text_data
    ):

        try:

            data = json.loads(text_data)

        except Exception:

            return

        action = data.get("action")

        state = ACTIVE_GAMES.get(
            self.game_id
        )

        if not state:

            return

        self.normalize_turn_order(state)

        # ==================================================
        # SURRENDER
        # ==================================================

        if action == "surrender":

            await self.handle_surrender(state)

            return

        # ==================================================
        # Already finished
        # ==================================================

        if state.get("game_status") in (
            "COMPLETED",
            "PAYOUT_ERROR",
        ):

            await self.broadcast_current_state()

            return

        # ==================================================
        # ROLL DICE
        # ==================================================

        if action == "roll_dice":

            if not self.is_current_players_turn(
                state
            ):

                print(
                    f"⚠️ REJECTED ROLL | "
                    f"Game={self.game_id} | "
                    f"Player="
                    f"{self.player_token}"
                )

                return

            await self.handle_dice_roll(state)

        # ==================================================
        # MOVE TOKEN
        # ==================================================

        elif action == "move_token":

            if not self.is_current_players_turn(
                state
            ):

                print(
                    f"⚠️ REJECTED MOVE | "
                    f"Game={self.game_id} | "
                    f"Player="
                    f"{self.player_token}"
                )

                return

            await self.handle_token_movement(
                state,
                data.get("token_id"),
                data.get("color")
            )

        await self.broadcast_current_state()

    # ======================================================
    # DICE ROLL
    # ======================================================

    async def handle_dice_roll(
        self,
        state
    ):

        self.normalize_turn_order(state)

        if state["has_rolled"]:

            return

        roll = random.randint(1, 6)

        state[
            "current_dice_value"
        ] = roll

        state["has_rolled"] = True

        current_player = (
            state[
                "player_turn_order"
            ][state["turn_index"]]
        )

        player_tokens = [
            token
            for token in state["tokens"]
            if token["color"]
            == current_player
        ]

        valid_moves = 0

        for token in player_tokens:

            if (
                token["position"] == -1
                and roll == 6
            ):

                valid_moves += 1

            elif (
                0
                <= token["position"]
                <= 55
                and (
                    token["position"]
                    + roll
                    <= 56
                )
            ):

                valid_moves += 1

        if valid_moves == 0:

            state["has_rolled"] = False

            state["turn_index"] = (
                state["turn_index"] + 1
            ) % len(
                state["player_turn_order"]
            )

            next_player = (
                state[
                    "player_turn_order"
                ][state["turn_index"]]
            )

            state[
                "status_text"
            ] = (
                f"{current_player} rolled "
                f"{roll} (No Moves)! "
                f"Pass to {next_player}."
            )

        else:

            state[
                "status_text"
            ] = (
                f"{current_player} rolled "
                f"{roll}! Select your token."
            )

    # ======================================================
    # TOKEN MOVEMENT
    # ======================================================

    async def handle_token_movement(
        self,
        state,
        token_id,
        color
    ):

        if not state["has_rolled"]:

            return

        self.normalize_turn_order(state)

        current_player = (
            state[
                "player_turn_order"
            ][state["turn_index"]]
        )

        if color != current_player:

            state[
                "status_text"
            ] = (
                f"It is not {color}'s turn! "
                f"It is {current_player}'s turn."
            )

            return

        token = next(
            (
                token
                for token in state["tokens"]
                if (
                    token["id"] == token_id
                    and token["color"] == color
                )
            ),
            None
        )

        if not token:

            return

        roll = state["current_dice_value"]

        move_executed = False

        grant_bonus_roll = (
            roll == 6
        )

        # ----------------------------------------------
        # Base yard
        # ----------------------------------------------

        if (
            token["position"] == -1
            and roll == 6
        ):

            token["position"] = 0

            move_executed = True

            state[
                "status_text"
            ] = (
                f"{color} moved a token "
                f"out of the base!"
            )

        # ----------------------------------------------
        # Normal movement
        # ----------------------------------------------

        elif (
            0
            <= token["position"]
            <= 55
        ):

            target_destination = (
                token["position"] + roll
            )

            if target_destination <= 56:

                token[
                    "position"
                ] = target_destination

                move_executed = True

                state[
                    "status_text"
                ] = (
                    f"{color} moved a token "
                    f"forward by {roll}."
                )

                target_global_cell = (
                    get_global_cell_index(
                        color,
                        target_destination
                    )
                )

                if (
                    target_global_cell
                    is not None
                    and target_global_cell
                    not in SAFE_GLOBAL_CELLS
                ):

                    for enemy_token in (
                        state["tokens"]
                    ):

                        if (
                            enemy_token["color"]
                            != color
                            and enemy_token[
                                "position"
                            ] >= 0
                        ):

                            enemy_global_cell = (
                                get_global_cell_index(
                                    enemy_token[
                                        "color"
                                    ],
                                    enemy_token[
                                        "position"
                                    ]
                                )
                            )

                            if (
                                enemy_global_cell
                                == target_global_cell
                            ):

                                enemy_token[
                                    "position"
                                ] = -1

                                grant_bonus_roll = True

                                state[
                                    "status_text"
                                ] = (
                                    f"{color} "
                                    f"kicked out "
                                    f"{enemy_token['color']}! "
                                    f"Bonus roll granted."
                                )

        if not move_executed:

            state[
                "status_text"
            ] = (
                "Invalid move for that "
                f"token! {color}, select "
                "a valid token."
            )

            return

        state["has_rolled"] = False

        # ----------------------------------------------
        # Win check
        # ----------------------------------------------

        if self.has_player_won(
            state,
            color
        ):

            state["winner"] = color

            state["game_status"] = "WON"

            state[
                "status_text"
            ] = (
                f"{color} WON THE GAME! "
                f"Processing payout..."
            )

            await self.handle_game_winner(
                state,
                color
            )

            return

        # ----------------------------------------------
        # Reached home
        # ----------------------------------------------

        if token["position"] == 56:

            grant_bonus_roll = True

            state[
                "status_text"
            ] = (
                f"{color} reached the goal! "
                f"Bonus roll granted."
            )

        # ----------------------------------------------
        # Turn change
        # ----------------------------------------------

        if not grant_bonus_roll:

            self.normalize_turn_order(state)

            state["turn_index"] = (
                state["turn_index"] + 1
            ) % len(
                state["player_turn_order"]
            )

            next_player = (
                state[
                    "player_turn_order"
                ][state["turn_index"]]
            )

            state[
                "status_text"
            ] = (
                f"{next_player}'s Turn! "
                f"Tap the dice to roll."
            )

        else:

            state[
                "status_text"
            ] = (
                f"{color}'s Bonus Roll! "
                f"Tap the dice."
            )

    # ======================================================
    # BROADCAST
    # ======================================================

    async def broadcast_current_state(
        self
    ):

        state = ACTIVE_GAMES.get(
            self.game_id
        )

        if not state:

            return

        self.normalize_turn_order(state)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type":
                    "send_state_payload",

                "payload":
                    state,
            }
        )

    # ======================================================
    # SEND STATE
    # ======================================================

    async def send_state_payload(
        self,
        event
    ):

        await self.send(
            text_data=json.dumps({
                "status":
                    "success",

                "game_state":
                    event["payload"],
            })
        )
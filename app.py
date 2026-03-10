"""HexaStack - Async turn-based hex tile sorting game. Phase 2.5."""

import copy
import json
import os
import random
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, jsonify, make_response, render_template, request

app = Flask(__name__)
app.config["DATABASE"] = os.environ.get(
    "HEXASTACK_DB", os.path.join(os.path.dirname(__file__), "hexastack.db")
)

# ---------------------------------------------------------------------------
# Board geometry
# ---------------------------------------------------------------------------

BOARD_CONFIGS = {
    "small": {"radius": 1, "hexes": 7, "colours": 3, "target_score": 30},
    "medium": {"radius": 2, "hexes": 19, "colours": 4, "target_score": 60},
    "large": {"radius": 3, "hexes": 37, "colours": 5, "target_score": 100},
}

COLOUR_PALETTE = ["#E8A87C", "#85CDCA", "#D4A5A5", "#C3B1E1", "#A8D8B9"]
COLOUR_NAMES = ["peach", "teal", "rose", "lavender", "sage"]


def hex_grid_coords(radius):
    coords = []
    for q in range(-radius, radius + 1):
        for r in range(-radius, radius + 1):
            if abs(q + r) <= radius:
                coords.append((q, r))
    return coords


def hex_neighbours(q, r):
    directions = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, -1), (-1, 1)]
    return [(q + dq, r + dr) for dq, dr in directions]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(app.config["DATABASE"])
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            board_size TEXT NOT NULL,
            board_state TEXT NOT NULL,
            current_turn INTEGER NOT NULL DEFAULT 1,
            player1_score INTEGER NOT NULL DEFAULT 0,
            player2_score INTEGER NOT NULL DEFAULT 0,
            offered_stacks TEXT NOT NULL,
            placed_this_turn INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            last_move TEXT,
            seed INTEGER NOT NULL,
            move_count INTEGER NOT NULL DEFAULT 0,
            game_mode TEXT NOT NULL DEFAULT 'link',
            player1_token TEXT,
            player2_token TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Game logic
# ---------------------------------------------------------------------------

def generate_stacks(game_seed, move_count, num_colours):
    rng = random.Random(game_seed + move_count * 1000)
    stacks = []
    for _ in range(3):
        num_layers = rng.choice([1, 1, 2])
        layers = [rng.randint(0, num_colours - 1) for _ in range(num_layers)]
        stacks.append(layers)
    return stacks


def _count_top(stack):
    """Count consecutive same-colour layers from the top."""
    if not stack:
        return 0, -1
    top = stack[-1]
    count = 0
    for layer in reversed(stack):
        if layer == top:
            count += 1
        else:
            break
    return count, top


def _find_all_mergeable_pairs(board, coords_set):
    """Find all adjacent pairs with matching top colours."""
    pairs = []
    seen = set()
    for coord_str in board:
        stack = board[coord_str]
        if not stack or coord_str not in coords_set:
            continue
        our_count, top_colour = _count_top(stack)
        q, r = map(int, coord_str.split(","))

        for nq, nr in hex_neighbours(q, r):
            nkey = f"{nq},{nr}"
            if nkey not in coords_set or nkey not in board or not board[nkey]:
                continue
            if board[nkey][-1] != top_colour:
                continue
            pair_key = tuple(sorted([coord_str, nkey]))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            their_count, _ = _count_top(board[nkey])
            pairs.append((coord_str, nkey, top_colour, our_count, their_count))
    return pairs


def _execute_merge(board, from_key, to_key, count):
    """Execute a single merge: move top `count` layers from from_key to to_key."""
    from_stack = board[from_key]
    transferred = from_stack[-count:]
    board[from_key] = from_stack[:-count]
    board[to_key] = board[to_key] + transferred
    return board


def _simulate_merges(board, coords_set):
    """Run merges to completion on a board copy. Returns (board, total_merges, total_layers_moved)."""
    board = copy.deepcopy(board)
    total_merges = 0
    total_layers = 0
    changed = True
    while changed:
        changed = False
        pairs = _find_all_mergeable_pairs(board, coords_set)
        if not pairs:
            break
        # Pick first pair, smaller into larger
        coord_a, coord_b, colour, count_a, count_b = pairs[0]
        if count_a <= count_b:
            board = _execute_merge(board, coord_a, coord_b, count_a)
            total_layers += count_a
        else:
            board = _execute_merge(board, coord_b, coord_a, count_b)
            total_layers += count_b
        total_merges += 1
        changed = True
    return board, total_merges, total_layers


def process_merges(board, coords_set):
    """
    Smart merge: when multiple merge directions exist, try each and pick
    the path that produces the most total chain merges (be kind to player).

    Returns (board, merge_events).
    """
    merge_events = []
    changed = True
    while changed:
        changed = False
        pairs = _find_all_mergeable_pairs(board, coords_set)
        if not pairs:
            break

        # If only one pair, just do it
        if len(pairs) == 1:
            coord_a, coord_b, colour, count_a, count_b = pairs[0]
            if count_a <= count_b:
                from_key, to_key, count = coord_a, coord_b, count_a
            else:
                from_key, to_key, count = coord_b, coord_a, count_b
        else:
            # Multiple possible merges — simulate each to find best outcome
            best_score = -1
            best_merge = None

            for coord_a, coord_b, colour, count_a, count_b in pairs:
                # Try direction A -> B
                sim_board = copy.deepcopy(board)
                sim_board = _execute_merge(sim_board, coord_a, coord_b, count_a)
                _, merges_ab, layers_ab = _simulate_merges(sim_board, coords_set)
                score_ab = merges_ab * 100 + layers_ab + count_a

                # Try direction B -> A
                sim_board = copy.deepcopy(board)
                sim_board = _execute_merge(sim_board, coord_b, coord_a, count_b)
                _, merges_ba, layers_ba = _simulate_merges(sim_board, coords_set)
                score_ba = merges_ba * 100 + layers_ba + count_b

                if score_ab >= score_ba:
                    if score_ab > best_score:
                        best_score = score_ab
                        best_merge = (coord_a, coord_b, count_a, colour)
                else:
                    if score_ba > best_score:
                        best_score = score_ba
                        best_merge = (coord_b, coord_a, count_b, colour)

            from_key, to_key, count, colour = best_merge

        board = _execute_merge(board, from_key, to_key, count)
        colour_val = board[to_key][-1]  # colour that was merged
        merge_events.append({
            "from": from_key, "to": to_key,
            "colour": colour_val, "count": count,
        })
        changed = True

    return board, merge_events


def process_clears(board):
    total_points = 0
    clear_events = []
    cleared = True
    while cleared:
        cleared = False
        for coord_str, stack in list(board.items()):
            if not stack:
                continue
            top_colour = stack[-1]
            count = 0
            for layer in reversed(stack):
                if layer == top_colour:
                    count += 1
                else:
                    break
            if count >= 10:
                board[coord_str] = stack[:len(stack) - count]
                total_points += count
                clear_events.append({
                    "hex": coord_str, "colour": top_colour, "count": count,
                })
                cleared = True
                break
    return board, total_points, clear_events


def calculate_tidiness_bonus(board, num_colours):
    bonus = 0
    for coord_str, stack in board.items():
        if not stack:
            continue
        top_colour = stack[-1]
        count = 0
        for layer in reversed(stack):
            if layer == top_colour:
                count += 1
            else:
                break
        if count >= 5:
            bonus += count - 4
    return bonus


def check_game_over(board, coords_set, target_score, p1_score, p2_score):
    if p1_score >= target_score or p2_score >= target_score:
        return True
    for coord in coords_set:
        key = f"{coord[0]},{coord[1]}"
        if key not in board or not board[key]:
            return False
    return True


# ---------------------------------------------------------------------------
# Player identity
# ---------------------------------------------------------------------------

def get_player_token():
    return request.cookies.get("hexastack_player")


def ensure_token_cookie(resp):
    if not request.cookies.get("hexastack_player"):
        resp.set_cookie(
            "hexastack_player",
            secrets.token_urlsafe(16),
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="Lax",
        )
    return resp


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    resp = make_response(render_template("index.html"))
    return ensure_token_cookie(resp)


@app.route("/api/create", methods=["POST"])
def create_game():
    data = request.get_json() or {}
    board_size = data.get("board_size", "medium")
    game_mode = data.get("game_mode", "link")

    if board_size not in BOARD_CONFIGS:
        return jsonify({"error": "Invalid board size"}), 400
    if game_mode not in ("local", "link"):
        return jsonify({"error": "Invalid game mode"}), 400

    config = BOARD_CONFIGS[board_size]
    game_id = secrets.token_urlsafe(8)
    seed = random.randint(0, 2**31)
    coords = hex_grid_coords(config["radius"])

    board = {}
    for q, r in coords:
        board[f"{q},{r}"] = []

    stacks = generate_stacks(seed, 0, config["colours"])
    now = datetime.now(timezone.utc).isoformat()

    player_token = get_player_token()
    if not player_token:
        player_token = secrets.token_urlsafe(16)

    db = get_db()
    db.execute(
        """
        INSERT INTO games (id, board_size, board_state, offered_stacks, seed,
                          game_mode, player1_token, placed_this_turn, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            game_id, board_size, json.dumps(board), json.dumps(stacks), seed,
            game_mode,
            player_token if game_mode == "link" else None,
            now, now,
        ),
    )
    db.commit()

    resp = make_response(jsonify({"game_id": game_id, "url": f"/game/{game_id}"}))
    if not request.cookies.get("hexastack_player"):
        resp.set_cookie(
            "hexastack_player", player_token,
            max_age=60 * 60 * 24 * 365, httponly=True, samesite="Lax",
        )
    return resp


@app.route("/game/<game_id>")
def game_page(game_id):
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return render_template("not_found.html"), 404
    resp = make_response(render_template("game.html", game_id=game_id))
    return ensure_token_cookie(resp)


@app.route("/api/game/<game_id>")
def get_game(game_id):
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return jsonify({"error": "Game not found"}), 404

    config = BOARD_CONFIGS[game["board_size"]]
    colours = COLOUR_PALETTE[:config["colours"]]
    colour_names = COLOUR_NAMES[:config["colours"]]

    player_number = None
    if game["game_mode"] == "link":
        token = get_player_token()
        if token:
            if game["player1_token"] == token:
                player_number = 1
            elif game["player2_token"] == token:
                player_number = 2
            elif game["player2_token"] is None:
                player_number = 2
                db.execute(
                    "UPDATE games SET player2_token = ? WHERE id = ?",
                    (token, game_id),
                )
                db.commit()

    return jsonify({
        "id": game["id"],
        "board_size": game["board_size"],
        "radius": config["radius"],
        "board": json.loads(game["board_state"]),
        "current_turn": game["current_turn"],
        "player1_score": game["player1_score"],
        "player2_score": game["player2_score"],
        "offered_stacks": json.loads(game["offered_stacks"]),
        "placed_this_turn": game["placed_this_turn"],
        "status": game["status"],
        "last_move": json.loads(game["last_move"]) if game["last_move"] else None,
        "move_count": game["move_count"],
        "target_score": config["target_score"],
        "colours": colours,
        "colour_names": colour_names,
        "coords": hex_grid_coords(config["radius"]),
        "game_mode": game["game_mode"],
        "player_number": player_number,
    })


@app.route("/api/game/<game_id>/move", methods=["POST"])
def make_move(game_id):
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return jsonify({"error": "Game not found"}), 404
    if game["status"] != "active":
        return jsonify({"error": "Game is over"}), 400

    # Turn enforcement for link mode
    if game["game_mode"] == "link":
        token = get_player_token()
        current = game["current_turn"]
        if current == 1 and game["player1_token"] != token:
            return jsonify({"error": "Not your turn"}), 403
        if current == 2 and game["player2_token"] != token:
            return jsonify({"error": "Not your turn"}), 403

    data = request.get_json()
    stack_index = data.get("stack_index")
    target_hex = data.get("target_hex")

    if stack_index is None or target_hex is None:
        return jsonify({"error": "Missing stack_index or target_hex"}), 400

    board = json.loads(game["board_state"])
    offered = json.loads(game["offered_stacks"])
    config = BOARD_CONFIGS[game["board_size"]]
    coords = hex_grid_coords(config["radius"])
    coords_set = set(f"{q},{r}" for q, r in coords)

    if stack_index < 0 or stack_index >= len(offered):
        return jsonify({"error": "Invalid stack index"}), 400
    if offered[stack_index] is None:
        return jsonify({"error": "Stack already placed"}), 400
    if target_hex not in coords_set:
        return jsonify({"error": "Invalid hex coordinate"}), 400
    if board.get(target_hex) and len(board[target_hex]) > 0:
        return jsonify({"error": "Hex is not empty"}), 400

    # Place the stack
    chosen_stack = offered[stack_index]
    board[target_hex] = list(chosen_stack)

    # Mark stack as used (None = placed)
    offered[stack_index] = None

    # Process merges then clears, chain if needed
    board, merge_events = process_merges(board, coords_set)
    board, points, clear_events = process_clears(board)

    if clear_events:
        board, extra_merges = process_merges(board, coords_set)
        merge_events.extend(extra_merges)
        board, extra_points, extra_clears = process_clears(board)
        points += extra_points
        clear_events.extend(extra_clears)

    # Update scores
    current_player = game["current_turn"]
    p1_score = game["player1_score"]
    p2_score = game["player2_score"]

    if points > 0:
        if current_player == 1:
            p1_score += points
        else:
            p2_score += points

    placed_this_turn = game["placed_this_turn"] + 1

    # Check if all 3 stacks placed or no empty hexes remain
    remaining_stacks = [s for s in offered if s is not None]
    has_empty_hex = any(
        not board.get(f"{q},{r}") for q, r in coords
    )
    turn_complete = (placed_this_turn >= 3) or (len(remaining_stacks) == 0) or (not has_empty_hex)

    if turn_complete:
        next_turn = 2 if current_player == 1 else 1
        move_count = game["move_count"] + 1
        next_stacks = generate_stacks(game["seed"], move_count, config["colours"])
        placed_count = 0
    else:
        next_turn = current_player
        move_count = game["move_count"]
        next_stacks = offered  # keep the same stacks with nulls for placed ones
        placed_count = placed_this_turn

    # Check game over
    status = "active"
    if check_game_over(board, coords_set, config["target_score"], p1_score, p2_score):
        status = "finished"
        tidiness = calculate_tidiness_bonus(board, config["colours"])
        p1_score += tidiness
        p2_score += tidiness

    now = datetime.now(timezone.utc).isoformat()

    last_move = {
        "hex": target_hex,
        "stack": chosen_stack,
        "player": current_player,
        "merge_events": merge_events,
        "clear_events": clear_events,
    }

    db.execute(
        """
        UPDATE games SET
            board_state = ?, current_turn = ?,
            player1_score = ?, player2_score = ?,
            offered_stacks = ?, placed_this_turn = ?,
            status = ?, last_move = ?,
            move_count = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(board), next_turn, p1_score, p2_score,
            json.dumps(next_stacks), placed_count,
            status, json.dumps(last_move),
            move_count, now, game_id,
        ),
    )
    db.commit()

    return jsonify({
        "success": True,
        "points_scored": points,
        "board": board,
        "current_turn": next_turn,
        "player1_score": p1_score,
        "player2_score": p2_score,
        "offered_stacks": next_stacks,
        "placed_this_turn": placed_count,
        "turn_complete": turn_complete,
        "status": status,
        "last_move": last_move,
        "merge_events": merge_events,
        "clear_events": clear_events,
    })


# ---------------------------------------------------------------------------
# Init & run
# ---------------------------------------------------------------------------

with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

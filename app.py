"""HexaStack - Async turn-based hex tile sorting game."""

import json
import math
import os
import random
import secrets
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)
app.config["DATABASE"] = os.environ.get(
    "HEXASTACK_DB", os.path.join(os.path.dirname(__file__), "hexastack.db")
)

# ---------------------------------------------------------------------------
# Board geometry helpers
# ---------------------------------------------------------------------------

BOARD_CONFIGS = {
    "small": {"radius": 1, "hexes": 7, "colours": 3, "target_score": 30},
    "medium": {"radius": 2, "hexes": 19, "colours": 4, "target_score": 60},
    "large": {"radius": 3, "hexes": 37, "colours": 5, "target_score": 100},
}

COLOUR_PALETTE = ["#E8A87C", "#85CDCA", "#D4A5A5", "#C3B1E1", "#A8D8B9"]
COLOUR_NAMES = ["peach", "teal", "rose", "lavender", "sage"]


def hex_grid_coords(radius):
    """Generate axial hex coordinates for a hex grid of given radius."""
    coords = []
    for q in range(-radius, radius + 1):
        for r in range(-radius, radius + 1):
            if abs(q + r) <= radius:
                coords.append((q, r))
    return coords


def hex_neighbours(q, r):
    """Return the 6 neighbours of a hex in axial coordinates."""
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
            status TEXT NOT NULL DEFAULT 'active',
            last_move TEXT,
            seed INTEGER NOT NULL,
            move_count INTEGER NOT NULL DEFAULT 0,
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
    """Generate 3 random tile stacks for a turn."""
    rng = random.Random(game_seed + move_count * 1000)
    stacks = []
    for _ in range(3):
        num_layers = rng.choice([1, 1, 2])  # bias toward single-layer
        layers = [rng.randint(0, num_colours - 1) for _ in range(num_layers)]
        stacks.append(layers)
    return stacks


def process_merges(board, coords_set):
    """Process auto-merging of adjacent same-colour tops. Returns updated board."""
    changed = True
    while changed:
        changed = False
        for coord_str, stack in list(board.items()):
            if not stack:
                continue
            q, r = map(int, coord_str.split(","))
            top_colour = stack[-1]
            for nq, nr in hex_neighbours(q, r):
                nkey = f"{nq},{nr}"
                if nkey not in board or not board[nkey]:
                    continue
                if nkey not in coords_set:
                    continue
                neighbour_stack = board[nkey]
                if neighbour_stack[-1] == top_colour:
                    # Transfer matching top layers from neighbour to this stack
                    transfer = []
                    while neighbour_stack and neighbour_stack[-1] == top_colour:
                        transfer.append(neighbour_stack.pop())
                    stack.extend(transfer)
                    board[coord_str] = stack
                    board[nkey] = neighbour_stack
                    changed = True
                    break  # restart scan after a merge
    return board


def process_clears(board):
    """Check for stacks with 10+ same-colour layers. Returns (board, points_scored)."""
    total_points = 0
    cleared = True
    while cleared:
        cleared = False
        for coord_str, stack in list(board.items()):
            if not stack:
                continue
            # Count consecutive same-colour from top
            top_colour = stack[-1]
            count = 0
            for layer in reversed(stack):
                if layer == top_colour:
                    count += 1
                else:
                    break
            if count >= 10:
                # Clear those layers
                board[coord_str] = stack[: len(stack) - count]
                total_points += count
                cleared = True
                break  # restart scan
    return board, total_points


def calculate_tidiness_bonus(board, num_colours):
    """End-of-game bonus for large same-colour groups."""
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
            bonus += count - 4  # 1 point per layer above 4
    return bonus


def check_game_over(board, coords_set, target_score, p1_score, p2_score):
    """Check if game should end."""
    if p1_score >= target_score or p2_score >= target_score:
        return True
    # Check if board is full
    for coord in coords_set:
        key = f"{coord[0]},{coord[1]}"
        if key not in board or not board[key]:
            return False
    return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/create", methods=["POST"])
def create_game():
    data = request.get_json() or {}
    board_size = data.get("board_size", "medium")
    if board_size not in BOARD_CONFIGS:
        return jsonify({"error": "Invalid board size"}), 400

    config = BOARD_CONFIGS[board_size]
    game_id = secrets.token_urlsafe(8)
    seed = random.randint(0, 2**31)
    coords = hex_grid_coords(config["radius"])

    # Empty board
    board = {}
    for q, r in coords:
        board[f"{q},{r}"] = []

    # Generate first set of offered stacks
    stacks = generate_stacks(seed, 0, config["colours"])
    now = datetime.now(timezone.utc).isoformat()

    db = get_db()
    db.execute(
        """
        INSERT INTO games (id, board_size, board_state, offered_stacks, seed, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, board_size, json.dumps(board), json.dumps(stacks), seed, now, now),
    )
    db.commit()

    return jsonify({"game_id": game_id, "url": f"/game/{game_id}"})


@app.route("/game/<game_id>")
def game_page(game_id):
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return render_template("not_found.html"), 404
    return render_template("game.html", game_id=game_id)


@app.route("/api/game/<game_id>")
def get_game(game_id):
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return jsonify({"error": "Game not found"}), 404

    config = BOARD_CONFIGS[game["board_size"]]
    colours = COLOUR_PALETTE[: config["colours"]]
    colour_names = COLOUR_NAMES[: config["colours"]]

    return jsonify(
        {
            "id": game["id"],
            "board_size": game["board_size"],
            "radius": config["radius"],
            "board": json.loads(game["board_state"]),
            "current_turn": game["current_turn"],
            "player1_score": game["player1_score"],
            "player2_score": game["player2_score"],
            "offered_stacks": json.loads(game["offered_stacks"]),
            "status": game["status"],
            "last_move": json.loads(game["last_move"]) if game["last_move"] else None,
            "move_count": game["move_count"],
            "target_score": config["target_score"],
            "colours": colours,
            "colour_names": colour_names,
            "coords": hex_grid_coords(config["radius"]),
        }
    )


@app.route("/api/game/<game_id>/move", methods=["POST"])
def make_move(game_id):
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return jsonify({"error": "Game not found"}), 404
    if game["status"] != "active":
        return jsonify({"error": "Game is over"}), 400

    data = request.get_json()
    stack_index = data.get("stack_index")
    target_hex = data.get("target_hex")  # "q,r" string

    if stack_index is None or target_hex is None:
        return jsonify({"error": "Missing stack_index or target_hex"}), 400

    board = json.loads(game["board_state"])
    offered = json.loads(game["offered_stacks"])
    config = BOARD_CONFIGS[game["board_size"]]
    coords = hex_grid_coords(config["radius"])
    coords_set = set(f"{q},{r}" for q, r in coords)

    if stack_index < 0 or stack_index >= len(offered):
        return jsonify({"error": "Invalid stack index"}), 400
    if target_hex not in coords_set:
        return jsonify({"error": "Invalid hex coordinate"}), 400
    if board.get(target_hex) and len(board[target_hex]) > 0:
        return jsonify({"error": "Hex is not empty"}), 400

    # Place the stack
    chosen_stack = offered[stack_index]
    board[target_hex] = chosen_stack

    # Process merges
    board = process_merges(board, coords_set)

    # Process clears
    board, points = process_clears(board)

    # Update scores
    current_player = game["current_turn"]
    p1_score = game["player1_score"]
    p2_score = game["player2_score"]

    if points > 0:
        if current_player == 1:
            p1_score += points
        else:
            p2_score += points

    move_count = game["move_count"] + 1
    next_turn = 2 if current_player == 1 else 1

    # Check game over
    status = "active"
    if check_game_over(board, coords_set, config["target_score"], p1_score, p2_score):
        status = "finished"
        # Add tidiness bonus
        tidiness = calculate_tidiness_bonus(board, config["colours"])
        p1_score += tidiness
        p2_score += tidiness

    # Generate next stacks
    next_stacks = generate_stacks(game["seed"], move_count, config["colours"])
    now = datetime.now(timezone.utc).isoformat()

    last_move = {"hex": target_hex, "stack": chosen_stack, "player": current_player}

    db.execute(
        """
        UPDATE games SET
            board_state = ?,
            current_turn = ?,
            player1_score = ?,
            player2_score = ?,
            offered_stacks = ?,
            status = ?,
            last_move = ?,
            move_count = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(board),
            next_turn,
            p1_score,
            p2_score,
            json.dumps(next_stacks),
            status,
            json.dumps(last_move),
            move_count,
            now,
            game_id,
        ),
    )
    db.commit()

    return jsonify(
        {
            "success": True,
            "points_scored": points,
            "board": board,
            "current_turn": next_turn,
            "player1_score": p1_score,
            "player2_score": p2_score,
            "offered_stacks": next_stacks,
            "status": status,
            "last_move": last_move,
        }
    )


# ---------------------------------------------------------------------------
# Init & run
# ---------------------------------------------------------------------------

with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

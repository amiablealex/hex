"""HexaStack - Async turn-based hex tile sorting game. Phase 4 (Production)."""

import copy
import json
import os
import random
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, make_response, render_template, request

app = Flask(__name__)
app.config["DATABASE"] = os.environ.get(
    "HEXASTACK_DB", os.path.join(os.path.dirname(__file__), "hexastack.db")
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GAME_EXPIRY_DAYS = 7
MAX_ACTIVE_GAMES_PER_TOKEN = 20
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_CREATES = 10  # max game creates per window

BOARD_CONFIGS = {
    "small": {"radius": 1, "hexes": 7, "colours": 3, "target_score": 60, "fertile": 1},
    "medium": {"radius": 2, "hexes": 19, "colours": 4, "target_score": 120, "fertile": 2},
    "large": {"radius": 3, "hexes": 37, "colours": 5, "target_score": 200, "fertile": 3},
}

COLOUR_PALETTE = ["#E07B5A", "#5AAFB8", "#C482D0", "#D4B84A", "#6B9FDE"]
COLOUR_NAMES = ["coral", "teal", "plum", "honey", "cornflower"]

# In-memory rate limiter (resets on restart, fine for low-traffic Pi)
_rate_limits = {}  # token -> [(timestamp, ...)]


# ---------------------------------------------------------------------------
# Board geometry
# ---------------------------------------------------------------------------

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
        g.db = sqlite3.connect(app.config["DATABASE"], timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(app.config["DATABASE"], timeout=10)
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
            fertile_hexes TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # Indices for cleanup and queries
    db.execute("CREATE INDEX IF NOT EXISTS idx_games_status ON games(status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_games_updated ON games(updated_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_games_p1token ON games(player1_token)")
    db.commit()
    db.close()


def cleanup_expired_games():
    """Delete games older than GAME_EXPIRY_DAYS. Run on startup."""
    try:
        db = sqlite3.connect(app.config["DATABASE"], timeout=10)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=GAME_EXPIRY_DAYS)).isoformat()
        result = db.execute("DELETE FROM games WHERE updated_at < ?", (cutoff,))
        deleted = result.rowcount
        db.commit()
        db.close()
        if deleted > 0:
            app.logger.info(f"Cleaned up {deleted} expired games")
    except Exception as e:
        app.logger.error(f"Cleanup failed: {e}")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def check_rate_limit(token, limit=RATE_LIMIT_MAX_CREATES, window=RATE_LIMIT_WINDOW):
    """Simple in-memory rate limiter. Returns True if allowed."""
    now = time.time()
    if token not in _rate_limits:
        _rate_limits[token] = []

    # Prune old entries
    _rate_limits[token] = [t for t in _rate_limits[token] if now - t < window]

    if len(_rate_limits[token]) >= limit:
        return False

    _rate_limits[token].append(now)
    return True


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

HEX_COORD_RE = re.compile(r"^-?\d+,-?\d+$")


def validate_hex_coord(coord_str):
    """Validate hex coordinate string format."""
    return bool(HEX_COORD_RE.match(coord_str))


def require_json(f):
    """Decorator to ensure request has JSON content type."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not request.is_json:
            return jsonify({"error": "Content-Type must be application/json"}), 415
        return f(*args, **kwargs)
    return decorated


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
    from_stack = board[from_key]
    transferred = from_stack[-count:]
    board[from_key] = from_stack[:-count]
    board[to_key] = board[to_key] + transferred
    return board


def _simulate_merges(board, coords_set):
    board = copy.deepcopy(board)
    total_merges = 0
    total_layers = 0
    changed = True
    while changed:
        changed = False
        pairs = _find_all_mergeable_pairs(board, coords_set)
        if not pairs:
            break
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
    merge_events = []
    changed = True
    while changed:
        changed = False
        pairs = _find_all_mergeable_pairs(board, coords_set)
        if not pairs:
            break

        if len(pairs) == 1:
            coord_a, coord_b, colour, count_a, count_b = pairs[0]
            if count_a <= count_b:
                from_key, to_key, count = coord_a, coord_b, count_a
            else:
                from_key, to_key, count = coord_b, coord_a, count_b
        else:
            best_score = -1
            best_merge = None
            for coord_a, coord_b, colour, count_a, count_b in pairs:
                sim_board = copy.deepcopy(board)
                sim_board = _execute_merge(sim_board, coord_a, coord_b, count_a)
                _, merges_ab, layers_ab = _simulate_merges(sim_board, coords_set)
                score_ab = merges_ab * 100 + layers_ab + count_a

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
        colour_val = board[to_key][-1]
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
        is_secure = request.headers.get("X-Forwarded-Proto") == "https"
        resp.set_cookie(
            "hexastack_player",
            secrets.token_urlsafe(16),
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="Lax",
            secure=is_secure,
        )
    return resp


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    resp = make_response(render_template("index.html"))
    return ensure_token_cookie(resp)


@app.route("/health")
def health():
    """Health check endpoint for monitoring."""
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/api/create", methods=["POST"])
@require_json
def create_game():
    token = get_player_token()

    # Rate limit game creation
    if token and not check_rate_limit(token):
        return jsonify({"error": "Too many games created. Try again later."}), 429

    data = request.get_json() or {}
    board_size = data.get("board_size", "medium")
    game_mode = data.get("game_mode", "link")

    if board_size not in BOARD_CONFIGS:
        return jsonify({"error": "Invalid board size"}), 400
    if game_mode not in ("local", "link"):
        return jsonify({"error": "Invalid game mode"}), 400

    # Cap active games per player
    if token:
        db = get_db()
        active_count = db.execute(
            "SELECT COUNT(*) FROM games WHERE player1_token = ? AND status = 'active'",
            (token,),
        ).fetchone()[0]
        if active_count >= MAX_ACTIVE_GAMES_PER_TOKEN:
            return jsonify({"error": "Too many active games. Finish or abandon some first."}), 429

    config = BOARD_CONFIGS[board_size]
    game_id = secrets.token_urlsafe(8)
    seed = random.randint(0, 2**31)
    coords = hex_grid_coords(config["radius"])

    board = {}
    for q, r in coords:
        board[f"{q},{r}"] = []

    # Select fertile hexes (random, excluding centre on small boards for fairness)
    rng = random.Random(seed)
    coord_keys = [f"{q},{r}" for q, r in coords]
    fertile_hexes = rng.sample(coord_keys, min(config["fertile"], len(coord_keys)))

    stacks = generate_stacks(seed, 0, config["colours"])
    now = datetime.now(timezone.utc).isoformat()

    if not token:
        token = secrets.token_urlsafe(16)

    db = get_db()
    db.execute(
        """
        INSERT INTO games (id, board_size, board_state, offered_stacks, seed,
                          game_mode, player1_token, fertile_hexes, placed_this_turn,
                          created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            game_id, board_size, json.dumps(board), json.dumps(stacks), seed,
            game_mode,
            token if game_mode == "link" else None,
            json.dumps(fertile_hexes),
            now, now,
        ),
    )
    db.commit()

    resp = make_response(jsonify({"game_id": game_id, "url": f"/game/{game_id}"}))
    if not request.cookies.get("hexastack_player"):
        is_secure = request.headers.get("X-Forwarded-Proto") == "https"
        resp.set_cookie(
            "hexastack_player", token,
            max_age=60 * 60 * 24 * 365, httponly=True, samesite="Lax",
            secure=is_secure,
        )
    return resp


@app.route("/game/<game_id>")
def game_page(game_id):
    # Validate game_id format
    if len(game_id) > 20 or not re.match(r'^[A-Za-z0-9_-]+$', game_id):
        return render_template("not_found.html"), 404

    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return render_template("not_found.html"), 404
    resp = make_response(render_template("game.html", game_id=game_id))
    return ensure_token_cookie(resp)


@app.route("/api/game/<game_id>")
def get_game(game_id):
    if len(game_id) > 20:
        return jsonify({"error": "Game not found"}), 404

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
            elif game["player2_token"] is None and game["status"] == "active":
                # Race condition guard: use UPDATE WHERE to atomically claim P2
                result = db.execute(
                    "UPDATE games SET player2_token = ? WHERE id = ? AND player2_token IS NULL",
                    (token, game_id),
                )
                db.commit()
                if result.rowcount > 0:
                    player_number = 2

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
        "fertile_hexes": json.loads(game["fertile_hexes"]) if game["fertile_hexes"] else [],
    })


@app.route("/api/game/<game_id>/move", methods=["POST"])
@require_json
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

    # Input validation
    if stack_index is None or target_hex is None:
        return jsonify({"error": "Missing stack_index or target_hex"}), 400
    if not isinstance(stack_index, int) or not isinstance(target_hex, str):
        return jsonify({"error": "Invalid input types"}), 400
    if not validate_hex_coord(target_hex):
        return jsonify({"error": "Invalid hex coordinate format"}), 400

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

    chosen_stack = offered[stack_index]
    board[target_hex] = list(chosen_stack)
    offered[stack_index] = None

    board_after_place = copy.deepcopy(board)
    fertile_hexes = json.loads(game["fertile_hexes"]) if game["fertile_hexes"] else []
    fertile_set = set(fertile_hexes)

    # Process merges → clears, with cascading multiplier
    # Each successive clear round gets a higher multiplier: 1x, 2x, 3x, ...
    board, merge_events = process_merges(board, coords_set)
    board_after_merges = copy.deepcopy(board)

    total_points = 0
    all_clear_events = []
    cascade_level = 0

    # First round of clears
    board, round_pts, round_clears = process_clears(board)
    while round_clears:
        cascade_level += 1
        multiplier = cascade_level  # 1x first, 2x second, 3x third...

        for evt in round_clears:
            base_pts = evt["count"]
            is_fertile = evt["hex"] in fertile_set
            fertile_mult = 2 if is_fertile else 1
            scored = base_pts * fertile_mult * multiplier
            evt["points"] = scored
            evt["multiplier"] = multiplier
            evt["fertile"] = is_fertile
            total_points += scored

        all_clear_events.extend(round_clears)

        # After clears, new merges may be possible → which may cause more clears
        board, extra_merges = process_merges(board, coords_set)
        merge_events.extend(extra_merges)
        board, round_pts, round_clears = process_clears(board)

    current_player = game["current_turn"]
    p1_score = game["player1_score"]
    p2_score = game["player2_score"]

    if total_points > 0:
        if current_player == 1:
            p1_score += total_points
        else:
            p2_score += total_points

    placed_this_turn = game["placed_this_turn"] + 1

    remaining_stacks = [s for s in offered if s is not None]
    has_empty_hex = any(not board.get(f"{q},{r}") for q, r in coords)
    turn_complete = (placed_this_turn >= 3) or (len(remaining_stacks) == 0) or (not has_empty_hex)

    if turn_complete:
        next_turn = 2 if current_player == 1 else 1
        move_count = game["move_count"] + 1
        next_stacks = generate_stacks(game["seed"], move_count, config["colours"])
        placed_count = 0
    else:
        next_turn = current_player
        move_count = game["move_count"]
        next_stacks = offered
        placed_count = placed_this_turn

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
        "clear_events": all_clear_events,
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
        "points_scored": total_points,
        "board": board,
        "board_after_place": board_after_place,
        "board_after_merges": board_after_merges,
        "current_turn": next_turn,
        "player1_score": p1_score,
        "player2_score": p2_score,
        "offered_stacks": next_stacks,
        "placed_this_turn": placed_count,
        "turn_complete": turn_complete,
        "status": status,
        "last_move": last_move,
        "merge_events": merge_events,
        "clear_events": all_clear_events,
    })


@app.route("/api/game/<game_id>/abandon", methods=["POST"])
def abandon_game(game_id):
    """Mark a game as abandoned. Either player can abandon."""
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return jsonify({"error": "Game not found"}), 404
    if game["status"] != "active":
        return jsonify({"error": "Game is already over"}), 400

    # In link mode, only participants can abandon
    if game["game_mode"] == "link":
        token = get_player_token()
        if token != game["player1_token"] and token != game["player2_token"]:
            return jsonify({"error": "Not a participant"}), 403

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE games SET status = 'abandoned', updated_at = ? WHERE id = ?",
        (now, game_id),
    )
    db.commit()
    return jsonify({"success": True, "status": "abandoned"})


# ---------------------------------------------------------------------------
# Init & run
# ---------------------------------------------------------------------------

with app.app_context():
    init_db()
    cleanup_expired_games()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

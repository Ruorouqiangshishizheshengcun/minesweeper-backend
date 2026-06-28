import random
import string
import time
from typing import Dict, Set

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import socketio

app = FastAPI()

# ---- CORS 配置 ----
# 生产环境使用精确白名单，本地开发兼容 localhost
ALLOWED_ORIGINS = [
    "https://www.minesweeper-game.site",
    "https://minesweeper-game.vercel.app",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins=ALLOWED_ORIGINS,
)
socket_app = socketio.ASGIApp(sio, app)

class Room:
    def __init__(self, room_id, host_token, board, rows=9, cols=9, mines=10):
        self.room_id = room_id
        self.host_token = host_token
        self.guest_token = None
        self.board = board
        self.rows = rows
        self.cols = cols
        self.mines = mines
        self.game_started = False
        self.players: Set[str] = set()
        self.current_turn = None
        self.rematch_votes = set()
        self.first_click_done = False
        # 每名玩家独立统计已翻开的安全格子
        self.host_revealed: Set[tuple] = set()
        self.guest_revealed: Set[tuple] = set()

rooms: Dict[str, Room] = {}
sid_info: Dict[str, dict] = {}

def generate_board(rows=9, cols=9, mines=10):
    """动态尺寸棋盘生成，rows×cols 非必须正方形"""
    board = [[0]*cols for _ in range(rows)]
    positions = random.sample([(r, c) for r in range(rows) for c in range(cols)], mines)
    for r, c in positions:
        board[r][c] = -1
    recompute_adjacent_mines(board)
    return board

def recompute_adjacent_mines(board):
    rows = len(board)
    cols = len(board[0])
    for r in range(rows):
        for c in range(cols):
            if board[r][c] == -1:
                continue
            cnt = 0
            for i in range(max(0, r-1), min(rows, r+2)):
                for j in range(max(0, c-1), min(cols, c+2)):
                    if board[i][j] == -1:
                        cnt += 1
            board[r][c] = cnt

def safe_view(board):
    return [[-2 if cell == -1 else cell for cell in row] for row in board]

def random_room_id():
    return ''.join(random.choices(string.ascii_uppercase, k=6))

def random_token():
    return '%08x%08x' % (random.getrandbits(32), random.getrandbits(32))

def reveal_cell(board, row, col, revealed_set):
    rows = len(board)
    cols = len(board[0])
    new_revealed = []
    queue = [(row, col)]
    visited = set()
    while queue:
        r, c = queue.pop(0)
        if (r, c) in visited or (r, c) in revealed_set:
            continue
        visited.add((r, c))
        cell = board[r][c]
        new_revealed.append((r, c, cell))
        revealed_set.add((r, c))
        if cell == 0:
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < rows and 0 <= nc < cols:
                        queue.append((nr, nc))
    return new_revealed

@app.post("/create_room")
async def create_room(payload: dict = None):
    # 解析参数
    if payload is None:
        payload = {}
    rows = payload.get("rows", 9)
    cols = payload.get("cols", 9)
    mines = payload.get("mineCount", 10)

    # 严格校验
    if not (5 <= rows <= 30):
        raise HTTPException(400, f"行数必须在 5~30 之间，当前值: {rows}")
    if not (5 <= cols <= 50):
        raise HTTPException(400, f"列数必须在 5~50 之间，当前值: {cols}")
    max_mines = rows * cols - 9
    if not (1 <= mines <= max_mines):
        raise HTTPException(400, f"雷数必须在 1~{max_mines} 之间（保证首次点击安全），当前值: {mines}")

    room_id = random_room_id()
    board = generate_board(rows=rows, cols=cols, mines=mines)
    token = random_token()
    rooms[room_id] = Room(
        room_id=room_id,
        host_token=token,
        board=board,
        rows=rows,
        cols=cols,
        mines=mines,
    )
    print(f"[CREATE] room={room_id}, host_token={token}, rows={rows}, cols={cols}, mines={mines}")
    return {
        "room_id": room_id,
        "board": safe_view(board),
        "token": token,
        "rows": rows,
        "cols": cols,
        "mines": mines,
    }

@app.post("/join_room")
async def join_room(payload: dict):
    room_id = payload.get("room_id")
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(404, "房间不存在")
    if room.guest_token is not None:
        raise HTTPException(400, "房间已满")
    token = random_token()
    room.guest_token = token
    print(f"[JOIN] room={room_id}, guest_token={token}")
    return {
        "board": safe_view(room.board),
        "token": token,
        "rows": room.rows,
        "cols": room.cols,
        "mines": room.mines,
    }

@sio.event
async def connect(sid, environ):
    print(f"[WS] 新连接 {sid}")

@sio.event
async def join_room_event(sid, data):
    print(f"[JOIN_EVENT] sid={sid}, data={data}")
    try:
        room_id = data.get("room_id")
        token = data.get("token")

        room = rooms.get(room_id)
        if not room:
            await sio.emit("error", {"msg": "房间不存在"}, to=sid)
            print(f"[JOIN_FAIL]房间{room_id}不存在")
            return

        if token != room.host_token and token != room.guest_token:
            await sio.emit("error", {"msg": "凭据无效"}, to=sid)
            print(f"[JOIN_FAIL] sid={sid}, token={token}, host={room.host_token}, guest={room.guest_token}")
            return

        sid_info[sid] = {"room_id": room_id, "token": token}
        await sio.enter_room(sid, room_id)
        room.players.add(sid)

        await sio.emit("player_joined", {
            "message": f"玩家加入（{len(room.players)}/2）"
        }, room=room_id)

        print(f"[JOIN_ROOM] sid={sid}, room={room_id}, players={room.players}")

        if (room.host_token and room.guest_token and
            len(room.players) >= 2 and not room.game_started):
            room.game_started = True
            host_sid = next(sid for sid in room.players if sid_info[sid]["token"] == room.host_token)
            room.current_turn = host_sid
            await sio.emit("game_started", {
                "message": "对手已加入，游戏开始！",
                "current_turn": room.current_turn,
                "host_sid": host_sid,
                "start_time": int(time.time() * 1000),
                "rows": room.rows,
                "cols": room.cols,
                "mines": room.mines,
            }, room=room_id)
            await sio.emit("turn_changed", {"current_turn": room.current_turn}, room=room_id)
            print(f"[GAME_START] room={room_id}, current_turn={room.current_turn}")
        else:
            await sio.emit("waiting_for_opponent", {"message": "等待对手加入..."}, to=sid)

    except Exception as e:
        print(f"[JOIN_ERROR] sid={sid}, error={e}")
        await sio.emit("error", {"msg": "服务器内部错误"}, to=sid)

@sio.event
async def cell_click(sid, data):
    info = sid_info.get(sid)
    if not info: return
    room_id = info["room_id"]
    room = rooms.get(room_id)
    if not room or not room.game_started: return

    if sid != room.current_turn:
        await sio.emit("error", {"msg": "还没轮到你操作"}, to=sid)
        return

    # 确定当前玩家身份（host 或 guest）
    player_token = sid_info[sid]["token"]
    is_host = player_token == room.host_token
    revealed_set = room.host_revealed if is_host else room.guest_revealed

    row, col = data["row"], data["col"]
    board = room.board

    if not room.first_click_done:
        room.first_click_done = True
        if board[row][col] == -1:
            non_mines = [(r, c) for r in range(room.rows) for c in range(room.cols) if board[r][c] != -1]
            if non_mines:
                nr, nc = random.choice(non_mines)
                board[nr][nc] = -1
                board[row][col] = 0
                recompute_adjacent_mines(board)

    cell = board[row][col]

    if cell == -1:
        # 踩雷：当前玩家输，对手赢
        room.game_started = False
        room.current_turn = None
        opponent_sid = next(p for p in room.players if p != sid)
        await sio.emit("game_over", {
            "type": "mine",
            "loser_sid": sid,
            "winner_sid": opponent_sid,
            "row": row,
            "col": col,
        }, room=room_id)
    else:
        new_revealed = reveal_cell(board, row, col, revealed_set)
        for r, c, val in new_revealed:
            revealed_set.add((r, c))
            await sio.emit("cell_revealed", {
                "row": r,
                "col": c,
                "value": val,
                "by": sid
            }, room=room_id)

        total_cells = room.rows * room.cols
        # 获胜条件：该玩家翻开的格子 + 地雷总数 = 棋盘总格子（所有安全格全部翻开）
        if len(revealed_set) + room.mines == total_cells:
            room.game_started = False
            room.current_turn = None
            opponent_sid = next(p for p in room.players if p != sid)
            await sio.emit("game_over", {
                "type": "win",
                "winner_sid": sid,
                "loser_sid": opponent_sid,
                "row": row,
                "col": col,
            }, room=room_id)
        elif room.game_started:
            room.current_turn = next(p for p in room.players if p != sid)
            await sio.emit("turn_changed", {"current_turn": room.current_turn}, room=room_id)

@sio.event
async def request_rematch(sid, data):
    info = sid_info.get(sid)
    if not info:
        print(f"[REMATCH_FAIL] sid={sid} 不在 sid_info 中")
        await sio.emit("error", {"msg": "连接信息丢失，请刷新页面"}, to=sid)
        return
    room = rooms.get(info["room_id"])
    if not room:
        print(f"[REMATCH_FAIL] 房间 {info.get('room_id')} 不存在")
        await sio.emit("error", {"msg": "房间不存在"}, to=sid)
        return
    if room.game_started:
        await sio.emit("error", {"msg": "游戏还未结束"}, to=sid)
        print(f"[REMATCH_FAIL] room={room.room_id} 游戏进行中，拒绝 rematch bid={sid}")
        return

    room.rematch_votes.add(sid)

    if len(room.rematch_votes) >= 2:
        # 重置房间，沿用当前难度配置
        room.board = generate_board(rows=room.rows, cols=room.cols, mines=room.mines)
        room.host_revealed.clear()
        room.guest_revealed.clear()
        room.first_click_done = False
        room.game_started = True
        room.rematch_votes.clear()

        # 重新设定先手（host 先手，若 host 已断开则任意玩家）
        host_sid = None
        for s in room.players:
            player_info = sid_info.get(s)
            if player_info and player_info["token"] == room.host_token:
                host_sid = s
                break
        if host_sid is None:
            # host 已断开，选第一个在线的玩家
            print(f"[REMATCH_WARN] host 断开，选首个玩家为先手")
            host_sid = next(iter(room.players), None)
        if host_sid is None:
            print(f"[REMATCH_FAIL] room={room.room_id} 无有效玩家")
            return
        room.current_turn = host_sid

        safe_board = safe_view(room.board)
        await sio.emit("game_restarted", {
            "board": safe_board,
            "current_turn": room.current_turn,
            "host_sid": host_sid,
            "rows": room.rows,
            "cols": room.cols,
            "mines": room.mines,
        }, room=room.room_id)
        await sio.emit("turn_changed", {"current_turn": room.current_turn}, room=room.room_id)
        print(f"[REMATCH] room={room.room_id}, game restarted")
    else:
        await sio.emit("rematch_waiting", {
            "message": f"已投票 {len(room.rematch_votes)}/2",
            "votes": len(room.rematch_votes),
            "voter_sid": sid,
        }, room=room.room_id)
        print(f"[REMATCH_VOTE] sid={sid}, votes={len(room.rematch_votes)}/2")

@sio.event
async def cancel_rematch(sid, data):
    """取消再来一局的投票"""
    info = sid_info.get(sid)
    if not info:
        await sio.emit("error", {"msg": "连接信息丢失，请刷新页面"}, to=sid)
        return
    room = rooms.get(info["room_id"])
    if not room:
        await sio.emit("error", {"msg": "房间不存在"}, to=sid)
        return

    room.rematch_votes.discard(sid)

    await sio.emit("rematch_votes_update", {
        "votes": len(room.rematch_votes),
    }, room=room.room_id)
    print(f"[REMATCH_CANCEL] sid={sid}, votes={len(room.rematch_votes)}/2")

@sio.event
async def disconnect(sid):
    print(f"[WS] 断开 {sid}")
    info = sid_info.pop(sid, None)
    if info:
        room_id = info["room_id"]
        room = rooms.get(room_id)
        if room:
            room.players.discard(sid)
            # 清除该玩家的投票，并通知房间
            had_votes = sid in room.rematch_votes
            room.rematch_votes.discard(sid)
            # 通知对手：对方断开 + 投票信息
            await sio.emit("opponent_disconnected", {
                "votes_cleared": had_votes,
                "remaining_votes": len(room.rematch_votes),
            }, room=room_id)
            # 同时清除游戏状态中的投票计数提示
            await sio.emit("rematch_votes_update", {
                "votes": len(room.rematch_votes),
            }, room=room.room_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(socket_app, host="0.0.0.0", port=8000)
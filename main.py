import random
import string
import time
import asyncio
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
        self.revealed_cells: Set[tuple] = set()
        self.leaver_token = None  # 主动退出玩家的 token，防止 disconnect 重复处理
        self.player_reveal_count: Dict[str, int] = {}  # sid → 该玩家累计翻开的安全格数

rooms: Dict[str, Room] = {}
sid_info: Dict[str, dict] = {}
# token → sid 反向映射，用于重连时快速定位旧sid
token_to_sid: Dict[str, str] = {}
# 待定断线任务：{old_sid: asyncio.Task}，5秒内重连则取消
pending_disconnects: Dict[str, asyncio.Task] = {}
DISCONNECT_GRACE_SECONDS = 5  # 断线缓冲窗口（秒）

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
        if cell == -1:
            # 跳过地雷：地雷不应被 BFS 翻开，也不应计入 revealed_cells
            continue
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

@app.get("/room_state/{room_id}")
async def get_room_state(room_id: str):
    """获取房间完整状态，用于前端刷新后恢复对局"""
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(404, "房间不存在")
    
    # 收集已翻开的格子列表
    revealed = [[r, c] for r, c in room.revealed_cells]
    
    return {
        "room_id": room.room_id,
        "board": safe_view(room.board),
        "rows": room.rows,
        "cols": room.cols,
        "mines": room.mines,
        "game_started": room.game_started,
        "current_turn": room.current_turn,
        "player_count": len(room.players),
        "revealed_cells": revealed,
    }

@app.post("/join_room")
async def join_room(payload: dict):
    room_id = payload.get("room_id")
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(404, "房间不存在")
    # 检查可用槽位（支持玩家退出后重新加入）
    if room.host_token is None:
        role = 'host'
    elif room.guest_token is None:
        role = 'guest'
    else:
        raise HTTPException(400, "房间已满")
    token = random_token()
    if role == 'host':
        room.host_token = token
    else:
        room.guest_token = token
    print(f"[JOIN] room={room_id}, {role}_token={token}")
    return {
        "board": safe_view(room.board),
        "token": token,
        "rows": room.rows,
        "cols": room.cols,
        "mines": room.mines,
        "role": role,
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

        # ---- 重连检测：同一 token 已有旧 sid ----
        old_sid = token_to_sid.get(token)
        is_reconnect = old_sid is not None and old_sid != sid

        if is_reconnect:
            print(f"[RECONNECT] token={token[:8]}..., old_sid={old_sid} → new_sid={sid}")
            # 取消待定断线任务
            cancel_task = pending_disconnects.pop(old_sid, None)
            if cancel_task:
                cancel_task.cancel()
                print(f"[RECONNECT] 已取消断线定时器 old_sid={old_sid}")
            # 替换旧 sid
            sid_info.pop(old_sid, None)
            room.players.discard(old_sid)
            try:
                await sio.leave_room(old_sid, room_id)
            except Exception:
                pass

        # ---- 录入新 sid ----
        sid_info[sid] = {"room_id": room_id, "token": token}
        token_to_sid[token] = sid
        await sio.enter_room(sid, room_id)
        room.players.add(sid)

        if is_reconnect:
            # 通知房间内其他玩家：该玩家已重连上线
            role = "host" if token == room.host_token else "guest"
            await sio.emit("player_reonline", {
                "sid": sid,
                "token": token,
                "role": role,
                "message": f"{'房主' if role == 'host' else '对手'}已重新上线",
            }, room=room_id)
            # 如果当前回合属于重连玩家，将回合归属 remap 到新 sid
            if room.current_turn == old_sid:
                room.current_turn = sid
            # 全房间同步最新回合归属（修复双方全部显示「对手回合」的bug）
            await sio.emit("turn_changed", {"current_turn": room.current_turn}, room=room_id)
            print(f"[RECONNECT] 已推送 player_reonline + turn_changed, room={room_id}, current_turn={room.current_turn}")

        await sio.emit("player_joined", {
            "message": f"玩家加入（{len(room.players)}/2）"
        }, room=room_id)

        print(f"[JOIN_ROOM] sid={sid}, room={room_id}, players={room.players}")

        if (room.host_token and room.guest_token and
            len(room.players) >= 2 and not room.game_started):
            room.game_started = True
            room.player_reveal_count = {}  # 新一局重置双方翻开计数
            host_sid = next(s for s in room.players if sid_info[s]["token"] == room.host_token)
            room.current_turn = host_sid
            await sio.emit("game_started", {
                "message": "对手已加入，游戏开始！",
                "board": safe_view(room.board),
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
        # 踩雷：当前操作玩家判负，对手获胜
        room.game_started = False
        room.current_turn = None
        other_sid = next((p for p in room.players if p != sid), None)
        print(f"[GAME_OVER] room={room_id}, reason=mine_hit, winner={other_sid}, loser={sid}")
        await sio.emit("game_over", {
            "winner": other_sid,
            "loser": sid,
            "reason": "mine_hit",
            "total_safe": room.rows * room.cols - room.mines,
            "winner_progress": room.player_reveal_count.get(other_sid, 0),
            "loser_progress": room.player_reveal_count.get(sid, 0),
            "row": row,
            "col": col,
        }, room=room_id)
    else:
        new_revealed = reveal_cell(board, row, col, room.revealed_cells)
        for r, c, val in new_revealed:
            await sio.emit("cell_revealed", {
                "row": r,
                "col": c,
                "value": val,
                "by": sid
            }, room=room_id)

        # 累计当前玩家的翻开格数
        room.player_reveal_count[sid] = room.player_reveal_count.get(sid, 0) + len(new_revealed)

        total_non_mine = room.rows * room.cols - room.mines
        revealed_count = len(room.revealed_cells)
        print(f"[CELL_CLICK] room={room_id}, sid={sid}, revealed={revealed_count}/{total_non_mine}, player_counts={dict(room.player_reveal_count)}")
        if revealed_count >= total_non_mine:
            # 翻完最后一个安全格：当前回合玩家直接获胜
            room.game_started = False
            room.current_turn = None
            other_sid = next((p for p in room.players if p != sid), None)
            print(f"[GAME_OVER] room={room_id}, reason=last_cell_opened, winner={sid}, loser={other_sid}")
            await sio.emit("game_over", {
                "winner": sid,
                "loser": other_sid,
                "reason": "last_cell_opened",
                "total_safe": total_non_mine,
                "winner_progress": room.player_reveal_count.get(sid, 0),
                "loser_progress": room.player_reveal_count.get(other_sid, 0),
            }, room=room_id)
        elif room.game_started:
            room.current_turn = next(p for p in room.players if p != sid)
            await sio.emit("turn_changed", {"current_turn": room.current_turn}, room=room_id)

@sio.event
async def leave_room(sid, data):
    """玩家主动点击「返回大厅」退出房间"""
    print(f"[LEAVE_ROOM] sid={sid}, data={data}")
    room_id = data.get("room_id")
    token = data.get("token")

    room = rooms.get(room_id)
    if not room:
        await sio.emit("error", {"msg": "房间不存在"}, to=sid)
        return

    # 验证 token
    if token != room.host_token and token != room.guest_token:
        await sio.emit("error", {"msg": "凭据无效"}, to=sid)
        return

    # 标记该玩家已主动离开（防止后续 disconnect 重复处理）
    room.leaver_token = token

    # 取消该 sid 的待定断线任务
    cancel_task = pending_disconnects.pop(sid, None)
    if cancel_task:
        cancel_task.cancel()
        print(f"[LEAVE_ROOM] 已取消断线定时器 sid={sid}")

    # 清理该玩家数据
    sid_info.pop(sid, None)
    token_to_sid.pop(token, None)
    room.players.discard(sid)
    room.rematch_votes.discard(sid)

    # 离开 socket 房间
    try:
        await sio.leave_room(sid, room_id)
    except Exception:
        pass

    role = "host" if token == room.host_token else "guest"
    print(f"[LEAVE_ROOM] room={room_id}, {role} 主动离开, 剩余玩家={room.players}")

    # 清除离开玩家的 token 槽位，允许后续重新加入
    if token == room.host_token:
        room.host_token = None
    else:
        room.guest_token = None

    # 重置游戏状态（保留棋盘不重新生成，下次加入直接复用）
    room.game_started = False
    room.first_click_done = False
    room.revealed_cells.clear()
    room.rematch_votes.clear()
    room.current_turn = None
    room.player_reveal_count.clear()

    if len(room.players) == 0:
        # 房间已空，销毁
        rooms.pop(room_id, None)
        print(f"[LEAVE_ROOM] room={room_id} 已销毁（无剩余玩家）")
    else:
        # 通知剩余玩家：对手已主动下线
        await sio.emit("opponent_offline", {
            "leaver_role": role,
            "message": f"{'房主' if role == 'host' else '对手'}已退出房间",
            "permanent": True,
        }, room=room_id)
        await sio.emit("rematch_votes_update", {
            "votes": len(room.rematch_votes),
        }, room=room_id)
        print(f"[LEAVE_ROOM] 已广播 opponent_offline, room={room_id}")

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
        room.revealed_cells.clear()
        room.first_click_done = False
        room.game_started = True
        room.rematch_votes.clear()
        room.player_reveal_count.clear()

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
    info = sid_info.get(sid)
    if not info:
        return

    room_id = info["room_id"]
    token = info.get("token")
    if not token:
        return

    print(f"[DISCONNECT_PENDING] sid={sid}, room={room_id}, token={token[:8]}..., 等待{DISCONNECT_GRACE_SECONDS}s窗口期")
    # 启动延迟断线任务
    task = asyncio.create_task(
        _delayed_disconnect(sid, room_id, token)
    )
    pending_disconnects[sid] = task


async def _delayed_disconnect(sid: str, room_id: str, token: str):
    """延迟 DISCONNECT_GRACE_SECONDS 秒后执行真正断线"""
    try:
        await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
    except asyncio.CancelledError:
        print(f"[DISCONNECT_CANCELLED] sid={sid} 在窗口期内重连，取消断线")
        return

    # 超时未重连 → 真正断开
    await _do_real_disconnect(room_id, token, sid)


async def _do_real_disconnect(room_id: str, token: str, old_sid: str):
    """延迟执行真正的断线逻辑"""
    room = rooms.get(room_id)
    if not room:
        return

    # 如果该 token 已通过 leave_room 主动退出，跳过（避免重复通知）
    if room.leaver_token == token:
        print(f"[DISCONNECT_SKIP] room={room_id}, token 已主动离开，跳过断线处理")
        return

    # 清除该 sid 的遗留数据
    sid_info.pop(old_sid, None)
    token_to_sid.pop(token, None)
    pending_disconnects.pop(old_sid, None)

    room.players.discard(old_sid)
    # 清除该玩家的投票
    had_votes = old_sid in room.rematch_votes
    room.rematch_votes.discard(old_sid)

    # 通知房间：对方已真正断开
    await sio.emit("opponent_disconnected", {
        "votes_cleared": had_votes,
        "remaining_votes": len(room.rematch_votes),
        "permanent": True,
    }, room=room_id)
    await sio.emit("rematch_votes_update", {
        "votes": len(room.rematch_votes),
    }, room=room_id)
    print(f"[DISCONNECT_FINAL] room={room_id}, old_sid={old_sid}, permanent disconnect")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(socket_app, host="0.0.0.0", port=8000)
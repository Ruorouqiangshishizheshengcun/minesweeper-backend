import random
import string
import time
from typing import Dict, List, Tuple, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import socketio

# -------------------- 常量 --------------------
ROWS = 9
COLS = 9
MINES = 10
BOARD_DIMENSION = (ROWS, COLS)

# -------------------- 数据模型 --------------------
class Room:
    def __init__(self, room_id: str, board: List[List[int]], host_token: str):
        self.room_id = room_id
        self.board = board  # 完整棋盘，-1 为雷
        self.host_token = host_token
        self.guest_token: Optional[str] = None
        self.created_at = time.time()
        self.game_started = False

class CreateRoomResponse(BaseModel):
    room_id: str
    board: List[List[int]]  # 安全视图（仅显示已知数字，雷格位置记为 -2 或 9 等占位）
    token: str

class JoinRoomRequest(BaseModel):
    room_id: str

class JoinRoomResponse(BaseModel):
    board: List[List[int]]
    token: str

# -------------------- 棋盘生成逻辑 --------------------
def generate_board(safe_row: int = None, safe_col: int = None) -> List[List[int]]:
    """生成棋盘，若提供安全坐标则避开该位置及周围3x3"""
    board = [[0 for _ in range(COLS)] for _ in range(ROWS)]
    # 放置地雷
    mines_placed = 0
    while mines_placed < MINES:
        r = random.randint(0, ROWS - 1)
        c = random.randint(0, COLS - 1)
        if board[r][c] == -1:
            continue
        if safe_row is not None and safe_col is not None:
            if abs(r - safe_row) <= 1 and abs(c - safe_col) <= 1:
                continue
        board[r][c] = -1
        mines_placed += 1
    # 计算相邻雷数
    for r in range(ROWS):
        for c in range(COLS):
            if board[r][c] == -1:
                continue
            count = 0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < ROWS and 0 <= nc < COLS and board[nr][nc] == -1:
                        count += 1
            board[r][c] = count
    return board

def board_safe_view(board: List[List[int]]) -> List[List[int]]:
    """返回仅包含已知信息的棋盘视图：雷位置用 -2 表示（前端可识别为未揭开）"""
    safe = []
    for row in board:
        safe_row = []
        for cell in row:
            if cell == -1:
                safe_row.append(-2)  # 隐藏雷，用-2表示未揭开且无数字
            else:
                safe_row.append(cell)
        safe.append(safe_row)
    return safe

# -------------------- 内存存储 --------------------
rooms: Dict[str, Room] = {}

def generate_room_id(length: int = 6) -> str:
    """生成随机房间码（大写字母+数字）"""
    chars = string.ascii_uppercase + string.digits
    while True:
        rid = ''.join(random.choices(chars, k=length))
        if rid not in rooms:
            return rid

def generate_token() -> str:
    """简单令牌，实际项目可用 uuid"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=32))

# -------------------- FastAPI 应用 --------------------
app = FastAPI(title="MineSweeper Battle Backend")

# CORS：允许所有来源（生产环境请限制为前端域名）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- Socket.IO 服务器 --------------------
# 使用 ASGI 模式挂载
sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*'
)
socket_app = socketio.ASGIApp(sio, app)

# 目前仅记录连接，备用
@sio.event
async def connect(sid, environ):
    print(f"Socket.IO 连接: {sid}")

@sio.event
async def disconnect(sid):
    print(f"Socket.IO 断开: {sid}")

# -------------------- REST API --------------------
@app.post("/create_room", response_model=CreateRoomResponse)
async def create_room():
    """创建房间，生成棋盘和房主令牌"""
    board = generate_board()
    room_id = generate_room_id()
    host_token = generate_token()
    room = Room(room_id=room_id, board=board, host_token=host_token)
    rooms[room_id] = room
    safe_board = board_safe_view(board)
    return CreateRoomResponse(
        room_id=room_id,
        board=safe_board,
        token=host_token
    )

@app.post("/join_room", response_model=JoinRoomResponse)
async def join_room(req: JoinRoomRequest):
    """加入一个已存在的房间"""
    room = rooms.get(req.room_id)
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    if room.guest_token is not None:
        raise HTTPException(status_code=400, detail="房间已满")
    # 生成访客令牌
    guest_token = generate_token()
    room.guest_token = guest_token
    safe_board = board_safe_view(room.board)
    return JoinRoomResponse(
        board=safe_board,
        token=guest_token
    )

# 可选：轻量状态查看（调试用）
@app.get("/room/{room_id}")
async def get_room_info(room_id: str):
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(status_code=404)
    return {
        "room_id": room.room_id,
        "has_guest": room.guest_token is not None,
        "game_started": room.game_started
    }

# -------------------- 启动入口 --------------------
if __name__ == "__main__":
    import uvicorn
    # 启动 FastAPI + Socket.IO 联合应用
    uvicorn.run(socket_app, host="0.0.0.0", port=8000)
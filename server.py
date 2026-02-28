import socket
import threading
import time
import json
import random

HOST = "0.0.0.0"
PORT = 5555

rooms        = {}   # room_code → [conn1, conn2]
addr_to_room = {}   # conn_id   → room_code
clients      = {}   # conn_id   → conn
lock         = threading.Lock()

def conn_id(conn):
    return id(conn)

def send_msg(conn, msg):
    try:
        data = json.dumps(msg).encode()
        length = len(data).to_bytes(4, 'big')
        conn.sendall(length + data)
    except Exception as e:
        print(f"[SEND ERROR] {e}")

def recv_msg(conn):
    try:
        raw_len = recvall(conn, 4)
        if not raw_len:
            return None
        length = int.from_bytes(raw_len, 'big')
        data = recvall(conn, length)
        if not data:
            return None
        return json.loads(data.decode())
    except Exception:
        return None

def recvall(conn, n):
    data = b""
    while len(data) < n:
        packet = conn.recv(n - len(data))
        if not packet:
            return None
        data += packet
    return data

def handle_client(conn, addr):
    cid = conn_id(conn)
    print(f"[CONNECT] {addr} id={cid}")
    with lock:
        clients[cid] = conn

    try:
        while True:
            msg = recv_msg(conn)
            if msg is None:
                break
            handle_msg(conn, cid, msg)
    except Exception as e:
        print(f"[ERROR] {addr}: {e}")
    finally:
        disconnect(conn, cid)

def handle_msg(conn, cid, msg):
    msg_type = msg.get("type")

    if msg_type == "search":
        with lock:
            # Find a waiting room with 1 player
            waiting = next(
                (code for code, conns in rooms.items() if len(conns) == 1),
                None
            )
            if waiting:
                rooms[waiting].append(conn)
                addr_to_room[cid] = waiting
                p1 = rooms[waiting][0]
                p2 = rooms[waiting][1]
                seed = int(waiting)
            else:
                room_code = str(random.randint(100000, 999999))
                rooms[room_code] = [conn]
                addr_to_room[cid] = room_code
                waiting = None

        if waiting:
            send_msg(p1, {"type": "start", "role": "p1", "seed": seed})
            send_msg(p2, {"type": "start", "role": "p2", "seed": seed})
            print(f"[MATCH] Room {waiting} started")
        else:
            send_msg(conn, {"type": "waiting"})
            print(f"[WAITING] Player {cid} waiting")

    elif msg_type == "input":
        with lock:
            room = addr_to_room.get(cid)
            if not room or room not in rooms:
                return
            other = next((c for c in rooms[room] if conn_id(c) != cid), None)
        if other:
            send_msg(other, {
                "type":  "input",
                "frame": msg.get("frame"),
                "keys":  msg.get("keys"),
            })

    elif msg_type == "event":
        with lock:
            room = addr_to_room.get(cid)
            if not room or room not in rooms:
                return
            other = next((c for c in rooms[room] if conn_id(c) != cid), None)
        if other:
            send_msg(other, msg)

    elif msg_type == "ping":
        send_msg(conn, {"type": "pong"})

def disconnect(conn, cid):
    print(f"[DISCONNECT] id={cid}")
    with lock:
        room = addr_to_room.get(cid)
        if room and room in rooms:
            other = next((c for c in rooms[room] if conn_id(c) != cid), None)
            if other:
                try:
                    send_msg(other, {"type": "disconnect"})
                except Exception:
                    pass
            del rooms[room]
        addr_to_room.pop(cid, None)
        clients.pop(cid, None)
    try:
        conn.close()
    except Exception:
        pass

def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(10)
    print(f"[SERVER] PuyoXTetris relay running on port {PORT}")
    while True:
        try:
            conn, addr = server.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except Exception as e:
            print(f"[ERROR] {e}")

if __name__ == "__main__":
    main()

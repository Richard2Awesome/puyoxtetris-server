import socket
import threading
import time
import json
import random

HOST = "0.0.0.0"
PORT = 5555

rooms        = {}  # room_code → [player1_addr, player2_addr]
addr_to_room = {}  # addr_key  → room_code
last_seen    = {}  # addr_key  → timestamp

def addr_key(addr):
    return f"{addr[0]}:{addr[1]}"

def cleanup_loop(sock):
    """Remove players who haven't pinged in 10 seconds and notify their opponent."""
    while True:
        now  = time.time()
        dead = [a for a, t in list(last_seen.items()) if now - t > 10]
        for a in dead:
            print(f"[CLEANUP] Removing inactive player {a}")
            room = addr_to_room.get(a)
            if room and room in rooms:
                other = next((p for p in rooms[room] if addr_key(p) != a), None)
                if other:
                    try:
                        sock.sendto(json.dumps({"type": "disconnect"}).encode(), other)
                    except Exception:
                        pass
                del rooms[room]
            addr_to_room.pop(a, None)
            last_seen.pop(a, None)
        time.sleep(3)

def handle_packet(sock, data, addr):
    last_seen[addr_key(addr)] = time.time()

    try:
        msg = json.loads(data.decode())
    except Exception:
        return

    msg_type = msg.get("type")

    # ── SEARCH: player looking for a game ──────────────────────────────────
    if msg_type == "search":
        # Find a room with exactly 1 player waiting
        waiting_room = next(
            (code for code, players in rooms.items() if len(players) == 1),
            None
        )

        if waiting_room:
            # Join the waiting player
            rooms[waiting_room].append(addr)
            addr_to_room[addr_key(addr)] = waiting_room

            p1 = rooms[waiting_room][0]
            p2 = rooms[waiting_room][1]

            # Room code doubles as the shared random seed for piece queue
            seed = int(waiting_room)

            sock.sendto(json.dumps({
                "type": "start",
                "role": "p1",
                "seed": seed
            }).encode(), p1)

            sock.sendto(json.dumps({
                "type": "start",
                "role": "p2",
                "seed": seed
            }).encode(), p2)

            print(f"[MATCH] Room {waiting_room}: {p1} vs {p2}")

        else:
            # No waiting room — create one and wait
            room_code = str(random.randint(100000, 999999))
            rooms[room_code] = [addr]
            addr_to_room[addr_key(addr)] = room_code

            sock.sendto(json.dumps({
                "type": "waiting"
            }).encode(), addr)

            print(f"[WAITING] Player {addr} waiting in room {room_code}")

    # ── INPUT: forward this player's inputs to opponent ────────────────────
    elif msg_type == "input":
        room = addr_to_room.get(addr_key(addr))
        if not room or room not in rooms:
            return
        other = next((p for p in rooms[room] if p != addr), None)
        if other:
            sock.sendto(json.dumps({
                "type":  "input",
                "frame": msg.get("frame"),
                "keys":  msg.get("keys")
            }).encode(), other)

    # ── EVENT: rematch requests, forfeit, etc ──────────────────────────────
    elif msg_type == "event":
        room = addr_to_room.get(addr_key(addr))
        if not room or room not in rooms:
            return
        other = next((p for p in rooms[room] if p != addr), None)
        if other:
            sock.sendto(json.dumps(msg).encode(), other)

    # ── PING: keep-alive so server knows player is still connected ─────────
    elif msg_type == "ping":
        sock.sendto(json.dumps({"type": "pong"}).encode(), addr)


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((HOST, PORT))
    print(f"[SERVER] PuyoXTetris relay running on port {PORT}")

    threading.Thread(target=cleanup_loop, args=(sock,), daemon=True).start()

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            threading.Thread(
                target=handle_packet,
                args=(sock, data, addr),
                daemon=True
            ).start()
        except Exception as e:
            print(f"[ERROR] {e}")


if __name__ == "__main__":
    main()


import socketserver
import os
import hashlib
import time

USER_DB = {}
DB_FILE = "users.db"
USERS = {}  

NAME_W = 20  # total chars including leading '#'



def fmt_name(name: str) -> str:
    return f"#{name:<{NAME_W-1}}"[:NAME_W]

def fmt_auth_name(name: str) -> str:
    return f"#{name:<19}"[:20]   # 20 total

def fmt_lobby_name(name: str) -> str:
    return f"#{name:<20}"[:21]   # 21 total


def normalize_room_name(name: str) -> str:
    return name.rstrip().rstrip("\x00")


# Load users from file: username;hash;id
next_id = 1
if os.path.exists(DB_FILE):
    max_id = 0
    with open(DB_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            user, pwd_hash, uid = line.split(";")
            USER_DB[user] = (pwd_hash, uid)
            try:
                max_id = max(max_id, int(uid))
            except ValueError:
                pass
    next_id = max_id + 1


def md5_hash(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def save_user(username, password):
    global next_id
    h = md5_hash(password)
    uid = str(next_id)          # variable-length ID: "1", "2", ... "10"
    next_id += 1

    USER_DB[username] = (h, uid)
    with open(DB_FILE, "a", encoding="utf-8") as f:
        f.write(f"{username};{h};{wire_id(uid)}\n")

    return uid



MAP_LOOKUP = {
    "A": "Paid Parking",
    "B": "Shady Warehouse",
    "C": "Suburbia",
    "D": "The Woods",
    "E": "Bastille",
    "F": "Hedge Maze",
    "G": "Temple"
}

def decode_settings_string(settings):
    """
    Decode a map cycle string like 'FBCDEGA', 'FG', or 'F'.

    Returns:
        dict with:
            - map_cycle (ordered list of map names)
            - raw_codes (list of letter codes)
    """
    if not settings:
        raise ValueError("Empty settings string.")
    
    try:
        map_cycle = [MAP_LOOKUP[c] for c in settings]
    except KeyError as e:
        raise ValueError(f"Invalid map code: {e.args[0]}")
    
    return {
        "map_cycle": map_cycle,
        "raw_codes": list(settings)
    }

def parse_map_rotation(settings_string):
    return [MAP_LOOKUP.get(c, "UNKNOWN") for c in settings_string]

def parse_movement(msg: str):
    """
    Parses a movement message of format:
    1XXXXXYYYYYMDF
    Returns dict with x, y, move_dir, dir, collided.
    """

    return {
        "type": "movement",
        "x": int(msg[1:6]) / 100.0,
        "y": int(msg[6:11]) / 100.0,
        "move_dir": int(msg[11]),        # 0 = not moving, 1-8 = direction index
        "dir": int(msg[12]),             # facing direction
        "collided": bool(int(msg[13]))   # 1 = collided, 0 = normal
    }

def parse_weapon_switch(msg: str):
    """
    Parses weapon switch messages of form:
    0qNN
    """

    return {
        "type": "weapon_switch",
        "weapon_id": int(msg[2:4])
    }

def parse_fire(msg: str):
    """
    Parses a fire message:
    4AAAP
    """

    return {
        "type": "fire",
        "angle_deg": int(msg[1:4]),
        "fire_param": int(msg[4])
    }

def parse_0k1(msg: str):
    if msg == "0k1":
        return {
            "type": "player_ready",
            "meaning": "Player has loaded the map and is ready"
        }
    return None

def parse_weapon_action(msg: str):
    if not msg.startswith("0l"):
        return None
    
    body = msg[2:].strip("\x00")
    if len(body) != 3 or not body.isdigit():
        return {"type": "weapon_action", "error": "Invalid weapon action format", "raw": msg}
    
    weapon_id = int(body[:2])  # first 2 digits = weapon ID
    action_type = body[2]      # last digit = upgrade or base

    if action_type == "0":
        return {
            "type": "weapon_purchase",
            "weapon_id": weapon_id,
        }
    elif action_type in ("1", "2"):
        return {
            "type": "weapon_upgrade",
            "weapon_id": weapon_id,
            "upgrade_index": int(action_type)
        }
    else:
        return {
            "type": "weapon_action",
            "error": "Unknown action type",
            "weapon_id": weapon_id,
            "raw": msg
        }

def parse_acknowledgment(msg: str):
    if msg.strip("\x00") == "p":
        return {
            "type": "ack",
            "ack_for": "round_time",
            "meaning": "Client acknowledged round timer sync"
        }
    return None

def parse_lobby_chat(msg: str):
    if msg.startswith("97"):
        content = msg[2:].strip("\x00")
        if len(content) >= 2:
            text = content[:-1]
            sender_id = content[-1]
            return {
                "type": "lobby_chat",
                "from_id": sender_id,
                "text": text
            }
    return None

def parse_game_chat(msg: str):
    if msg.startswith("98"):
        content = msg[2:].strip("\x00")
        if ";" in content:
            player_id = content[0]
            text = content[1:-1]  # omit ';'
            return {
                "type": "game_chat",
                "from_id": player_id,
                "text": text
            }
    return None

def game_user_packet(uid: str) -> bytes:
    # This is NOT text. It is a fixed-length binary-style blob.
    # Placeholder values that Boxhead accepts safely:
    blob = (
        "00"        # team
        "000"       # score
        "00000"     # head model
        "00000"     # head color
        "0"         # gender
        "00"        # body model
        "00"        # body color
        "0"         # level
        "0;0;0;10000;"  # kills;deaths;assists;cash
    )
    return f"U{wire_id(uid)}{blob}\x00".encode()




stats = "1;1;1;1;1;1"

def lobby_user_packet(uid: str) -> bytes:
    uname = USERS[uid]["username"]
    return f"U{wire_id(uid)}{fmt_lobby_name(uname)}{stats}\x00".encode("utf-8")


def wire_id(uid):
    return f"{USERS[uid]['slot']:03d}"





class SlotAllocator:
    def __init__(self):
        self.free = set(range(1, 1000))
        self.used = {}

    def allocate(self, account_id):
        slot = min(self.free)
        self.free.remove(slot)
        self.used[account_id] = slot
        return slot

    def release(self, account_id):
        slot = self.used.pop(account_id, None)
        if slot is not None:
            self.free.add(slot)

SLOTS = SlotAllocator()

class FlashGameHandler(socketserver.BaseRequestHandler):

    def send(self, msg):
        if isinstance(msg, str):
            b = msg.encode("utf-8")
        else:
            b = msg
        print(f"[<] SEND len={len(b)} repr={repr(b)}")
        self.request.sendall(b)

    def cleanup_room_if_empty(self, room_name):
        if room_name == "_":
            return  # NEVER delete lobby

        room = self.server.rooms.get(room_name)
        if not room:
            return

        if not room["players"]:
            print(f"[x] Deleting empty room '{room_name}'")
            del self.server.rooms[room_name]
            self.broadcast_room_list_to_lobby()


    def broadcast_lobby_userlist(self):
        for user in USERS.values():
            if user.get("room") == "_":
                try:
                    self.send_lobby_userlist(user["socket"])
                except:
                    pass

    def send_room_list(self):
        out = "01"
        for name, room in self.server.rooms.items():
            if name == "_":
                continue
            count = len(room.get("players", set()))
            out += f"{count:02d}{name};"
        out += "\x00"
        self.send(out.encode("utf-8"))

    def broadcast_to_peers(self, message: str, exclude_id: str = None):
        dead_clients = []

        for user_id, user_data in USERS.items():
            if user_id == exclude_id:
                continue

            sock = user_data.get("socket")
            if not sock:
                dead_clients.append(user_id)
                continue

            try:
                sock.sendall(message.encode("utf-8"))
            except (OSError, AttributeError):
                print(f"[!] Failed to send to {user_id}. Marking as dead.")
                dead_clients.append(user_id)

        for user_id in dead_clients:
            self.remove_user(user_id)

    def remove_user(self, uid: str):
        user = USERS.get(uid)
        if not user:
            return

        # Always leave room cleanly first
        self.leave_current_room(uid)

        try:
            user["socket"].close()
        except OSError:
            pass

        # Release slot
        if user.get("slot") is not None:
            SLOTS.release(uid)

        USERS.pop(uid, None)



    def leave_current_room(self, uid):
        user = USERS.get(uid)
        if not user:
            return

        room_name = user.get("room")
        if not room_name:
            return

        room = self.server.rooms.get(room_name)

        # Clear user's room FIRST
        user["room"] = None

        if not room:
            return

        # REMOVE from room player list
        if uid in room["players"]:
            room["players"].remove(uid)

        # Notify remaining peers
        for peer_id in list(room["players"]):
            peer = USERS.get(peer_id)
            if peer:
                try:
                    peer["socket"].sendall(f"D{wire_id(uid)}\x00".encode())
                except OSError:
                    pass

        # Cleanup if empty
        self.cleanup_room_if_empty(room_name)






    def broadcast_room_list_to_lobby(self):
        for uid, user in USERS.items():
            if user["room"] == "_":
                try:
                    self.send_room_list()  # Pass the server here
                except OSError:
                    pass

    def handle(self):
        try:
            self.lobby_ready = False
            print(f"[+] Connected: {self.client_address}")
            buffer = ""


            self.username = None
            self.user_id = None
            self.room = None
            while True:

                data = self.request.recv(1024).decode("utf-8")
                if not data:
                    break

                buffer += data
                print(f"[>] Received: {repr(data)}")

                if "<policy-file-request/>" in buffer:
                    policy = (
                        '<?xml version="1.0"?>'
                        '<cross-domain-policy>'
                        '<allow-access-from domain="*" to-ports="6123"/>'
                        '</cross-domain-policy>\x00'
                    )
                    self.send(policy.encode("utf-8"))
                    buffer = ""
                    continue

                #if buffer.startswith("09"):
                    # creds = buffer[2:].strip("\x00")
                    # if ";" not in creds:
                    #     self.send(b"10;0;Bad format\x00")
                    #     buffer = ""
                    #     continue

                    # username, password = creds.split(";")
                    # pwd_hash = md5_hash(password)

                    # # handshake 00;1
                    # self.send(b"00;1\x00")
                    # print("[<] Sent delayed handshake")

                    # global next_id

                    # # existing user?
                    # if username in USER_DB:
                    #     stored_hash = USER_DB[username]
                    #     if pwd_hash != stored_hash:
                    #         self.send(b"10;0;Incorrect password\x00")
                    #         buffer = ""
                    #         continue
                    # else:
                    #     save_user(username, password)
                        
                    # login_ack = f"10;1;{uid};{username};{username};{pwd_hash};1\x00"
                    # self.send(login_ack.encode("utf-8"))


                    # # store identity correctly
                    # self.username = username
                    # #self.user_id = str(uid)
                    # self.user_id = f"{session_id:03d}"

                    # USERS[self.user_id] = {
                    #     "username": username,
                    #     "socket": self.request,
                    #     "room": None
                    # }

                    # # AUTH PACKET
                    # #auth_name = f"#{username:<19}"[:20]
                    # stats = "1;1;1;1;1;1"  # 6 fields REQUIRED
                    # #auth_msg = f"A{self.user_id}{auth_name}{stats}\x00"

                    # auth_name = fmt_auth_name(username)
                    # auth_msg = f"A{self.user_id}{auth_name}{stats}\x00"

                    # self.send(auth_msg.encode("utf-8"))
                    # print(f"[<] Sent AUTH: {auth_msg}")

                    # # Premiums refreshed
                    # self.send(b"0p\x00")

                if buffer.startswith("09"):
                    creds = buffer[2:].strip("\x00")
                    if ";" not in creds:
                        self.send(b"10;0;Bad format\x00")
                        buffer = ""
                        continue

                    username, password = creds.split(";")
                    pwd_hash = md5_hash(password)

                    # handshake 00;1
                    self.send(b"00;1\x00")
                    print("[<] Sent delayed handshake")

                    # existing user?
                    if username in USER_DB:
                        stored_hash, uid = USER_DB[username]
                        if pwd_hash != stored_hash:
                            self.send(b"10;0;Incorrect password\x00")
                            buffer = ""
                            continue
                    else:
                        uid = save_user(username, password)
                        stored_hash, _ = USER_DB[username]

                    # Use persistent DB id as the player id everywhere
                    player_id = uid

                    login_ack = f"10;1;{player_id};{username};{username};{pwd_hash};1\x00"
                    self.send(login_ack.encode("utf-8"))

                    self.username = username
                    self.user_id = player_id   # you can rename this to self.player_id later if you want

                    USERS[self.user_id] = {
                        "username": username,
                        "socket": self.request,
                        "room": None,
                        "slot": None
                    }

                    slot = SLOTS.allocate(self.user_id)
                    USERS[self.user_id]["slot"] = slot

                    # AUTH PACKET
                    auth_name = fmt_auth_name(username)
                    stats = "1;1;1;1;1;1"
                    self.send(f"A{wire_id(self.user_id)}{auth_name}{stats}\x00".encode("utf-8"))
                    print(f"[<] Sent AUTH: A{wire_id(self.user_id)}{auth_name}{stats}")


                    # READY
                    self.send(b"0p\x00")

                    buffer = ""
                    continue


                if buffer.startswith("03"):
                    self.leave_current_room(self.user_id)
                    old_room = USERS[self.user_id].get("room")
                    if old_room and old_room != room_name:
                        self.leave_current_room(self.user_id)

                    room_name = normalize_room_name(buffer[2:])
                    print(f"[=] User {wire_id(self.user_id)} joining room: {room_name}")


                    # ---- lobby join ----
                    if room_name == "_":
                        room = self.server.rooms["_"]
                    else:
                        room = self.server.rooms.get(room_name)
                        if not room:
                            print(f"[!] Tried to join missing room {room_name}")
                            buffer = ""
                            continue


                    # ---- add to room ----
                    # room["players"].add(self.user_id)
                    # USERS[self.user_id]["room"] = room_name

                    # ============================================================
                    # ======================= JOIN LOGIC =========================
                    # ============================================================

                    if room_name == "_":
                        # ===========================
                        # ========== LOBBY ==========
                        # ===========================

                        lobby = self.server.rooms["_"]

                        USERS[self.user_id]["room"] = "_"
                        lobby["players"].add(self.user_id)

                        # Tell THIS client it joined
                        self.send(f"C{wire_id(self.user_id)}\x00")


                        # Send existing lobby users to THIS client
                        for uid in lobby["players"]:
                            if uid == self.user_id:
                                continue
                            self.send(f"C{wire_id(uid)}\x00")
                            self.send(lobby_user_packet(uid))

                        # Tell existing lobby users about THIS client
                        for uid in lobby["players"]:
                            if uid == self.user_id:
                                continue
                            try:
                                sock = USERS[uid]["socket"]
                                sock.sendall(f"C{wire_id(self.user_id)}\x00".encode())
                                sock.sendall(lobby_user_packet(self.user_id))
                            except OSError:
                                pass

                        self.broadcast_room_list_to_lobby()
                        self.lobby_ready = True
                        print("[<] Lobby join completed.")

                    else:
                        # ===========================
                        # ========== GAME ===========
                        # ===========================

                        room = self.server.rooms.get(room_name)
                        if not room:
                            print(f"[!] Tried to join missing room {room_name}")
                            buffer = ""
                            continue

                        # Now officially add to room
                        room["players"].add(self.user_id)
                        USERS[self.user_id]["room"] = room_name

                        # Tell THIS client it joined
                        self.send(f"C{wire_id(self.user_id)}\x00")

                        # Send existing players to THIS client
                        for uid in room["players"]:
                            if uid == self.user_id:
                                continue
                            self.send(f"C{wire_id(uid)}\x00")
                            self.send(game_user_packet(uid))



                        # Broadcast THIS player to existing players
                        for uid in room["players"]:
                            try:
                                sock = USERS[uid]["socket"]
                                sock.sendall(f"C{wire_id(self.user_id)}\x00".encode())
                                sock.sendall(game_user_packet(self.user_id))
                            except OSError:
                                pass

                        # Start round ONCE
                        if room.get("round_start") is None:
                            room["round_start"] = time.time()

                        # Send synchronized timer
                        length = room.get("round_length", 600)
                        elapsed = int(time.time() - room["round_start"])
                        remaining = max(0, length - elapsed)
                        self.send(f"p{remaining}\x00")

                        # Send game settings
                        self.send(f"s{room['settings_string']}\x00".encode())

                        # Round init + game start
                        self.send(f"R{wire_id(uid)}\x00")
                        self.send(f"G{wire_id(uid)}\x00")
                        self.send(f"I{wire_id(uid)}\x00")

                        # Send map data (same one used during host creation)
                        map_packet = room.get("map_packet")
                        if map_packet:
                            self.send(map_packet.encode())




                        print("[<] Game join completed.")



                    buffer = ""
                    continue




                if buffer.startswith("9?"):
                    index = buffer[2:].strip("\x00")
                    msg = f"M{wire_id(self.user_id)}9?{index}\x00"
                    self.send(msg.encode("utf-8"))
                    print(f"[<] Echoed ping: {msg}")
                    buffer = ""
                    continue

                if buffer.startswith("01"):
                    if self.lobby_ready:
                        self.send_room_list()
                    buffer = ""
                    continue


                if buffer.startswith("04"):
                    payload = buffer[2:].strip("\x00")
                    print(f"[=] Received ROOM_INFO request for: {payload}")

                    # Skip lobby
                    if payload == "_":
                        print("[~] Ignoring ROOM_INFO request for lobby '_'")
                        buffer = ""
                        continue

                    # Lookup room by name
                    room = self.server.rooms.get(payload)
                    if not room:
                        print(f"[!] Room '{payload}' not found — sending fallback")
                        self.send(b"0411D01180\x00")
                        buffer = ""
                        continue

                    # Get settings
                    settings = room.get("settings_string", "")
                    decoded = decode_settings_string(settings)
                    user_count = len(room["players"])



                    # Format ROOM_INFO:
                    # 041<customMapFlag><mapIdChar><playerCount><roundTime>\x00

                    custom_map_flag = "0"  # We assume built-in maps only right now
                    map_codes = decoded["raw_codes"]
                    first_map_code = map_codes[0] if map_codes else "A"
                    start = room.get("round_start")
                    length = room.get("round_length", 600)

                    if start:
                        remaining = max(0, int(length - (time.time() - start)))
                    else:
                        remaining = length

                    round_time = str(remaining).zfill(3)
                    player_count = str(user_count).zfill(2)

                    info = f"041{custom_map_flag}{first_map_code}{player_count}{round_time}\x00"
                    self.send(info.encode("utf-8"))
                    print(f"[<] Sent ROOM_INFO: {repr(info)}")

                    for uid, user in USERS.items():
                        if user["room"] == "_":
                            self.send_room_list()


                    self.broadcast_room_list_to_lobby()


                    buffer = ""
                    continue

                if buffer.startswith("02A00") or buffer.startswith("02"):
                    self.leave_current_room(self.user_id)
                    payload = buffer[5:].strip("\x00")
                    raw_room_name, settings = payload.split(";", 1)
                    room_name = normalize_room_name(raw_room_name)

                    decoded_settings = decode_settings_string(settings)

                    # Host leaves lobby cleanly
                    old_room = USERS[self.user_id].get("room")
                    if old_room == "_":
                        lobby = self.server.rooms["_"]

                    USERS[self.user_id]["room"] = None


                    self.server.rooms[room_name] = {
                        "name": room_name,
                        "settings_string": settings,
                        "decoded_settings": decoded_settings,
                        "players": {self.user_id},   # HOST IS IN GAME
                        "round_start": time.time(),
                        "round_length": 600,
                    }

                    USERS[self.user_id]["room"] = room_name



                    print(f"[+] Created room '{room_name}'")

                    # confirm creation (this is all the client expects here)
                    self.send(f"C{wire_id(self.user_id)}\x00")

                    self.broadcast_room_list_to_lobby()

                    buffer = ""
                    continue





                if buffer.startswith("0d"):

                    payload = buffer.strip()
                    room = USERS[self.user_id].get("room")
                    if room:
                        for uid in self.server.rooms[room]["players"]:
                            if uid == self.user_id:
                                continue
                            self.broadcast_to_peers(f"U{wire_id(self.user_id)}#{self.username:<20}1;2;3;4;5;6\x00", exclude_id=self.user_id)
                    buffer = ""
                    continue

                if buffer.startswith("07"):

                    print("[=] Host started the match")
                    room = USERS[self.user_id].get("room")
                    if room:
                        for uid in self.server.rooms[room]["players"]:
                            if uid == self.user_id:
                                continue
                            USERS[uid]["socket"].sendall(f"G{wire_id(self.user_id)}{self.username}\x00".encode("utf-8"))
                            USERS[uid]["socket"].sendall(b"F00200\x00")
                    for uid, user in USERS.items():
                        if user["room"] == "_":
                            self.send_room_list()

                    buffer = ""
                    self.broadcast_room_list_to_lobby()
                    continue

                if buffer.startswith("1") and len(buffer) >= 14:
                    room = USERS[self.user_id].get("room")
                    if room and room in self.server.rooms:
                        out = f"M{wire_id(self.user_id)}{buffer}\x00"
                        for uid in self.server.rooms[room]["players"]:
                            if uid != self.user_id:
                                USERS[uid]["socket"].sendall(out.encode("utf-8"))
                    buffer = ""
                    continue

                if buffer.startswith("0q") and len(buffer) >= 4:
                    room = USERS[self.user_id].get("room")
                    if room and room in self.server.rooms:
                        out = f"M{wire_id(self.user_id)}{buffer}\x00"
                        for uid in self.server.rooms[room]["players"]:
                            if uid != self.user_id:
                                USERS[uid]["socket"].sendall(out.encode("utf-8"))
                    buffer = ""
                    continue
                
                if buffer.startswith("4") and len(buffer) >= 5:
                    room = USERS[self.user_id].get("room")
                    if room and room in self.server.rooms:
                        out = f"M{wire_id(self.user_id)}{buffer}\x00"
                        for uid in self.server.rooms[room]["players"]:
                            if uid != self.user_id:
                                USERS[uid]["socket"].sendall(out.encode("utf-8"))
                    buffer = ""
                    continue

                if buffer.startswith("0k1"):
                    parse_0k1(buffer)
                    buffer = ""
                    continue
                
                if buffer.startswith("0l"):
                    parse_weapon_action(msg)
                    buffer = ""
                    continue

                if buffer.startswith("p"):
                    parse_acknowledgment(buffer)
                    buffer = ""
                    continue

                # Private message: 00<target><encrypted>
                if buffer.startswith("00"):
                    rest = buffer[2:].rstrip("\x00")
                    # first numeric prefix = target ID
                    i = 0
                    while i < len(rest) and rest[i].isdigit():
                        i += 1
                    target_id = rest[:i]
                    payload = rest[i:]

                    if target_id in USERS:
                        out = f"M{wire_id(self.user_id)}{payload}\x00"
                        USERS[target_id]["socket"].sendall(out.encode())


                    if target_id in USERS:
                        out = f"M{wire_id(self.user_id)}{payload}\x00"
                        USERS[target_id]["socket"].sendall(out.encode("utf-8"))
                        print(f"[PM] {wire_id(self.user_id)} -> {target_id}: {out!r}")
                    else:
                        print(f"[PM] Target {target_id} not online")

                    buffer = ""
                    continue


                # Chat packets: 91–99
                # Client-to-server "9x..." messages (chat / misc encrypted)
                if len(buffer) >= 2 and buffer[0] == "9" and buffer[1] != "?":
                    room = USERS[self.user_id]["room"]
                    if room:
                        payload = buffer[1:].rstrip("\x00")  # keep the subtype digit + encrypted blob
                        out = f"M{wire_id(self.user_id)}9{payload}\x00"

                        for uid in self.server.rooms[room]["players"]:
                            USERS[uid]["socket"].sendall(out.encode("utf-8"))

                    buffer = ""
                    continue

                if buffer:
                    print(f"[?] Unknown/unhandled message: {repr(buffer)}")
                    buffer = ""

                buffer = ""
        finally:
            if self.user_id:
                old_room = USERS[self.user_id].get("room")
                self.remove_user(self.user_id)
                if old_room and old_room != "_":
                    self.cleanup_room_if_empty(old_room)


with socketserver.ThreadingTCPServer(("0.0.0.0", 6123), FlashGameHandler) as server:
    server.rooms = {
        "_": {
            "name": "_",
            "players": set(),
            "settings_string": None,
            "decoded_settings": None,
        }
    }
    print("[*] Listening on port 6123...")
    server.serve_forever()


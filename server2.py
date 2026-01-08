import socketserver
import os
import hashlib
import time

DB_FILE = "users.db"

# username -> (md5_hash, account_id_string)
USER_DB = {}
USERS = {}  # account_id -> dict(socket, username, slot, room)

###############################################################################
# Helpers
###############################################################################

def md5_hash(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def normalize_room_name(name: str) -> str:
    # Strip null, spaces, and the \x01 padding you are using
    return name.rstrip("\x00").rstrip().rstrip("\x01")

def fmt_name_20(username: str) -> str:
    """
    Flash client reads EXACTLY 20 chars for name.
    Client strips leading '#'.
    If we pad with normal spaces, it SHOWS them.
    So we pad with \x01 (non-printing) instead.
    """
    base = "#" + (username or "")
    base = base[:20]
    return base.ljust(20, "\x01")

class SlotAllocator:
    def __init__(self):
        self.free = set(range(1, 1000))
        self.used = {}  # account_id -> slot

    def allocate(self, account_id: str) -> int:
        slot = min(self.free)
        self.free.remove(slot)
        self.used[account_id] = slot
        return slot

    def release(self, account_id: str):
        slot = self.used.pop(account_id, None)
        if slot is not None:
            self.free.add(slot)

SLOTS = SlotAllocator()

def wire_id(account_id: str) -> str:
    return f"{USERS[account_id]['slot']:03d}"

###############################################################################
# User DB load/save
###############################################################################

next_id = 1
if os.path.exists(DB_FILE):
    max_id = 0
    with open(DB_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            user, pwd_hash, acc_id = line.split(";")
            USER_DB[user] = (pwd_hash, acc_id)
            try:
                max_id = max(max_id, int(acc_id))
            except ValueError:
                pass
    next_id = max_id + 1

def save_user(username: str, password: str) -> str:
    global next_id
    h = md5_hash(password)
    acc_id = str(next_id)
    next_id += 1

    USER_DB[username] = (h, acc_id)
    with open(DB_FILE, "a", encoding="utf-8") as f:
        f.write(f"{username};{h};{acc_id}\n")
    return acc_id

###############################################################################
# Packet builders (MATCH AS3 EXPECTATIONS)
###############################################################################

def auth_packet(account_id: str) -> bytes:
    """
    updateUserFromAuthenticate(param2):
      name20 + level + gender + headM2 + headC2 + bodyM2 + bodyC2 + stats5 + wanted
    """
    username = USERS[account_id]["username"]
    name20 = fmt_name_20(username)

    level = "0"
    gender = "0"
    head_model = "00"
    head_color = "00"
    body_model = "00"
    body_color = "00"

    stats5 = "0;0;0;0;0"
    wanted = "0"

    payload = f"{name20}{level}{gender}{head_model}{head_color}{body_model}{body_color}{stats5}{wanted}"
    return f"A{wire_id(account_id)}{payload}\x00".encode("utf-8")

def lobby_user_packet(account_id: str) -> bytes:
    """
    updateUserFromLobbyHandshake(param2):
      name20 + stats6 + wanted
    """
    if USERS[account_id].get("room") != "_":
        return b""

    username = USERS[account_id]["username"]
    name20 = fmt_name_20(username)

    stats6 = "0;0;0;0;0;0"
    wanted = "0"

    payload = f"{name20}{stats6}{wanted}"
    return f"U{wire_id(account_id)}{payload}\x00".encode("utf-8")

def game_user_packet(account_id: str) -> bytes:
    u = USERS[account_id]
    name20 = fmt_name_20(u["username"])

    # FIXED-WIDTH HEADER (35 chars total before semicolons)

    weapon_id = "00"     # 2 digits
    hp        = "100"    # 3 digits (player health)
    gender    = "0"      # 0 = monster
    head_m    = "00"
    head_c    = "00"
    body_m    = "00"
    body_c    = "00"
    team      = "0"      # single digit

    # SEMICOLON-DELIMITED SECTION (NO width limits)

    score  = "10000"     # money
    kills  = "0"
    deaths = "0"
    bounty = "0"

    weapons = ""         # empty upgrade list allowed
    wanted  = "0"

    payload = (
        f"{weapon_id}{hp}{name20}"
        f"{gender}{head_m}{head_c}{body_m}{body_c}{team}"
        f"{score};{kills};{deaths};{bounty};"
        f"{weapons}"
        f"{wanted}"
    )

    return f"U{wire_id(account_id)}{payload}\x00".encode("utf-8")




###############################################################################
# Room list / broadcast
###############################################################################

def build_room_list_bytes(server) -> bytes:
    out = "01"
    for room_name, room in server.rooms.items():
        if room_name == "_":
            continue
        count = len(room["players"])
        out += f"{count:02d}{room_name};"
    out += "\x00"
    return out.encode("utf-8")

def broadcast_room_list_to_lobby(server):
    packet = build_room_list_bytes(server)
    for acc_id, u in USERS.items():
        if u.get("room") == "_":
            try:
                u["socket"].sendall(packet)
            except OSError:
                pass

###############################################################################
# Handler
###############################################################################

class FlashGameHandler(socketserver.BaseRequestHandler):
    def send(self, b: bytes):
        if isinstance(b, str):
            b = b.encode("utf-8")
        print(f"[<] SEND len={len(b)} repr={repr(b)}")
        self.request.sendall(b)

    def send_private_lobby(self, packet: str):
        """
        Client sendPrivate(): "00" + targetID(3 digits) + "9" + encrypt(msg)
        We only support PMs in the lobby (room "_").

        Incoming example: 0000293chi
        - "00"
        - target wire id: "002"
        - payload: "93chi"   (starts with 9)

        Receiver must get: M<senderID><payload>\x00
        ex: M00193chi\x00
        """
        # Must be at least: "00" + 3-digit id + "9x"
        if len(packet) < 6:
            return

        # Only allow PMs in lobby
        room_name = USERS[self.account_id].get("room")
        if room_name != "_":
            return

        target_wire = packet[2:5]
        payload = packet[5:]

        # Must be an encrypted "9..." payload (the client expects 9-prefixed messages)
        if not payload.startswith("9"):
            return

        # Find target account by slot/wire id
        target_acc = None
        for acc_id, u in USERS.items():
            if f"{u.get('slot', 0):03d}" == target_wire:
                target_acc = acc_id
                break

        if not target_acc:
            print(f"[PM] Target {target_wire} not online")
            return

        out = f"M{wire_id(self.account_id)}{payload}\x00".encode("utf-8")
        try:
            USERS[target_acc]["socket"].sendall(out)
            print(f"[PM] {wire_id(self.account_id)} -> {target_wire}: {out!r}")
        except OSError:
            pass


    def relay_raw_to_room(self, room_name: str, raw_packet: str, include_self: bool = False):

        if not room_name or room_name not in self.server.rooms:
            return
        out = (raw_packet + "\x00").encode("utf-8")
        for peer_acc in self.server.rooms[room_name]["players"]:
            if not include_self and peer_acc == self.account_id:
                continue
            try:
                USERS[peer_acc]["socket"].sendall(out)
            except OSError:
                pass

    def relay_state_to_room(self, room_name: str, packet: str):
        if not room_name or room_name not in self.server.rooms:
            return

        # ONLY rewrite packets with 2-byte opcodes
        if len(packet) < 3:
            return

        opcode = packet[:2]

        # Allowed opcodes to rewrite
        if opcode not in ("10", "11", "12", "80"):
            return

        payload = packet[2:]
        out = f"{opcode}{wire_id(self.account_id)}{payload}\x00".encode("utf-8")

        for peer_acc in self.server.rooms[room_name]["players"]:
            if peer_acc == self.account_id:
                continue
            try:
                USERS[peer_acc]["socket"].sendall(out)
            except OSError:
                pass


    def relay_chat9_to_room(self, room_name: str, packet: str, include_self: bool = False):
        """
        The client's sendMessage() sends: 9<encrypted>
        The receiver expects it wrapped as: M<senderID>9<encrypted>
        """
        if not room_name or room_name not in self.server.rooms:
            return
        out = f"M{wire_id(self.account_id)}{packet}\x00".encode("utf-8")
        for peer_acc in self.server.rooms[room_name]["players"]:
            if not include_self and peer_acc == self.account_id:
                continue
            try:
                USERS[peer_acc]["socket"].sendall(out)
            except OSError:
                pass


    def broadcast_to_room(self, room_name: str, payload: str, exclude_self: bool = True):
        """
        Sends: M<senderID><payload>\x00 to everyone in room.
        """
        if not room_name or room_name not in self.server.rooms:
            return
        out = f"M{wire_id(self.account_id)}{payload}\x00".encode("utf-8")
        for peer_acc in self.server.rooms[room_name]["players"]:
            if exclude_self and peer_acc == self.account_id:
                continue
            try:
                USERS[peer_acc]["socket"].sendall(out)
            except OSError:
                pass

    def leave_current_room(self, account_id: str):
        user = USERS.get(account_id)
        if not user:
            return

        room_name = user.get("room")
        if not room_name:
            return

        room = self.server.rooms.get(room_name)
        user["room"] = None

        if not room:
            return

        if account_id in room["players"]:
            room["players"].remove(account_id)

        # notify peers
        for peer_acc in list(room["players"]):
            peer = USERS.get(peer_acc)
            if not peer:
                continue
            try:
                peer["socket"].sendall(f"D{wire_id(account_id)}\x00".encode("utf-8"))
            except OSError:
                pass

        # cleanup empty non-lobby room
        if room_name != "_" and not room["players"]:
            print(f"[x] Deleting empty room '{room_name}'")
            del self.server.rooms[room_name]
            broadcast_room_list_to_lobby(self.server)

    def remove_user(self, account_id: str):
        user = USERS.get(account_id)
        if not user:
            return
        self.leave_current_room(account_id)
        try:
            user["socket"].close()
        except OSError:
            pass
        SLOTS.release(account_id)
        USERS.pop(account_id, None)

    def handle_packet(self, packet: str):
        if not packet:
            return

        print(f"[>] Received packet: {repr(packet)}")

        # POLICY FILE
        if packet == "<policy-file-request/>":
            policy = (
                '<?xml version="1.0"?>'
                '<cross-domain-policy>'
                '<allow-access-from domain="*" to-ports="6123"/>'
                '</cross-domain-policy>\x00'
            )
            self.send(policy.encode("utf-8"))
            return

        # AUTH REQUEST
        if packet.startswith("09"):
            creds = packet[2:]
            if ";" not in creds:
                self.send(b"10;0;Bad format\x00")
                return

            username, password = creds.split(";", 1)
            pwd_hash = md5_hash(password)

            # handshake ack first
            self.send(b"00;1\x00")
            print("[<] Sent delayed handshake")

            if username in USER_DB:
                stored_hash, acc_id = USER_DB[username]
                if pwd_hash != stored_hash:
                    self.send(b"10;0;Incorrect password\x00")
                    return
            else:
                acc_id = save_user(username, password)

            # login ack
            login_ack = f"10;1;{acc_id};{username};{username};{pwd_hash};1\x00"
            self.send(login_ack.encode("utf-8"))

            # create user record
            self.username = username
            self.account_id = acc_id

            USERS[acc_id] = {
                "username": username,
                "socket": self.request,
                "room": None,
                "slot": SLOTS.allocate(acc_id),
            }

            self.send(auth_packet(acc_id))
            self.send(b"0p\x00")
            return

        # Everything below requires auth
        if not getattr(self, "account_id", None) or self.account_id not in USERS:
            # ignore any pre-auth junk
            return

        # JOIN ROOM
        if packet.startswith("03"):
            room_name = normalize_room_name(packet[2:])

            old_room = USERS[self.account_id].get("room")
            if old_room:
                # notify peers in old room BEFORE switching
                for peer_acc in list(self.server.rooms.get(old_room, {}).get("players", [])):
                    if peer_acc != self.account_id:
                        try:
                            USERS[peer_acc]["socket"].sendall(
                                f"D{wire_id(self.account_id)}\x00".encode("utf-8")
                            )
                        except OSError:
                            pass

            # now actually leave
            self.leave_current_room(self.account_id)

            print(f"[=] User {wire_id(self.account_id)} joining room: {room_name}")

            if room_name not in self.server.rooms:
                print(f"[!] Missing room '{room_name}', ignoring join.")
                return

            room = self.server.rooms[room_name]
            room["players"].add(self.account_id)
            USERS[self.account_id]["room"] = room_name

            # tell self it joined
            self.send(f"C{wire_id(self.account_id)}\x00".encode("utf-8"))

            # IMPORTANT: send self game handshake
            self.send(game_user_packet(self.account_id))

            # sync peers
            for peer_acc in list(room["players"]):
                if peer_acc == self.account_id:
                    continue

                # tell self about peer
                self.send(f"C{wire_id(peer_acc)}\x00".encode("utf-8"))
                if room_name == "_" and USERS[peer_acc].get("room") == "_":
                    self.send(lobby_user_packet(peer_acc))

                # tell peer about self
                try:
                    peer_sock = USERS[peer_acc]["socket"]
                    if room_name == "_":
                        peer_sock.sendall(f"C{wire_id(self.account_id)}\x00".encode("utf-8"))
                        peer_sock.sendall(lobby_user_packet(self.account_id))
                except OSError:
                    pass


            # after lobby join, send room list
            if room_name == "_":
                self.send(build_room_list_bytes(self.server))
                print("[<] Lobby join completed.")
                return

            # joining a game room -> send timer/settings/RGI
            if room.get("round_start") is None:
                room["round_start"] = time.time()

            length = room.get("round_length", 600)
            elapsed = int(time.time() - room["round_start"])
            remaining = max(0, length - elapsed)

            # Send timer to the joiner (and also send to everyone, so both clients stay synced)
            for peer_acc in room["players"]:
                try:
                    USERS[peer_acc]["socket"].sendall(f"p{remaining}\x00".encode("utf-8"))
                except OSError:
                    pass

            self.send(f"s{room['settings_string']}\x00".encode("utf-8"))
            self.send(f"R{wire_id(self.account_id)}\x00".encode("utf-8"))
            self.send(f"G{wire_id(self.account_id)}\x00".encode("utf-8"))
            self.send(f"I{wire_id(self.account_id)}\x00".encode("utf-8"))


            # AFTER all C packets
            for peer_acc in room["players"]:
                if peer_acc == self.account_id:
                    continue

                # send peer handshake to self
                self.send(game_user_packet(peer_acc))

                # send self handshake to peer
                # send self to existing peer (CREATE + HANDSHAKE)
                peer_sock = USERS[peer_acc]["socket"]
                peer_sock.sendall(f"C{wire_id(self.account_id)}\x00".encode("utf-8"))
                peer_sock.sendall(game_user_packet(self.account_id))



            print("[<] Game join completed.")
            return

        # ROOM LIST REQUEST
        if packet == "01":
            self.send(build_room_list_bytes(self.server))
            return

        # ROOM INFO REQUEST
        if packet.startswith("04"):
            room_name = normalize_room_name(packet[2:])
            room = self.server.rooms.get(room_name)
            if not room or room_name == "_":
                return

            gameType = "1"
            useCustom = "0"
            mapID = "A"
            players = f"{len(room['players']):02d}"

            length = room.get("round_length", 600)
            start = room.get("round_start") or time.time()
            elapsed = int(time.time() - start)
            remaining = max(0, length - elapsed)

            msg = f"04{gameType}{useCustom}{mapID}{players}{remaining}\x00"
            self.send(msg.encode("utf-8"))
            return

        # CREATE ROOM
        if packet.startswith("02"):
            self.leave_current_room(self.account_id)

            payload = packet[2:]
            if ";" not in payload:
                return

            header = payload[:3]  # gameType/useCustom/isPrivate
            rest = payload[3:]
            room_part, settings = rest.split(";", 1)

            room_name = normalize_room_name(room_part)
            settings = settings.strip()

            self.server.rooms[room_name] = {
                "name": room_name,
                "settings_string": settings,
                "players": {self.account_id},
                "round_start": time.time(),
                "round_length": 600,
            }
            USERS[self.account_id]["room"] = room_name

            print(f"[+] Created room '{room_name}'")
            self.send(f"C{wire_id(self.account_id)}\x00".encode("utf-8"))
            broadcast_room_list_to_lobby(self.server)
            return

        # PING ECHO
        if packet.startswith("9?"):
            idx = packet[2:]
            msg = f"M{wire_id(self.account_id)}9?{idx}\x00"
            self.send(msg.encode("utf-8"))
            return

        #######################################################################
        # GAME / LOBBY TRAFFIC
        #######################################################################

        room_name = USERS[self.account_id].get("room")

        # --- CHAT / ENCRYPTED "9..." (NOT ping) ---
        # Client sends: 9<encrypted>
        # Peer must receive: M<ID>9<encrypted>
        if packet.startswith("9") and not packet.startswith("9?"):
            if room_name:
                self.relay_chat9_to_room(room_name, packet, include_self=True)
            return

        # --- MOVEMENT / PLAYER STATE ---
        # These arrive WITHOUT sender ID, so we must prefix sender ID.
        if packet.startswith("1"):
            if room_name:
                self.relay_state_to_room(room_name, packet)
            return

        # --- "8..." packets are ALSO state updates (weapons / actions / etc) ---
        if packet.startswith("8"):
            if room_name:
                self.relay_state_to_room(room_name, packet)
            return

        # # --- weapon switch / misc state ---
        # if packet.startswith("0q"):
        #     if room_name:
        #         self.relay_state_to_room(room_name, packet)
        #     return

        # # --- projectile / fire packets ---
        # if packet.startswith("4"):
        #     if room_name:
        #         self.relay_state_to_room(room_name, packet)
        #     return

        # # --- 0k... (this happens in-game; treat as state and forward) ---
        # # You were seeing it as unhandled. It MUST propagate.
        # if packet.startswith("0k"):
        #     if room_name:
        #         self.relay_state_to_room(room_name, packet)
        #     return

        if packet.startswith(("4", "0k", "0q")):
            if room_name:
                self.relay_raw_to_room(room_name, packet)
            return


        # --- customization ---
        if packet.startswith("0d"):
            # rebroadcast updated handshake so peers see the new look
            if room_name and room_name in self.server.rooms:
                for peer_acc in self.server.rooms[room_name]["players"]:
                    if peer_acc == self.account_id:
                        continue
                    try:
                        USERS[peer_acc]["socket"].sendall(f"C{wire_id(self.account_id)}\x00".encode("utf-8"))
                        USERS[peer_acc]["socket"].sendall(game_user_packet(self.account_id))
                    except OSError:
                        pass
            return

        # --- round time request ---
        if packet == "p":
            if room_name and room_name in self.server.rooms:
                room = self.server.rooms[room_name]
                length = room.get("round_length", 600)
                start = room.get("round_start") or time.time()
                elapsed = int(time.time() - start)
                remaining = max(0, length - elapsed)

                # IMPORTANT: send timer to the requester AND broadcast it
                # so both clients stay synced.
                self.send(f"p{remaining}\x00".encode("utf-8"))
                self.relay_raw_to_room(room_name, f"p{remaining}", include_self=False)
            return

        # --- Ignore "00" junk (NOT PMs for this protocol) ---
        if packet.startswith("00"):
            room_name = USERS[self.account_id].get("room")
            if room_name != "_":
                print(f"[00-IN-GAME] from {wire_id(self.account_id)}: {packet!r}")
            self.send_private_lobby(packet)  # will no-op in game
            return



        print(f"[?] Unhandled: {repr(packet)}")

    def handle(self):
        self.username = None
        self.account_id = None

        print(f"[+] Connected: {self.client_address}")

        buf = ""
        try:
            while True:
                data = self.request.recv(4096)
                if not data:
                    break

                chunk = data.decode("utf-8", errors="ignore")
                buf += chunk

                # packets delimited by \x00
                while "\x00" in buf:
                    packet, buf = buf.split("\x00", 1)
                    self.handle_packet(packet)

        finally:
            if self.account_id and self.account_id in USERS:
                self.remove_user(self.account_id)

###############################################################################
# Server
###############################################################################

class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

with ThreadedTCPServer(("0.0.0.0", 6123), FlashGameHandler) as server:
    server.rooms = {
        "_": {"name": "_", "players": set(), "settings_string": "", "round_start": None, "round_length": 600}
    }
    print("[*] Listening on port 6123...")
    server.serve_forever()

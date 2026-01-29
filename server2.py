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
    # Remove NULL bytes, Padding (\x01), and surrounding whitespace
    # This fixes the "Unhandled: 03..." error
    return name.replace("\x00", "").replace("\x01", "").strip()

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
    
    # 1. NAME (20 chars)
    # Filter to ASCII, remove #, pad to 20
    raw = (u["username"] or "Player").replace("#", "")
    clean = "".join(c for c in raw if 32 <= ord(c) <= 126)
    name_20 = ("#" + clean).rjust(20, "#")[-20:]

    # 2. HEADER (Strictly 35 chars)
    # We construct it, then slice it to be 100% sure.
    # 00(Wpn) + 100(HP) + Name + 1(Gen) + 01(Head) + 01(Color) + 01(Body) + 01(Color) + 0(Team)
    header_raw = "00100" + name_20 + "1010101010"
    header_35 = header_raw[:35].ljust(35, "0")

    # 3. STATS & WEAPONS (The Crash Fix)
    # We manually type the string to guarantee 4 semicolons.
    # Stats: Score=0; Kills=0; Deaths=0; Bounty=0;
    # Weapons: 000 (Pistol)
    # Wanted: 0
    
    # This string explicitly includes the missing semicolon
    variable_data = "0;0;0;0;0000"

    # 4. ASSEMBLE
    # U + WireID + Header + Data + Null
    payload = "U" + wire_id(account_id) + header_35 + variable_data + "\x00"
    
    # Debug to verify the fix
    print(f"[DEBUG-FINAL] Semicolon Check: {variable_data}")
    print(f"[DEBUG-FINAL] Full Payload: {payload!r}")
    
    return payload.encode("utf-8")


def spawn_packet(account_id: str, x=200, y=200, direction=0, hp=100):
    # NOTE: opcode MUST be 1 char before the 3-char sender id
    # Using "1" here as a safe framing match; payload format may still need tuning.
    return f"1{wire_id(account_id)}{x};{y};{direction};{hp}\x00".encode("utf-8")

def spawn_player_packet(acc_id, x=200, y=200, direction=0, hp=100):
    return f"n{wire_id(acc_id)}{x},{y},{direction},{hp}\x00".encode("utf-8")



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
        Formerly restricted to lobby only. 
        NOW: Allowed in-game because Boxhead uses this for P2P state syncing.
        """
        # Must be at least: "00" + 3-digit id + "9x"
        if len(packet) < 6:
            return

        # --- DELETE OR COMMENT OUT THIS BLOCK ---
        # room_name = USERS[self.account_id].get("room")
        # if room_name != "_":
        #    return
        # ----------------------------------------

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
            # Optional: Don't print this for every packet to reduce log spam
            # print(f"[PM] Target {target_wire} not online")
            return

        out = f"M{wire_id(self.account_id)}{payload}\x00".encode("utf-8")
        try:
            USERS[target_acc]["socket"].sendall(out)
            # Optional: Comment this out if logs get too spammy
            # print(f"[PM] {wire_id(self.account_id)} -> {target_wire}: {out!r}")
        except OSError:
            pass


    def relay_raw_to_room(self, room_name: str, raw_packet: str, include_self: bool = False):

        if not room_name or room_name not in self.server.rooms:
            return
        out = (raw_packet + "\x00").encode("utf-8")
        print(f"[relay_raw] {wire_id(self.account_id)} -> room {room_name}: {raw_packet!r}")
        for peer_acc in self.server.rooms[room_name]["players"]:
            if not include_self and peer_acc == self.account_id:
                continue
            try:
                USERS[peer_acc]["socket"].sendall(out)
            except OSError:
                pass

    def relay_state_to_room(self, room_name, packet):
        if not room_name or room_name not in self.server.rooms:
            return
        
        # Logic: 
        # If it's a movement packet (starts with 1), prepend opcode + ID.
        # If it's a P2P packet (starts with M), it's already formatted.
        
        sender_id = wire_id(self.account_id)
        
        # Standard Movement/State Packet (Opcode 1 or 8)
        # Incoming: "1050..." -> Outgoing: "1001050..."
        if packet[0] in ('1', '8'): 
            out = f"{packet[0]}{sender_id}{packet[1:]}\x00".encode("utf-8")
        else:
            # Pass through other packets (like M...) unmodified if they have ID
            # But usually, relay logic is specific to movement.
            # If you are blindly relaying everything, use the safe format:
            # (This is the safest fallback for unknown opcodes)
            out = f"{packet[0]}{sender_id}{packet[1:]}\x00".encode("utf-8")

        room = self.server.rooms[room_name]
        for peer_acc in room["players"]:
             if peer_acc == self.account_id:
                 continue
             try:
                 USERS[peer_acc]["socket"].sendall(out)
             except:
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
        
        if packet.startswith(("1", "8")):
            USERS[self.account_id]["last_state"] = packet



        # AUTH REQUEST
        elif packet.startswith("09"):
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
        elif not getattr(self, "account_id", None) or self.account_id not in USERS:
            # ignore any pre-auth junk
            return

        # JOIN ROOM
        elif packet.startswith("03"):
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
            self.send(f"6{wire_id(self.account_id)}100000000000\x00".encode("utf-8"))


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

                # "Spawn" via last known real state packet (do NOT invent a semicolon payload)
                # 1) Send existing players' last_state to the joiner (so they appear immediately if they have moved once)
                for peer_acc in room["players"]:
                    if peer_acc == self.account_id:
                        continue
                    last = USERS.get(peer_acc, {}).get("last_state")
                    if last:
                        # last is like: "10575001250000" (no sender id inside it)
                        # relay_state_to_room injects sender, but here we want "from peer_acc -> self"
                        opcode = last[:1]
                        payload = last[1:]
                        self.send(f"{opcode}{wire_id(peer_acc)}{payload}\x00".encode("utf-8"))

                # 2) Send joiner’s last_state to everyone else (if they already emitted one)
                my_last = USERS.get(self.account_id, {}).get("last_state")
                if my_last:
                    opcode = my_last[:1]
                    payload = my_last[1:]
                    # encode() creates bytes
                    out = f"{opcode}{wire_id(self.account_id)}{payload}\x00".encode("utf-8")
                    for peer_acc in room["players"]:
                        if peer_acc == self.account_id:
                            continue
                        try:
                            # FIX 1: removed .encode("utf-8") because 'out' is already bytes
                            USERS[peer_acc]["socket"].sendall(out)
                        except OSError:
                            pass

                # --- SPAWN EXISTING PLAYERS FOR JOINER ---
                for peer_acc in room["players"]:
                    if peer_acc == self.account_id:
                        continue
                    # REMOVED: self.send(spawn_packet(peer_acc)) <--- DELETE THIS LINE
                    
                    # Only send the "Spawn Ready" signal (Opcode 6)
                    self.send(f"6{wire_id(peer_acc)}100000000000\x00".encode("utf-8"))

                # --- SPAWN JOINER FOR EXISTING PLAYERS ---
                # REMOVED: spawn = spawn_packet(self.account_id) <--- DELETE THIS LINE
                
                for peer_acc in room["players"]:
                    if peer_acc == self.account_id:
                        continue
                    # REMOVED: USERS[peer_acc]["socket"].sendall(spawn) <--- DELETE THIS LINE
                    
                    # Only send the "Spawn Ready" signal
                    USERS[peer_acc]["socket"].sendall(
                        f"6{wire_id(self.account_id)}100000000000\x00".encode("utf-8")
                    )

        # ROOM LIST REQUEST
        elif packet == "01":
            self.send(build_room_list_bytes(self.server))
            return

        # ROOM INFO REQUEST
        elif packet.startswith("04"):
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
        elif packet.startswith("02"):
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

            # Tell creator it joined
            self.send(f"C{wire_id(self.account_id)}\x00".encode("utf-8"))

            # Now initialize creator like a game-room joiner (same as JOIN ROOM non-lobby path)
            room = self.server.rooms[room_name]
            remaining = room["round_length"]
            self.send(f"p{remaining}\x00".encode("utf-8"))
            self.send(f"s{room['settings_string']}\x00".encode("utf-8"))
            self.send(f"R{wire_id(self.account_id)}\x00".encode("utf-8"))
            self.send(f"G{wire_id(self.account_id)}\x00".encode("utf-8"))
            self.send(f"I{wire_id(self.account_id)}\x00".encode("utf-8"))

            # Send creator's own game handshake
            self.send(game_user_packet(self.account_id))

            broadcast_room_list_to_lobby(self.server)
            return


        # PING ECHO
        elif packet.startswith("9?"):
            idx = packet[2:]
            msg = f"M{wire_id(self.account_id)}9?{idx}\x00"
            self.send(msg.encode("utf-8"))
            return

        #######################################################################
        # GAME / LOBBY TRAFFIC
        #######################################################################

        # 1-char opcodes that need sender injection
        elif packet.startswith(("1", "8", "4")):
            room_name = USERS[self.account_id].get("room")
            if room_name:
                self.relay_state_to_room(room_name, packet)
            return
        
        # --- SPAWN READY ---
        elif packet.startswith("6"):
            room_name = USERS[self.account_id].get("room")
            if room_name:
                self.relay_state_to_room(room_name, packet)
            return
        
        # --- PLAYER DEATH / DESPAWN ---
        elif packet.startswith("7"):
            room_name = USERS[self.account_id].get("room")
            if room_name:
                self.relay_state_to_room(room_name, packet)
            return



        # 2-char 0* opcodes that should be forwarded intact (no framing injection)
        elif packet.startswith(("0k", "0q")):
            room_name = USERS[self.account_id].get("room")
            if room_name:
                self.relay_raw_to_room(room_name, packet, include_self=False)
            return



        # --- CHAT / ENCRYPTED "9..." (NOT ping) ---
        # Client sends: 9<encrypted>
        # Peer must receive: M<ID>9<encrypted>
        elif packet.startswith("9") and not packet.startswith("9?"):
            room_name = USERS[self.account_id].get("room")
            if room_name:
                self.relay_chat9_to_room(room_name, packet, include_self=True)
            return

        # --- customization ---
        elif packet.startswith("0d"):
            room_name = USERS[self.account_id].get("room")
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
        elif packet == "p":
            room_name = USERS[self.account_id].get("room")
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
        
        elif packet.startswith("00"):
            try:
                # Format: 00 + TargetID(3) + Payload
                if len(packet) < 5: return

                target_wire = packet[2:5]
                payload = packet[5:]

                # Relay as "M" packet to target
                # Client sends "00", Receiver expects "M"
                for acc, u in USERS.items():
                    if wire_id(acc) == target_wire:
                        sender_wire = wire_id(self.account_id)
                        out = f"M{sender_wire}{payload}\x00".encode("utf-8")
                        u["socket"].sendall(out)
                        # print(f"[P2P] Relayed {sender_wire}->{target_wire}") # Uncomment to verify
                        break
            except Exception as e:
                print(f"P2P Error: {e}")


        else:
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

                # Append new data to buffer
                chunk = data.decode("utf-8", errors="ignore")
                buf += chunk

                # Process ALL complete packets in the buffer
                while "\x00" in buf:
                    # split ONLY on the first null terminator
                    packet, buf = buf.split("\x00", 1)
                    
                    if not packet:
                        continue
                        
                    # Clean and handle the packet
                    try:
                        self.handle_packet(packet)
                    except Exception as e:
                        print(f"Error handling packet {packet!r}: {e}")

        except Exception as e:
            print(f"Connection error: {e}")
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

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
            parts = line.split(";")
            
            # Map parts to names safely
            # Format: user;hash;id;level;gender;h_m;h_c;b_m;b_c;bounty;kills;deaths;wins;rounds;wanted
            u_data = {
                "password_hash": parts[1] if len(parts) > 1 else "",
                "account_id": parts[2] if len(parts) > 2 else "0",
                "level": parts[3] if len(parts) > 3 else "0",
                "gender": parts[4] if len(parts) > 4 else "0",
                "head_model": parts[5] if len(parts) > 5 else "00",
                "head_color": parts[6] if len(parts) > 6 else "00",
                "body_model": parts[7] if len(parts) > 7 else "00",
                "body_color": parts[8] if len(parts) > 8 else "00",
                "bounty": parts[9] if len(parts) > 9 else "0",
                "kills": parts[10] if len(parts) > 10 else "0",
                "deaths": parts[11] if len(parts) > 11 else "0",
                "wins": parts[12] if len(parts) > 12 else "0",
                "rounds": parts[13] if len(parts) > 13 else "0",
                "wanted": parts[14] if len(parts) > 14 else "0",
            }
            
            USER_DB[parts[0]] = u_data
            try:
                max_id = max(max_id, int(u_data["account_id"]))
            except ValueError:
                pass
    next_id = max_id + 1

def save_user(username: str, password: str) -> str:
    global next_id
    h = md5_hash(password)
    acc_id = str(next_id)
    next_id += 1

    # Create new user dict with defaults
    new_user = {
        "password_hash": h,
        "account_id": acc_id,
        "level": "0", "gender": "0",
        "head_model": "00", "head_color": "00",
        "body_model": "00", "body_color": "00",
        "bounty": "0", "kills": "0", "deaths": "0",
        "wins": "0", "rounds": "0", "wanted": "0"
    }

    USER_DB[username] = new_user

    # Append to file
    with open(DB_FILE, "a", encoding="utf-8") as f:
        line = (f"{username};{h};{acc_id};"
                f"{new_user['level']};{new_user['gender']};"
                f"{new_user['head_model']};{new_user['head_color']};"
                f"{new_user['body_model']};{new_user['body_color']};"
                f"{new_user['bounty']};{new_user['kills']};"
                f"{new_user['deaths']};{new_user['wins']};"
                f"{new_user['rounds']};{new_user['wanted']}\n")
        f.write(line)
    return acc_id

def save_all_users():
    """Overwrites the DB file with the current state of USER_DB"""
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            for uname, data in USER_DB.items():
                line = (f"{uname};{data['password_hash']};{data['account_id']};"
                        f"{data['level']};{data['gender']};"
                        f"{data['head_model']};{data['head_color']};"
                        f"{data['body_model']};{data['body_color']};"
                        f"{data['bounty']};{data['kills']};"
                        f"{data['deaths']};{data['wins']};"
                        f"{data['rounds']};{data['wanted']}\n")
                f.write(line)
    except Exception as e:
        print(f"Error saving DB: {e}")

###############################################################################
# Packet builders (MATCH AS3 EXPECTATIONS)
###############################################################################

def auth_packet(account_id: str) -> bytes:
    username = USERS[account_id]["username"]
    name20 = fmt_name_20(username)
    
    # Access by Key
    data = USER_DB[username] 
    
    level = data["level"]
    gender = data["gender"]
    head_model = data["head_model"]
    head_color = data["head_color"]
    body_model = data["body_model"]
    body_color = data["body_color"]
    
    stats5 = f"{data['bounty']};{data['kills']};{data['deaths']};{data['wins']};{data['rounds']}"
    wanted = data["wanted"]

    payload = f"{name20}{level}{gender}{head_model}{head_color}{body_model}{body_color}{stats5}{wanted}"
    return f"A{wire_id(account_id)}{payload}\x00".encode("utf-8")

def lobby_user_packet(account_id: str) -> bytes:
    if USERS[account_id].get("room") != "_":
        return b""

    username = USERS[account_id]["username"]
    name20 = fmt_name_20(username)
    data = USER_DB[username]
    
    # stats6: level;bounty;kills;deaths;wins;rounds
    stats6 = f"{data['level']};{data['bounty']};{data['kills']};{data['deaths']};{data['wins']};{data['rounds']}"
    wanted = data["wanted"]

    payload = f"{name20}{stats6}{wanted}"
    return f"U{wire_id(account_id)}{payload}\x00".encode("utf-8")


def game_user_packet(account_id: str) -> bytes:
    u = USERS[account_id]
    username = u["username"]
    name20 = fmt_name_20(username)
    data = USER_DB[username]
    
    # 1. FIXED HEADER (Exactly 35 characters)
    weapon_2 = "00"
    hp_3 = "100"
    
    # Name is strictly 20 chars (handled by fmt_name_20)
    
    # Graphics and Team (10 chars)
    # Using zfill(2) ensures single digits become "01", "02", maintaining strict lengths
    gender_1 = str(data.get('gender', '1'))[:1]
    hm_2 = str(data.get('head_model', '0')).zfill(2)
    hc_2 = str(data.get('head_color', '0')).zfill(2)
    bm_2 = str(data.get('body_model', '0')).zfill(2)
    bc_2 = str(data.get('body_color', '0')).zfill(2)
    team_1 = "0"
    
    fixed_header = weapon_2 + hp_3 + name20 + gender_1 + hm_2 + hc_2 + bm_2 + bc_2 + team_1

    # 2. DELIMITED STATS (Exactly 4 semicolons required!)
    score = "10000"
    kills = str(data.get("kills", "0"))
    deaths = str(data.get("deaths", "0"))
    bounty = "0"
    stats_string = f"{score};{kills};{deaths};{bounty};"
    
    # 3. UPGRADES (Chunks of 3: 2 for weapon, 1 for flag). We can leave it blank.
    upgrades = ""
    
    # 4. WANTED BIT (Always the last character)
    wanted = str(data.get("wanted", "0"))

    # Combine all parts
    variable_data = stats_string + upgrades + wanted
    
    # Construct: U + WireID + FixedHeader + VariableData + Null
    payload = "U" + wire_id(account_id) + fixed_header + variable_data + "\x00"
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
        
        sender_wire = wire_id(self.account_id)
        
        # Format the packet with Sender ID
        if packet.startswith("0l"):
            out_str = f"0l{sender_wire}{packet[2:]}"
        else:
            out_str = f"{packet[0]}{sender_wire}{packet[1:]}"
            
        out = out_str.encode("utf-8") + b"\x00"

        room = self.server.rooms[room_name]
        sent_count = 0
        
        for peer_acc in room["players"]:
             if peer_acc == self.account_id: continue
             if peer_acc in USERS:
                 try: 
                     USERS[peer_acc]["socket"].sendall(out)
                     sent_count += 1
                 except: pass
        
        # DEBUG PRINT
        if sent_count > 0:
            print(f"[RELAY] Relayed {out_str!r} to {sent_count} peers")
        else:
            # If you see this, it means you are alone in the room or peers are diconnected
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

    def broadcast_to_room(self, message_bytes):
        """Sends bytes to everyone in the current user's room and logs it."""
        room_name = USERS.get(self.account_id, {}).get("room")
        if not room_name or room_name not in self.server.rooms:
            return
            
        players = self.server.rooms[room_name]["players"]
        for p_acc in list(players):
            if p_acc in USERS:
                try:
                    # Using sendall directly but adding a print so you can see it
                    USERS[p_acc]["socket"].sendall(message_bytes)
                except:
                    continue
        # This allows you to see the broadcast in your console logs
        print(f"[BROADCAST] {repr(message_bytes)}")

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
        elif packet.startswith("09"):
            creds = packet[2:]
            if ";" not in creds:
                self.send(b"10;0;Bad format\x00")
                return

            username, password = creds.split(";", 1)
            pwd_hash = md5_hash(password)

            self.send(b"00;1\x00")
            print("[<] Sent delayed handshake")

            if username in USER_DB:
                # Access by key
                stored_hash = USER_DB[username]["password_hash"]
                acc_id = USER_DB[username]["account_id"]
                
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
            # Blasting "100" into the positional and state slots 
            self.send(f"M{wire_id(self.account_id)}6100\x00".encode("utf-8"))


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
                    # Blasting "100" into the positional and state slots 
                    self.send(f"M{wire_id(peer_acc)}6100\x00".encode("utf-8"))

                # --- SPAWN JOINER FOR EXISTING PLAYERS ---
                # REMOVED: spawn = spawn_packet(self.account_id) <--- DELETE THIS LINE
                
                for peer_acc in room["players"]:
                    if peer_acc == self.account_id:
                        continue
                    # REMOVED: USERS[peer_acc]["socket"].sendall(spawn) <--- DELETE THIS LINE
                    
                    # Only send the "Spawn Ready" signal
                    USERS[peer_acc]["socket"].sendall(
                        # Blasting "100" into the positional and state slots 
                        self.send(f"M{wire_id(peer_acc)}6100\x00".encode("utf-8"))
                    )

        # === NEW: RELAY MOVEMENT & ACTIONS ===
        # === MOVEMENT (Types 1, 4) ===
        elif packet.startswith("1") or packet.startswith("4"):
            if self.account_id not in USERS: return
            
            # FIX: Force slot to be a string (e.g. 1 -> "1")
            slot = str(USERS[self.account_id]["slot"])
            
            current_room_name = USERS[self.account_id].get("room")
            if not current_room_name or current_room_name not in self.server.rooms:
                return

            room = self.server.rooms[current_room_name]
            room["players"].add(self.account_id)

            raw = packet[1:] # Strip Type (e.g. "1")

            if packet.startswith("1") and len(packet) >= 11:
            # Opcode 1 contains the 10-digit position right after the "1"
            # Example: '10541600657...' -> '0541600657'
                USERS[self.account_id]["last_pos"] = packet[1:11]
            
            if len(raw) >= 10:
                # 1. READ 5 DIGITS (e.g., "05175")
                x_str = raw[0:5]
                y_str = raw[5:10]
                rest = raw[10:]
                
                try:
                    # 2. 5-Digit Precision Fix
                    val_x = int(x_str)
                    val_y = int(y_str)
                    
                    x_final = str(val_x).zfill(5)
                    y_final = str(val_y).zfill(5)

                    # 3. CONSTRUCT PAYLOAD
                    # Structure: M + Slot(3) + Type(1) + X(5) + Y(5) + Rest
                    # We ensure slot is 3 chars (e.g. "1" -> "001")
                    safe_slot = slot.zfill(3)
                    
                    payload = "M" + safe_slot + packet[0] + x_final + y_final + rest
                        
                except ValueError:
                    # Fallback
                    payload = "M" + slot.zfill(3) + packet
            else:
                payload = "M" + slot.zfill(3) + packet

            # 4. BROADCAST
            for peer_acc in list(room["players"]):
                if peer_acc == self.account_id: continue
                if peer_acc in USERS:
                    try: USERS[peer_acc]["socket"].sendall(payload.encode("utf-8") + b"\x00")
                    except: pass

        # === 2. SHOOTING (Types 2, 3) ===
        elif packet.startswith("2") or packet.startswith("3"):
            # Retrieve room safely
            if self.account_id in USERS:
                current_room_name = USERS[self.account_id].get("room")
                if current_room_name and current_room_name in self.server.rooms:
                    room = self.server.rooms[current_room_name]
                    
                    # Just insert ID and forward
                    payload = packet[0] + self.account_id.zfill(3) + packet[1:]
                    
                    for peer_acc in list(room["players"]):
                        if peer_acc == self.account_id: continue
                        if peer_acc in USERS:
                            try: USERS[peer_acc]["socket"].sendall(payload.encode("utf-8") + b"\x00")
                            except: pass

        # ROOM LIST REQUEST
        elif packet == "01":
            self.send(build_room_list_bytes(self.server))
            return
        
        # === ROOM INFO REQUEST (04) ===
        elif packet.startswith("04"):
            room_name = normalize_room_name(packet[2:])
            room = self.server.rooms.get(room_name)
            
            if not room or room_name == "_":
                return

            # Pull header saved during 02 (Fallback to "100" just in case)
            header = room.get("header", "100")
            gameType = header[0] if len(header) > 0 else "1"
            useCustom = header[1] if len(header) > 1 else "0"
            
            # The map ID is the first character in the settings_string (e.g. "D" from "DECBAGF")
            settings = room.get("settings_string", "")
            mapID = settings[0] if settings else "A"
            
            players = f"{len(room['players']):02d}"

            # Calculate time
            length = room.get("round_length", 600)
            start = room.get("round_start") or time.time()
            elapsed = int(time.time() - start)
            remaining = max(0, length - elapsed)

            msg = f"04{gameType}{useCustom}{mapID}{players}{remaining}\x00"
            self.send(msg.encode("utf-8"))
            return

        # === CREATE ROOM (02) ===
        elif packet.startswith("02"):
            self.leave_current_room(self.account_id)

            payload = packet[2:]
            if ";" not in payload:
                return

            header = payload[:3]  # gameType(1)/useCustom(1)/isPrivate(1)
            rest = payload[3:]
            room_part, settings = rest.split(";", 1)

            room_name = normalize_room_name(room_part)
            settings = settings.strip()

            self.server.rooms[room_name] = {
                "name": room_name,
                "settings_string": settings,
                "header": header,  # FIX: Store the header so 04 can use it!
                "players": {self.account_id},
                "round_start": time.time(),
                "round_length": 600,
            }
            USERS[self.account_id]["room"] = room_name

            print(f"[+] Created room '{room_name}'")

            # Tell creator it joined
            self.send(f"C{wire_id(self.account_id)}\x00".encode("utf-8"))

            # Now initialize creator like a game-room joiner
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


        # Catches Move(1), Rotate(8), Shoot(4), and Loadout(0l)
        elif packet.startswith(("8")) or packet.startswith("0l"):
            # 1. Save state (moved from top of function)
            if packet.startswith("8"):
                    current_hp = USERS[self.account_id].get("hp", 100)
                    if current_hp <= 0:
                        USERS[self.account_id]["hp"] = 100
                        print(f"[SYSTEM] Reset HP for {self.username} (Respawn via Opcode 8)")
            
            # 2. Relay (now guaranteed to run)
            room_name = USERS[self.account_id].get("room")
            if room_name:
                self.relay_state_to_room(room_name, packet)
            return
        
        elif packet.startswith("6"):
            if len(packet) < 8: return
            attacker_wire = packet[1:4]
            weapon_id = packet[4:6]
            try:
                damage = int(packet[6:8])
            except: return

            target_acc = self.account_id
            if not target_acc or target_acc not in USERS: return

            # GUARD: Stop if already dead to prevent infinite kill loops
            current_hp = USERS[target_acc].get("hp", 100)
            if current_hp <= 0: return

            # Calculate new HP
            new_hp = max(0, current_hp - damage)
            USERS[target_acc]["hp"] = new_hp
            
            target_wire = f"{USERS[target_acc].get('slot', 0):03d}"

            # 1. Broadcast HP Update (M...6...)
            # We construct the FULL message here because your function just sends what it's given
            hp_out = f"M{target_wire}6{new_hp:03d}\x00".encode("utf-8")
            self.broadcast_to_room(hp_out)

            # 2. If dead, handle the Kill sequence
            if new_hp == 0:
                print(f"[DEBUG] Target {target_wire} KILLED by {attacker_wire}")
                
                room_name = USERS[target_acc].get("room")
                room = self.server.rooms.get(room_name)
                b_idx, pos = 0, "0050000500"
                if room:
                    b_idx = room.get("bounty_idx", 0)
                    room["bounty_idx"] = (b_idx + 1) % 100
                    pos = USERS[target_acc].get("last_pos", pos)

                # Construct the FULL Kill packet (M + Target + 7 + Details)
                kill_out = f"M{target_wire}7{attacker_wire}{weapon_id}0{b_idx:02d}{pos}\x00".encode("utf-8")
                
                # This sends to everyone in the room, including the victim
                self.broadcast_to_room(kill_out)

        # --- PLAYER DEATH / DESPAWN ---
        elif packet.startswith("7"):
            room_name = USERS[self.account_id].get("room")
            if room_name:
                self.relay_state_to_room(room_name, packet)
            return



        # 2-char 0* opcodes that should be forwarded intact (no framing injection)
        elif packet.startswith(("0k", "0q")):
            room_name = USERS[self.account_id].get("room")
            
            # --- RESET HP ON RESPAWN ---
            if packet == "0k1":
                if self.account_id in USERS:
                    USERS[self.account_id]["hp"] = 100
                    print(f"[SYSTEM] Reset HP for {self.username} (Respawn)")
            
            if room_name:
                # include_self=True ensures your client gets the 'respawn' confirmation
                self.relay_raw_to_room(room_name, packet, include_self=True)
            return



        # --- CHAT / ENCRYPTED "9..." (NOT ping) ---
        # Client sends: 9<encrypted>
        # Peer must receive: M<ID>9<encrypted>
        elif packet.startswith("9") and not packet.startswith("9?"):
            room_name = USERS[self.account_id].get("room")
            if room_name:
                self.relay_chat9_to_room(room_name, packet, include_self=True)
            return

        # === CUSTOMIZATION (0d) ===
        elif packet.startswith("0d"):
            # Ensure the user is authenticated
            if not getattr(self, "username", None) or self.username not in USER_DB:
                return
            
            cat = packet[2:3]   # Category (0=Head, 1=Body, 2=Gender)
            data = packet[3:]   # The values
            
            db_user = USER_DB[self.username]
            changed = False
            
            if cat == "0" and len(data) >= 4:  # Head
                db_user["head_model"] = data[0:2]
                db_user["head_color"] = data[2:4]
                changed = True
                
            elif cat == "1" and len(data) >= 4:  # Body
                db_user["body_model"] = data[0:2]
                db_user["body_color"] = data[2:4]
                changed = True
                
            elif cat == "2" and len(data) >= 1:  # Gender
                db_user["gender"] = data[0:1]
                changed = True
                
            if changed:
                save_all_users()
                print(f"[*] Saved customization for {self.username}: {packet}")
                
                # If they are currently in a game room, you might need to broadcast 
                # their new look to other players so they update visually in real-time.
                current_room_name = USERS[self.account_id].get("room")
                if current_room_name and current_room_name != "_":
                    # Generate an updated game U packet for myself and send it to peers
                    my_update = game_user_packet(self.account_id)
                    for peer_acc in self.server.rooms[current_room_name]["players"]:
                        if peer_acc != self.account_id:
                            try:
                                USERS[peer_acc]["socket"].sendall(my_update)
                            except OSError:
                                pass

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

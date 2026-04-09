import socketserver
import os
import hashlib
import time
import math
import random
import xml.etree.ElementTree as ET
from xml.dom import minidom

DB_FILE = "users.db"

# username -> (md5_hash, account_id_string)
USER_DB = {}
USERS = {}  # account_id -> dict(socket, username, slot, room)

###############################################################################
# Helpers
###############################################################################

def update_most_wanted_xml():
    """Generates the mostwanted.xml based on the current USER_DB in memory"""
    try:
        # 1. Convert USER_DB dictionary to a list and sort by bounty
        # Format: user;hash;id;level;gender;head_model;head_color;body_model;body_color;bounty...
        users_list = []
        for uname, data in USER_DB.items():
            users_list.append({
                "name": uname,
                "h_model": data.get("head_model", "0"),
                "h_color": data.get("head_color", "0"),
                "b_model": data.get("body_model", "0"),
                "b_color": data.get("body_color", "0"),
                "bounty": int(data.get("bounty", 0))
            })

        # 2. Sort by bounty points (highest first) and take top 100
        users_list.sort(key=lambda x: x['bounty'], reverse=True)
        top_users = users_list[:100]

        # 3. Build the XML structure
        root = ET.Element("rsp")
        root.set("stat", "ok")
        users_node = ET.SubElement(root, "users")

        for u in top_users:
            user_node = ET.SubElement(users_node, "user")
            ET.SubElement(user_node, "name").text = u["name"]
            ET.SubElement(user_node, "bountyPoints").text = str(u["bounty"])
            
            # Head Node (removing leading zeros)
            head_node = ET.SubElement(user_node, "head")
            ET.SubElement(head_node, "color").text = str(int(u["h_color"]))
            ET.SubElement(head_node, "model").text = str(int(u["h_model"]))
            
            # Body Node (removing leading zeros)
            body_node = ET.SubElement(user_node, "body")
            ET.SubElement(body_node, "color").text = str(int(u["b_color"]))
            ET.SubElement(body_node, "model").text = str(int(u["b_model"]))

        # 4. Save to file
        xml_str = ET.tostring(root, encoding='utf-8')
        pretty_xml = minidom.parseString(xml_str).toprettyxml(indent="	")
        
        # Adjust path if needed, e.g., r"C:\Users\austi\OneDrive\Desktop\mostwanted.xml"
        with open("mostwanted.xml", "w", encoding="utf-8") as f:
            f.write(pretty_xml)
            
    except Exception as e:
        print(f"[!] Error updating Most Wanted XML: {e}")

def create_bounty_string(crate_type, index, pos):
    """
    Creates a 13-character string for a single bounty item.
    [Type: 1char][Index: 2char][Pos: 10char]
    
    :param pos: A 10-character string (e.g., '0045001200')
    """
    # Type: 1 character (e.g., '1' for health)
    type_part = str(crate_type)[0]
    
    # Index: 2 characters (e.g., '01')
    index_part = f"{index:02d}"
    
    # Pos: Use the 10-character string directly
    return f"{type_part}{index_part}{pos}"

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
            # Format: user;hash;id;level;gender;h_m;h_c;b_m;b_c;bounty;kills;deaths;wins;losses;wanted
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
                "losses": parts[13] if len(parts) > 13 else "0",
                "wanted": parts[14] if len(parts) > 14 else "0",
            }
            
            USER_DB[parts[0].lower()] = u_data  # Convert key to lowercase
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

    username = username.lower()

    # if username == "schooler":
    #     lvl = "1"
    # else:
    #     lvl = "0"
    lvl = "0"

    # Create new user dict with defaults
    new_user = {
        "password_hash": h,
        "account_id": acc_id,
        "level": lvl, "gender": "0",
        "head_model": "00", "head_color": "00",
        "body_model": "00", "body_color": "00",
        "bounty": "0", "kills": "0", "deaths": "0",
        "wins": "0", "losses": "0", "wanted": "1"
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
                f"{new_user['losses']};{new_user['wanted']}\n")
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
                        f"{data['losses']};{data['wanted']}\n")
                f.write(line)
        update_most_wanted_xml()
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
    
    stats5 = f"{data['kills']};{data['deaths']};{data['wins']};{data['losses']};{data['bounty']}"
    wanted = data["wanted"]

    payload = f"{name20}{level}{gender}{head_model}{head_color}{body_model}{body_color}{stats5}{wanted}"
    return f"A{wire_id(account_id)}{payload}\x00".encode("utf-8")

def lobby_user_packet(account_id: str) -> bytes:
    if USERS[account_id].get("room") != "_":
        return b""

    username = USERS[account_id]["username"]
    name20 = fmt_name_20(username)
    data = USER_DB[username]
    

    stats6 = f"{data['kills']};{data['deaths']};{data['wins']};{data['losses']};{data['bounty']};{data['level']}"
    wanted = data["wanted"]

    payload = f"{name20}{stats6}{wanted}"
    return f"U{wire_id(account_id)}{payload}\x00".encode("utf-8")


def game_user_packet(account_id: str) -> bytes:
    u = USERS[account_id]
    username = u["username"]
    name20 = fmt_name_20(username)
    data = USER_DB[username]
    
    # Get the live stats the server is already tracking
    stats = u.get("stats", {"score": 10000, "kills": 0, "deaths": 0, "bounty_points": 0})
    
    # 1. FIXED HEADER (Exactly 35 characters)
    weapon_2 = "00"
    hp_3 = f"{u.get('hp', 100):03d}"  # <-- Use live HP padded to 3 digits!
    
    # Graphics and Team (10 chars)
    gender_1 = str(data.get('gender', '1'))[:1]
    hm_2 = str(data.get('head_model', '0')).zfill(2)
    hc_2 = str(data.get('head_color', '0')).zfill(2)
    bm_2 = str(data.get('body_model', '0')).zfill(2)
    bc_2 = str(data.get('body_color', '0')).zfill(2)
    team_1 = "0"
    
    fixed_header = weapon_2 + hp_3 + name20 + gender_1 + hm_2 + hc_2 + bm_2 + bc_2 + team_1

    # 2. DELIMITED STATS (Exactly 4 semicolons required!)
    # <-- Use the live stats dictionary instead of hardcoded 0s!
    score = str(stats.get("score", 10000))
    kills = str(stats.get("kills", 0))
    deaths = str(stats.get("deaths", 0))
    bounty = str(stats.get("bounty_points", 0))
    
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
        
        # --- NEW: Hide the room if it has 16 or more players! ---
        if count >= 16:
            continue 
            
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

    def check_timer(self, room_name):
        # 1. Ignore the lobby or missing rooms
        if room_name == "_" or not room_name:
            return
            
        room = self.server.rooms.get(room_name)
        if not room:
            return
            
        # 2. Start timer on the first packet received in the room
        if "round_start" not in room:
            room["round_start"] = time.time()
            print(f"[*] Round timer started for room: {room_name}")
            
        # 3. Check if 30 seconds have passed
        elapsed = time.time() - room["round_start"]
        if elapsed >= 600:
            print(f"[*] 10 MINUTES REACHED in {room_name}! Sending End Game packet.")
            awards_payload = self.get_awards_and_save_db(room_name)
            # Send the End Game packet
            end_game_packet = f"0r{awards_payload}\x00"
            self.relay_raw_to_room(room_name, end_game_packet, include_self=True)
            # Push the timer into the future so it doesn't spam
            room["round_start"] = time.time() + 30
            update_most_wanted_xml()

    def get_awards_and_save_db(self, room_name):
        room = self.server.rooms.get(room_name)
        if not room: return "000" * 5
        
        # 1. Gather all players' stats in the room
        players = []
        for acc_id in room["players"]:
            info = USERS.get(acc_id)
            if info and "slot" in info:
                p_data = info.setdefault("stats", {"score": 10000, "kills": 0, "deaths": 0, "bounty_points": 0}).copy()
                p_data["slot"] = f"{int(info['slot']):03d}"
                p_data["username"] = info["username"]
                players.append(p_data)
                
        if not players:
            return "001" * 5 
            
        # 2. Calculate Award Winners
        winner = max(players, key=lambda x: x["score"])
        winner_id = winner["slot"]
        hunter_id = max(players, key=lambda x: x["bounty_points"])["slot"] if len(players) >= 6 else winner_id
        prof_id = max(players, key=lambda x: x["kills"] if x["deaths"] == 0 else (x["kills"] / x["deaths"]))["slot"]
        poacher_id = max(players, key=lambda x: 0 if x["kills"] == 0 else ((x["score"] - 10000) / x["kills"]))["slot"]
        dummy_id = max(players, key=lambda x: x["deaths"])["slot"]
        
        # 3. UPDATE USER_DB DICTIONARY
        for p in players:
            uname = p["username"]
            if uname in USER_DB:
                db_user = USER_DB[uname]
                
                # Math applied cleanly
                db_user["kills"] = str(int(db_user.get("kills", "0")) + p["kills"])
                db_user["deaths"] = str(int(db_user.get("deaths", "0")) + p["deaths"])
                db_user["bounty"] = str(int(db_user.get("bounty", "0")) + p["bounty_points"])
                
                if p["username"] == winner["username"]:
                    db_user["wins"] = str(int(db_user.get("wins", "0")) + 1)
                else:
                    db_user["losses"] = str(int(db_user.get("losses", "0")) + 1)
                

        # 4. OVERWRITE USERS.DB EXACTLY MATCHING YOUR FORMAT
        try:
            with open(DB_FILE, "w", encoding="utf-8") as f:
                for uname, data in USER_DB.items():
                    # Format: user;hash;id;level;gender;h_m;h_c;b_m;b_c;bounty;kills;deaths;wins;losses;wanted
                    line = (f"{uname};{data.get('password_hash', '')};{data.get('account_id', '0')};"
                            f"{data.get('level', '0')};{data.get('gender', '0')};"
                            f"{data.get('head_model', '00')};{data.get('head_color', '00')};"
                            f"{data.get('body_model', '00')};{data.get('body_color', '00')};"
                            f"{data.get('bounty', '0')};{data.get('kills', '0')};"
                            f"{data.get('deaths', '0')};{data.get('wins', '0')};"
                            f"{data.get('losses', '0')};{data.get('wanted', '0')}\n")
                    f.write(line)
        except Exception as e:
            print(f"Error saving DB: {e}")
        
        # 5. Reset round stats
        for acc_id in room["players"]:
            if acc_id in USERS:
                USERS[acc_id]["stats"] = {"score": 10000, "kills": 0, "deaths": 0, "bounty_points": 0}
        
        return f"{winner_id}{hunter_id}{prof_id}{poacher_id}{dummy_id}"

    def handle_packet(self, packet: str):
        if not packet:
            return
        
        user_info = USERS.get(self.account_id)
        if user_info and "room" in user_info:
            self.check_timer(user_info["room"])

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
            username = username.lower()
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
                "hp": 100,
                "score": 1000, # Starting score or load from DB
                "kills": 0,
                "deaths": 0,
                "bounty_points": 0
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

            # --- NEW: ENFORCE 16 PLAYER CAP ---
            if room_name != "_" and room_name in self.server.rooms:
                if len(self.server.rooms[room_name]["players"]) >= 16:
                    print(f"[!] Room '{room_name}' is full! Bouncing {self.username} back to lobby.")
                    room_name = "_" # Rewrite their destination to the lobby

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

            if self.account_id in USERS:
                USERS[self.account_id]["stats"] = {
                    "score": 10000, 
                    "kills": 0, 
                    "deaths": 0, 
                    "bounty_points": 0
                }
                # Also reset temporary health/state if needed
                USERS[self.account_id]["hp"] = 100
                USERS[self.account_id]["last_state"] = None
                print(f"[*] Session reset for {self.username}. Score set to 10000.")

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

            length = room.get("round_length", 630)
            elapsed = int(time.time() - room["round_start"])
            remaining = max(0, length - elapsed)

            # Send timer to the joiner (and also send to everyone, so both clients stay synced)
            for peer_acc in room["players"]:
                try:
                    USERS[peer_acc]["socket"].sendall(f"p{remaining}\x00".encode("utf-8"))
                except OSError:
                    pass

            # --- NATIVE CRATE SYNC ---
            # 's' is the opcode for existingPickupsString, NOT settings!
            existing_crates_str = "".join([c["str"] for c in room.get("crates", {}).values()])
            self.send(f"s{existing_crates_str}\x00".encode("utf-8"))
            
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

                # --- SYNC EXISTING PLAYERS HEALTH & WEAPONS FOR JOINER ---
                for peer_acc in room["players"]:
                    if peer_acc == self.account_id:
                        continue
                    
                    # 1. Sync the exact live health bar (Opcode 8)
                    # Format: 8 + SystemAttacker(000) + Target(3) + HP(3)
                    live_hp = USERS[peer_acc].get("hp", 100)
                    health_sync = f"8000{wire_id(peer_acc)}{live_hp:03d}\x00"
                    self.send(health_sync.encode("utf-8"))
                    
                    # 2. Sync their currently equipped weapon (Opcode 0q)
                    # Format: M + Sender(3) + 0q + Weapon(2)
                    live_weapon = USERS[peer_acc].get("weapon", "00")
                    weapon_sync = f"M{wire_id(peer_acc)}0q{live_weapon}\x00"
                    self.send(weapon_sync.encode("utf-8"))

                # --- SPAWN JOINER FOR EXISTING PLAYERS ---
                # REMOVED: spawn = spawn_packet(self.account_id) <--- DELETE THIS LINE
                
                # --- SPAWN JOINER FOR EXISTING PLAYERS ---
                for peer_acc in room["players"]:
                    if peer_acc == self.account_id:
                        continue
                    
                    # Tell the existing peer that the joiner has spawned!
                    USERS[peer_acc]["socket"].sendall(
                        f"M{wire_id(self.account_id)}6100\x00".encode("utf-8")
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

            # Store last known position for deploying items
            if packet.startswith("1") and len(packet) >= 11:
                # Opcode 1 contains the 10-digit position right after the "1"
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
                    safe_slot = slot.zfill(3)
                    payload = "M" + safe_slot + packet[0] + x_final + y_final + rest
                        
                except ValueError:
                    # Fallback
                    payload = "M" + slot.zfill(3) + packet
            else:
                payload = "M" + slot.zfill(3) + packet

            # === NEW: DEPLOYABLE MANAGER (Opcode 4) ===
            # If this is a shooting packet, check if they fired a barricade/barrel
            if packet.startswith("4") and len(packet) >= 3:
                weapon_id = packet[1:3]
                
                # '18' = Barricade Planter, '19' = Barrel Planter
                if weapon_id in ["18", "19"]:
                    last_pos = USERS[self.account_id].get("last_pos")
                    
                    if last_pos and len(last_pos) >= 10:
                        # NEW DISCOVERY: The 5-digit number is a fixed-point CELL coordinate!
                        # Example: '06242' means 62.42 cells. The integer cell is just the first 3 digits!
                        cell_x_str = last_pos[0:3] 
                        cell_y_str = last_pos[5:8]
                        
                        # Increment the room's deployable index (00 to 99)
                        room.setdefault("deploy_idx", 0)
                        idx_str = f"{room['deploy_idx']:02d}"
                        room["deploy_idx"] = (room["deploy_idx"] + 1) % 100
                        
                        # "1" = Barricade, "0" = Barrel
                        dep_code = "1" if weapon_id == "18" else "0"
                        
                        # Construct 'n' packet: n + slot(3) + depCode(1) + index(2) + cellX(3) + cellY(3)
                        # Example result: n001100062010
                        n_packet = f"n{slot.zfill(3)}{dep_code}{idx_str}{cell_x_str}{cell_y_str}"
                        
                        print(f"\n[!!!] BROADCASTING DEPLOYABLE: {n_packet}\n")
                        
                        # Send the confirmation 'n' packet to EVERYONE
                        n_bytes = (n_packet + "\x00").encode("utf-8")
                        for peer_acc in list(room["players"]):
                            if peer_acc in USERS:
                                try:
                                    USERS[peer_acc]["socket"].sendall(n_bytes)
                                except OSError:
                                    pass
            # ==========================================

            # 4. BROADCAST SHOOTING/MOVEMENT TO PEERS
            # We skip the sender here so they don't get duplicate shooting sounds
            for peer_acc in list(room["players"]):
                if peer_acc == self.account_id: continue
                if peer_acc in USERS:
                    try: 
                        USERS[peer_acc]["socket"].sendall(payload.encode("utf-8") + b"\x00")
                    except OSError: 
                        pass

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
            length = room.get("round_length", 630)
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
                "round_length": 630,
                "crates": {}
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
                        new_hp = 100
                        #self.broadcast_to_room(f"M{int(self.account_id):03d}6{new_hp:03d}\x00".encode("utf-8"))
                        self.broadcast_to_room(f"M{int(wire_id(self.account_id)):03d}6{new_hp:03d}\x00".encode("utf-8"))
                        print(f"[SYSTEM] Reset HP for {self.username} (Respawn via Opcode 8)")
            
            # 2. Relay (now guaranteed to run)
            room_name = USERS[self.account_id].get("room")
            if room_name:
                self.relay_state_to_room(room_name, packet)
            return
        
        # === 1. Damage / Kill Logic (Opcode 6) ===
        elif packet.startswith("6"):
            if len(packet) < 8: return
            
            # 1. Parse Attacker, Weapon, and Damage from incoming packet (e.g., '60010007')
            raw_attacker = packet[1:4]  # Extracts '001'
            raw_weapon = packet[4:6]    # Extracts '00'
            try:
                damage = int(packet[6:8])
            except:
                return

            target_acc = self.account_id
            if not target_acc or target_acc not in USERS:
                return

            # GUARD: Stop if already dead to prevent infinite loop
            current_hp = USERS[target_acc].get("hp", 100)
            if current_hp <= 0:
                return

            new_hp = max(0, current_hp - damage)
            USERS[target_acc]["hp"] = new_hp
            
            # 2. Correctly retrieve the slot for the target
            # Use USERS[target_acc]['slot'] instead of self.slot
            target_slot_val = USERS[target_acc].get("slot", 0)
            
            # 3. Format IDs to exact widths (3 for Target/Killer, 2 for Weapon)
            target_wire = f"{int(target_slot_val):03d}"
            attacker_wire = f"{int(raw_attacker):03d}"
            weapon_wire = f"{int(raw_weapon):02d}"

            if new_hp > 0:
                # Normal damage update
                self.broadcast_to_room(f"M{target_wire}6{new_hp:03d}\x00".encode("utf-8"))
            else:
                # --- 1. KILL & DEATH TRACKING ---
                target_info = USERS.get(target_acc)
                target_info.setdefault("stats", {"score": 10000, "kills": 0, "deaths": 0, "bounty_points": 0})
                target_info["stats"]["deaths"] += 1
                
                room_name = target_info.get("room")
                
                # Find the attacker by their slot ID to give them the kill
                attacker_acc = None
                for a_id, a_info in USERS.items():
                    if a_info.get("room") == room_name and str(a_info.get("slot", "")) == str(int(raw_attacker)):
                        attacker_acc = a_id
                        break
                        
                if attacker_acc:
                    a_info = USERS[attacker_acc]
                    a_info.setdefault("stats", {"score": 10000, "kills": 0, "deaths": 0, "bounty_points": 0})
                    a_info["stats"]["kills"] += 1

                # --- 2. KILL BROADCAST & CRATE SPAWN ---
                attacker_wire = f"{int(raw_attacker):03d}"
                weapon_wire = f"{int(raw_weapon):02d}"
                dead_pos = target_info.get("last_pos", "0050000500")
                
                # Calculate 10% of score, rounded up to the nearest 250
                target_score = target_info.get("stats", {}).get("score", 10000)
                bounty_value = target_score * 0.10
                bounty_value = math.ceil(bounty_value / 250.0) * 250
                
                # Figure out which crates to spawn (Greedy algorithm)
                crates_to_spawn = []
                while bounty_value >= 1000:
                    crates_to_spawn.append(2)
                    bounty_value -= 1000
                while bounty_value >= 500:
                    crates_to_spawn.append(1)
                    bounty_value -= 500
                while bounty_value >= 250:
                    crates_to_spawn.append(0)
                    bounty_value -= 250

                # Generate 9 distinct tile offsets (Center + 8 adjacent)
                TILE_SIZE = 100 # If the crates barely separate visually, change this to 500
                grid_offsets = [(dx * TILE_SIZE, dy * TILE_SIZE) for dx in [-1, 0, 1] for dy in [-1, 0, 1]]
                random.shuffle(grid_offsets)

                # --- NEW: CALCULATE BOUNTY POINTS (BP) BASED ON RANK ---
                death_bp = 0
                death_id = f"{target_acc}_{int(time.time() * 1000)}" # Unique ID for this specific death event
                
                bounty_str = ""
                if room_name and room_name in self.server.rooms:
                    room = self.server.rooms[room_name]
                    room.setdefault("crates", {})
                    
                    # 1. Determine the victim's rank to calculate BP worth
                    players_in_room = list(room["players"])
                    num_players = len(players_in_room)
                    
                    # Sort by score descending to find rank
                    players_in_room.sort(key=lambda acc: USERS.get(acc, {}).get("stats", {}).get("score", 10000), reverse=True)
                    try:
                        victim_rank = players_in_room.index(target_acc) + 1 # 1st = 1, 2nd = 2, etc.
                    except ValueError:
                        victim_rank = num_players # Fallback

                    # 2. Assign BP based on room size and victim's rank
                    if num_players >= 16:
                        if victim_rank == 1: death_bp = 6
                        elif victim_rank == 2: death_bp = 5
                        elif victim_rank == 3: death_bp = 4
                        else: death_bp = 3
                    elif 10 <= num_players <= 15:
                        if victim_rank == 1: death_bp = 4
                        elif victim_rank == 2: death_bp = 3
                        #elif victim_rank == 3: death_bp = 1
                        else: death_bp = 2
                    elif 6 <= num_players <= 9:
                        if victim_rank == 1: death_bp = 2
                        #elif victim_rank == 2: death_bp = 1
                        else: death_bp = 1
                    
                    # Safely extract X and Y from the 10-character position string
                    try:
                        base_x = int(dead_pos[0:5])
                        base_y = int(dead_pos[5:10])
                    except ValueError:
                        base_x, base_y = 500, 500
                    
                    # Assign an available index and offset for each crate
                    for i, c_type in enumerate(crates_to_spawn):
                        crate_index = -1
                        # Find the first free index between 00 and 99
                        for j in range(100):
                            idx_str = f"{j:02d}"
                            if idx_str not in room["crates"]:
                                crate_index = j
                                break
                        
                        # --- NEW: CRATE LIMIT REACHED (Overwrite Oldest) ---
                        if crate_index == -1 and room["crates"]:
                            # Find the crate index with the oldest timestamp
                            oldest_idx_str = min(room["crates"], key=lambda k: room["crates"][k].get("timestamp", 0))
                            crate_index = int(oldest_idx_str)
                            
                            # Pop it out of the dictionary so we can overwrite it cleanly
                            room["crates"].pop(oldest_idx_str)
                            # print(f"[*] Crate limit reached. Overwriting oldest crate: {oldest_idx_str}")
                        # ---------------------------------------------------
                        
                        if crate_index != -1:
                            idx_str = f"{crate_index:02d}"
                            
                            # Pick a distinct tile if possible, otherwise stack randomly
                            if i < len(grid_offsets):
                                dx, dy = grid_offsets[i]
                            else:
                                dx, dy = random.choice(grid_offsets)
                                
                            # Apply the offset and clamp values between 0 and 99999
                            new_x = max(0, min(99999, base_x + dx))
                            new_y = max(0, min(99999, base_y + dy))
                            spread_pos = f"{new_x:05d}{new_y:05d}" # Must be exactly 10 chars
                            
                            crate_str = create_bounty_string(c_type, crate_index, spread_pos)
                            
                            # Claim the index in the DB AND save the BP/Death ID/Timestamp
                            room["crates"][idx_str] = {
                                "type": c_type,
                                "str": crate_str,
                                "bp": death_bp,           
                                "death_id": death_id,     
                                "timestamp": time.time()  # <-- NEW: Track when it spawned
                            }
                            
                            bounty_str += crate_str
                
                kill_out = f"M{target_wire}7{attacker_wire}{weapon_wire}{bounty_str}"
                self.broadcast_to_room((kill_out + "\x00").encode("utf-8"))
                
                # ... (Keep your existing RESPAWN LOGIC Opcodes 6 and 8 below this) ...

        # --- PLAYER DEATH / DESPAWN ---
        elif packet.startswith("7"):
            room_name = USERS[self.account_id].get("room")
            if room_name:
                self.relay_state_to_room(room_name, packet)
                print(f"[DEBUG] Broadcast DEATH: Packet={packet}")
            return

        # --- 3. HANDLE CRATE PICKUP & SCORE ---
        # --- 3. HANDLE CRATE PICKUP & SCORE ---
        elif packet.startswith("0m"):
            payload = packet[2:] # e.g., '00' or '01'
            crate_index = payload[:2]
            
            user_info = USERS.get(self.account_id)
            if not user_info: return
            
            room_name = user_info.get("room")
            if room_name and room_name in self.server.rooms:
                room = self.server.rooms[room_name]
                
                # 1. Check if the crate actually exists, and FREE the index using .pop()
                if "crates" in room and crate_index in room["crates"]:
                    crate_data = room["crates"].pop(crate_index)
                    crate_type = crate_data["type"]
                    
                    # Extract BP and Death ID from the popped crate
                    crate_bp = crate_data.get("bp", 0)
                    death_id = crate_data.get("death_id")
                    
                    points = {0: 250, 1: 500, 2: 1000}.get(crate_type, 250)
                    
                    # 2. Add Stats
                    user_info.setdefault("stats", {"score": 10000, "kills": 0, "deaths": 0, "bounty_points": 0})
                    user_info["stats"]["score"] += points
                    
                    # 3. Handle Bounty Points (Only 1 time per specific death event per user)
                    bounty_awarded = 0
                    if death_id and crate_bp > 0:
                        claimed_deaths = user_info.setdefault("claimed_deaths", set())
                        if death_id not in claimed_deaths:
                            # User hasn't claimed BP for this death yet!
                            bounty_awarded = crate_bp
                            user_info["stats"]["bounty_points"] += bounty_awarded
                            
                            # Remember this death so they don't get BP for the other crates in the pile
                            claimed_deaths.add(death_id) 
                    
                    # 4. Broadcast removal to clients (param3 = bounty points)
                    slot_str = f"{int(user_info['slot']):03d}"
                    out_packet = f"0m{slot_str}{crate_index}{bounty_awarded}"
                    self.relay_raw_to_room(room_name, out_packet, include_self=True)

        # --- RESET HP ON RESPAWN (0k) ---
        elif packet.startswith("0k"):
            room_name = USERS[self.account_id].get("room")
            if packet == "0k1":
                if self.account_id in USERS:
                    USERS[self.account_id]["hp"] = 100
                    print(f"[SYSTEM] Reset HP for {self.username} (Respawn)")

            if room_name:
                self.relay_raw_to_room(room_name, packet, include_self=False)

        # --- WEAPON SWITCH (0q) ---
        elif packet.startswith("0q"):
            user_info = USERS.get(self.account_id)
            if not user_info: return
            
            # Save the active weapon for late joiners!
            user_info["weapon"] = packet[2:4]
            
            room_name = user_info.get("room")
            if room_name and room_name in self.server.rooms:
                slot_str = f"{int(user_info['slot']):03d}"
                out_packet = f"M{slot_str}{packet}"
                self.relay_raw_to_room(room_name, out_packet, include_self=False)



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
                length = room.get("round_length",630)
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
        "_": {"name": "_", "players": set(), "settings_string": "", "round_start": None, "round_length": 630, "crates": {}}
    }
    print("[*] Listening on port 6123...")
    server.serve_forever()

import asyncio
import json
import random
import time
import os

PORT = 9000
HOST = '0.0.0.0'

# File paths for persistence
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
LINKS_FILE = os.path.join(DATA_DIR, 'links.json')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')

# Active connections: platform -> writer
active_clients = {}

STATUS_FILE = os.path.join(DATA_DIR, 'status.json')

def update_status_file():
    try:
        status_data = {
            "timestamp": time.time(),
            "active_platforms": list(active_clients.keys())
        }
        with open(STATUS_FILE, 'w') as f:
            json.dump(status_data, f, indent=4)
    except Exception as e:
        print(f"Error updating status file: {e}")

# Pending link codes: code -> {"platform": platform, "user_id": user_id, "expires_at": timestamp}
pending_links = {}

# Links: list of dicts {"stoat_id": str, "fluxer_id": str, "discord_id": str}
links = []

# Bridges config: list of dicts {"id": str, "stoat_channel": str, "fluxer_channel": str, "discord_channel": str}
bridges = []

# Pending bridges config: code -> {"src_platform": platform, "src_channel": channel_id, "user_id": user_id, "expires_at": timestamp}
pending_bridges = {}

# Pending server sync codes: code -> {"platform": platform, "user_id": user_id, "server_id": server_id, "channels": channels, "expires_at": timestamp}
pending_server_syncs = {}

# Custom emojis registered by each platform: platform -> channel_id -> {"names": {name.lower(): id}, "ids": {id: name}}
emoji_registries = {
    "discord": {},
    "stoat": {},
    "fluxer": {}
}

# Relayed messages cache: mapping dest_message_id -> origin_metadata
relayed_messages = {}
max_relayed_messages = 10000

# Message mappings for reactions and replies: msg_id -> group dict
message_mappings = {}
message_mappings_keys = []
max_mappings = 20000

def cache_mapping(msg_id, group):
    global message_mappings, message_mappings_keys
    if msg_id not in message_mappings:
        message_mappings_keys.append(msg_id)
    message_mappings[msg_id] = group
    
    if len(message_mappings_keys) > max_mappings:
        oldest_key = message_mappings_keys.pop(0)
        if oldest_key in message_mappings:
            del message_mappings[oldest_key]

def cache_relayed_message(dest_msg_id, metadata):
    global relayed_messages
    relayed_messages[dest_msg_id] = metadata
    if len(relayed_messages) > max_relayed_messages:
        first_key = next(iter(relayed_messages))
        del relayed_messages[first_key]


def load_data():
    global links, bridges
    if os.path.exists(LINKS_FILE):
        try:
            with open(LINKS_FILE, 'r') as f:
                links = json.load(f)
        except Exception as e:
            print(f"Error loading links: {e}")
    else:
        links = []

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                bridges = json.load(f).get("bridges", [])
        except Exception as e:
            print(f"Error loading config: {e}")
    else:
        # Default config structure
        bridges = [
            {
                "id": "main_bridge",
                "stoat_channel": "01J27KDR2Y1B2Y4V5A7C8E9D0F", # Example Stoat Channel ID
                "fluxer_channel": "1481000000000000000",       # Example Fluxer Channel ID
                "discord_channel": "1250000000000000000"       # Example Discord Channel ID
            }
        ]
        save_config()

def save_links():
    try:
        with open(LINKS_FILE, 'w') as f:
            json.dump(links, f, indent=4)
    except Exception as e:
        print(f"Error saving links: {e}")

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({"bridges": bridges}, f, indent=4)
    except Exception as e:
        print(f"Error saving config: {e}")

# Helper to find a link by any platform ID
def find_link(platform, user_id):
    key = f"{platform}_id"
    for l in links:
        if l.get(key) == user_id:
            return l
    return None

def merge_or_create_link(platform1, user_id1, platform2, user_id2):
    l1 = find_link(platform1, user_id1)
    l2 = find_link(platform2, user_id2)

    if l1 and l2:
        if l1 == l2:
            return l1
        # Merge the two links
        for k, v in l2.items():
            if v and not l1.get(k):
                l1[k] = v
        links.remove(l2)
        save_links()
        return l1
    elif l1:
        l1[f"{platform2}_id"] = user_id2
        save_links()
        return l1
    elif l2:
        l2[f"{platform1}_id"] = user_id1
        save_links()
        return l2
    else:
        new_link = {
            "stoat_id": user_id1 if platform1 == "stoat" else (user_id2 if platform2 == "stoat" else None),
            "fluxer_id": user_id1 if platform1 == "fluxer" else (user_id2 if platform2 == "fluxer" else None),
            "discord_id": user_id1 if platform1 == "discord" else (user_id2 if platform2 == "discord" else None)
        }
        links.append(new_link)
        save_links()
        return new_link

async def broadcast(message, exclude_platform=None):
    data = json.dumps(message) + "\n"
    for platform, writer in list(active_clients.items()):
        if platform == exclude_platform:
            continue
        try:
            writer.write(data.encode())
            await writer.drain()
        except Exception as e:
            print(f"Error broadcasting to {platform}: {e}")

async def handle_client(reader, writer):
    peer = writer.get_extra_info('peername')
    print(f"New connection from {peer}")
    platform = None

    try:
        while True:
            line = await reader.readline()
            if not line:
                break

            print(f"[daemon-debug] received raw line from {platform or peer}: '{line.decode().strip()}'")

            try:
                msg = json.loads(line.decode().strip())
            except Exception as e:
                print(f"Failed to parse JSON: {e}")
                continue

            msg_type = msg.get("type")
            
            if msg_type == "register":
                platform = msg.get("platform")
                if platform in ["fluxer", "stoat", "discord"]:
                    active_clients[platform] = writer
                    print(f"Platform '{platform}' registered from {peer}")
                    update_status_file()
                    
                    # Send bridged channel list for this platform
                    bridged_channels = []
                    key = f"{platform}_channel"
                    for b in bridges:
                        if b.get(key):
                            bridged_channels.append(b[key])
                    
                    response = {
                        "type": "init",
                        "bridged_channels": bridged_channels
                    }
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()
                else:
                    print(f"Unknown platform registration: {platform}")
                    writer.close()
                    return

            elif msg_type == "message":
                channel_id = msg.get("channel_id")
                content = msg.get("content")
                author_name = msg.get("author_name")
                author_avatar = msg.get("author_avatar")
                author_id = msg.get("author_id", "")
                channel_name = msg.get("channel_name", "")
                server_id = msg.get("server_id", "")
                server_name = msg.get("server_name", "")
                message_id = msg.get("message_id")
                reply_to_message_id = msg.get("reply_to_message_id")

                # Map origin message ID
                if message_id:
                    group = {
                        "stoat": message_id if platform == "stoat" else None,
                        "fluxer": message_id if platform == "fluxer" else None,
                        "discord": message_id if platform == "discord" else None,
                        "origin_platform": platform,
                        "origin_user_id": author_id,
                        "origin_username": author_name
                    }
                    cache_mapping(message_id, group)

                # Check for replies
                reply_to_group = None
                if reply_to_message_id:
                    reply_to_group = message_mappings.get(reply_to_message_id)

                # Find the bridge config
                src_key = f"{platform}_channel"
                for b in bridges:
                    if b.get(src_key) == channel_id:
                        # Relay to other platforms
                        for dest_platform in ["fluxer", "stoat", "discord"]:
                            if dest_platform == platform:
                                continue
                            dest_key = f"{dest_platform}_channel"
                            dest_channel = b.get(dest_key)
                            if dest_channel and dest_platform in active_clients:
                                dest_reply_to_msg_id = None
                                dest_content = content
                                
                                if reply_to_group:
                                    dest_reply_to_msg_id = reply_to_group.get(dest_platform)
                                    
                                    # Add mention logic
                                    target_user_id = None
                                    origin_user_platform = reply_to_group.get("origin_platform")
                                    origin_user_id = reply_to_group.get("origin_user_id")
                                    origin_username = reply_to_group.get("origin_username", "User")
                                    
                                    # Try to find a linked account for the destination platform
                                    link = find_link(origin_user_platform, origin_user_id)
                                    if link:
                                        target_user_id = link.get(f"{dest_platform}_id")
                                    elif origin_user_platform == dest_platform:
                                        target_user_id = origin_user_id
                                        
                                    if target_user_id:
                                        # Native mention format
                                        dest_content = f"<@{target_user_id}> " + dest_content
                                    else:
                                        # Text mention fallback
                                        dest_content = f"*[Replying to @{origin_username}]* " + dest_content

                                relay_msg = {
                                    "type": "relay",
                                    "channel_id": dest_channel,
                                    "author_name": author_name,
                                    "author_avatar": author_avatar,
                                    "content": dest_content,
                                    "src_platform": platform,
                                    "src_channel_id": channel_id,
                                    "src_channel_name": channel_name,
                                    "src_server_id": server_id,
                                    "src_server_name": server_name,
                                    "src_user_id": author_id,
                                    "src_username": author_name,
                                    "src_message_id": message_id
                                }
                                if dest_reply_to_msg_id:
                                    relay_msg["reply_to_message_id"] = dest_reply_to_msg_id

                                try:
                                    active_clients[dest_platform].write((json.dumps(relay_msg) + "\n").encode())
                                    await active_clients[dest_platform].drain()
                                except Exception as e:
                                    print(f"Error relaying to {dest_platform}: {e}")
                        break

            elif msg_type == "link_generate":
                user_id = msg.get("user_id")
                # Generate 6-digit code
                code = f"{random.randint(100000, 999999)}"
                pending_links[code] = {
                    "platform": platform,
                    "user_id": user_id,
                    "expires_at": time.time() + 300
                }
                response = {
                    "type": "link_generated",
                    "code": code,
                    "user_id": user_id
                }
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()

            elif msg_type == "link_redeem":
                user_id = msg.get("user_id")
                code = msg.get("code")

                # Clean up expired codes
                now = time.time()
                for c in list(pending_links.keys()):
                    if pending_links[c]["expires_at"] < now:
                        del pending_links[c]

                if code in pending_links:
                    pending = pending_links[code]
                    platform1 = pending["platform"]
                    user_id1 = pending["user_id"]
                    
                    if platform1 == platform:
                        response = {"type": "link_error", "message": "Cannot link accounts on the same platform.", "user_id": user_id}
                        writer.write((json.dumps(response) + "\n").encode())
                        await writer.drain()
                        continue

                    # Merge / create linkage
                    link = merge_or_create_link(platform1, user_id1, platform, user_id)
                    del pending_links[code]

                    # Broadcast link_created event
                    event = {
                        "type": "link_created",
                        "stoat_id": link.get("stoat_id"),
                        "fluxer_id": link.get("fluxer_id"),
                        "discord_id": link.get("discord_id")
                    }
                    await broadcast(event)

                    # Trigger balance merge sequence
                    merge_trigger = {
                        "type": "merge_balances_request",
                        "stoat_id": link.get("stoat_id"),
                        "fluxer_id": link.get("fluxer_id"),
                        "discord_id": link.get("discord_id")
                    }
                    await broadcast(merge_trigger)
                else:
                    response = {"type": "link_error", "message": "Invalid or expired link code.", "user_id": user_id}
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()

            elif msg_type == "report_balance":
                # A bot reports the balance of a user in response to merge_balances_request
                stoat_id = msg.get("stoat_id")
                fluxer_id = msg.get("fluxer_id")
                discord_id = msg.get("discord_id")
                wallet = msg.get("wallet", 0)
                bank = msg.get("bank", 0)

                # Broadcast to merge
                sync_msg = {
                    "type": "balance_sync",
                    "stoat_id": stoat_id,
                    "fluxer_id": fluxer_id,
                    "discord_id": discord_id,
                    "wallet": wallet,
                    "bank": bank
                }
                await broadcast(sync_msg)

            elif msg_type == "balance_update":
                user_id = msg.get("user_id")
                wallet_delta = msg.get("wallet_delta", 0)
                bank_delta = msg.get("bank_delta", 0)

                link = find_link(platform, user_id)
                if link:
                    # Propagate to all platforms
                    sync_msg = {
                        "type": "balance_sync_delta",
                        "stoat_id": link.get("stoat_id"),
                        "fluxer_id": link.get("fluxer_id"),
                        "discord_id": link.get("discord_id"),
                        "wallet_delta": wallet_delta,
                        "bank_delta": bank_delta
                    }
                    await broadcast(sync_msg, exclude_platform=platform)

            elif msg_type == "afk_update":
                user_id = msg.get("user_id")
                is_afk = msg.get("is_afk")
                reason = msg.get("reason", "")

                link = find_link(platform, user_id)
                if link:
                    sync_msg = {
                        "type": "afk_sync",
                        "stoat_id": link.get("stoat_id"),
                        "fluxer_id": link.get("fluxer_id"),
                        "discord_id": link.get("discord_id"),
                        "is_afk": is_afk,
                        "reason": reason
                    }
                    await broadcast(sync_msg, exclude_platform=platform)

            elif msg_type == "mod_action":
                action = msg.get("action")
                target_user_id = msg.get("target_user_id")
                reason = msg.get("reason", "")

                link = find_link(platform, target_user_id)
                if link:
                    sync_msg = {
                        "type": "mod_sync",
                        "stoat_id": link.get("stoat_id"),
                        "fluxer_id": link.get("fluxer_id"),
                        "discord_id": link.get("discord_id"),
                        "action": action,
                        "reason": reason
                    }
                    await broadcast(sync_msg, exclude_platform=platform)

            elif msg_type == "track_relayed_message":
                dest_msg_id = msg.get("dest_message_id")
                src_msg_id = msg.get("src_message_id")
                
                # Update message mappings for reactions/replies
                if dest_msg_id and src_msg_id:
                    group = message_mappings.get(src_msg_id)
                    if group:
                        group[platform] = dest_msg_id
                        cache_mapping(dest_msg_id, group)
                
                if dest_msg_id:
                    metadata = {
                        "src_platform": msg.get("src_platform"),
                        "src_channel_id": msg.get("src_channel_id"),
                        "src_channel_name": msg.get("src_channel_name"),
                        "src_server_id": msg.get("src_server_id"),
                        "src_server_name": msg.get("src_server_name"),
                        "src_user_id": msg.get("src_user_id"),
                        "src_username": msg.get("src_username")
                    }
                    cache_relayed_message(dest_msg_id, metadata)

            elif msg_type == "emoji_registry":
                emojis_list = msg.get("emojis", [])
                channel_ids = {item.get("channel_id") for item in emojis_list if item.get("channel_id")}
                single_channel_id = msg.get("channel_id")
                if single_channel_id:
                    channel_ids.add(single_channel_id)
                
                for ch_id in channel_ids:
                    emoji_registries.setdefault(platform, {})[ch_id] = {"names": {}, "ids": {}}
                    
                for item in emojis_list:
                    name = item.get("name")
                    emoji_id = item.get("id")
                    ch_id = item.get("channel_id")
                    if name and emoji_id and ch_id:
                        registry = emoji_registries[platform][ch_id]
                        registry["names"][name.lower()] = emoji_id
                        registry["ids"][emoji_id] = name

            elif msg_type == "reaction_add":
                channel_id = msg.get("channel_id")
                message_id = msg.get("message_id")
                emoji = msg.get("emoji")
                user_id = msg.get("user_id")

                # Detect if the emoji is custom
                src_registry = emoji_registries.get(platform, {}).get(channel_id, {})
                emoji_name = None
                if emoji in src_registry.get("names", {}):
                    emoji_name = emoji
                elif emoji in src_registry.get("ids", {}):
                    emoji_name = src_registry["ids"][emoji]

                group = message_mappings.get(message_id)
                if group:
                    for dest_platform in ["fluxer", "stoat", "discord"]:
                        if dest_platform == platform:
                            continue
                        dest_channel_key = f"{dest_platform}_channel"
                        dest_msg_id = group.get(dest_platform)
                        for b in bridges:
                            if b.get(f"{platform}_channel") == channel_id:
                                dest_channel = b.get(dest_channel_key)
                                if dest_channel and dest_msg_id and dest_platform in active_clients:
                                    dest_emoji = emoji
                                    if emoji_name:
                                        dest_registry = emoji_registries.get(dest_platform, {}).get(dest_channel, {})
                                        dest_id = dest_registry.get("names", {}).get(emoji_name.lower())
                                        if dest_id:
                                            if dest_platform == "discord":
                                                dest_emoji = f"{emoji_name}:{dest_id}"
                                            else:
                                                dest_emoji = dest_id
                                        else:
                                            continue  # custom emoji not matched on target platform

                                    relay_rx = {
                                        "type": "reaction_add",
                                        "channel_id": dest_channel,
                                        "message_id": dest_msg_id,
                                        "emoji": dest_emoji,
                                        "user_id": user_id
                                    }
                                    try:
                                        active_clients[dest_platform].write((json.dumps(relay_rx) + "\n").encode())
                                        await active_clients[dest_platform].drain()
                                    except Exception as e:
                                        print(f"Error relaying reaction_add to {dest_platform}: {e}")
                                break

            elif msg_type == "reaction_remove":
                channel_id = msg.get("channel_id")
                message_id = msg.get("message_id")
                emoji = msg.get("emoji")
                user_id = msg.get("user_id")

                # Detect if the emoji is custom
                src_registry = emoji_registries.get(platform, {}).get(channel_id, {})
                emoji_name = None
                if emoji in src_registry.get("names", {}):
                    emoji_name = emoji
                elif emoji in src_registry.get("ids", {}):
                    emoji_name = src_registry["ids"][emoji]

                group = message_mappings.get(message_id)
                if group:
                    for dest_platform in ["fluxer", "stoat", "discord"]:
                        if dest_platform == platform:
                            continue
                        dest_channel_key = f"{dest_platform}_channel"
                        dest_msg_id = group.get(dest_platform)
                        for b in bridges:
                            if b.get(f"{platform}_channel") == channel_id:
                                dest_channel = b.get(dest_channel_key)
                                if dest_channel and dest_msg_id and dest_platform in active_clients:
                                    dest_emoji = emoji
                                    if emoji_name:
                                        dest_registry = emoji_registries.get(dest_platform, {}).get(dest_channel, {})
                                        dest_id = dest_registry.get("names", {}).get(emoji_name.lower())
                                        if dest_id:
                                            if dest_platform == "discord":
                                                dest_emoji = f"{emoji_name}:{dest_id}"
                                            else:
                                                dest_emoji = dest_id
                                        else:
                                            continue  # custom emoji not matched on target platform

                                    relay_rx = {
                                        "type": "reaction_remove",
                                        "channel_id": dest_channel,
                                        "message_id": dest_msg_id,
                                        "emoji": dest_emoji,
                                        "user_id": user_id
                                    }
                                    try:
                                        active_clients[dest_platform].write((json.dumps(relay_rx) + "\n").encode())
                                        await active_clients[dest_platform].drain()
                                    except Exception as e:
                                        print(f"Error relaying reaction_remove to {dest_platform}: {e}")
                                break

            elif msg_type == "bridge_emojis_query":
                user_id = msg.get("user_id")
                channel_id = msg.get("channel_id")
                
                found_bridge = None
                for b in bridges:
                    if b.get(f"{platform}_channel") == channel_id:
                        found_bridge = b
                        break
                
                if found_bridge:
                    stoat_ch = found_bridge.get("stoat_channel", "")
                    fluxer_ch = found_bridge.get("fluxer_channel", "")
                    discord_ch = found_bridge.get("discord_channel", "")
                    
                    stoat_emojis = emoji_registries.get("stoat", {}).get(stoat_ch, {})
                    fluxer_emojis = emoji_registries.get("fluxer", {}).get(fluxer_ch, {})
                    discord_emojis = emoji_registries.get("discord", {}).get(discord_ch, {})
                    
                    stoat_names = set(stoat_emojis.get("names", {}).keys())
                    fluxer_names = set(fluxer_emojis.get("names", {}).keys())
                    discord_names = set(discord_emojis.get("names", {}).keys())
                    
                    all_names = stoat_names.union(fluxer_names).union(discord_names)
                    
                    matches = []
                    for name in all_names:
                        has_discord = name in discord_names
                        has_stoat = name in stoat_names
                        has_fluxer = name in fluxer_names
                        
                        matches.append({
                            "name": name,
                            "discord": discord_emojis.get("names", {}).get(name) if has_discord else None,
                            "stoat": stoat_emojis.get("names", {}).get(name) if has_stoat else None,
                            "fluxer": fluxer_emojis.get("names", {}).get(name) if has_fluxer else None
                        })
                    
                    response = {
                        "type": "bridge_emojis_response",
                        "user_id": user_id,
                        "channel_id": channel_id,
                        "found": True,
                        "emojis": matches
                    }
                else:
                    response = {
                        "type": "bridge_emojis_response",
                        "user_id": user_id,
                        "channel_id": channel_id,
                        "found": False
                    }
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()

            elif msg_type == "bridge_info_query":
                msg_id = msg.get("message_id")
                user_id = msg.get("user_id")
                metadata = relayed_messages.get(msg_id)
                if metadata:
                    response = {
                        "type": "bridge_info_response",
                        "message_id": msg_id,
                        "user_id": user_id,
                        "found": True,
                        "src_platform": metadata["src_platform"],
                        "src_channel_id": metadata["src_channel_id"],
                        "src_channel_name": metadata["src_channel_name"],
                        "src_server_id": metadata["src_server_id"],
                        "src_server_name": metadata["src_server_name"],
                        "src_user_id": metadata["src_user_id"],
                        "src_username": metadata["src_username"]
                    }
                else:
                    response = {
                        "type": "bridge_info_response",
                        "message_id": msg_id,
                        "user_id": user_id,
                        "found": False
                    }
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()

            elif msg_type == "bridge_add_generate":
                user_id = msg.get("user_id")
                channel_id = msg.get("channel_id")
                code = f"{random.randint(100000, 999999)}"
                pending_bridges[code] = {
                    "src_platform": platform,
                    "src_channel": channel_id,
                    "user_id": user_id,
                    "expires_at": time.time() + 300
                }
                response = {
                    "type": "bridge_add_generated",
                    "code": code,
                    "user_id": user_id,
                    "channel_id": channel_id
                }
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()

            elif msg_type == "bridge_add_redeem":
                user_id = msg.get("user_id")
                channel_id = msg.get("channel_id")
                code = msg.get("code")
                
                now = time.time()
                for c in list(pending_bridges.keys()):
                    if pending_bridges[c]["expires_at"] < now:
                        del pending_bridges[c]
                        
                if code in pending_bridges:
                    pending = pending_bridges[code]
                    src_platform = pending["src_platform"]
                    src_channel = pending["src_channel"]
                    
                    if src_platform == platform:
                        response = {
                            "type": "bridge_add_error",
                            "user_id": user_id,
                            "channel_id": channel_id,
                            "message": "cannot bridge channels on the same platform."
                        }
                        writer.write((json.dumps(response) + "\n").encode())
                        await writer.drain()
                        continue
                        
                    existing_bridge = None
                    for b in bridges:
                        if b.get(f"{src_platform}_channel") == src_channel:
                            existing_bridge = b
                            break
                    if not existing_bridge:
                        for b in bridges:
                            if b.get(f"{platform}_channel") == channel_id:
                                existing_bridge = b
                                break
                                
                    if existing_bridge:
                        existing_bridge[f"{src_platform}_channel"] = src_channel
                        existing_bridge[f"{platform}_channel"] = channel_id
                    else:
                        bridge_id = f"bridge_{random.randint(1000, 9999)}"
                        existing_bridge = {
                            "id": bridge_id,
                            "stoat_channel": src_channel if src_platform == "stoat" else (channel_id if platform == "stoat" else None),
                            "fluxer_channel": src_channel if src_platform == "fluxer" else (channel_id if platform == "fluxer" else None),
                            "discord_channel": src_channel if src_platform == "discord" else (channel_id if platform == "discord" else None)
                        }
                        bridges.append(existing_bridge)
                        
                    save_config()
                    del pending_bridges[code]
                    
                    for p, w in active_clients.items():
                        bridged_channels = []
                        k = f"{p}_channel"
                        for b in bridges:
                            if b.get(k):
                                bridged_channels.append(b[k])
                        init_msg = {
                            "type": "init",
                            "bridged_channels": bridged_channels
                        }
                        try:
                            w.write((json.dumps(init_msg) + "\n").encode())
                            await w.drain()
                        except Exception as e:
                            print(f"Error broadcasting init to {p}: {e}")
                            
                    response = {
                        "type": "bridge_add_success",
                        "user_id": user_id,
                        "channel_id": channel_id,
                        "other_channel_id": src_channel,
                        "other_platform": src_platform
                    }
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()
                    
                    if src_platform in active_clients:
                        notify_msg = {
                            "type": "bridge_established_notification",
                            "channel_id": src_channel,
                            "other_channel_id": channel_id,
                            "other_platform": platform
                        }
                        try:
                            active_clients[src_platform].write((json.dumps(notify_msg) + "\n").encode())
                            await active_clients[src_platform].drain()
                        except Exception as e:
                            print(f"Error sending establish notification to {src_platform}: {e}")
                else:
                    response = {
                        "type": "bridge_add_error",
                        "user_id": user_id,
                        "channel_id": channel_id,
                        "message": "invalid or expired verification code."
                    }
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()

            elif msg_type == "bridge_remove":
                user_id = msg.get("user_id")
                channel_id = msg.get("channel_id")
                
                bridge_to_remove = None
                for b in bridges:
                    if b.get(f"{platform}_channel") == channel_id:
                        bridge_to_remove = b
                        break
                        
                if bridge_to_remove:
                    bridges.remove(bridge_to_remove)
                    save_config()
                    
                    for p, w in active_clients.items():
                        bridged_channels = []
                        k = f"{p}_channel"
                        for b in bridges:
                            if b.get(k):
                                bridged_channels.append(b[k])
                        init_msg = {
                            "type": "init",
                            "bridged_channels": bridged_channels
                        }
                        try:
                            w.write((json.dumps(init_msg) + "\n").encode())
                            await w.drain()
                        except Exception as e:
                            print(f"Error broadcasting init to {p}: {e}")
                            
                    response = {
                        "type": "bridge_remove_success",
                        "user_id": user_id,
                        "channel_id": channel_id
                    }
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()
                    
                    for dest_platform in ["fluxer", "stoat", "discord"]:
                        if dest_platform == platform:
                            continue
                        dest_key = f"{dest_platform}_channel"
                        dest_channel = bridge_to_remove.get(dest_key)
                        if dest_channel and dest_platform in active_clients:
                            notify_msg = {
                                "type": "bridge_removed_notification",
                                "channel_id": dest_channel
                            }
                            try:
                                active_clients[dest_platform].write((json.dumps(notify_msg) + "\n").encode())
                                await active_clients[dest_platform].drain()
                            except Exception as e:
                                print(f"Error sending remove notification to {dest_platform}: {e}")
                else:
                    response = {
                        "type": "bridge_remove_error",
                        "user_id": user_id,
                        "channel_id": channel_id,
                        "message": "this channel is not bridged."
                    }
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()

            elif msg_type == "bridge_check":
                user_id = msg.get("user_id")
                channel_id = msg.get("channel_id")
                
                found_bridge = None
                for b in bridges:
                    if b.get(f"{platform}_channel") == channel_id:
                        found_bridge = b
                        break
                        
                if found_bridge:
                    response = {
                        "type": "bridge_check_response",
                        "user_id": user_id,
                        "channel_id": channel_id,
                        "found": True,
                        "stoat_channel": found_bridge.get("stoat_channel"),
                        "fluxer_channel": found_bridge.get("fluxer_channel"),
                        "discord_channel": found_bridge.get("discord_channel")
                    }
                else:
                    response = {
                        "type": "bridge_check_response",
                        "user_id": user_id,
                        "channel_id": channel_id,
                        "found": False
                    }
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()

            elif msg_type == "server_sync_generate":
                user_id = msg.get("user_id")
                server_id = msg.get("server_id")
                channels = msg.get("channels", [])
                code = f"{random.randint(100000, 999999)}"
                pending_server_syncs[code] = {
                    "platform": platform,
                    "user_id": user_id,
                    "server_id": server_id,
                    "channels": channels,
                    "expires_at": time.time() + 300
                }
                response = {
                    "type": "server_sync_generated",
                    "code": code,
                    "user_id": user_id
                }
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()

            elif msg_type == "server_sync_redeem":
                user_id = msg.get("user_id")
                server_id = msg.get("server_id")
                code = msg.get("code")
                target_channels = msg.get("channels", [])

                # Clean up expired codes
                now = time.time()
                for c in list(pending_server_syncs.keys()):
                    if pending_server_syncs[c]["expires_at"] < now:
                        del pending_server_syncs[c]

                if code in pending_server_syncs:
                    src_info = pending_server_syncs[code]
                    src_platform = src_info["platform"]
                    src_server_id = src_info["server_id"]
                    src_channels = src_info["channels"]

                    if src_platform == platform:
                        response = {
                            "type": "server_sync_error",
                            "user_id": user_id,
                            "message": "Cannot sync servers on the same platform."
                        }
                        writer.write((json.dumps(response) + "\n").encode())
                        await writer.drain()
                        continue

                    # Match channels by name
                    target_by_name = {c["name"].lower(): c for c in target_channels if isinstance(c, dict) and "name" in c}
                    pairs_json = []
                    used_target_ids = set()

                    for src_ch in src_channels:
                        if not isinstance(src_ch, dict):
                            continue
                        src_name = src_ch.get("name", "")
                        src_id = src_ch.get("id", "")
                        src_name_lower = src_name.lower()

                        if src_name_lower in target_by_name:
                            t_ch = target_by_name[src_name_lower]
                            used_target_ids.add(t_ch.get("id"))
                            pairs_json.append({
                                "local_channel_id": t_ch.get("id", ""),
                                "local_channel_name": t_ch.get("name", t_ch.get("id", "")),
                                "remote_channel_id": src_id,
                                "remote_channel_name": src_name,
                                "auto_matched": True
                            })
                        else:
                            pairs_json.append({
                                "remote_channel_id": src_id,
                                "remote_channel_name": src_name,
                                "auto_matched": False
                            })

                    for t_ch in target_channels:
                        if isinstance(t_ch, dict) and t_ch.get("id") not in used_target_ids:
                            pairs_json.append({
                                "local_channel_id": t_ch.get("id", ""),
                                "local_channel_name": t_ch.get("name", ""),
                                "auto_matched": False
                            })

                    response = {
                        "type": "server_sync_proposal",
                        "user_id": user_id,
                        "server_id": server_id,
                        "remote_platform": src_platform,
                        "remote_server_id": src_server_id,
                        "pairs": pairs_json
                    }
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()
                else:
                    response = {
                        "type": "server_sync_error",
                        "user_id": user_id,
                        "message": "Invalid or expired server sync code."
                    }
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()

            elif msg_type == "server_sync_confirm":
                user_id = msg.get("user_id")
                server_id = msg.get("server_id")

                response = {
                    "type": "server_sync_complete",
                    "user_id": user_id,
                    "server_id": server_id
                }
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()

            elif msg_type == "server_sync_remove":
                user_id = msg.get("user_id")
                server_id = msg.get("server_id")
                target_platform = msg.get("platform", "")

            elif msg_type == "server_sync_status":
                user_id = msg.get("user_id")
                server_id = msg.get("server_id")

                pairs = []
                for b in bridges:
                    local_ch = b.get(f"{platform}_channel")
                    if local_ch:
                        for other_platform in ["stoat", "discord", "fluxer"]:
                            if other_platform != platform:
                                remote_ch = b.get(f"{other_platform}_channel")
                                if remote_ch:
                                    pairs.append({
                                        "local_channel_id": local_ch,
                                        "remote_channel_id": remote_ch,
                                        "remote_platform": other_platform
                                    })

                response = {
                    "type": "server_sync_status_response",
                    "user_id": user_id,
                    "found": len(pairs) > 0,
                    "pairs": pairs
                }
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()

    except Exception as e:
        print(f"Error in handle_client for {platform or peer}: {e}")
    finally:
        if platform and platform in active_clients:
            del active_clients[platform]
            print(f"Platform '{platform}' disconnected")
            update_status_file()
        writer.close()
        await writer.wait_closed()

async def main():
    load_data()
    update_status_file()
    server = await asyncio.start_server(handle_client, HOST, PORT)
    addr = server.sockets[0].getsockname()
    print(f"Bridge Daemon running on {addr}")
    async with server:
        await server.serve_forever()

if __name__ == '__main__':
    asyncio.run(main())

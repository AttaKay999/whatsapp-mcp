import re
import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import time
import audio

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"
# whatsmeow's session DB: holds the synced address book (saved names + WhatsApp display names)
WHATSAPP_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'whatsapp.db')

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

# ---------------------------------------------------------------------------
# Name resolution (never raises; always falls back to the raw ID)
# ---------------------------------------------------------------------------

def _user_part(jid: str) -> str:
    return (jid or "").split('@')[0].split(':')[0]

def _is_placeholder(name: Optional[str], jid: str) -> bool:
    """True if a stored name is really just a number / JID / generic fallback."""
    if not name:
        return True
    n = name.strip()
    if n in (_user_part(jid), jid) or n.lstrip('+').isdigit():
        return True
    return bool(re.fullmatch(r"Group \d+(-\d+)?", n))

def _open_address_book():
    if not os.path.isfile(WHATSAPP_DB_PATH):
        return None
    return sqlite3.connect(f"file:{WHATSAPP_DB_PATH}?mode=ro", uri=True, timeout=2)

def _address_book_name(jid: str) -> Optional[str]:
    """Look a JID up in whatsmeow's contacts (mapping @lid IDs to phone numbers)."""
    conn = None
    try:
        conn = _open_address_book()
        if conn is None:
            return None
        cur = conn.cursor()
        user = _user_part(jid)
        candidates = [user]
        try:
            cur.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (user,))
            candidates += [r[0] for r in cur.fetchall() if r[0]]
            cur.execute("SELECT lid FROM whatsmeow_lid_map WHERE pn = ?", (user,))
            candidates += [r[0] for r in cur.fetchall() if r[0]]
        except sqlite3.Error:
            pass
        for c in candidates:
            cur.execute("""
                SELECT COALESCE(NULLIF(full_name,''), NULLIF(first_name,''),
                                NULLIF(push_name,''), NULLIF(business_name,''))
                FROM whatsmeow_contacts WHERE their_jid LIKE ? LIMIT 1
            """, (f"{c}@%",))
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    except sqlite3.Error:
        return None
    finally:
        if conn:
            conn.close()
    return None

def _address_book_jids_matching(query: str) -> List[str]:
    """JIDs from the address book whose saved/display name matches query."""
    conn = None
    try:
        conn = _open_address_book()
        if conn is None:
            return []
        pat = f"%{query}%"
        rows = conn.execute("""
            SELECT DISTINCT their_jid FROM whatsmeow_contacts
            WHERE LOWER(full_name) LIKE LOWER(?) OR LOWER(first_name) LIKE LOWER(?)
               OR LOWER(push_name) LIKE LOWER(?) OR LOWER(business_name) LIKE LOWER(?)
            LIMIT 50
        """, (pat, pat, pat, pat)).fetchall()
        return [r[0] for r in rows if r[0]]
    except sqlite3.Error:
        return []
    finally:
        if conn:
            conn.close()

def resolve_name(jid: str, stored_name: Optional[str] = None) -> str:
    """Best human-readable name for a JID (contact or group)."""
    try:
        if stored_name and not _is_placeholder(stored_name, jid):
            return stored_name
        if jid and not jid.endswith("@g.us"):
            name = _address_book_name(jid if '@' in jid else f"{jid}@s.whatsapp.net")
            if name:
                return name
    except Exception:
        pass
    return stored_name or jid

def get_sender_name(sender_jid: str) -> str:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # First try matching by exact JID
        cursor.execute("""
            SELECT name
            FROM chats
            WHERE jid = ?
            LIMIT 1
        """, (sender_jid,))
        
        result = cursor.fetchone()
        
        # If no result, try looking for the number within JIDs
        if not result:
            # Extract the phone number part if it's a JID
            if '@' in sender_jid:
                phone_part = sender_jid.split('@')[0]
            else:
                phone_part = sender_jid
                
            cursor.execute("""
                SELECT name
                FROM chats
                WHERE jid LIKE ?
                LIMIT 1
            """, (f"%{phone_part}%",))
            
            result = cursor.fetchone()
        
        return resolve_name(sender_jid, result[0] if result else None)
        
    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    chat_name = resolve_name(message.chat_jid, message.chat_name) if message.chat_jid else message.chat_name
    
    if show_chat_info and chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    
    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        
        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            where_clauses.append("messages.sender = ?")
            params.append(sender_phone_number)
            
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            )
            result.append(message)
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )
        
        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))
        
        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))
        
        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        if include_last_message:
            last_cols = """messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me"""
        else:
            last_cols = """NULL as last_message,
                NULL as last_sender,
                NULL as last_is_from_me"""
        query_parts = [f"""
            SELECT 
                chats.jid,
                chats.name,
                chats.last_message_time,
                {last_cols}
            FROM chats
        """]
        
        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid 
                AND chats.last_message_time = messages.timestamp
            """)
            
        where_clauses = []
        params = []
        
        if query:
            clause = "(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?"
            params.extend([f"%{query}%", f"%{query}%"])
            for ab_jid in _address_book_jids_matching(query):
                clause += " OR chats.jid LIKE ?"
                params.append(f"{_user_part(ab_jid)}@%")
            where_clauses.append(clause + ")")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=resolve_name(chat_data[0], chat_data[1]),
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Split query into characters to support partial matching
        search_pattern = '%' +query + '%'
        
        cursor.execute("""
            SELECT DISTINCT 
                jid,
                name
            FROM chats
            WHERE 
                (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
                AND jid NOT LIKE '%@g.us'
            ORDER BY name, jid
            LIMIT 50
        """, (search_pattern, search_pattern))
        
        contacts = cursor.fetchall()
        
        result = []
        for contact_data in contacts:
            contact = Contact(
                phone_number=contact_data[0].split('@')[0],
                name=resolve_name(contact_data[0], contact_data[1]),
                jid=contact_data[0]
            )
            result.append(contact)

        # Also include saved contacts you haven't chatted with yet
        seen = {c.jid for c in result}
        for ab_jid in _address_book_jids_matching(query):
            if ab_jid.endswith("@g.us") or ab_jid in seen:
                continue
            seen.add(ab_jid)
            result.append(Contact(phone_number=_user_part(ab_jid), name=resolve_name(ab_jid), jid=ab_jid))
            
        return result[:50]
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (jid, jid, limit, page * limit))
        
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=resolve_name(chat_data[0], chat_data[1]),
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, (jid, jid))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            """
            
        query += " WHERE c.jid = ?"
        
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=resolve_name(chat_data[0], chat_data[1]),
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
            LIMIT 1
        """, (f"%{sender_phone_number}%",))
        
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=resolve_name(chat_data[0], chat_data[1]),
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def resolve_recipient_name(name: str) -> str:
    """Turn a contact/group name into a JID. Raises ValueError if none or several match."""
    candidates = {}
    for c in search_contacts(name):
        candidates[c.jid] = c.name or c.phone_number
    for chat in list_chats(query=name, limit=50, include_last_message=False):
        candidates[chat.jid] = chat.name or chat.jid
    if not candidates:
        raise ValueError(f"No contact or group found matching '{name}'. Nothing was sent.")
    exact = {j: n for j, n in candidates.items() if n and n.strip().lower() == name.strip().lower()}
    pool = exact or candidates
    if len(pool) == 1:
        return next(iter(pool))
    options = "; ".join(f"{n} -> {j}" for j, n in list(pool.items())[:10])
    raise ValueError(
        f"'{name}' matches several chats, so nothing was sent. "
        f"Ask which one, then resend using its JID. Options: {options}"
    )


def normalize_recipient(recipient: str) -> str:
    """Accept '+233 55 815 1342', '233558151342', a full JID, or a contact/group name."""
    r = (recipient or "").strip()
    if "@" in r:
        return r
    if re.search(r"[A-Za-z]", r):
        return resolve_recipient_name(r)
    digits = re.sub(r"\D", "", r)
    if not 7 <= len(digits) <= 15:
        raise ValueError(
            f"Invalid phone number '{recipient}'. Use the country code and digits only, e.g. 233558151342."
        )
    return digits


def get_message_caption(message_id: str, chat_jid: str) -> str:
    """Return the stored text/caption of a message ('' if none)."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        try:
            row = conn.execute(
                "SELECT content FROM messages WHERE id = ? AND chat_jid = ?", (message_id, chat_jid)
            ).fetchone()
        finally:
            conn.close()
        return (row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        try:
            recipient = normalize_recipient(recipient)
        except ValueError as e:
            return False, str(e)
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload, timeout=60)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str, caption: str = "") -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        try:
            recipient = normalize_recipient(recipient)
        except ValueError as e:
            return False, str(e)
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        if caption:
            payload["message"] = caption
        
        response = requests.post(url, json=payload, timeout=60)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        try:
            recipient = normalize_recipient(recipient)
        except ValueError as e:
            return False, str(e)
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload, timeout=60)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media_with_error(message_id: str, chat_jid: str) -> Tuple[Optional[str], str]:
    """Download media; return (path, error_message). error_message is '' on success."""
    try:
        response = requests.post(
            f"{WHATSAPP_API_BASE_URL}/download",
            json={"message_id": message_id, "chat_jid": chat_jid},
            timeout=120,
        )
        try:
            result = response.json()
        except ValueError:
            return None, f"HTTP {response.status_code}: {response.text[:300]}"
        if response.status_code == 200 and result.get("success", False):
            return result.get("path"), ""
        return None, result.get("message") or f"HTTP {response.status_code}"
    except requests.RequestException as e:
        return None, f"Could not reach the WhatsApp bridge: {e}"


def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload, timeout=60)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None


# ---------------------------------------------------------------------------
# Group members
# ---------------------------------------------------------------------------

def _resolve_group_jid(group: str) -> str:
    """Accept a group JID or a group name; return the group JID."""
    g = (group or "").strip()
    if g.endswith("@g.us"):
        return g
    jid = normalize_recipient(g)  # raises ValueError if no/several matches
    if not jid.endswith("@g.us"):
        raise ValueError(f"'{group}' is a contact, not a group.")
    return jid


def get_group_members(group: str) -> Tuple[bool, dict]:
    """Return (ok, {group_name, group_jid, members:[{jid, phone_number, name, is_admin}]})."""
    try:
        group_jid = _resolve_group_jid(group)
    except ValueError as e:
        return False, {"message": str(e)}
    try:
        response = requests.post(f"{WHATSAPP_API_BASE_URL}/group-members",
                                 json={"group_jid": group_jid}, timeout=60)
        result = response.json()
    except requests.RequestException as e:
        return False, {"message": f"Request error: {e}"}
    except ValueError:
        return False, {"message": f"Bridge returned HTTP {response.status_code}: {response.text[:200]}. "
                                  "Is the updated whatsapp-bridge running?"}
    if not result.get("success"):
        return False, {"message": result.get("message", "Unknown error")}
    members = []
    for m in result.get("members", []):
        jid = m.get("jid", "")
        members.append({
            "jid": jid,
            "phone_number": _user_part(jid),
            "name": (lambda n: _user_part(jid) if n == jid else n)(resolve_name(jid)),
            "is_admin": bool(m.get("is_admin") or m.get("is_super_admin")),
        })
    members.sort(key=lambda x: (not x["is_admin"], (x["name"] or "").lower()))
    return True, {"group_name": result.get("group_name") or group, "group_jid": group_jid,
                  "member_count": len(members), "members": members}


def send_to_group_members(group: str, message: str, exclude: Optional[List[str]] = None,
                          confirm: bool = False, delay_seconds: float = 4.0,
                          max_recipients: int = 50) -> Tuple[bool, dict]:
    """Message each group member privately. Without confirm=True it only previews who would get it."""
    if not message:
        return False, {"message": "Message must be provided"}
    ok, info = get_group_members(group)
    if not ok:
        return False, info
    excl = {e.strip().lower() for e in (exclude or []) if e.strip()}
    targets = [m for m in info["members"]
               if m["jid"].lower() not in excl and m["phone_number"] not in excl
               and (m["name"] or "").lower() not in excl]
    preview = {"group_name": info["group_name"], "recipient_count": len(targets),
               "recipients": [f'{m["name"]} ({m["phone_number"]})' for m in targets]}
    if len(targets) > max_recipients:
        return False, {**preview, "message": f"{len(targets)} recipients is above the limit of "
                       f"{max_recipients}. Raise max_recipients if you really mean it."}
    if not confirm:
        return True, {**preview, "sent": False,
                      "message": "Preview only. Nothing sent. Call again with confirm=true to send."}
    results = []
    for i, m in enumerate(targets):
        if i:
            time.sleep(max(delay_seconds, 2.0))  # spacing reduces the risk of WhatsApp flagging the number
        sent, status = send_message(m["jid"], message)
        results.append({"name": m["name"], "jid": m["jid"], "success": sent, "message": status})
    n_ok = sum(r["success"] for r in results)
    return n_ok == len(results), {"group_name": info["group_name"], "sent": n_ok,
                                  "failed": len(results) - n_ok, "results": results}

import dataclasses
from datetime import datetime
from typing import List, Dict, Any, Optional, Union
from mcp.server.fastmcp import FastMCP
import media_share
import scheduler
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
    list_messages as whatsapp_list_messages,
    list_chats as whatsapp_list_chats,
    get_chat as whatsapp_get_chat,
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
    get_contact_chats as whatsapp_get_contact_chats,
    get_last_interaction as whatsapp_get_last_interaction,
    get_message_context as whatsapp_get_message_context,
    send_message as whatsapp_send_message,
    send_file as whatsapp_send_file,
    send_audio_message as whatsapp_audio_voice_message,
    download_media as whatsapp_download_media,
    download_media_with_error as whatsapp_download_media_with_error,
    normalize_recipient as whatsapp_normalize_recipient,
    get_message_caption as whatsapp_get_message_caption
)

# Initialize FastMCP server
mcp = FastMCP("whatsapp", host="0.0.0.0", port=8000, stateless_http=True)
mcp.custom_route("/media/{token}/{filename}", methods=["GET"])(media_share.serve_media)

def _plain(obj: Any) -> Any:
    """Convert dataclasses/datetimes into plain JSON-safe dicts/lists."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        d = {k: _plain(v) for k, v in dataclasses.asdict(obj).items()}
        if hasattr(obj, "is_group"):
            d["is_group"] = obj.is_group
        return d
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj

@mcp.tool()
def search_contacts(query: str) -> Any:
    """Search WhatsApp contacts by name or phone number.
    
    Args:
        query: Search term to match against contact names or phone numbers
    """
    contacts = whatsapp_search_contacts(query)
    return _plain(contacts)

@mcp.tool()
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
) -> str:
    """Get WhatsApp messages matching specified criteria with optional context.
    
    Args:
        after: Optional ISO-8601 formatted string to only return messages after this date
        before: Optional ISO-8601 formatted string to only return messages before this date
        sender_phone_number: Optional phone number to filter messages by sender
        chat_jid: Optional chat JID to filter messages by chat
        query: Optional search term to filter messages by content
        limit: Maximum number of messages to return (default 20)
        page: Page number for pagination (default 0)
        include_context: Whether to include messages before and after matches (default True)
        context_before: Number of messages to include before each match (default 1)
        context_after: Number of messages to include after each match (default 1)
    """
    messages = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after
    )
    return messages

@mcp.tool()
def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> Any:
    """Get WhatsApp chats matching specified criteria.
    
    Args:
        query: Optional search term to filter chats by name or JID
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
        include_last_message: Whether to include the last message in each chat (default True)
        sort_by: Field to sort results by, either "last_active" or "name" (default "last_active")
    """
    chats = whatsapp_list_chats(
        query=query,
        limit=limit,
        page=page,
        include_last_message=include_last_message,
        sort_by=sort_by
    )
    return _plain(chats)

@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True) -> Any:
    """Get WhatsApp chat metadata by JID.
    
    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return _plain(chat)

@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> Any:
    """Get WhatsApp chat metadata by sender phone number.
    
    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    return _plain(chat)

@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> Any:
    """Get all WhatsApp chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    chats = whatsapp_get_contact_chats(jid, limit, page)
    return _plain(chats)

@mcp.tool()
def get_last_interaction(jid: str) -> str:
    """Get most recent WhatsApp message involving the contact.
    
    Args:
        jid: The JID of the contact to search for
    """
    message = whatsapp_get_last_interaction(jid)
    return message

@mcp.tool()
def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> Any:
    """Get context around a specific WhatsApp message.
    
    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
    """
    context = whatsapp_get_message_context(message_id, before, after)
    return _plain(context)

@mcp.tool()
def send_message(
    recipient: str,
    message: str
) -> Dict[str, Any]:
    """Send a WhatsApp message to a person or group. For group chats use the JID.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        message: The message text to send
    
    Returns:
        A dictionary containing success status and a status message
    """
    # Validate input
    if not recipient:
        return {
            "success": False,
            "message": "Recipient must be provided"
        }
    
    # Call the whatsapp_send_message function with the unified recipient parameter
    success, status_message = whatsapp_send_message(recipient, message)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def send_file(recipient: str, media_path: str, caption: str = "") -> Dict[str, Any]:
    """Send a file such as a picture, raw audio, video or document via WhatsApp to the specified recipient. For group messages use the JID.
    
    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the media file to send (image, video, document)
        caption: Optional caption shown with the image, video or document
    
    Returns:
        A dictionary containing success status and a status message
    """
    
    # Call the whatsapp_send_file function
    success, status_message = whatsapp_send_file(recipient, media_path, caption)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def send_audio_message(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send any audio file as a WhatsApp audio message to the specified recipient. For group messages use the JID. If it errors due to ffmpeg not being installed, use send_file instead.
    
    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the audio file to send (will be converted to Opus .ogg if it's not a .ogg file)
    
    Returns:
        A dictionary containing success status and a status message
    """
    success, status_message = whatsapp_audio_voice_message(recipient, media_path)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> Dict[str, Any]:
    """Download media from a WhatsApp message to the SERVER's disk (not your computer).

    The returned file_path is only valid on the server. To pass the file on to someone,
    use forward_media instead.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    """
    file_path, error = whatsapp_download_media_with_error(message_id, chat_jid)
    if file_path:
        return {"success": True, "message": "Media downloaded successfully", "file_path": file_path}
    return {"success": False, "message": error or "Failed to download media"}

@mcp.tool()
def get_media(message_id: str, chat_jid: str) -> Dict[str, Any]:
    """Get a temporary download link for the media (image, video, audio, voice note, document) of a WhatsApp message.

    The file is downloaded on the server and shared via a short-lived link. Fetch the link with
    an HTTP GET (for example the returned `download_with` command) to save the file on your own
    machine, then open or analyze it there. Works for any media type.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    """
    file_path, error = whatsapp_download_media_with_error(message_id, chat_jid)
    if not file_path:
        return {"success": False, "stage": "download", "message": error or "Failed to download media"}
    try:
        return {"success": True, **media_share.share(file_path)}
    except OSError as e:
        return {"success": False, "stage": "share", "message": str(e)}

@mcp.tool()
def forward_media(message_id: str, chat_jid: str, recipient: str, caption: str = "") -> Dict[str, Any]:
    """Forward the media of a WhatsApp message to another chat or phone number, entirely on the server.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
        recipient: Phone number with country code (no +) or a JID to send the media to
        caption: Optional caption. If empty, the original message's caption is reused.
    """
    file_path, error = whatsapp_download_media_with_error(message_id, chat_jid)
    if not file_path:
        return {"success": False, "stage": "download", "message": error or "Failed to download media"}
    caption = caption or whatsapp_get_message_caption(message_id, chat_jid)
    success, message = whatsapp_send_file(recipient, file_path, caption)
    return {"success": success, "stage": "send", "message": message, "caption_sent": caption}

@mcp.tool()
def bridge_status() -> Dict[str, Any]:
    """Diagnose the WhatsApp bridge: database location, row counts, and whether the bridge is reachable."""
    import os
    import socket
    import sqlite3
    from whatsapp import MESSAGES_DB_PATH

    info: Dict[str, Any] = {
        "db_path": os.path.abspath(MESSAGES_DB_PATH),
        "db_exists": os.path.exists(MESSAGES_DB_PATH),
    }
    store_dir = os.path.dirname(os.path.abspath(MESSAGES_DB_PATH))
    info["store_files"] = sorted(os.listdir(store_dir)) if os.path.isdir(store_dir) else None
    if info["db_exists"]:
        try:
            conn = sqlite3.connect(MESSAGES_DB_PATH)
            cur = conn.cursor()
            info["tables"] = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            info["chat_count"] = cur.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            info["message_count"] = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            info["latest_message_time"] = cur.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
            conn.close()
        except sqlite3.Error as e:
            info["db_error"] = str(e)
    try:
        socket.create_connection(("localhost", 8080), timeout=2).close()
        info["bridge_rest_reachable"] = True
    except OSError as e:
        info["bridge_rest_reachable"] = False
        info["bridge_error"] = str(e)
    return info

def _send_job(job: Dict[str, Any]):
    """Called by the scheduler thread for each due job."""
    if job["media_message_id"]:
        path, err = whatsapp_download_media_with_error(job["media_message_id"], job["media_chat_jid"])
        if not path:
            return False, f"media download failed: {err}"
        caption = job["message"] or whatsapp_get_message_caption(job["media_message_id"], job["media_chat_jid"])
        return whatsapp_send_file(job["recipient"], path, caption)
    return whatsapp_send_message(job["recipient"], job["message"])


@mcp.tool()
def schedule_message(
    recipients: str,
    message: str = "",
    send_at: str = "",
    delay_minutes: float = 0,
    repeat: str = "none",
    media_message_id: str = "",
    media_chat_jid: str = "",
) -> Dict[str, Any]:
    """Schedule a WhatsApp message (and/or media) to be sent later. Survives server restarts.

    Give either send_at or delay_minutes. send_at is ISO-8601; with no timezone it is UTC
    (the user's timezone, Africa/Accra, is UTC+0). Several recipients are sent one after
    another with a few seconds between them.

    Args:
        recipients: Phone number(s) with country code and no + (e.g. "233558151342"), or JID(s)
                    such as a group JID ending in @g.us. For several, separate them with commas.
        message: Text to send (used as the caption when media is attached)
        send_at: When to send, e.g. "2026-11-26T09:00:00Z" or "2026-11-26T09:00:00+00:00"
        delay_minutes: Alternative to send_at: send this many minutes from now
        repeat: "none" (default), "daily" or "weekly"
        media_message_id: Optional ID of an existing message whose image/video/audio/document should be sent
        media_chat_jid: The chat JID that message is in (required with media_message_id)
    """
    if isinstance(recipients, str):
        recipients = [r for r in recipients.replace(";", ",").split(",") if r.strip()]
    try:
        cleaned = [whatsapp_normalize_recipient(r) for r in recipients]
        when = scheduler.parse_send_time(send_at, delay_minutes)
        ids = scheduler.add_jobs(cleaned, message, when, repeat, media_message_id, media_chat_jid)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {
        "success": True,
        "job_ids": ids,
        "recipients": cleaned,
        "send_at_utc": when.isoformat(timespec="seconds"),
        "repeat": repeat,
    }


@mcp.tool()
def list_scheduled(status: str = "pending", limit: int = 50) -> Dict[str, Any]:
    """List scheduled messages.

    Args:
        status: pending (default), sent, failed, missed, cancelled, unknown, or all
        limit: Maximum number of jobs to return (default 50)
    """
    jobs = scheduler.list_jobs(status, limit)
    return {"count": len(jobs), "jobs": jobs}


@mcp.tool()
def cancel_scheduled(job_id: int) -> Dict[str, Any]:
    """Cancel a pending scheduled message by its job id (from schedule_message or list_scheduled).

    Args:
        job_id: The id of the scheduled job
    """
    if scheduler.cancel(job_id):
        return {"success": True, "message": f"Job {job_id} cancelled"}
    return {"success": False, "message": f"Job {job_id} not found or is no longer pending"}


if __name__ == "__main__":
    import os
    try:
        scheduler.start(_send_job)
    except Exception as e:  # the MCP server must still come up if the scheduler cannot
        print(f"[scheduler] failed to start: {e}", flush=True)
    auth_token = os.environ.get("MCP_AUTH_TOKEN")
    oauth_password = os.environ.get("OAUTH_PASSWORD")
    if auth_token or oauth_password:
        import uvicorn
        import oauth_server

        oauth = None
        if oauth_password:
            oauth = oauth_server.OAuth(
                media_share.PUBLIC_BASE_URL,
                oauth_password,
                static_client_id=os.environ.get("OAUTH_CLIENT_ID", ""),
                static_client_secret=os.environ.get("OAUTH_CLIENT_SECRET", ""),
            )
        uvicorn.run(
            oauth_server.AuthLayer(mcp.streamable_http_app(), auth_token, oauth),
            host="0.0.0.0",
            port=8000,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    else:
        mcp.run(transport="streamable-http")

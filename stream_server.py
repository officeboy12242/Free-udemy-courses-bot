import os
import logging
from aiohttp import web
from pyrogram import Client
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Load credentials from environment variables
API_ID = int(os.environ.get("API_ID", 0) or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("STREAM_BOT_TOKEN", "")
STREAM_CHANNEL_ID = int(os.environ.get("STREAM_CHANNEL_ID", 0) or 0)
PORT = int(os.environ.get("PORT", 8080))

# Initialize Pyrogram Client (MTProto API)
# in_memory=True prevents creating .session files on disk (better for cloud hosting)
tg_app = Client(
    "stream_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

class Streamer:
    async def handle_stream(self, request):
        """Handle incoming HTTP requests and stream the file from Telegram."""
        try:
            message_id = int(request.match_info.get("message_id"))
        except ValueError:
            return web.Response(status=400, text="Invalid message ID")

        try:
            # Fetch the message from your private channel
            message = await tg_app.get_messages(STREAM_CHANNEL_ID, message_id)
        except Exception as e:
            log.error("Error fetching message: %s", e)
            return web.Response(status=500, text="Error fetching message from Telegram")

        if not message or getattr(message, "empty", True):
            return web.Response(status=404, text="Message not found")

        # Extract media (video, document, or audio)
        media = message.document or message.video or message.audio
        if not media:
            return web.Response(status=404, text="No media found in message")

        file_size = media.file_size
        file_name = getattr(media, "file_name", "video.mp4")
        mime_type = getattr(media, "mime_type", "application/octet-stream")

        headers = {
            "Content-Type": mime_type,
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{file_name}"',
            "Access-Control-Allow-Origin": "*" # Allow embedding in other sites/players
        }

        # Handle HTTP Range requests (crucial for video seeking/skipping)
        range_header = request.headers.get("Range")
        if range_header:
            range_match = range_header.replace("bytes=", "").split("-")
            start = int(range_match[0]) if range_match[0] else 0
            end = int(range_match[1]) if len(range_match) > 1 and range_match[1] else file_size - 1

            if start >= file_size or end >= file_size:
                return web.Response(status=416, text="Requested Range Not Satisfiable")

            chunk_size = end - start + 1
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            headers["Content-Length"] = str(chunk_size)

            response = web.StreamResponse(status=206, headers=headers)
            await response.prepare(request)

            offset = start
            limit = chunk_size
            
            try:
                # Stream directly from Telegram servers to the user's browser
                async for chunk in tg_app.stream_media(message, offset=offset, limit=limit):
                    await response.write(chunk)
            except Exception as e:
                log.debug("Client disconnected or error: %s", e)
            
            return response
        else:
            # Full file download
            headers["Content-Length"] = str(file_size)
            response = web.StreamResponse(status=200, headers=headers)
            await response.prepare(request)

            try:
                async for chunk in tg_app.stream_media(message):
                    await response.write(chunk)
            except Exception as e:
                log.debug("Client disconnected or error: %s", e)
            
            return response

    async def handle_search(self, request):
        """Search the Telegram channel for messages matching the query."""
        query = request.query.get("q", "")
        if not query:
            return web.json_response({"error": "Missing query parameter 'q'"}, status=400)

        results = []
        try:
            # Search messages in the channel
            async for message in tg_app.search_messages(STREAM_CHANNEL_ID, query=query, limit=20):
                if not message or getattr(message, "empty", True):
                    continue
                
                # Check if it has media
                media = message.document or message.video or message.audio
                if not media:
                    continue
                
                file_name = getattr(media, "file_name", "Unknown File")
                file_size = getattr(media, "file_size", 0)
                caption = message.caption or ""
                
                results.append({
                    "message_id": message.id,
                    "file_name": file_name,
                    "file_size": file_size,
                    "caption": caption
                })
        except Exception as e:
            log.error("Error searching messages: %s", e)
            return web.json_response({"error": "Error searching Telegram channel"}, status=500)

        return web.json_response({"results": results})

async def on_startup(app_web):
    """Start the Telegram client when the web server starts."""
    if API_ID and API_HASH and BOT_TOKEN and STREAM_CHANNEL_ID:
        await tg_app.start()
        log.info("✅ Pyrogram Client Started Successfully")
    else:
        log.warning("⚠️ Missing Telegram API credentials in .env! Streamer won't work.")

async def on_cleanup(app_web):
    """Stop the Telegram client gracefully on shutdown."""
    if tg_app.is_initialized:
        await tg_app.stop()
        log.info("🛑 Pyrogram Client Stopped")

def main():
    server = web.Application()
    streamer = Streamer()
    
    # Define our routes
    server.router.add_get("/watch/{message_id}", streamer.handle_stream)
    server.router.add_get("/search", streamer.handle_search)
    
    # Hook into startup/shutdown
    server.on_startup.append(on_startup)
    server.on_cleanup.append(on_cleanup)
    
    log.info("Starting web server on port %s...", PORT)
    web.run_app(server, port=PORT)

if __name__ == "__main__":
    main()

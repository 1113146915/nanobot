"""JsonBot channel implementation."""

import asyncio
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import JsonBotConfig


class WebhookHandler(BaseHTTPRequestHandler):
    """Handler for JsonBot webhooks."""
    channel: "JsonBotChannel" = None

    def do_POST(self):
        """Handle POST requests."""
        if self.path.startswith("/webhook"):
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(body)
                
                # Extract fields based on MoltbotClient payload
                # {"message": "...", "sender": "...", "timestamp": ..., "type": "text"}
                message = data.get("message", "")
                sender = data.get("sender", "")
                msg_type = data.get("type", "text")
                
                if message and sender and self.channel:
                    # Run in loop
                    asyncio.run_coroutine_threadsafe(
                        self.channel.handle_webhook_message(sender, message, msg_type),
                        self.channel.loop
                    )
                
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
                
            except Exception as e:
                logger.error(f"Error handling webhook: {e}")
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
            
    def log_message(self, format, *args):
        """Silence logs."""
        pass


class JsonBotChannel(BaseChannel):
    """
    Channel for integrating with json_bot (WeChat bridge).
    Receives messages via webhook and sends replies via API.
    """
    
    name: str = "json_bot"
    
    def __init__(self, config: JsonBotConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: JsonBotConfig = config
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        
    async def start(self) -> None:
        """Start the webhook server."""
        self.loop = asyncio.get_running_loop()
        self._running = True
        
        # Start webhook server in a separate thread
        WebhookHandler.channel = self
        self._server = HTTPServer(("0.0.0.0", self.config.listen_port), WebhookHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        
        logger.info(f"JsonBot webhook listening on port {self.config.listen_port}")
        
        # Keep running
        while self._running:
            await asyncio.sleep(1.0)
            
    async def stop(self) -> None:
        """Stop the channel."""
        self._running = False
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        logger.info("JsonBot channel stopped")
        
    async def send(self, msg: OutboundMessage) -> None:
        """Send message to json_bot."""
        url = f"http://{self.config.host}:{self.config.port}/reply"
        
        # Send text content if present
        if msg.content:
            payload = {
                "session_name": msg.chat_id,
                "content": msg.content,
                "type": "text"
            }
            
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, json=payload, timeout=10.0)
                    resp.raise_for_status()
            except Exception as e:
                logger.error(f"Failed to send text to json_bot: {e}")

        # Send media if present
        if msg.media:
            for media_item in msg.media:
                payload = {
                    "session_name": msg.chat_id,
                    "content": media_item,
                    "type": "file"
                }
                
                try:
                    async with httpx.AsyncClient() as client:
                        # Longer timeout for files
                        resp = await client.post(url, json=payload, timeout=60.0)
                        resp.raise_for_status()
                except Exception as e:
                    logger.error(f"Failed to send file to json_bot: {e}")
            
    async def handle_webhook_message(self, sender: str, content: str, msg_type: str) -> None:
        """Handle incoming message from webhook."""
        if not self.is_allowed(sender):
            logger.warning(f"Ignoring message from unauthorized sender: {sender}")
            return
            
        await self._handle_message(
            sender_id=sender,
            chat_id=sender,
            content=content,
            metadata={"type": msg_type}
        )

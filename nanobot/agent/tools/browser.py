"""Browser automation tool using Chrome extension relay."""

import asyncio
import json
import logging
from typing import Any, Dict, Optional
import weakref

import websockets
import websockets.server
from websockets.server import WebSocketServerProtocol

from nanobot.agent.tools.base import Tool

logger = logging.getLogger(__name__)

# Constants
DEFAULT_RELAY_PORT = 18792
RELAY_PATH = "/extension"

class HeadAwareProtocol(WebSocketServerProtocol):
    """
    Custom protocol that handles HTTP HEAD requests (used by extension for health checks).
    """
    async def handshake(self, *args: Any, **kwargs: Any) -> str:
        try:
            return await super().handshake(*args, **kwargs)
        except ValueError as e:
            if "got HEAD" in str(e):
                logger.debug("Intercepted HEAD request, sending 200 OK")
                self.transport.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                self.transport.close()
                return "/health-check"
            raise

class BrowserRelay:
    """
    WebSocket relay server for Chrome extension.
    Matches OpenClaw's extension protocol.
    """
    
    def __init__(self, port: int = DEFAULT_RELAY_PORT):
        self.port = port
        self.server = None
        self.extension_ws: Optional[WebSocketServerProtocol] = None
        self.command_id = 0
        self.pending_commands: Dict[int, asyncio.Future] = {}
        self._running = False
        
    async def start(self):
        """Start the relay server."""
        if self._running:
            return
            
        self._running = True
        logger.info(f"Starting Browser Relay on port {self.port}...")
        
        try:
            # Serve strictly on 127.0.0.1 for security
            self.server = await websockets.server.serve(
                self._handle_connection, 
                "127.0.0.1", 
                self.port,
                create_protocol=HeadAwareProtocol
            )
            logger.info(f"Browser Relay listening on ws://127.0.0.1:{self.port}")
        except OSError as e:
            logger.error(f"Failed to start Browser Relay on port {self.port}: {e}")
            self._running = False
            # Don't crash the agent if port is taken (e.g. by OpenClaw)
            # We'll just fail when the tool is used.

    async def stop(self):
        """Stop the relay server."""
        self._running = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            
    async def _handle_connection(self, websocket: WebSocketServerProtocol, path: str):
        """Handle incoming WebSocket connection."""
        if path != RELAY_PATH:
            logger.warning(f"Rejected connection to {path}")
            return
            
        logger.info("Browser extension connected!")
        self.extension_ws = websocket
        
        try:
            async for message in websocket:
                await self._handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Browser extension disconnected")
        finally:
            if self.extension_ws == websocket:
                self.extension_ws = None

    async def _handle_message(self, message: str):
        """Handle message from extension."""
        try:
            data = json.loads(message)
            
            # Handle response to our command
            if "id" in data and ("result" in data or "error" in data):
                cmd_id = data["id"]
                if cmd_id in self.pending_commands:
                    future = self.pending_commands.pop(cmd_id)
                    if not future.done():
                        if "error" in data:
                            future.set_exception(Exception(data["error"]))
                        else:
                            future.set_result(data.get("result"))
            
            # Handle heartbeat
            elif data.get("method") == "ping":
                await self.extension_ws.send(json.dumps({"method": "pong"}))
                
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    async def send_command(self, method: str, params: Optional[Dict] = None) -> Any:
        """Send command to extension and wait for result."""
        if not self.extension_ws:
            raise RuntimeError("Browser extension not connected. Please install the OpenClaw Browser Relay extension.")
            
        self.command_id += 1
        cmd_id = self.command_id
        
        # The extension expects "forwardCDPCommand" for raw CDP,
        # but we might want to implement high-level actions if the extension supports them.
        # OpenClaw's extension forwards CDP commands via "forwardCDPCommand".
        # We need to wrap our CDP commands.
        
        # However, for high-level actions like "click", we need to inject JS or use CDP input events.
        # Since we are "lightweight", let's try to implement a few helpers using Runtime.evaluate.
        
        # Protocol: { id, method: "forwardCDPCommand", params: { method: "Page.navigate", params: {...} } }
        
        payload = {
            "id": cmd_id,
            "method": "forwardCDPCommand",
            "params": {
                "method": method,
                "params": params or {}
            }
        }
        
        future = asyncio.get_running_loop().create_future()
        self.pending_commands[cmd_id] = future
        
        await self.extension_ws.send(json.dumps(payload))
        
        # Wait for response with timeout
        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            if cmd_id in self.pending_commands:
                del self.pending_commands[cmd_id]
            raise TimeoutError("Browser command timed out")

# Singleton relay instance
_relay: Optional[BrowserRelay] = None

def get_relay() -> BrowserRelay:
    global _relay
    if _relay is None:
        _relay = BrowserRelay()
    return _relay


class BrowserUseTool(Tool):
    """Control the browser via Chrome extension."""
    
    name = "browser_use"
    description = (
        "Control the browser. Requires OpenClaw Browser Relay extension. "
        "Actions: navigate, click, type, read, screenshot, evaluate."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string", 
                "enum": ["navigate", "click", "type", "read", "screenshot", "evaluate"],
                "description": "Action to perform"
            },
            "url": {"type": "string", "description": "URL for navigate"},
            "selector": {"type": "string", "description": "CSS selector for click/type/read"},
            "text": {"type": "string", "description": "Text to type"},
            "script": {"type": "string", "description": "Javascript for evaluate"}
        },
        "required": ["action"]
    }
    
    def __init__(self):
        self.relay = get_relay()
        
    async def start(self):
        """Start the relay server."""
        if not self.relay._running:
            await self.relay.start()
    
    async def execute(self, action: str, **kwargs: Any) -> str:
        try:
            if action == "navigate":
                url = kwargs.get("url")
                if not url:
                    return "Error: url is required for navigate"
                if not url.startswith("http"):
                    url = "https://" + url
                
                # Page.navigate
                await self.relay.send_command("Page.navigate", {"url": url})
                return f"Navigated to {url}"
                
            elif action == "click":
                selector = kwargs.get("selector")
                if not selector:
                    return "Error: selector is required for click"
                
                # Use JS to click
                js = f"document.querySelector('{selector}').click()"
                res = await self.relay.send_command("Runtime.evaluate", {"expression": js})
                if res and "exceptionDetails" in res:
                    return f"Error clicking {selector}: {res['exceptionDetails']}"
                return f"Clicked {selector}"
                
            elif action == "type":
                selector = kwargs.get("selector")
                text = kwargs.get("text")
                if not selector or text is None:
                    return "Error: selector and text are required for type"
                
                # Use JS to set value (simple version)
                # Ideally we should use Input.dispatchKeyEvent for real typing, but JS is easier for now.
                js = f"""
                    var el = document.querySelector('{selector}');
                    if (el) {{
                        el.value = '{text}';
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }} else {{
                        throw new Error('Element not found');
                    }}
                """
                res = await self.relay.send_command("Runtime.evaluate", {"expression": js})
                if res and "exceptionDetails" in res:
                    return f"Error typing in {selector}: {res['exceptionDetails']}"
                return f"Typed '{text}' into {selector}"
                
            elif action == "read":
                selector = kwargs.get("selector", "body") # Default to full page
                
                if selector == "body":
                    js = "document.body.innerText"
                else:
                    js = f"document.querySelector('{selector}').innerText"
                    
                res = await self.relay.send_command("Runtime.evaluate", {"expression": js, "returnByValue": True})
                if res and "exceptionDetails" in res:
                    return f"Error reading {selector}: {res['exceptionDetails']}"
                
                text = res.get("result", {}).get("value", "")
                return str(text)[:2000] # Limit output
                
            elif action == "screenshot":
                # Page.captureScreenshot
                res = await self.relay.send_command("Page.captureScreenshot", {"format": "png"})
                # We could save this to a file and return the path, or return base64.
                # Since nanobot sends files now, let's save it.
                data = res.get("data")
                if data:
                    import base64
                    import time
                    filename = f"screenshot_{int(time.time())}.png"
                    # Assume we can write to CWD or a temp dir.
                    # nanobot workspace is typically where we are.
                    import os
                    with open(filename, "wb") as f:
                        f.write(base64.b64decode(data))
                    return f"Screenshot saved to {os.path.abspath(filename)}"
                return "Failed to capture screenshot"
            
            elif action == "evaluate":
                script = kwargs.get("script")
                if not script:
                    return "Error: script is required for evaluate"
                
                res = await self.relay.send_command("Runtime.evaluate", {"expression": script, "returnByValue": True})
                if res and "exceptionDetails" in res:
                    return f"Error evaluating script: {res['exceptionDetails']}"
                
                return str(res.get("result", {}).get("value", ""))
                
            else:
                return f"Unknown action: {action}"
                
        except Exception as e:
            return f"Error executing {action}: {e}"

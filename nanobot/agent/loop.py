"""Agent loop: the core processing engine."""

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.browser import BrowserUseTool
from nanobot.agent.subagent import SubagentManager
from nanobot.session.manager import SessionManager


class AgentLoop:
    """
    The agent loop is the core processing engine.
    
    It listens for messages on the bus, maintains conversation history,
    calls tools, and interacts with the LLM.
    """
    
    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        max_iterations: int = 10,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.max_iterations = max_iterations
        
        # Components
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config
        )
        self.context = ContextBuilder(self.workspace)
        self.sessions = SessionManager(workspace)
        
        self._running = False
        
        # Register tools
        self._register_default_tools()
        
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools
        self.tools.register(ReadFileTool())
        self.tools.register(WriteFileTool())
        self.tools.register(EditFileTool())
        self.tools.register(ListDirTool())
        
        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.exec_config.restrict_to_workspace,
        ))
        
        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)
        
        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        # Browser tool
        self.tools.register(BrowserUseTool())
    
    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")
        
        # Start browser relay if present
        browser_tool = self.tools.get("browser_use")
        if browser_tool and hasattr(browser_tool, "start"):
            await browser_tool.start()
        
        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                
                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue
    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
        
        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)
        
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}")
        
        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)
        
        # Build initial messages (use get_history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            media=msg.media if msg.media else None,
        )
        
        # Agent loop
        iteration = 0
        final_content = None
        media_from_tool_results = []
        
        while iteration < self.max_iterations:  # Max turns
            iteration += 1
            
            # Call LLM
            try:
                # Stream the response to the user
                # TODO: Implement streaming to user via bus (needs update to bus/channels)
                # For now, we just wait for the full response
                
                response = await self.provider.chat(messages, self.tools.get_definitions())
            except Exception as e:
                logger.error(f"LLM error: {e}")
                final_content = f"Error calling LLM: {str(e)}"
                break
                
            # Add assistant response to history
            response_text = response.content or ""
            tool_calls = response.tool_calls
            messages.append({"role": "assistant", "content": response_text})
            if tool_calls:
                messages[-1]["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in tool_calls
                ]
            
            # If no tool calls, we're done
            if not tool_calls:
                final_content = response_text
                break
            
            # Execute tools
            for tool_call in tool_calls:
                function_name = tool_call.name
                function_args = tool_call.arguments
                tool_call_id = tool_call.id
                
                logger.info(f"Executing tool: {function_name}")
                
                try:
                    result = await self.tools.execute(function_name, function_args)
                    
                    # Special handling for tools that return media/files
                    # Check if result looks like a file path or has media indicator
                    # For now, let's assume if it starts with "Screenshot saved to", we extract it
                    if function_name == "browser_use" and str(result).startswith("Screenshot saved to "):
                        path = str(result).replace("Screenshot saved to ", "").strip()
                        media_from_tool_results.append(path)
                        
                except Exception as e:
                    logger.error(f"Tool execution error: {e}")
                    result = f"Error: {str(e)}"
                
                # Add tool result to history
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": str(result)
                })
        
        # Save session history
        # We only save the new interactions
        # session.add_message("user", msg.content) # Already done implicitly by being in history?
        # No, session.get_history() returns past messages. We need to append new ones.
        
        session.add_message("user", msg.content, media=msg.media)
        if final_content:
            session.add_message("assistant", final_content)
        
        # If we had tool calls, we should probably save them too?
        # For simplicity, we only save the final assistant response in the simple history.
        # A more advanced history would save the full chain.
        # nanobot's simple session manager might just store user/assistant pairs.
        # Let's check session manager later. For now, this is fine.
        
        if not final_content and not tool_calls:
             final_content = "I'm not sure how to respond to that."
             
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            media=media_from_tool_results
        )

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """Process system messages (e.g. from subagents)."""
        # Parse content: "origin_channel:origin_chat_id:result"
        try:
            parts = msg.content.split(":", 2)
            if len(parts) < 3:
                return None
            
            origin_channel = parts[0]
            origin_chat_id = parts[1]
            result = parts[2]
            
            # Forward result to the user
            return OutboundMessage(
                channel=origin_channel,
                chat_id=origin_chat_id,
                content=f"Subagent task completed:\n{result}"
            )
        except Exception as e:
            logger.error(f"Error processing system message: {e}")
            return None

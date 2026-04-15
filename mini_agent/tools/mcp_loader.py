"""MCP tool loader with real MCP client integration and timeout handling.

三者关系：

    MCPServerConnection (一个 server 对应一个)
        └─ ClientSession  (一个 server 一个，代表与该 server 的通信通道)
             ├─ MCPTool("tool_a")   ┐
             ├─ MCPTool("tool_b")   ├─ 同一 server 的所有工具共享同一个 session
             └─ MCPTool("tool_c")   ┘

    如果 mcp.json 里配了多个 server，则每个 server 各自有独立的
    MCPServerConnection 和 ClientSession，互不影响：

    server_A → MCPServerConnection → session_A → MCPTool("bash"), MCPTool("read_file")
    server_B → MCPServerConnection → session_B → MCPTool("search"), MCPTool("fetch")

    所有工具最终汇总到同一个 all_tools list，统一交给 Agent 使用。
"""

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# StdioServerParameters -- stdio 连接方式的配置对象 -- 用来描述"怎么启动 MCP server 进程" -- 主要是 command、args、env 这三个字段
# ClientSession --- 协议/会话层    不关心底层是什么 --- 只要给他 read_stream 和 write_stream 就能按照 MCP 协议工作
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client      # 也是传输层 -- -不过走HTTP + SSE
                                            # 服务端通过SSE持续往客户端推消息 --- 客户端通过HTTP POST把请求发回去
                                            # 适合远程MCP server -- 不需要本地启动进程
from mcp.client.stdio import stdio_client     # 这是传输层 -- 按 StdioServerParameters 启动本地进程 通过这个进程 stdin/stdout 跟 MCP server 通信
                                            # 最终产出一对流 --- read—stream 和 write-stream
from mcp.client.streamable_http import streamablehttp_client    # 还是传输层 ---- 比如sse_client 更完整
                                               #  支持 Streamable HTTP 这种 MCP 新一点的 HTTP 传输方式 -- 响应可以是普通 JSON -- 也可以是 SSE 流 -- 支持 session_id 恢复会话等机制      

from ..config import Config
from .base import Tool, ToolResult

# Connection type aliases
ConnectionType = Literal["stdio", "sse", "http", "streamable_http"]

# 5️⃣ ✅
# MCPTimeoutConfig 管理超时配置 -- 比如连接超时、执行超时、SSE读取超时等
@dataclass
class MCPTimeoutConfig:
    """MCP timeout configuration."""

    connect_timeout: float = 10.0  # Connection timeout (seconds)
    execute_timeout: float = 60.0  # Tool execution timeout (seconds)
    sse_read_timeout: float = 120.0  # SSE read timeout (seconds)


# Global default timeout config
_default_timeout_config = MCPTimeoutConfig()

# ✅
def set_mcp_timeout_config(
    connect_timeout: float | None = None,
    execute_timeout: float | None = None,
    sse_read_timeout: float | None = None,
) -> None:
    """Set global MCP timeout configuration.

    Args:
        connect_timeout: Connection timeout in seconds
        execute_timeout: Tool execution timeout in seconds
        sse_read_timeout: SSE read timeout in seconds
    """
    global _default_timeout_config
    if connect_timeout is not None:
        _default_timeout_config.connect_timeout = connect_timeout
    if execute_timeout is not None:
        _default_timeout_config.execute_timeout = execute_timeout
    if sse_read_timeout is not None:
        _default_timeout_config.sse_read_timeout = sse_read_timeout

# ✅
def get_mcp_timeout_config() -> MCPTimeoutConfig:
    """Get current MCP timeout configuration."""
    return _default_timeout_config

# 4️⃣ ✅
# MCPTool -- 单个MCP工具的包装类 --- 最终在 Agent 眼里 —— MCP 工具、bash_tool、get_skill 实际是同一类工具
class MCPTool(Tool):
    """Wrapper for MCP tools with timeout handling."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        session: ClientSession,       # ClientSession 当前程序作为 MCP client 和 某个 MCP server 之间建立好的会话对象
                                      # 一个 server 通常暴露多个 tools --- 这些 tools 共同使用同一个 session 来通信
        execute_timeout: float | None = None,
    ):
        self._name = name
        self._description = description
        self._parameters = parameters
        self._session = session
        self._execute_timeout = execute_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs) -> ToolResult:
        """Execute MCP tool via the session with timeout protection."""
        timeout = self._execute_timeout or _default_timeout_config.execute_timeout

        try:
            # Wrap call_tool with timeout
            async with asyncio.timeout(timeout):
                result = await self._session.call_tool(self._name, arguments=kwargs)
                # ClientSession 是一个 MCP 客户端会话对象 --- 负责按 MCP 协议和服务器通信 
                # 他会提供一组协议方法 -- 比如 initialize() list_tools() call_tool() 

            # MCP tool results are a list of content items
            content_parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    content_parts.append(item.text)
                else:
                    content_parts.append(str(item))

            content_str = "\n".join(content_parts)

            is_error = result.isError if hasattr(result, "isError") else False

            return ToolResult(success=not is_error, content=content_str, error=None if not is_error else "Tool returned error")

        except TimeoutError:
            return ToolResult(
                success=False,
                content="",
                error=f"MCP tool execution timed out after {timeout}s. The remote server may be slow or unresponsive.",
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"MCP tool execution failed: {str(e)}")

# 2️⃣
# MCPServerConnection -- 单个 MCP 服务器链接管理器
# 1. 根据配置决定是 stdio、sse 还是 http
# 2. 建立连接
# 3. 创建 ClientSession
# 4. 调 list_tools() 把服务器上的工具拉下来
# 5. 把这些工具包装成 MCPTool
class MCPServerConnection:
    """Manages connection to a single MCP server (STDIO or URL-based) with timeout handling."""

    def __init__(
        self,
        name: str,
        connection_type: ConnectionType = "stdio",
        # STDIO params
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        # URL-based params
        url: str | None = None,
        headers: dict[str, str] | None = None,
        # Timeout overrides (per-server)
        connect_timeout: float | None = None,
        execute_timeout: float | None = None,
        sse_read_timeout: float | None = None,
    ):
        self.name = name
        self.connection_type = connection_type
        # STDIO
        self.command = command
        self.args = args or []
        self.env = env or {}
        # URL-based
        self.url = url
        self.headers = headers or {}
        # Timeout settings (per-server overrides)
        self.connect_timeout = connect_timeout
        self.execute_timeout = execute_timeout
        self.sse_read_timeout = sse_read_timeout
        # Connection state
        self.session: ClientSession | None = None
        self.exit_stack: AsyncExitStack | None = None
        self.tools: list[MCPTool] = []

    def _get_connect_timeout(self) -> float:
        """Get effective connect timeout."""
        return self.connect_timeout or _default_timeout_config.connect_timeout

    def _get_sse_read_timeout(self) -> float:
        """Get effective SSE read timeout."""
        return self.sse_read_timeout or _default_timeout_config.sse_read_timeout

    def _get_execute_timeout(self) -> float:
        """Get effective execute timeout."""
        return self.execute_timeout or _default_timeout_config.execute_timeout

    # 3️⃣
    async def connect(self) -> bool:
        """Connect to the MCP server with timeout protection."""
        connect_timeout = self._get_connect_timeout()

        try:
            # AsyncExitStack 资源回收管理器 -- 不管是哪一个 client -- 只要加入了 exit_stack 管理 -- 以后调用 exit_stack.aclose() 就能自动清理连接资源
            self.exit_stack = AsyncExitStack()

            # Wrap connection with timeout
            async with asyncio.timeout(connect_timeout):
                if self.connection_type == "stdio":
                    read_stream, write_stream = await self._connect_stdio()
                elif self.connection_type == "sse":
                    read_stream, write_stream = await self._connect_sse()
                else:  # http / streamable_http
                    read_stream, write_stream = await self._connect_streamable_http()

                # Enter client session context
                session = await self.exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
                self.session = session

                # Initialize the session
                await session.initialize()

                # List available tools
                tools_list = await session.list_tools()

            # Wrap each tool with execute timeout
            execute_timeout = self._get_execute_timeout()
            for tool in tools_list.tools:
                parameters = tool.inputSchema if hasattr(tool, "inputSchema") else {}
                mcp_tool = MCPTool(
                    name=tool.name,
                    description=tool.description or "",
                    parameters=parameters,
                    session=session,
                    execute_timeout=execute_timeout,
                )
                self.tools.append(mcp_tool)

            # 打印连接摘要
            conn_info = self.url if self.url else self.command
            print(f"✓ Connected to MCP server '{self.name}' ({self.connection_type}: {conn_info}) - loaded {len(self.tools)} tools")
            for tool in self.tools:
                desc = tool.description[:60] if len(tool.description) > 60 else tool.description
                print(f"  - {tool.name}: {desc}...")
            return True

        except TimeoutError:
            print(f"✗ Connection to MCP server '{self.name}' timed out after {connect_timeout}s")
            if self.exit_stack:
                await self.exit_stack.aclose()
                self.exit_stack = None
            return False

        except Exception as e:
            print(f"✗ Failed to connect to MCP server '{self.name}': {e}")
            if self.exit_stack:
                await self.exit_stack.aclose()
                self.exit_stack = None
            import traceback

            traceback.print_exc()
            return False

    # 用启动本地线程的方式连上 MCP Server
    async def _connect_stdio(self):
        """Connect via STDIO transport."""
        server_params = StdioServerParameters(command=self.command, args=self.args, env=self.env if self.env else None)
        return await self.exit_stack.enter_async_context(stdio_client(server_params))

    # 用 HTTP + SSE 的方式连上 MCP Server
    async def _connect_sse(self):
        """Connect via SSE transport with timeout parameters."""
        connect_timeout = self._get_connect_timeout()
        sse_read_timeout = self._get_sse_read_timeout()

        return await self.exit_stack.enter_async_context(
            sse_client(
                url=self.url,
                headers=self.headers if self.headers else None,
                timeout=connect_timeout,
                sse_read_timeout=sse_read_timeout,
            )
        )

    # 用 Streamable HTTP 的方式连上 MCP Server
    async def _connect_streamable_http(self):
        """Connect via Streamable HTTP transport with timeout parameters."""
        connect_timeout = self._get_connect_timeout()
        sse_read_timeout = self._get_sse_read_timeout()

        # streamablehttp_client returns (read, write, get_session_id)
        read_stream, write_stream, _ = await self.exit_stack.enter_async_context(
            streamablehttp_client(
                url=self.url,
                headers=self.headers if self.headers else None,
                timeout=connect_timeout,
                sse_read_timeout=sse_read_timeout,
            )
        )
        return read_stream, write_stream

    async def disconnect(self):
        """Properly disconnect from the MCP server."""
        if self.exit_stack:
            try:
                await self.exit_stack.aclose()
            except Exception:
                # anyio cancel scope may raise RuntimeError or ExceptionGroup
                # when stdio_client's task group is closed from a different
                # task context during shutdown.
                pass
            finally:
                self.exit_stack = None
                self.session = None


# Global connections registry
_mcp_connections: list[MCPServerConnection] = []


# ✅
def _determine_connection_type(server_config: dict) -> ConnectionType:
    """Determine connection type from server config."""
    explicit_type = server_config.get("type", "").lower()
    if explicit_type in ("stdio", "sse", "http", "streamable_http"):
        return explicit_type
    # Auto-detect: if url exists, default to streamable_http; otherwise stdio
    if server_config.get("url"):
        return "streamable_http"
    return "stdio"

# ✅
def _resolve_mcp_config_path(config_path: str) -> Path | None:
    """
    Resolve MCP config path with fallback logic.

    Priority:
    1. If the specified path exists, use it
    2. If mcp.json doesn't exist, try mcp-example.json in the same directory
    3. Return None if no config found

    Args:
        config_path: User-specified config path

    Returns:
        Resolved Path object or None if not found
    """
    config_file = Path(config_path)

    # If specified path exists, use it directly
    if config_file.exists():
        return config_file

    # Reuse the same priority search as the CLI for bare config filenames.
    # This covers the common case where callers use the default "mcp.json"
    # but the actual file lives under mini_agent/config/.
    if config_file.name == config_path:
        found = Config.find_config_file(config_file.name)
        if found:
            return found

    # Fallback: if looking for mcp.json, try mcp-example.json
    if config_file.name == "mcp.json":
        example_candidates = []

        if config_file.parent != Path():
            example_candidates.append(config_file.parent / "mcp-example.json")

        found_example = Config.find_config_file("mcp-example.json")
        if found_example:
            example_candidates.append(found_example)

        for example_file in example_candidates:
            if example_file.exists():
                print(f"mcp.json not found, using template: {example_file}")
                return example_file

    return None

# 1️⃣
# 总入口函数
# 1. 读取 mcp.json
# 2. 遍历每一个 MCP server 配置
# 3. 对每个 server 建立连接
# 4. 汇总所有工具
# 5. 返回 list[Tool]
async def load_mcp_tools_async(config_path: str = "mcp.json") -> list[Tool]:
    """
    Load MCP tools from config file.

    This function:
    1. Reads the MCP config file (with fallback to mcp-example.json)
    2. Connects to each server (STDIO or URL-based)
    3. Fetches tool definitions
    4. Wraps them as Tool objects

    Supported config formats:
    - STDIO: {"command": "...", "args": [...], "env": {...}}
    - URL-based: {"url": "https://...", "type": "sse|http|streamable_http", "headers": {...}}

    Per-server timeout overrides (optional):
    - "connect_timeout": float - Connection timeout in seconds
    - "execute_timeout": float - Tool execution timeout in seconds
    - "sse_read_timeout": float - SSE read timeout in seconds

    Note:
    - If mcp.json is not found, will automatically fallback to mcp-example.json
    - User-specific mcp.json should be created by copying mcp-example.json

    Args:
        config_path: Path to MCP configuration file (default: "mcp.json")

    Returns:
        List of Tool objects representing MCP tools
    """
    # global -- -函数赋值时 -- 明确操作外面的全局变量
    global _mcp_connections

    config_file = _resolve_mcp_config_path(config_path)

    if config_file is None:
        print(f"MCP config not found: {config_path}")
        return []

    try:
        with open(config_file, encoding="utf-8") as f:
            config = json.load(f)

        mcp_servers = config.get("mcpServers", {})

        if not mcp_servers:
            print("No MCP servers configured")
            return []

        all_tools = []

        # Connect to each enabled server
        for server_name, server_config in mcp_servers.items():
            if server_config.get("disabled", False):
                print(f"Skipping disabled server: {server_name}")
                continue

            conn_type = _determine_connection_type(server_config)
            url = server_config.get("url")
            command = server_config.get("command")

            # Validate config
            if conn_type == "stdio" and not command:
                print(f"No command specified for STDIO server: {server_name}")
                continue
            if conn_type in ("sse", "http", "streamable_http") and not url:
                print(f"No url specified for {conn_type.upper()} server: {server_name}")
                continue

            connection = MCPServerConnection(
                name=server_name,
                connection_type=conn_type,
                command=command,
                args=server_config.get("args", []),
                env=server_config.get("env", {}),
                url=url,
                headers=server_config.get("headers", {}),
                # Per-server timeout overrides from mcp.json
                connect_timeout=server_config.get("connect_timeout"),
                execute_timeout=server_config.get("execute_timeout"),
                sse_read_timeout=server_config.get("sse_read_timeout"),
            )
            success = await connection.connect()

            if success:
                _mcp_connections.append(connection)
                all_tools.extend(connection.tools)

        print(f"\nTotal MCP tools loaded: {len(all_tools)}")

        return all_tools

    except Exception as e:
        print(f"Error loading MCP config: {e}")
        import traceback

        traceback.print_exc()
        return []


# 7️⃣
async def cleanup_mcp_connections():
    """Clean up all MCP connections."""
    global _mcp_connections
    for connection in _mcp_connections:
        await connection.disconnect()
    _mcp_connections.clear()

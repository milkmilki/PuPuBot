from mcp.server import MCPServer

server = MCPServer("fake-modern")


@server.tool()
def echo(query: str) -> str:
    """Echo a query for MCP transport tests."""

    return f"modern result for {query}"


if __name__ == "__main__":
    server.run(transport="stdio")

import uvicorn
from fastapi import FastAPI
import asyncio
import threading

# Import the MCP server app
from mcp_server import app as mcp_app

# Create a simple health endpoint app
health_app = FastAPI(title="MCP Health")

@health_app.get("/health")
async def health():
    return {"status": "healthy"}

@health_app.get("/ready")
async def ready():
    return {"status": "ready"}

# Run both apps on different ports
if __name__ == "__main__":
    # Start health server on port 8001
    config = uvicorn.Config(health_app, host="0.0.0.0", port=8001, log_level="info")
    health_server = uvicorn.Server(config)
    
    # Note: FastMCP's app.run() blocks, so we need a different approach
    # Actually, let's just run the MCP server directly and accept no health endpoint
    from mcp_server import app
    uvicorn.run(app, host="0.0.0.0", port=8000)
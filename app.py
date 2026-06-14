"""
==============================================================================
KubeOps-AI — FastAPI Dashboard Server
==============================================================================

Wraps the existing ADK/MCP troubleshooting agent inside a lightweight
FastAPI application. Serves the single-file frontend and exposes a
POST endpoint for the AI to process incident queries.

ARCHITECTURE:
    Browser (index.html)
        └─ POST /api/troubleshoot ──▶ FastAPI (this file)
                                          └─ ADK Agent ──stdio──▶ MCP Server (server.py)
                                                                      └─ K8s API

USAGE:
    uvicorn app:app --reload --host 0.0.0.0 --port 8000

==============================================================================
"""

import os
import sys
import uuid
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

# Import the SRE system prompt from the CLI agent module (avoids duplication)
from agent import SRE_SYSTEM_PROMPT

# ==============================================================================
# CONFIGURATION
# ==============================================================================
load_dotenv()

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("kubeops-dashboard")

AGENT_MODEL = "gemini-2.5-flash"
APP_NAME = "kubeops-ai"
SERVER_SCRIPT = str(Path(__file__).parent / "server.py")
HTML_FILE = str(Path(__file__).parent / "index.html")


# ==============================================================================
# FASTAPI LIFESPAN — Initialize Agent on Startup
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Initializes the ADK agent, MCP toolset, and session service when the
    server starts. Stores them on app.state for use by request handlers.
    Ensures clean shutdown on exit.
    """
    logger.info("=" * 60)
    logger.info("KubeOps-AI Dashboard Server starting...")
    logger.info("=" * 60)

    # Validate prerequisites
    if not os.environ.get("GOOGLE_API_KEY"):
        logger.critical("GOOGLE_API_KEY is not set. Aborting.")
        sys.exit(1)

    if not Path(SERVER_SCRIPT).exists():
        logger.critical("MCP server script not found: %s", SERVER_SCRIPT)
        sys.exit(1)

    # --- Initialize MCP Toolset (spawns server.py as subprocess) ---
    logger.info("Connecting to MCP server via stdio: %s", SERVER_SCRIPT)
    mcp_toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=[SERVER_SCRIPT],
            ),
            timeout=30,
        ),
    )

    # --- Create the SRE Agent ---
    logger.info("Creating SRE agent with model '%s'...", AGENT_MODEL)
    sre_agent = Agent(
        model=AGENT_MODEL,
        name="kubeops_sre_agent",
        description=(
            "Expert Kubernetes SRE troubleshooting agent that diagnoses "
            "cluster issues using read-only tools and provides remediation."
        ),
        instruction=SRE_SYSTEM_PROMPT,
        tools=[mcp_toolset],
    )

    # --- Set up Session Service and Runner ---
    session_service = InMemorySessionService()
    runner = Runner(
        agent=sre_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    # Store on app state for request handlers
    app.state.runner = runner
    app.state.session_service = session_service

    logger.info("✅ KubeOps-AI Dashboard ready at http://localhost:8000")
    yield

    # Cleanup
    logger.info("Shutting down KubeOps-AI Dashboard...")


# ==============================================================================
# FASTAPI APPLICATION
# ==============================================================================
app = FastAPI(
    title="KubeOps-AI Dashboard",
    version="1.0.0",
    description="AI-powered Kubernetes troubleshooting dashboard",
    lifespan=lifespan,
)

# CORS — allow the frontend (same-origin in production, but useful for dev)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==============================================================================
# REQUEST / RESPONSE MODELS
# ==============================================================================
class TroubleshootRequest(BaseModel):
    """Incoming troubleshooting query from the dashboard."""
    query: str


class TroubleshootResponse(BaseModel):
    """Agent's diagnostic response."""
    response: str
    session_id: str


# ==============================================================================
# ROUTES
# ==============================================================================
@app.get("/", include_in_schema=False)
async def serve_frontend():
    """Serve the single-file HTML dashboard."""
    if not Path(HTML_FILE).exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(HTML_FILE, media_type="text/html")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Prevent 404 for missing favicon."""
    return Response(status_code=204)


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {"status": "healthy", "service": "kubeops-ai-dashboard"}


@app.post("/api/troubleshoot", response_model=TroubleshootResponse)
async def troubleshoot(request: TroubleshootRequest):
    """
    Process an incident query through the KubeOps-AI SRE agent.

    The agent will autonomously:
    1. Parse the alert / incident description
    2. Call MCP tools (get_pod_status, fetch_pod_logs, describe_deployment)
    3. Analyze the gathered evidence
    4. Return a structured diagnosis with remediation steps
    """
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    # Generate a unique session ID for this troubleshooting request
    session_id = f"dashboard-{uuid.uuid4().hex[:12]}"
    logger.info("New troubleshooting request [session=%s]: %.100s...", session_id, request.query)

    try:
        # Create a fresh session for this incident
        await app.state.session_service.create_session(
            app_name=APP_NAME,
            user_id="dashboard_user",
            session_id=session_id,
        )

        # Build the user message
        content = types.Content(
            role="user",
            parts=[types.Part(text=request.query)],
        )

        # Run the agent and collect the final response
        final_response = ""
        async for event in app.state.runner.run_async(
            user_id="dashboard_user",
            session_id=session_id,
            new_message=content,
        ):
            # Log tool invocations for observability
            if event.actions and hasattr(event.actions, 'tool_calls') and event.actions.tool_calls:
                for tc in event.actions.tool_calls:
                    logger.info(
                        "[session=%s] Tool call: %s(%s)",
                        session_id,
                        tc.function_call.name,
                        tc.function_call.args,
                    )

            # Capture the final response text
            if event.is_final_response() and event.content and event.content.parts:
                final_response = event.content.parts[0].text

        if not final_response:
            final_response = (
                "⚠️ The agent completed its analysis but did not produce a "
                "final response. This may indicate a tool communication issue. "
                "Please check the server logs."
            )

        logger.info("[session=%s] Diagnosis complete (%d chars).", session_id, len(final_response))

        return TroubleshootResponse(
            response=final_response,
            session_id=session_id,
        )

    except Exception as e:
        logger.exception("[session=%s] Error during troubleshooting: %s", session_id, e)
        raise HTTPException(
            status_code=500,
            detail=f"Agent error: {str(e)}. Check the server logs for details.",
        )


# ==============================================================================
# ENTRY POINT (for `python app.py` convenience)
# ==============================================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )

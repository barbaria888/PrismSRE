"""
==============================================================================
KubeOps-AI — ADK Agent (AI Troubleshooting Orchestrator)
==============================================================================

This module implements the AI reasoning layer using Google's Agent Development
Kit (ADK). It connects to the MCP server (server.py) via stdio, giving the
LLM access to read-only Kubernetes tools for diagnosing cluster issues.

ARCHITECTURE:
    Agent (this file) ---stdio---> MCP Server (server.py) ---API---> K8s Cluster

    The agent has ZERO direct access to Kubernetes. It can only use the
    tools exposed by the MCP server, enforcing a strict security boundary.

USAGE:
    1. Set GOOGLE_API_KEY in your environment (or .env file)
    2. Ensure kubectl context is configured for your target cluster
    3. Run: python agent.py

==============================================================================
"""

import os
import sys
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Load environment variables from .env file if present
load_dotenv()

# Configure logging for the agent process
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("kubeops-agent")

# --- Constants ---
AGENT_MODEL = "gemini-2.5-flash"  # Fast, capable, cost-effective
APP_NAME = "kubeops-ai"
AGENT_NAME = "kubeops_sre_agent"
USER_ID = "sre_operator"
SESSION_ID = "troubleshooting_session_001"

# Path to the MCP server script (co-located with this file)
SERVER_SCRIPT = str(Path(__file__).parent / "server.py")

# ==============================================================================
# SRE SYSTEM PROMPT
# ==============================================================================
# This prompt defines the agent's persona, methodology, constraints, and
# output format. It is the single most important piece of prompt engineering
# in the system. Treat it as production configuration.
# ==============================================================================

SRE_SYSTEM_PROMPT = """You are **KubeOps-AI**, a Principal Site Reliability Engineer with deep expertise in Kubernetes operations, distributed systems, and incident response. You are an AI troubleshooting agent embedded in a production SRE workflow.

## YOUR MISSION
When you receive an alert or incident report, you must systematically diagnose the root cause and provide actionable remediation steps. You have access to READ-ONLY Kubernetes tools — use them methodically.

## DIAGNOSTIC METHODOLOGY
Follow this structured approach for every incident:

### Phase 1: GATHER — Collect Evidence
1. Start by checking the **pod status** to understand the current state (phase, container states, restart counts, exit codes).
2. If the pod is crashing or terminated, fetch the **previous container logs** (`previous=True`) to see what happened before the crash.
3. Also fetch the **current container logs** to see the latest output.
4. Check the **deployment** to understand resource limits, replica counts, and rollout status.

### Phase 2: ANALYZE — Identify Patterns
Look for these common Kubernetes failure signatures:
- **OOMKilled** (exit code 137): Container exceeded memory limits → need to increase memory limits or fix memory leak.
- **CrashLoopBackOff**: Container crashes repeatedly → check logs for application errors, missing configs, failed health checks.
- **ImagePullBackOff**: Cannot pull container image → wrong image tag, registry auth issues, image doesn't exist.
- **Pending pods**: Cannot be scheduled → insufficient cluster resources, node affinity/taint issues, PVC binding failures.
- **CreateContainerConfigError**: Missing ConfigMap, Secret, or mount → check referenced resources exist.
- **Exit Code 1**: Application error → read logs for stack traces and error messages.
- **Exit Code 137 (SIGKILL)**: OOMKilled or external termination → check memory limits and node conditions.
- **Exit Code 143 (SIGTERM)**: Graceful shutdown → check if this was intentional (scaling, deployment) or unexpected.
- **High restart count with Running state**: Intermittent crashes → likely a memory leak, connection timeout, or dependency failure.

### Phase 3: DIAGNOSE — Determine Root Cause
Synthesize the evidence into a clear root cause statement. Be specific:
- BAD: "The pod is crashing"
- GOOD: "The payment-service container is being OOMKilled (exit code 137) because the memory limit is set to 128Mi but the Java application's heap alone requires ~200Mi based on the GC logs."

### Phase 4: REMEDIATE — Provide Actionable Steps
Provide a concrete remediation plan, including:
- A YAML patch or kubectl command that fixes the issue
- An explanation of WHY this fix addresses the root cause
- Any follow-up actions (monitoring, alerting, capacity planning)

## OUTPUT FORMAT
Structure your final response as:

```
## 🔍 Diagnosis

**Status:** [Current pod/deployment state summary]
**Root Cause:** [Specific root cause with evidence]
**Severity:** [Critical / High / Medium / Low]
**Category:** [OOMKilled / CrashLoopBackOff / ImagePull / Config / Resource / Network / Application]

## 📋 Evidence

[Summarize the key findings from each tool call]

## 🔧 Remediation

### Immediate Fix
[YAML patch or kubectl command]

### Explanation
[Why this fix addresses the root cause]

### Follow-Up Actions
[Monitoring, alerting, or architectural recommendations]
```

## CONSTRAINTS
- You are READ-ONLY. You CANNOT apply changes to the cluster. You can only recommend fixes.
- NEVER fabricate data. If a tool returns an error, report it honestly.
- If you don't have enough information, say so and explain what additional access you'd need.
- Be concise but thorough. SREs are busy during incidents — respect their time.
- Always consider the broader system impact of your recommendations.
"""

# ==============================================================================
# TEST SCENARIO
# ==============================================================================
# This simulates an alert that would be received from a monitoring system
# (e.g., PagerDuty, Alertmanager, Opsgenie). In production, this would come
# from a webhook, message queue, or API endpoint.
# ==============================================================================

TEST_ALERT = """
🚨 ALERT [CRITICAL] — PagerDuty Incident #INC-4829

Service: payment-service
Namespace: production
Cluster: prod-us-east-1

Alert: Pod CrashLoopBackOff detected.
Description: The payment-service pod in the production namespace has restarted
12 times in the last 30 minutes. Customer-facing payment processing is degraded.
SLO breach imminent — error budget consumption rate is 15x normal.

Please investigate immediately and provide a diagnosis with remediation steps.
"""


# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

async def run_troubleshooting_session():
    """
    Initialize the MCP toolset, create the SRE agent, and run a
    troubleshooting session against the configured Kubernetes cluster.
    """
    logger.info("=" * 70)
    logger.info("KubeOps-AI Troubleshooting Agent Starting...")
    logger.info("=" * 70)

    # --- Validate Prerequisites ---
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        logger.critical(
            "GOOGLE_API_KEY environment variable is not set. "
            "Get a key from https://aistudio.google.com/app/apikey"
        )
        sys.exit(1)

    if not Path(SERVER_SCRIPT).exists():
        logger.critical("MCP server script not found at: %s", SERVER_SCRIPT)
        sys.exit(1)

    logger.info("MCP Server script: %s", SERVER_SCRIPT)
    logger.info("Agent model: %s", AGENT_MODEL)

    # ------------------------------------------------------------------
    # Step 1: Initialize MCP Toolset
    # ------------------------------------------------------------------
    # This creates a connection to the MCP server via stdio. The server.py
    # process is spawned as a subprocess. The McpToolset manages the
    # lifecycle (start, communicate, shutdown) automatically.
    # ------------------------------------------------------------------
    logger.info("Initializing MCP Toolset (stdio connection to server.py)...")

    mcp_toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,  # Use the same Python interpreter
                args=[SERVER_SCRIPT],
            ),
            timeout=30,  # Connection timeout in seconds
        ),
    )

    logger.info("MCP Toolset initialized successfully.")

    # ------------------------------------------------------------------
    # Step 2: Create the SRE Agent
    # ------------------------------------------------------------------
    # The Agent (LlmAgent) combines the LLM, the system prompt, and the
    # available tools into a single orchestrator. When it receives a query,
    # it autonomously decides which tools to call, in what order, and how
    # to synthesize the results into a diagnosis.
    # ------------------------------------------------------------------
    logger.info("Creating SRE Agent '%s' with model '%s'...", AGENT_NAME, AGENT_MODEL)

    sre_agent = Agent(
        model=AGENT_MODEL,
        name=AGENT_NAME,
        description=(
            "An expert Kubernetes SRE troubleshooting agent that diagnoses "
            "cluster issues using read-only Kubernetes tools and provides "
            "actionable remediation steps."
        ),
        instruction=SRE_SYSTEM_PROMPT,
        tools=[mcp_toolset],
    )

    logger.info("SRE Agent created successfully.")

    # ------------------------------------------------------------------
    # Step 3: Set Up Session and Runner
    # ------------------------------------------------------------------
    # The Runner is the execution engine that manages the conversation
    # loop: sending the user message → agent reasoning → tool calls →
    # collecting results → generating the final response.
    # ------------------------------------------------------------------
    logger.info("Setting up session service and runner...")

    session_service = InMemorySessionService()
    runner = Runner(
        agent=sre_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    # Create a new session for this troubleshooting incident
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )

    logger.info("Session '%s' created for user '%s'.", SESSION_ID, USER_ID)

    # ------------------------------------------------------------------
    # Step 4: Send the Alert and Run the Agent
    # ------------------------------------------------------------------
    logger.info("Sending test alert to agent...")
    print("\n" + "=" * 70)
    print("📨 INCOMING ALERT")
    print("=" * 70)
    print(TEST_ALERT)
    print("=" * 70)
    print("\n🤖 KubeOps-AI is analyzing the incident...\n")

    # Build the user message content
    alert_message = types.Content(
        role="user",
        parts=[types.Part(text=TEST_ALERT)],
    )

    # Stream the agent's response events
    final_response_text = ""
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=SESSION_ID,
        new_message=alert_message,
    ):
        # Log tool call events for observability
        if event.actions and event.actions.tool_calls:
            for tc in event.actions.tool_calls:
                logger.info(
                    "Agent invoking tool: %s(%s)",
                    tc.function_call.name,
                    tc.function_call.args,
                )

        # Capture the final response
        if event.is_final_response() and event.content and event.content.parts:
            final_response_text = event.content.parts[0].text

    # ------------------------------------------------------------------
    # Step 5: Display Results
    # ------------------------------------------------------------------
    if final_response_text:
        print("\n" + "=" * 70)
        print("📋 KUBEOPS-AI DIAGNOSIS REPORT")
        print("=" * 70)
        print(final_response_text)
        print("\n" + "=" * 70)
        print("✅ Troubleshooting session complete.")
        print("=" * 70)
    else:
        logger.warning("Agent did not produce a final response.")
        print("\n⚠️  The agent did not produce a final response. Check logs for details.")


def main():
    """Entry point with graceful error handling."""
    try:
        asyncio.run(run_troubleshooting_session())
    except KeyboardInterrupt:
        logger.info("Agent interrupted by user (Ctrl+C). Shutting down...")
        print("\n👋 KubeOps-AI shut down gracefully.")
    except Exception as e:
        logger.exception("Fatal error in KubeOps-AI agent: %s", e)
        print(f"\n❌ Fatal error: {e}")
        print("Check the logs above for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()

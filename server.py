"""
==============================================================================
KubeOps-AI — MCP Server (Kubernetes Gateway)
==============================================================================

This module implements a Model Context Protocol (MCP) server using FastMCP
that exposes a strict set of READ-ONLY Kubernetes tools to the ADK agent.

SECURITY DESIGN:
    - This server is the ONLY component that touches the Kubernetes API.
    - It exposes exactly 3 read-only tools — no write, no delete, no exec.
    - No arbitrary command execution is possible through this interface.
    - All inputs are validated; all K8s API errors are caught and sanitized.

COMMUNICATION:
    - Runs in stdio mode (default). stdout is reserved for MCP JSON-RPC.
    - ALL server logging goes to stderr via Python's logging module.

USAGE:
    python server.py

==============================================================================
"""

import sys
import re
import logging
from typing import Optional

from fastmcp import FastMCP
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# ==============================================================================
# LOGGING CONFIGURATION
# ==============================================================================
# CRITICAL: In stdio MCP mode, stdout is reserved for JSON-RPC messages.
# ALL logging MUST go to stderr to avoid corrupting the protocol stream.
# ==============================================================================
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("kubeops-mcp-server")

# ==============================================================================
# KUBERNETES CLIENT INITIALIZATION
# ==============================================================================
# Attempt in-cluster config first (for pod-based deployment), then fall back
# to the local kubeconfig file (for development).
# ==============================================================================
try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster Kubernetes configuration.")
except config.config_exception.ConfigException:
    try:
        config.load_kube_config()
        logger.info("Loaded kubeconfig from default location (~/.kube/config).")
    except config.config_exception.ConfigException as e:
        logger.critical(
            "FATAL: Could not load Kubernetes configuration. "
            "Ensure a valid kubeconfig exists or run inside a cluster. Error: %s", e
        )
        sys.exit(1)

# Initialize typed API clients for the specific resource groups we need.
core_v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

logger.info("Kubernetes API clients initialized successfully.")

# ==============================================================================
# INPUT VALIDATION
# ==============================================================================
# Kubernetes resource names follow RFC 1123: lowercase alphanumeric, hyphens,
# dots, and must start/end with an alphanumeric character. Max 253 chars.
# We enforce this to prevent injection or malformed API calls.
# ==============================================================================
K8S_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9\.\-]{0,251}[a-z0-9])?$")


def _validate_k8s_name(value: str, field_name: str) -> None:
    """
    Validate that a string conforms to Kubernetes naming conventions (RFC 1123).

    Args:
        value: The name to validate.
        field_name: Human-readable field name for error messages.

    Raises:
        ValueError: If the name is invalid.
    """
    if not value or not K8S_NAME_PATTERN.match(value):
        raise ValueError(
            f"Invalid {field_name}: '{value}'. Must be a valid Kubernetes name "
            f"(lowercase alphanumeric, hyphens, dots; 1-253 chars; "
            f"must start and end with alphanumeric)."
        )


def _format_api_error(e: ApiException, resource_type: str, name: str, namespace: str) -> str:
    """
    Format a Kubernetes API exception into a clean, LLM-friendly error message.

    Args:
        e: The ApiException from the kubernetes client.
        resource_type: Type of resource (e.g., "Pod", "Deployment").
        name: Name of the resource.
        namespace: Namespace of the resource.

    Returns:
        A sanitized, human-readable error string.
    """
    if e.status == 404:
        return (
            f"ERROR: {resource_type} '{name}' not found in namespace '{namespace}'. "
            f"Please verify the resource name and namespace are correct."
        )
    elif e.status == 403:
        return (
            f"ERROR: Permission denied when accessing {resource_type} '{name}' "
            f"in namespace '{namespace}'. The service account may lack the required "
            f"RBAC permissions (e.g., 'get' on '{resource_type.lower()}s')."
        )
    else:
        return (
            f"ERROR: Kubernetes API returned status {e.status} when accessing "
            f"{resource_type} '{name}' in namespace '{namespace}'. "
            f"Reason: {e.reason}."
        )


# ==============================================================================
# MCP SERVER INITIALIZATION
# ==============================================================================
mcp = FastMCP("KubeOps-AI Kubernetes Gateway")

logger.info("FastMCP server 'KubeOps-AI Kubernetes Gateway' initialized.")


# ==============================================================================
# TOOL 1: get_pod_status
# ==============================================================================
@mcp.tool()
def get_pod_status(namespace: str, pod_name: str) -> str:
    """
    Get the current status of a Kubernetes pod, including its phase and
    the state of each container. Use this tool to check if a pod is
    Running, Pending, Failed, or in a CrashLoopBackOff state.

    Args:
        namespace: The Kubernetes namespace where the pod resides (e.g., 'production', 'default').
        pod_name: The exact name of the pod to inspect (e.g., 'payment-service-7b9f4c6d8-x2k9p').

    Returns:
        A structured text report containing the pod phase, conditions,
        and detailed container state information including restart counts,
        exit codes, and termination reasons (e.g., OOMKilled).
    """
    logger.info("Tool invoked: get_pod_status(namespace='%s', pod_name='%s')", namespace, pod_name)

    # --- Input Validation ---
    try:
        _validate_k8s_name(namespace, "namespace")
        _validate_k8s_name(pod_name, "pod_name")
    except ValueError as e:
        return str(e)

    # --- Kubernetes API Call ---
    try:
        pod = core_v1.read_namespaced_pod_status(name=pod_name, namespace=namespace)
    except ApiException as e:
        return _format_api_error(e, "Pod", pod_name, namespace)

    # --- Build Response ---
    status = pod.status
    lines = [
        f"=== Pod Status Report ===",
        f"Name:       {pod.metadata.name}",
        f"Namespace:  {pod.metadata.namespace}",
        f"Phase:      {status.phase}",
        f"Pod IP:     {status.pod_ip or 'N/A'}",
        f"Host IP:    {status.host_ip or 'N/A'}",
        f"Start Time: {status.start_time or 'N/A'}",
    ]

    # Pod Conditions (e.g., Ready, PodScheduled, Initialized)
    if status.conditions:
        lines.append("\n--- Pod Conditions ---")
        for cond in status.conditions:
            lines.append(
                f"  {cond.type}: {cond.status}"
                f"{' (Reason: ' + cond.reason + ')' if cond.reason else ''}"
                f"{' — ' + cond.message if cond.message else ''}"
            )

    # Container Statuses (the core diagnostic data)
    if status.container_statuses:
        lines.append("\n--- Container Statuses ---")
        for cs in status.container_statuses:
            lines.append(f"\n  Container: {cs.name}")
            lines.append(f"    Image:          {cs.image}")
            lines.append(f"    Ready:          {cs.ready}")
            lines.append(f"    Restart Count:  {cs.restart_count}")
            lines.append(f"    Started:        {cs.started}")

            # Determine current state (exactly one of waiting/running/terminated is set)
            state = cs.state
            if state.waiting:
                lines.append(f"    State:          Waiting")
                lines.append(f"    Reason:         {state.waiting.reason or 'Unknown'}")
                if state.waiting.message:
                    lines.append(f"    Message:        {state.waiting.message}")
            elif state.running:
                lines.append(f"    State:          Running")
                lines.append(f"    Started At:     {state.running.started_at}")
            elif state.terminated:
                lines.append(f"    State:          Terminated")
                lines.append(f"    Reason:         {state.terminated.reason or 'Unknown'}")
                lines.append(f"    Exit Code:      {state.terminated.exit_code}")
                if state.terminated.signal:
                    lines.append(f"    Signal:         {state.terminated.signal}")
                if state.terminated.message:
                    lines.append(f"    Message:        {state.terminated.message}")
                lines.append(f"    Started At:     {state.terminated.started_at}")
                lines.append(f"    Finished At:    {state.terminated.finished_at}")

            # Last terminated state (crucial for diagnosing CrashLoopBackOff)
            if cs.last_state and cs.last_state.terminated:
                lt = cs.last_state.terminated
                lines.append(f"    --- Last Termination ---")
                lines.append(f"    Reason:         {lt.reason or 'Unknown'}")
                lines.append(f"    Exit Code:      {lt.exit_code}")
                if lt.signal:
                    lines.append(f"    Signal:         {lt.signal}")
                if lt.message:
                    lines.append(f"    Message:        {lt.message}")
                lines.append(f"    Finished At:    {lt.finished_at}")

    # Init Container Statuses
    if status.init_container_statuses:
        lines.append("\n--- Init Container Statuses ---")
        for ics in status.init_container_statuses:
            lines.append(f"  Init Container: {ics.name}")
            lines.append(f"    Ready:          {ics.ready}")
            lines.append(f"    Restart Count:  {ics.restart_count}")
            state = ics.state
            if state.waiting:
                lines.append(f"    State:          Waiting ({state.waiting.reason or 'Unknown'})")
            elif state.running:
                lines.append(f"    State:          Running (since {state.running.started_at})")
            elif state.terminated:
                lines.append(f"    State:          Terminated (exit code {state.terminated.exit_code})")

    logger.info("get_pod_status completed successfully for '%s/%s'.", namespace, pod_name)
    return "\n".join(lines)


# ==============================================================================
# TOOL 2: fetch_pod_logs
# ==============================================================================
@mcp.tool()
def fetch_pod_logs(
    namespace: str,
    pod_name: str,
    previous: bool = False,
    tail_lines: int = 50,
) -> str:
    """
    Fetch the logs from a Kubernetes pod's primary container. Use this tool
    to read application output, error messages, stack traces, and crash
    information for diagnosing failures.

    Args:
        namespace: The Kubernetes namespace where the pod resides (e.g., 'production').
        pod_name: The exact name of the pod to fetch logs from.
        previous: If True, fetch logs from the PREVIOUS terminated container instance.
                  Essential for diagnosing CrashLoopBackOff — the current container
                  may have just started and have no useful logs yet.
        tail_lines: Number of log lines to return from the end. Clamped to [1, 500]
                    to prevent excessive output. Default is 50.

    Returns:
        The requested log lines as a text string, or an error message if the
        pod/container is not found or logs are unavailable.
    """
    logger.info(
        "Tool invoked: fetch_pod_logs(namespace='%s', pod_name='%s', previous=%s, tail_lines=%d)",
        namespace, pod_name, previous, tail_lines,
    )

    # --- Input Validation ---
    try:
        _validate_k8s_name(namespace, "namespace")
        _validate_k8s_name(pod_name, "pod_name")
    except ValueError as e:
        return str(e)

    # Clamp tail_lines to a safe range to prevent excessive memory usage.
    tail_lines = max(1, min(tail_lines, 500))

    # --- Kubernetes API Call ---
    try:
        logs = core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            previous=previous,
            tail_lines=tail_lines,
            timestamps=True,  # Include timestamps for correlation with events
        )
    except ApiException as e:
        # Special handling: requesting previous logs when there's no previous container
        if e.status == 400 and "previous" in str(e.body).lower():
            return (
                f"No previous terminated container found for pod '{pod_name}' "
                f"in namespace '{namespace}'. The container may not have crashed yet, "
                f"or this is the first run. Try fetching current logs with previous=False."
            )
        return _format_api_error(e, "Pod", pod_name, namespace)

    # --- Build Response ---
    if not logs or logs.strip() == "":
        return (
            f"No logs available for pod '{pod_name}' in namespace '{namespace}'"
            f"{' (previous container)' if previous else ''}. "
            f"The container may have just started or produces no stdout/stderr output."
        )

    header = (
        f"=== Pod Logs: {pod_name} (namespace: {namespace}) ===\n"
        f"--- {'Previous' if previous else 'Current'} container | Last {tail_lines} lines ---\n"
    )

    logger.info("fetch_pod_logs completed successfully for '%s/%s'.", namespace, pod_name)
    return header + logs


# ==============================================================================
# TOOL 3: describe_deployment
# ==============================================================================
@mcp.tool()
def describe_deployment(namespace: str, deployment_name: str) -> str:
    """
    Describe a Kubernetes Deployment, including its replica status, resource
    requests and limits, deployment strategy, and conditions. Use this tool
    to understand the desired vs actual state, check if a rollout is stuck,
    and review resource allocation (which can cause OOMKilled if too low).

    Args:
        namespace: The Kubernetes namespace where the deployment resides.
        deployment_name: The exact name of the deployment to describe.

    Returns:
        A structured text report with replica counts, resource specs,
        deployment conditions, and strategy configuration.
    """
    logger.info(
        "Tool invoked: describe_deployment(namespace='%s', deployment_name='%s')",
        namespace, deployment_name,
    )

    # --- Input Validation ---
    try:
        _validate_k8s_name(namespace, "namespace")
        _validate_k8s_name(deployment_name, "deployment_name")
    except ValueError as e:
        return str(e)

    # --- Kubernetes API Call ---
    try:
        dep = apps_v1.read_namespaced_deployment(
            name=deployment_name, namespace=namespace
        )
    except ApiException as e:
        return _format_api_error(e, "Deployment", deployment_name, namespace)

    # --- Build Response ---
    spec = dep.spec
    status = dep.status

    lines = [
        f"=== Deployment Report ===",
        f"Name:               {dep.metadata.name}",
        f"Namespace:          {dep.metadata.namespace}",
        f"Created:            {dep.metadata.creation_timestamp}",
    ]

    # Labels and Selectors
    if dep.metadata.labels:
        labels_str = ", ".join(f"{k}={v}" for k, v in dep.metadata.labels.items())
        lines.append(f"Labels:             {labels_str}")

    if spec.selector and spec.selector.match_labels:
        selector_str = ", ".join(
            f"{k}={v}" for k, v in spec.selector.match_labels.items()
        )
        lines.append(f"Selector:           {selector_str}")

    # Replica Status
    lines.append("\n--- Replica Status ---")
    lines.append(f"  Desired Replicas:   {spec.replicas}")
    lines.append(f"  Current Replicas:   {status.replicas or 0}")
    lines.append(f"  Ready Replicas:     {status.ready_replicas or 0}")
    lines.append(f"  Updated Replicas:   {status.updated_replicas or 0}")
    lines.append(f"  Available Replicas: {status.available_replicas or 0}")
    if status.unavailable_replicas:
        lines.append(f"  Unavailable:        {status.unavailable_replicas}")

    # Deployment Strategy
    if spec.strategy:
        lines.append(f"\n--- Deployment Strategy ---")
        lines.append(f"  Type: {spec.strategy.type}")
        if spec.strategy.rolling_update:
            ru = spec.strategy.rolling_update
            lines.append(f"  Max Surge:       {ru.max_surge}")
            lines.append(f"  Max Unavailable: {ru.max_unavailable}")

    # Container Specs — Resource Requests and Limits
    if spec.template.spec.containers:
        lines.append("\n--- Container Specifications ---")
        for container in spec.template.spec.containers:
            lines.append(f"\n  Container: {container.name}")
            lines.append(f"    Image: {container.image}")

            if container.resources:
                res = container.resources
                if res.requests:
                    cpu_req = res.requests.get("cpu", "Not set")
                    mem_req = res.requests.get("memory", "Not set")
                    lines.append(f"    Resource Requests:")
                    lines.append(f"      CPU:    {cpu_req}")
                    lines.append(f"      Memory: {mem_req}")
                if res.limits:
                    cpu_lim = res.limits.get("cpu", "Not set")
                    mem_lim = res.limits.get("memory", "Not set")
                    lines.append(f"    Resource Limits:")
                    lines.append(f"      CPU:    {cpu_lim}")
                    lines.append(f"      Memory: {mem_lim}")
                if not res.requests and not res.limits:
                    lines.append(f"    Resources: ⚠️  No requests or limits set!")
            else:
                lines.append(f"    Resources: ⚠️  No resource spec defined!")

            # Ports
            if container.ports:
                ports_str = ", ".join(
                    f"{p.container_port}/{p.protocol or 'TCP'}"
                    for p in container.ports
                )
                lines.append(f"    Ports: {ports_str}")

            # Liveness & Readiness Probes
            if container.liveness_probe:
                lines.append(f"    Liveness Probe:  Configured")
            else:
                lines.append(f"    Liveness Probe:  ⚠️  Not configured")

            if container.readiness_probe:
                lines.append(f"    Readiness Probe: Configured")
            else:
                lines.append(f"    Readiness Probe: ⚠️  Not configured")

    # Deployment Conditions
    if status.conditions:
        lines.append("\n--- Deployment Conditions ---")
        for cond in status.conditions:
            lines.append(
                f"  {cond.type}: {cond.status}"
                f"{' (Reason: ' + cond.reason + ')' if cond.reason else ''}"
                f"{' — ' + cond.message if cond.message else ''}"
            )
            lines.append(f"    Last Update: {cond.last_update_time}")

    logger.info(
        "describe_deployment completed successfully for '%s/%s'.",
        namespace, deployment_name,
    )
    return "\n".join(lines)


# ==============================================================================
# SERVER ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    logger.info("Starting KubeOps-AI MCP Server in stdio mode...")
    logger.info("Exposed tools: get_pod_status, fetch_pod_logs, describe_deployment")
    logger.info("Waiting for MCP client connection on stdin/stdout...")

    # Run the FastMCP server. Defaults to stdio transport.
    # stdout -> MCP JSON-RPC messages (DO NOT PRINT TO STDOUT)
    # stderr -> Server logs (safe for debugging)
    mcp.run()

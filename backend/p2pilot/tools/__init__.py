"""Tool layer: system-of-record operations the agents call (ERP, files, tickets).

Same functions are exposed in-process (used by the invoice state machine) and over MCP
(p2pilot.mcp_server) for agents that pick tools dynamically. See docs/adr/0003.
"""

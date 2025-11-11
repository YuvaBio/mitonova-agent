#!/usr/bin/env python3
"""Think tool for internal reasoning"""

THINK_SPEC = {
    "toolSpec": {
        "name": "think",
        "description": "Internal reasoning - thoughts discarded, conclusions kept",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "thoughts": {"type": "string", "description": "Internal reasoning (discarded)"},
                    "conclusions": {"type": "string", "description": "Final conclusions (returned)"}
                },
                "required": ["thoughts", "conclusions"]
            }
        }
    }
}

def think_tool(params, task_id):
    """Think and return conclusions"""
    conclusions = params['conclusions']
    return {"conclusions": conclusions}

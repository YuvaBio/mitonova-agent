#!/usr/bin/env python3
"""Bash command execution tool"""

import subprocess

BASH_SPEC = {
    "toolSpec": {
        "name": "bash",
        "description": "Execute a bash command and return stdout, stderr, and exit code",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute"}
                },
                "required": ["command"]
            }
        }
    }
}

def bash_tool(params, task_id):
    """Execute bash command"""
    command = params['command']
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode
    }

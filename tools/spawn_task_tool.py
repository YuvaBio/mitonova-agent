#!/usr/bin/env python3
"""Spawn child task tool"""

SPAWN_TASK_SPEC = {
    "toolSpec": {
        "name": "spawn_task",
        "description": "Spawn a child task with initial message, or resume existing task with new message. By default, the child inherits the full conversation history from the parent (creating a branch point). Returns task_id and pid for monitoring.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "base_name": {"type": "string", "description": "Base name for new task (1-3 words describing the task, e.g., 'analyze data', 'fetch results'). Required when creating new task."},
                    "initial_message": {"type": "string", "description": "Initial user message for the child task"},
                    "task_id": {"type": "string", "description": "Optional: existing task_id to resume conversation. If provided, base_name is ignored."},
                    "model": {"type": "string", "description": "Model short name (default: sonnet45)"},
                    "zero_context": {"type": "boolean", "description": "If true, spawn child WITHOUT parent's conversation history (default: false). Only use when you need to explicitly deny the parent's knowledge to the child. Requires a very detailed initial_message since the child will have no context."}
                },
                "required": ["initial_message"]
            }
        }
    }
}

def spawn_task_tool(params, parent_task_id):
    """Spawn a child task or resume existing task
    
    By default, child tasks receive a transcription of the parent's conversation history
    in their system prompt, making spawn_task a conversation branch point. The child can 
    query the parent using query_task tool.
    """
    import redis
    import time
    from utils import launch_task_agent

    r = redis.Redis(decode_responses=True)
    
    initial_message = params['initial_message']
    child_task_id = params.get('task_id')  # Optional: resume existing task
    base_name = params.get('base_name')  # Required for new tasks
    model = params.get('model', 'haiku45')
    zero_context = params.get('zero_context', False)
    
    # Validate: if creating new task (no task_id), base_name is required
    if not child_task_id and not base_name:
        return {
            "success": False,
            "error": "base_name is required when creating a new child task (1-3 words describing the task)"
        }
    
    # Get parent task_id reference for transcription (unless zero_context is True)
    if zero_context:
        parent_conversation = r.json().get(f'task:{parent_task_id}')
    else:
        parent_conversation = None
    
    messages = []
    if parent_conversation:
        transcript = transcribe(parent_task_id, r)
        header = f"[SYSTEM]The following is a transcription of your parent task's conversation history. Use it to understand the context of the task:\n\n"
        footer = "\n\n[SYSTEM] Given the context above, you are now ready to begin your task:\n\n"
        messages.append({'role': 'user', 'content': [{'text': header + transcript + footer}]})

    messages.append({'role': 'user', 'content': [{'text': initial_message}]})
    
    # Launch the agent with parent task ID for context transcription
    pid, child_task_id = launch_task_agent(
        task_id=child_task_id,  # Pass existing task_id if provided
        model=model,
        messages=messages,
        parent_task_id=parent_task_id,
        base_name=base_name,  # Pass base_name for new tasks
        start_process=True,
    )

    # Get parent task's list of children and add new child task if needed
    parent_children = r.json().get(f'task_data:{parent_task_id}.children')
    if parent_children is None:
        parent_children = []
    if child_task_id not in parent_children:
        parent_children.append(child_task_id)
        r.json().set(f'task_data:{parent_task_id}.children', '$', parent_children)
    
    if child_task_id:
        action = "Resumed"
    else:
        action = "Spawned"
    
    return {
        "success": True,
        "task_id": child_task_id,
        "pid": pid,
        "message": f"{action} child task {child_task_id} (PID {pid})"
    }

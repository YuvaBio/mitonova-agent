#!/usr/bin/env python3
"""System prompt building for MicroCore agents"""

from datetime import datetime
import redis

def transcribe(task_id, r, include_tool_details=False):
   """
   Transcribe a task's conversation history into readable text format.
    
   Args:
      task_id: The task ID to transcribe
      r: Redis connection
      include_tool_details: If True, include full tool use/result details. 
                            If False, replace with [Used {tool_name} tool] and omit results.
    
   Returns:
      String containing the transcribed conversation
   """
   conversation = r.json().get(f'task:{task_id}')
   if not conversation:
      return f"No conversation found for task {task_id}"
    
   lines = []
    
   for turn in conversation:
      messages = turn.get('messages', [])
        
      for message in messages:
         role = message.get('role')
         content = message.get('content', [])
            
         if role == 'user':
            # Extract user messages
            for item in content:
               if 'text' in item:
                  lines.append(f"User: {item['text']}")
               elif 'toolResult' in item:
                  if include_tool_details:
                     tool_result = item['toolResult']
                     tool_use_id = tool_result.get('toolUseId', 'unknown')
                     result_content = tool_result.get('content', [])
                     result_text = ''
                     for res in result_content:
                        if 'text' in res:
                           result_text = res['text']
                     lines.append(f"Tool Result ({tool_use_id}): {result_text}")
                  # else: omit tool results
            
         elif role == 'assistant':
            # Extract assistant messages
            text_parts = []
            tool_uses = []
               
            for item in content:
               if 'text' in item:
                  text_parts.append(item['text'])
               elif 'toolUse' in item:
                  tool_uses.append(item['toolUse'])
               
            # Output text first
            if text_parts:
               combined_text = ' '.join(text_parts)
               lines.append(f"Assistant: {combined_text}")
               
            # Then tool uses
            for tool_use in tool_uses:
               tool_name = tool_use.get('name', 'unknown')
                  
               if include_tool_details:
                  tool_input = tool_use.get('input', {})
                  import json
                  args_str = json.dumps(tool_input, indent=2)
                  lines.append(f"Tool Use: {tool_name}")
                  lines.append(f"  Input: {args_str}")
               else:
                  lines.append(f"Assistant: [Used {tool_name} tool]")
    
   return '\n\n'.join(lines)

def build_static_system_prompt(model_name, parent_task_id=None):
   """Build static portion of system prompt
    
   Args:
      model_name: Model ARN or name
      parent_task_id: If provided, this is a child task; if None, this is root
   """
   base = """You are MitoNova, a master orchestration agent.

CORE PRINCIPLES:
- Fail-fast: No error handling, crash immediately on issues
- Tool-driven: Use tools to accomplish tasks
- Minimal: Keep responses concise
- Observable: All state in Redis

AVAILABLE TOOLS:
- bash: Execute bash commands (returns stdout, stderr, returncode)
- spawn_task: Spawn or restart child tasks for complex operations (returns task_id, pid)
- query_task: Passively query another task's status and conversation content

=== PATH MAPPINGS: CONTAINER vs HOST ===

When giving deployment/file copy instructions to the user:

Container paths (where YOU operate):
- /app → your code directory (actual host location varies)
- /mnt/persistent → persistent storage (maps to /work/agent_mount on host)
- /mnt/host → host filesystem root (read-only)

Host paths (where USER operates):
- /work/agent_mount → persistent storage (YOUR /mnt/persistent)
- <varies> → production agent code (YOUR /app, could be anywhere)
- / → host root filesystem (YOUR /mnt/host)

IMPORTANT: When instructing the user to copy/deploy files:

1. For persistent storage: /work/agent_mount (always the same)
   ✅ "Files are in /work/agent_mount/new_tools_XXXXXX/"

2. For production code: Check /mnt/host to find actual host path
   ❌ "Copy to /work/unicore/tools/" (assumes location)
   ✅ "Copy to your production agent's tools/ directory"
   OR check: ls -la /mnt/host/work/ to find agent location

The container could be launched from /work/unicore, /work/other_agent, /home/user/agent, etc.
Always use /work/agent_mount for persistent storage paths.

"""
    
   # Add task hierarchy information
   if parent_task_id is None:
      base += """TASK HIERARCHY: You are the ROOT task.

ROOT TASK RESPONSIBILITIES:
You are the project orchestrator. Your conversation context (tokens) is your most precious 
resource - every token spent on your own tool use or responses is a token NOT available for 
understanding project state and making strategic decisions.

NORMAL OPERATING MODE - ROOT TASK:
1. DELEGATE EVERYTHING: When given real work (not tests/diagnostics), immediately break it 
   into logical sub-tasks and spawn child tasks to handle them. Use spawn_task, not bash.

2. NEVER EDIT FILES YOURSELF: File editing requires trial and error, generating tokens that 
   add no understanding to your context. Always delegate file editing to child tasks with 
   specific, focused mandates.

3. MAXIMIZE DELEGATION VALUE: Each child task you spawn operates in its own context window.
   By delegating, you multiply your effective capacity. Think: "How can I break this into 
   N parallel or sequential tasks to accomplish N times the work?"

4. USE BASH FOR: Quick inspections, reading file contents, checking system state - things 
   that inform your delegation decisions but don't consume many tokens.

5. USE SPAWN_TASK FOR: Any actual work - code changes, analysis requiring multiple steps, 
   research, testing, documentation. If it will take >3 tool calls, delegate it.

6. COORDINATE AND INTEGRATE: Your role is to spawn tasks, monitor their completion (they 
   report back to you), and integrate their results. You are the conductor, not the 
   performer.

ANTI-PATTERNS FOR ROOT:
- Writing or editing code yourself (delegate to child task)
- Performing multi-step analyses yourself (delegate to child task)
- Iterating on solutions yourself (delegate to child task with clear success criteria)
- Using bash to accomplish real work (use bash only for inspection)

EXCEPTIONS TO NORMAL OPERATING MODE:
- When the user is testing and debugging or has indicated otherwise that there may be a problem with
   your task management system and that you should not delegate tasks, avoid delegation and use all
   available tools other than spawn_task as needed.

"""
   else:
      base += f"""TASK HIERARCHY: You are a CHILD task. Parent task ID: {parent_task_id}
You can query your parent's conversation using the query_task tool.

CHILD TASK RESPONSIBILITIES:
You have been delegated a specific task by your parent. Your mandate is focused and bounded.

OPERATING MODE - CHILD TASK:
1. FOCUS ON YOUR MANDATE: Your parent has given you a specific job. Complete it thoroughly 
   within scope. Don't expand beyond what was requested.

2. SPAWN SUB-TASKS CONSERVATIVELY: Only create sub-tasks if your mandate CLEARLY breaks into 
   logical subdivisions that would each require substantial work. Don't delegate for the sake 
   of delegating.

3. DELEGATE FILE EDITING: Like root, if your task involves editing files, consider spawning 
   focused sub-tasks for specific file edits. File editing is the most token-intensive work.

4. USE TOOLS DIRECTLY: Unlike root, you should use bash and other tools directly for most of 
   your work. You're here to execute, not just orchestrate.

5. REPORT THOROUGHLY: When you complete, your parent receives a summary. Make your final 
   response comprehensive - it's what your parent will see.

WHEN TO SPAWN SUB-TASKS (CHILD):
- Your task naturally breaks into 3+ independent pieces (e.g., "update these 5 files")
- Each sub-piece requires significant work (>5 tool calls)
- The sub-pieces could benefit from isolated contexts (e.g., different research topics)

WHEN NOT TO SPAWN SUB-TASKS (CHILD):
- Your task is already focused and specific
- The work is naturally sequential and interdependent
- You're doing diagnostic or analytical work that builds understanding iteratively

"""
    
   return base

def build_dynamic_system_prompt(task_data, turn_number):
   """Build dynamic portion of system prompt
    
   Args:
      task_data: Task data dictionary
      turn_number: Current turn number
   """
    
   r = redis.Redis(decode_responses=True)
    
   # Calculate current token count from task_data
   total_input_tokens = 0
   total_output_tokens = 0
    
   usage_data = task_data.get('last_usage', {})
   total_input_tokens = usage_data.get('inputTokens', 0)
   total_output_tokens = usage_data.get('outputTokens', 0)
    
   total_tokens = total_input_tokens + total_output_tokens
    
   dynamic = f"""
=== CURRENT CONTEXT ===
Date: {datetime.now().strftime('%Y-%m-%d')}
Time: {datetime.now().strftime('%H:%M:%S')} 
Turn: {turn_number}
Tokens used: {total_tokens:,} (input: {total_input_tokens:,}, output: {total_output_tokens:,})
"""
    
   # If this is a child task with parent context, add transcribed parent conversation
   parent_task_id = task_data.get('parent_task_id')
        
   if parent_task_id:   
      parent_transcription = transcribe(parent_task_id, r, include_tool_details=True)
        
      dynamic += f"""

=== PARENT TASK CONTEXT ===
You are a child process spawned to focus on a particular task. Below is a transcription 
of the conversation your parent process ({parent_task_id}) had that led to 
you being spawned. Use it to inform the full intent and context of the task you've been given.

{parent_transcription}

=== END PARENT CONTEXT ===
"""
    
   return dynamic

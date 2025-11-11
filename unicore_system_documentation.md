# UniCore Agent System - Complete Technical Documentation

## Table of Contents
1. [System Overview](#system-overview)
2. [Core Files and Their Roles](#core-files-and-their-roles)
3. [Data Structures in Redis](#data-structures-in-redis)
4. [Function Reference](#function-reference)
5. [Operational Sequences](#operational-sequences)
6. [Message Flow and Turn Management](#message-flow-and-turn-management)
7. [Parent-Child Task Interactions](#parent-child-task-interactions)

---

## System Overview

UniCore is a multi-agent system where each agent runs as an independent process, coordinating through Redis. Agents can spawn child tasks, execute tools, and maintain persistent conversation history across multiple turns.

**Key Concepts:**
- **Task**: An agent instance with a unique task_id
- **Turn**: A logical grouping of messages representing one complete interaction cycle
- **Iteration**: A single LLM API call within a turn
- **Message Queue**: Temporary storage for incoming messages (tool results, user messages, child completions)
- **Task Status**: Either 'running' (active process) or 'stopped' (no process, waiting for activation)

---

## Core Files and Their Roles

### core.py
Main agent execution loop and task lifecycle management.

**Key Functions:**
- `launch_agent()`: Creates or resumes tasks, spawns processes
- `run_agent()`: Main agent loop, executes iterations until turn ends
- `execute_iteration()`: Single iteration - processes queue, calls LLM, handles response
- `execute_tools()`: Executes tools from assistant response, queues results
- `summarize_and_store_turn()`: Generates turn summary when turn ends
- `notify_parent_of_completion()`: Sends completion message to parent task

### utils.py
Utility functions for message handling, conversation management, and Redis operations.

**Key Functions:**
- `process_task_queue()`: Processes queued messages and adds them to conversation
- `queue_message_for_task()`: Adds messages to task's input queue
- `cleanup_conversation_history()`: Fixes conversation structure for Bedrock API compliance
- `activate_task()`: Spawns new process for stopped task
- `check_task_activity()`: Verifies if task process is actually running
- `build_llm_input()`: Constructs Bedrock API request parameters
- `call_llm_api()`: Calls Bedrock with throttling and error handling
- `transcribe()`: Converts conversation to readable text format
- `is_turn_ending_message()`: Checks if message is text-only assistant response

### web_ui.py
FastAPI web server providing HTTP API and WebSocket interface.

**Key Endpoints:**
- `POST /api/task/create`: Creates new task
- `POST /api/task/{task_id}/message`: Sends message to task
- `POST /api/task/{task_id}/stop`: Stops running task
- `WebSocket /ws/{task_id}`: Real-time updates for task

### prompts.py
System prompt generation.

**Key Functions:**
- `build_static_system_prompt()`: Creates static portion of system prompt
- `build_dynamic_system_prompt()`: Creates dynamic portion with current context


---

## Data Structures in Redis

### task_data:{task_id}
Metadata about the task.

```json
{
  "task_id": "conversation_abc123",
  "parent_task_id": null,
  "model_name": "arn:aws:bedrock:...",
  "static_system_prompt": "...",
  "enable_recursion": true,
  "created_at": 1234567890.123,
  "status": "stopped",  // or "running"
  "pid": null,  // or process ID when running
  "last_usage": {
    "inputTokens": 1500,
    "outputTokens": 200,
    "totalTokens": 1700
  },
  "children": [],
  "command": "python core.py conversation_abc123",
  "max_iterations": 250,
  "process_started_at": 1234567890.456,
}
```

### task:{task_id}
Conversation history organized by turns.

```json
[
  {
    "turn_number": 0,
    "started_at": 1234567890.123,
    "messages": [
      {
        "role": "user",
        "content": [{"text": "Hello"}],
        "message_number": 0,
        "timestamp": 1234567890.456
      },
      {
        "role": "assistant",
        "content": [{"text": "Hi there!"}],
        "message_number": 1,
        "timestamp": 1234567890.789
      }
    ],
    "turn_summary": "User greeted, assistant responded."
  },
  {
    "turn_number": 1,
    "started_at": 1234567891.123,
    "messages": [...]
  }
]
```

### task_queue:{task_id}
Temporary queue for incoming messages.

```json
[
  {
    "type": "tool_result",  // or "user", "completion"
    "content": {
      "toolResult": {
        "toolUseId": "tooluse_abc123",
        "content": [{"text": "{\"result\": \"success\"}"}]
      }
    },
    "sender_id": "conversation_abc123",
    "timestamp": 1234567890.123,
    "tool_use_id": "tooluse_abc123"
  }
]
```


---

## Function Reference

### core.py Functions

#### launch_agent(task_id, model, enable_recursion, messages, parent_task_id, base_name, max_iterations, start_process)

**Purpose**: Creates new task or resumes existing task, optionally spawning process.

**Logic Flow**:
1. If task_id provided, check if task is already running via `check_task_activity()`
2. If not provided, generate new task_id
3. Resolve model ARN
4. Build static system prompt
5. **If existing_task = True** (process already running):
   - Update task_data with new command, timestamps
   - Append new messages via `queue_message_for_task()`
   - Print "Resuming existing task"
6. **If existing_task = False**:
   - Check if conversation already exists in Redis
   - **If conversation exists** (reactivating stopped task):
     - Create new task_data but DON'T overwrite conversation
     - Print "Reactivating existing task"
   - **If conversation doesn't exist** (brand new task):
     - Create new task_data AND new conversation with initial messages
     - Print "Created new task"
7. If start_process=True, spawn subprocess and update PID

**Critical Bug Fix**: Must check for existing conversation before overwriting to preserve history when reactivating stopped tasks.

#### run_agent(task_id, max_iterations)

**Purpose**: Main agent loop that executes iterations until turn ends.

**Logic Flow**:
1. Get PID, connect to Redis and Bedrock
2. Set task status to 'running', store PID
3. Subscribe to throttle state for model
4. **For each iteration**:
   - Build system message based on iteration count
   - Call `execute_iteration()`
   - If turn_ending=True:
     - Set status to 'stopped', clear PID
     - Publish completion notification
     - Call `notify_parent_of_completion()`
     - Return (exit process)
5. In finally block, ensure status set to 'stopped' and PID cleared

**Key Behavior**: Process exits completely when turn ends. New messages trigger new process via `activate_task()`.

#### execute_iteration(task_id, r, bedrock, system_message)

**Purpose**: Executes one iteration - processes queue, calls LLM, handles response.

**Logic Flow**:
1. Call `process_task_queue()` to add queued messages to conversation
2. Load conversation and run through `cleanup_conversation_history()`
3. Build bedrock_messages list from all turns and messages
4. Append system_message to last message if provided
5. Build full system prompt (static + dynamic)
6. Call `call_llm_api()` with bedrock_messages
7. Check if status changed to 'stopped' (interrupted), return if so
8. Extract response, log it, store usage stats
9. Create assistant message and append to current turn
10. Publish message notification
11. **If stop_reason = 'tool_use'**:
    - Check status again (might be interrupted)
    - Call `execute_tools()` to execute and queue results
12. Determine if turn_ending (stop_reason not in ['tool_use', 'max_tokens'])
13. If turn_ending, call `summarize_and_store_turn()`
14. Return turn_ending flag


#### execute_tools(output, task_id)

**Purpose**: Executes tools from assistant response and queues results.

**Logic Flow**:
1. Iterate through content blocks in assistant message
2. For each toolUse block:
   - Extract tool_name, tool_input, tool_use_id
   - Try to execute tool via TOOLS[tool_name](tool_input, task_id)
   - If successful, create toolResult with JSON-encoded result
   - If exception, create toolResult with error message and status='error'
   - Call `queue_message_for_task(task_id, 'tool_result', tool_result, tool_use_id=tool_use_id)`
3. Return child_spawned flag (True if spawn_task was used)

**Key Behavior**: Tool results are queued, not immediately added to conversation. They're processed at start of next iteration.

#### summarize_and_store_turn(task_id, r, bedrock, current_turn, last_req_time, throttle_multiplier)

**Purpose**: Generates and stores summary of completed turn.

**Logic Flow**:
1. Load conversation and extract turn_messages for current_turn
2. Build summary_messages with prompt to summarize the turn
3. Call LLM API with summary prompt (no tools)
4. Extract summary text from response
5. Store summary in Redis: `r.json().set(f'task:{task_id}', f'$[{current_turn}].turn_summary', summary)`
6. Return updated throttle state

**Note**: Turn 0 may not have summary if it was the initial task creation turn.

#### notify_parent_of_completion(task_id, r)

**Purpose**: Notifies parent task when child task completes.

**Logic Flow**:
1. Get task_data and extract parent_task_id
2. If no parent, return
3. Load conversation and build completion message via `build_completion_message()`
4. Queue completion message for parent: `queue_message_for_task(parent_task_id, 'completion', completion_msg, task_id)`
5. Check if parent is running via `check_task_activity()`
6. If parent not running, call `activate_task(parent_task_id)` to wake it up

**Key Behavior**: Child completion can arrive while parent is running (appended to current turn) or stopped (starts new turn).


### utils.py Functions

#### process_task_queue(task_id, r)

**Purpose**: SINGLE POINT OF ENTRY for adding messages to conversation. Processes queued messages and appends them to conversation with proper turn management.

**Logic Flow**:
1. Load queue from Redis: `task_queue:{task_id}`
2. If queue empty, return
3. Load conversation and task_data from Redis
4. **Determine if new turn needed**:
   - Check if task_is_stopped (status == 'stopped')
   - needs_new_turn = True if:
     - No conversation exists, OR
     - Conversation is empty, OR
     - Current turn has no messages, OR
     - (task_is_stopped AND last message is turn-ending)
5. **If needs_new_turn**:
   - Create new_turn dict with turn_number, started_at, empty messages list
   - If no conversation, initialize with `r.json().set()`
   - Else append turn with `r.json().arrappend()`
   - **CRITICAL**: Reload conversation from Redis after creating turn
6. Calculate current_turn index from conversation length
7. Load current_turn_messages from Redis (fresh read)
8. **Collect messages by type**:
   - tool_results list (type='tool_result')
   - text_messages list (type='user' or 'completion')
9. **Add tool results first** (as single user message):
   - Create user message with content=tool_results array
   - Append to Redis with `r.json().arrappend()`
   - Reload current_turn_messages to update count
10. **Add text messages** (each as separate user message):
    - For each text_content, create user message with content=[{'text': text_content}]
    - Append to Redis with `r.json().arrappend()`
    - Reload current_turn_messages to update count
11. Publish notification if any messages processed
12. Clear queue: `r.json().set(f'task_queue:{task_id}', '$', [])`

**Critical Behaviors**:
- Only creates new turn if task is stopped and last message is turn-ending
- If task is running, messages append to current turn regardless
- Must reload conversation after creating new turn to get correct current_turn index
- Must reload current_turn_messages after each append to keep count accurate
- Tool results batched into single user message (Bedrock requirement)
- Text messages each get separate user message

#### queue_message_for_task(task_id, message_type, content, sender_id, tool_use_id)

**Purpose**: Adds message to task's input queue.

**Logic Flow**:
1. Create queue_msg dict with type, content, sender_id, timestamp
2. Add tool_use_id if provided
3. Initialize queue if doesn't exist
4. Append message to queue with `r.json().arrappend()`

**Message Types**:
- 'tool_result': Tool execution results
- 'user': User text messages
- 'completion': Child task completion messages

#### cleanup_conversation_history(history)

**Purpose**: Fixes conversation structure to comply with Bedrock API requirements (alternating user/assistant, tool_use followed by tool_result).

**Logic Flow**:
1. For each turn in history:
   - Initialize tool_ids list (tracks pending tool results)
   - Initialize last_role = 'assistant'
   - For each message:
     - **If assistant after user** (normal):
       - Extract tool IDs from toolUse blocks, add to tool_ids list
       - Append message to new_messages
     - **If assistant after assistant** (error - two consecutive assistant messages):
       - Insert synthetic user message with error tool results for pending tool_ids
       - Clear tool_ids list
       - Extract tool IDs from new assistant message
       - Append message to new_messages
     - **If user message**:
       - For each toolResult, remove its tool_id from tool_ids list
       - For any remaining tool_ids, append synthetic error toolResults to message
       - Clear tool_ids list
       - Append message to new_messages
     - **Else** (catch-all for unexpected patterns):
       - Print warning
       - Append message anyway
   - Renumber all messages for consistency
   - Append cleaned turn to cleaned_history
2. Return cleaned_history

**Critical Behaviors**:
- Fixes conversation structure violations before sending to Bedrock
- Inserts synthetic error messages for missing tool results
- Writes last turn to /tmp/last_messages.jsonl for debugging (overwrites each time)

#### check_task_activity(task_id)

**Purpose**: Verifies if task process is actually running and updates Redis status.

**Logic Flow**:
1. Load task_data from Redis
2. Get PID from task_data
3. If PID exists:
   - Check process status via psutil
   - If process in good_statuses (running, sleeping, etc.):
     - Set status to 'running' in Redis
     - Return (True, pid, cpu_percent)
4. If process not running or PID doesn't exist:
   - Set status to 'stopped', clear PID in Redis
   - Publish process_ended notification
   - Return (False, None, None)

**Key Behavior**: Automatically cleans up stale status in Redis.


#### is_turn_ending_message(message)

**Purpose**: Checks if message is text-only assistant response (indicates turn should end).

**Logic Flow**:
1. Check if role is 'assistant'
2. Iterate through content blocks
3. Set has_text=True if any text block found
4. Set has_tool_use=True if any toolUse block found
5. Return (has_text AND NOT has_tool_use)

**Key Behavior**: Turn-ending messages are assistant responses with text but no tool use.

#### call_llm_api(bedrock, input_params, task_id, r, last_req_time, throttle_multiplier)

**Purpose**: Calls Bedrock Converse API with throttling and error handling.

**Logic Flow**:
1. Calculate required_delay via `proactive_delay()`
2. If required_delay is False (task went stopped), return False
3. Sleep to enforce rate limiting
4. Call bedrock.converse() with input_params
5. **If successful**:
   - Log response time
   - Update throttle_multiplier (decrease by 10%)
   - Publish throttle_success event
   - Return (response, new_last_req_time, new_throttle_multiplier)
6. **If throttling exception**:
   - Publish throttle_exception event
   - Increase throttle_multiplier (up to 3.0x)
   - Sleep for backoff period
   - Return updated throttle state (no retry in current implementation)
7. **If other error**:
   - Write input_params to /tmp/llm_api_error_*.json
   - Re-raise exception

**Key Behaviors**:
- Proactive rate limiting based on token usage
- Exponential backoff on throttling
- Does NOT automatically retry (caller must handle)

#### transcribe(task_id, r, include_tool_details)

**Purpose**: Converts conversation to readable text format.

**Logic Flow**:
1. Load conversation from Redis
2. For each turn, for each message:
   - **If user message**:
     - Extract text blocks, format as "User: {text}"
     - If include_tool_details=True, extract and format tool results
   - **If assistant message**:
     - Extract text blocks, format as "Assistant: {text}"
     - Extract tool uses
     - If include_tool_details=True, format with full input JSON
     - If include_tool_details=False, format as "[Used {tool_name} tool]"
3. Join all lines with double newlines
4. Return transcription string

**Key Behavior**: Used for parent context in child tasks (include_tool_details=True).


---

## Operational Sequences

### Sequence 1: New Task Creation (Web UI)

1. **User clicks "New Chat" in web UI**
2. **Web UI** calls `POST /api/task/create` with initial_message
3. **web_ui.py** `create_task()`:
   - Builds initial_messages list with user message
   - Calls `launch_agent(task_id=None, messages=initial_messages, start_process=False)`
4. **core.py** `launch_agent()`:
   - Generates new task_id (e.g., "conversation_abc123")
   - Creates task_data dict
   - Creates conversation_data with Turn 0 containing initial_messages
   - Writes both to Redis
   - Returns (None, task_id) since start_process=False
5. **Web UI** returns task_id to browser
6. **Browser** opens WebSocket connection to `/ws/{task_id}`
7. **Web UI** WebSocket handler subscribes to Redis pub/sub for task updates
8. **User sends first message** via WebSocket
9. **web_ui.py** `listen_websocket()`:
   - Calls `stop_task(task_id)` (no-op since not running)
   - Calls `queue_message_for_task(task_id, 'user', message)`
   - Calls `activate_task(task_id)`
10. **utils.py** `activate_task()`:
    - Loads task_data
    - Calls `launch_agent(task_id=task_id, model=..., enable_recursion=True)`
11. **core.py** `launch_agent()`:
    - Checks task activity (returns False since no process)
    - Checks if conversation exists (YES - reactivating)
    - Updates task_data only (doesn't overwrite conversation)
    - Spawns subprocess
    - Returns (pid, task_id)
12. **Subprocess starts**, calls `run_agent(task_id)`
13. **core.py** `run_agent()`:
    - Sets status='running', stores PID
    - Enters iteration loop
    - Calls `execute_iteration()`
14. **core.py** `execute_iteration()`:
    - Calls `process_task_queue()` - adds queued user message to conversation
    - Loads conversation, runs through `cleanup_conversation_history()`
    - Builds bedrock_messages from all turns
    - Calls LLM API
    - Stores assistant response
    - If tool_use, executes tools and queues results
    - If turn_ending, summarizes turn
    - Returns turn_ending flag
15. **If turn_ending=True**:
    - Sets status='stopped', clears PID
    - Process exits
16. **Web UI** receives updates via pub/sub, sends to browser via WebSocket


### Sequence 2: Continuing Conversation (New Turn)

1. **User sends new message** after previous turn ended (task is stopped)
2. **web_ui.py** receives message via WebSocket
3. **web_ui.py** `listen_websocket()`:
   - Calls `stop_task(task_id)` (no-op since already stopped)
   - Calls `queue_message_for_task(task_id, 'user', message)`
   - Calls `activate_task(task_id)`
4. **utils.py** `activate_task()`:
   - Calls `launch_agent(task_id=task_id, ...)`
5. **core.py** `launch_agent()`:
   - Checks task activity (returns False)
   - Checks if conversation exists (YES)
   - Updates task_data, does NOT overwrite conversation
   - Spawns subprocess
6. **Subprocess starts**, calls `run_agent(task_id)`
7. **core.py** `execute_iteration()`:
   - Calls `process_task_queue()`
8. **utils.py** `process_task_queue()`:
   - Loads queue (has user message)
   - Loads conversation (has previous turns)
   - Loads task_data (status='stopped' since just activated)
   - Checks needs_new_turn:
     - task_is_stopped = True
     - Last message is turn-ending assistant response
     - **needs_new_turn = True**
   - Creates new turn (Turn N)
   - Appends to Redis with `r.json().arrappend()`
   - **Reloads conversation** to get updated structure
   - Calculates current_turn = len(conversation) - 1
   - Adds user message to new turn
   - Clears queue
9. **core.py** `execute_iteration()` continues:
   - Loads conversation (now has N+1 turns)
   - Builds bedrock_messages from ALL turns (preserves history)
   - Calls LLM API
   - Processes response
10. **Turn completes**, process exits

**Key Point**: New turn is created because task_is_stopped=True AND last message was turn-ending. All previous turns are preserved and sent to Bedrock.


### Sequence 3: Tool Execution Within Turn

1. **Agent is running**, in middle of turn
2. **LLM responds** with stop_reason='tool_use'
3. **core.py** `execute_iteration()`:
   - Stores assistant message (contains toolUse blocks)
   - Calls `execute_tools(output, task_id)`
4. **core.py** `execute_tools()`:
   - For each toolUse block:
     - Executes tool function
     - Creates toolResult
     - Calls `queue_message_for_task(task_id, 'tool_result', tool_result, tool_use_id=...)`
   - Returns child_spawned flag
5. **core.py** `execute_iteration()`:
   - Returns turn_ending=False (since stop_reason='tool_use')
6. **core.py** `run_agent()`:
   - Continues to next iteration
   - Calls `execute_iteration()` again
7. **core.py** `execute_iteration()`:
   - Calls `process_task_queue()`
8. **utils.py** `process_task_queue()`:
   - Loads queue (has tool_result messages)
   - Loads task_data (status='running')
   - Checks needs_new_turn:
     - task_is_stopped = False
     - **needs_new_turn = False** (because task is running)
   - Uses current turn (doesn't create new turn)
   - Collects all tool_results into list
   - Creates single user message with content=tool_results array
   - Appends to current turn
   - Clears queue
9. **core.py** `execute_iteration()` continues:
   - Loads conversation (current turn now has tool results)
   - Builds bedrock_messages (includes assistant toolUse + user toolResults)
   - Calls LLM API
   - LLM processes tool results and responds

**Key Point**: Tool results are added to CURRENT turn (not new turn) because task is still running.


### Sequence 4: Parent Spawns Child Task

1. **Parent agent** uses spawn_task tool
2. **tools/task_tools.py** `spawn_task()`:
   - Extracts task_description, initial_prompt, parent_task_id, etc.
   - Builds initial_messages with user message containing initial_prompt
   - Calls `launch_agent(task_id=None, parent_task_id=parent_task_id, messages=initial_messages, ...)`
3. **core.py** `launch_agent()`:
   - Generates child task_id (e.g., "test_task_abc123")
   - Creates task_data with parent_task_id
   - Creates conversation_data with Turn 0 containing initial_messages
   - Writes to Redis
   - Spawns subprocess
   - Returns (pid, child_task_id)
4. **spawn_task tool** returns result with child_task_id
5. **Parent's execute_tools()** queues tool result
6. **Parent continues** to next iteration, processes tool result

**Meanwhile, child process starts**:

7. **Child subprocess** calls `run_agent(child_task_id)`
8. **core.py** `run_agent()`:
   - Sets status='running'
   - Calls `execute_iteration()`
9. **core.py** `execute_iteration()`:
   - Calls `process_task_queue()` (queue is empty for new child)
   - Loads conversation (Turn 0 with initial user message)
   - Builds system prompt via `build_dynamic_system_prompt()`
10. **prompts.py** `build_dynamic_system_prompt()`:
    - Detects parent_task_id in task_data
    - Calls `transcribe(parent_task_id, r, include_tool_details=True)`
    - Includes parent conversation in system prompt
11. **Child LLM call** receives parent context in system prompt
12. **Child executes**, performs work, eventually reaches turn end
13. **core.py** `run_agent()`:
    - Detects turn_ending=True
    - Calls `notify_parent_of_completion()`
14. **core.py** `notify_parent_of_completion()`:
    - Builds completion message with child's final response
    - Calls `queue_message_for_task(parent_task_id, 'completion', completion_msg, child_task_id)`
    - Checks if parent is running
    - If parent stopped, calls `activate_task(parent_task_id)` to wake it up
15. **Child process exits**


### Sequence 5: Child Completes While Parent Running

**Scenario**: Parent spawns child, continues working, child completes before parent's turn ends.

1. **Parent spawns child** (see Sequence 4, steps 1-6)
2. **Parent continues** with more tool uses or LLM calls (still in same turn)
3. **Child completes** (see Sequence 4, steps 7-14)
4. **Child's notify_parent_of_completion()**:
   - Queues completion message for parent
   - Checks parent activity: **parent is RUNNING**
   - Does NOT call activate_task (parent already running)
5. **Parent's next iteration**:
   - Calls `process_task_queue()`
6. **utils.py** `process_task_queue()`:
   - Loads queue (has completion message from child)
   - Loads task_data (status='running')
   - Checks needs_new_turn:
     - task_is_stopped = False
     - **needs_new_turn = False**
   - Uses current turn
   - Adds completion message as user message to current turn
   - Clears queue
7. **Parent's execute_iteration()** continues:
   - Loads conversation (current turn now has child completion)
   - Builds bedrock_messages (includes child completion)
   - Calls LLM API
   - Parent can respond to child's completion

**Key Point**: Child completion is added to parent's CURRENT turn because parent is still running. Parent sees child's result in same turn.


### Sequence 6: Child Completes While Parent Stopped

**Scenario**: Parent spawns child, parent's turn ends, child completes later.

1. **Parent spawns child** (see Sequence 4, steps 1-6)
2. **Parent's turn ends**:
   - LLM responds with text-only message (turn-ending)
   - `execute_iteration()` returns turn_ending=True
   - `run_agent()` sets status='stopped', clears PID
   - Process exits
3. **Child continues working** (parent is now stopped)
4. **Child completes**:
   - Calls `notify_parent_of_completion()`
5. **Child's notify_parent_of_completion()**:
   - Queues completion message for parent
   - Checks parent activity: **parent is STOPPED**
   - Calls `activate_task(parent_task_id)` to wake parent
6. **utils.py** `activate_task()`:
   - Calls `launch_agent(task_id=parent_task_id, ...)`
7. **core.py** `launch_agent()`:
   - Checks task activity (returns False)
   - Checks if conversation exists (YES)
   - Updates task_data, doesn't overwrite conversation
   - Spawns subprocess
8. **Parent subprocess starts**, calls `run_agent()`
9. **Parent's execute_iteration()**:
   - Calls `process_task_queue()`
10. **utils.py** `process_task_queue()`:
    - Loads queue (has completion message from child)
    - Loads conversation (has previous turns)
    - Loads task_data (status='stopped' since just activated)
    - Checks needs_new_turn:
      - task_is_stopped = True
      - Last message is turn-ending assistant response
      - **needs_new_turn = True**
    - Creates new turn
    - Adds completion message to new turn
    - Clears queue
11. **Parent's execute_iteration()** continues:
    - Loads conversation (now has new turn with child completion)
    - Builds bedrock_messages from ALL turns
    - Calls LLM API
    - Parent responds to child's completion in new turn

**Key Point**: Child completion starts a NEW turn because parent was stopped. Parent's conversation history is preserved.


### Sequence 7: Multiple Children Complete Simultaneously

**Scenario**: Parent spawns 4 children, all complete around the same time while parent is stopped.

1. **Parent spawns 4 children** via spawn_task tool
2. **Parent's turn ends**, process exits (status='stopped')
3. **Child 1 completes**:
   - Queues completion message for parent
   - Checks parent: stopped
   - Calls `activate_task(parent_task_id)`
   - Parent process spawns
4. **Child 2 completes** (while parent is starting up):
   - Queues completion message for parent
   - Checks parent: now running (process just started)
   - Does NOT call activate_task
5. **Child 3 completes** (while parent is starting up):
   - Queues completion message for parent
   - Checks parent: running
   - Does NOT call activate_task
6. **Child 4 completes** (while parent is starting up):
   - Queues completion message for parent
   - Checks parent: running
   - Does NOT call activate_task
7. **Parent's first iteration**:
   - Calls `process_task_queue()`
8. **utils.py** `process_task_queue()`:
   - Loads queue (has 4 completion messages)
   - task_is_stopped = True (status was 'stopped' when messages arrived, but process is now running)
   - needs_new_turn = True
   - Creates new turn
   - Collects all 4 completion messages into text_messages list
   - Adds each as separate user message to new turn
   - Clears queue
9. **Parent's execute_iteration()** continues:
   - Loads conversation (new turn has 4 user messages with child completions)
   - Builds bedrock_messages
   - Calls LLM API
   - Parent sees all 4 child completions at once

**Key Point**: Multiple messages are batched into same turn if they arrive before process_task_queue runs. Each text message gets separate user message, but all in same turn.


---

## Message Flow and Turn Management

### Turn Creation Rules

**A new turn is created if and only if ALL of these are true**:
1. Task status is 'stopped' (no process running when messages arrived)
2. Conversation exists and has at least one turn
3. Last turn has at least one message
4. Last message in last turn is turn-ending (text-only assistant response)

**Otherwise, messages are appended to the current turn.**

### Message Batching

**Tool Results**:
- All tool results in queue are batched into a SINGLE user message
- Content is array of toolResult blocks
- This satisfies Bedrock's requirement that all tool uses get results in next message

**Text Messages** (user messages, child completions):
- Each text message becomes a SEPARATE user message
- Each has content=[{'text': message_text}]
- Multiple text messages in queue = multiple consecutive user messages in turn

### Turn Lifecycle

**Turn Start**:
- Created by `process_task_queue()` when needs_new_turn=True
- Initialized with empty messages list
- turn_number incremented
- started_at timestamp recorded

**Turn Active**:
- Messages added via `process_task_queue()` and `execute_iteration()`
- Alternating user/assistant messages
- May span multiple iterations (tool use cycles)
- Task status is 'running'

**Turn End**:
- LLM responds with text-only message (no tool use)
- `execute_iteration()` returns turn_ending=True
- `summarize_and_store_turn()` generates summary
- Task status set to 'stopped'
- Process exits

**Turn Resumption** (NOT a new turn):
- If messages arrive while task is running
- Messages appended to current turn
- No new turn created


### Conversation History Preservation

**Critical Invariants**:
1. Conversation in Redis is the source of truth
2. All turns are preserved indefinitely (no automatic cleanup)
3. When building bedrock_messages, ALL turns are included
4. `cleanup_conversation_history()` fixes structure but doesn't remove turns
5. Turn summaries are stored but not used for context (full messages sent to LLM)

**How History is Preserved**:
- `launch_agent()` checks if conversation exists before creating new one
- If conversation exists (reactivating stopped task), only task_data is updated
- `process_task_queue()` appends to existing conversation structure
- `execute_iteration()` loads full conversation and sends all turns to Bedrock

**Common Pitfall** (now fixed):
- Old bug: `launch_agent()` would overwrite conversation when existing_task=False
- Fix: Check if conversation exists in Redis, only create if truly new task
- This preserves history when reactivating stopped tasks

---

## Parent-Child Task Interactions

### Parent Context in Child

**Mechanism**:
1. Parent spawns child with `parent_task_id=parent_task_id`
2. Child's task_data stores this field
3. Child's `build_dynamic_system_prompt()` detects this field
4. Calls `transcribe(parent_task_id, include_tool_details=True)`
5. Includes full parent conversation in child's system prompt

**What Child Sees**:
- Complete parent conversation up to spawn point
- All user messages, assistant messages, tool uses, tool results
- Formatted as readable text transcript
- Included in system prompt, not conversation history

### Child Completion Messages

**Structure**:
```
[SYSTEM] Child task {child_task_id} has completed successfully.
Ran {total_turns} turns with {total_tool_iterations} tool iterations.
You can continue the conversation by calling spawn_task with task_id='{child_task_id}' and a new message.

Final response from child:
{final_text}
```

**Delivery**:
- Queued as type='completion'
- Processed by `process_task_queue()` as text message
- Added to parent's conversation as user message
- Parent sees it in next LLM call

### Resuming Child Tasks

**Mechanism**:
- Parent can call spawn_task with existing child_task_id
- `launch_agent()` resumes child with existing conversation
- New message appended to child's conversation
- Child continues from where it left off

**Use Case**: Iterative refinement, follow-up questions to child


---

## Critical Bug Fixes and Lessons Learned

### Bug 1: Tool Results Not Added to Conversation

**Symptom**: ValidationException - tool_use IDs found without tool_result blocks

**Root Cause**: 
- `execute_tools()` queued results with type='tool_result'
- `process_task_queue()` only processed type='user'
- Tool results never added to conversation

**Fix**: 
- Updated `process_task_queue()` to handle type='tool_result'
- Batch all tool results into single user message

### Bug 2: Conversation History Lost Between Turns

**Symptom**: Agent has no memory of previous turns, conversation disappears from UI

**Root Cause**:
- `launch_agent()` checked `existing_task` (process running)
- If False, created brand new conversation
- When reactivating stopped task, conversation was overwritten

**Fix**:
- Check if conversation exists in Redis, not just if process is running
- Only create new conversation if truly new task
- Preserve conversation when reactivating stopped tasks

### Bug 3: Messages Added to Wrong Turn

**Symptom**: Tool results added to previous turn instead of current turn

**Root Cause**:
- `process_task_queue()` created new turn with `r.json().arrappend()`
- But didn't reload local `conversation` variable
- `current_turn = len(conversation) - 1` used stale length

**Fix**:
- Reload conversation from Redis after creating new turn
- Ensures current_turn index is correct

### Bug 4: Turn Created During Tool Execution

**Symptom**: Tool results in separate turn from tool use

**Root Cause**:
- `process_task_queue()` only checked if last message was turn-ending
- Didn't check task status (running vs stopped)
- Created new turn even when task was mid-execution

**Fix**:
- Check task_is_stopped in needs_new_turn logic
- Only create new turn if task is stopped AND last message is turn-ending
- If task is running, always append to current turn


---

## Design Principles and Invariants

### Process Model

**One Process Per Active Task**:
- Each task runs in separate subprocess
- Process exits when turn ends
- New process spawned when messages arrive for stopped task
- Enables clean isolation and resource management

**Stateless Processes**:
- All state stored in Redis
- Process can be killed and restarted without data loss
- Conversation history persists across process restarts

### Message Queue Pattern

**Why Queue?**:
- Decouples message arrival from processing
- Batches multiple messages (tool results, child completions)
- Allows messages to arrive while task is running or stopped
- Single processing point (`process_task_queue()`) ensures consistency

**Queue Lifecycle**:
1. Messages added via `queue_message_for_task()`
2. Queue processed at start of each iteration
3. Messages added to conversation
4. Queue cleared

### Turn Semantics

**Turn = Logical Interaction Unit**:
- Starts with user message(s)
- Contains alternating user/assistant messages
- May span multiple LLM calls (tool use cycles)
- Ends with text-only assistant response
- Summarized when complete

**Turn Boundaries**:
- New turn created when task is stopped and receives messages
- Same turn continued when task is running and receives messages
- Ensures tool use/result pairs stay in same turn
- Preserves conversation flow

### Bedrock API Compliance

**Requirements**:
- Messages must alternate user/assistant
- Each tool_use must have corresponding tool_result in next message
- All tool_use IDs must have results

**How We Comply**:
- `cleanup_conversation_history()` fixes violations before API call
- Tool results batched into single user message
- Synthetic error results inserted for missing tool results


---

## Debugging Guide

### Key Log Messages

**Task Lifecycle**:
- `[LAUNCHER] Created new task {task_id}` - Brand new task
- `[LAUNCHER] Reactivating existing task {task_id}` - Resuming stopped task
- `[LAUNCHER] Resuming existing task {task_id}` - Process already running
- `[CORE] Starting agent for {task_id}` - Process starting
- `[CORE] Turn {N} message {M} for {task_id}` - Iteration starting

**Message Processing**:
- `[CORE] Executing tool: {tool_name}` - Tool execution
- `[CORE] Queueing tool result {tool_use_id}` - Tool result queued
- `[CORE] Activated {task_id} with PID {pid}` - Task activated

**Warnings**:
- `[CORE] WARNING: catch-all for message of role {role}, last role {last_role}` - Conversation structure issue
- `[CORE] ERROR: {error_code}` - LLM API error

### Diagnostic Files

**`/tmp/bedrock_responses.jsonl`**:
- All Bedrock API responses
- One JSON object per line
- Contains full response including usage stats

**`/tmp/last_messages.jsonl`**:
- Last turn processed by `cleanup_conversation_history()`
- Overwritten each time (only shows most recent)
- Useful for debugging conversation structure issues

**`/tmp/llm_api_error_*.json`**:
- Input parameters for failed API calls
- Created when non-throttling error occurs
- Contains full request that caused error

### Redis Inspection

**Check conversation**:
```python
import redis, json
r = redis.Redis(decode_responses=True)
conv = r.json().get('task:conversation_abc123')
print(json.dumps(conv, indent=2))
```

**Check queue**:
```python
queue = r.json().get('task_queue:conversation_abc123')
print(f"Queue has {len(queue)} messages")
```

**Check task status**:
```python
task_data = r.json().get('task_data:conversation_abc123')
print(f"Status: {task_data['status']}, PID: {task_data['pid']}")
```

### Common Issues

**"No memory between turns"**:
- Check if conversation exists in Redis
- Verify `launch_agent()` not overwriting conversation
- Check logs for "Created new task" vs "Reactivating existing task"

**"Tool results not processed"**:
- Check queue has tool_result messages
- Verify `process_task_queue()` handles type='tool_result'
- Check if messages added to correct turn

**"ValidationException - tool_use without tool_result"**:
- Check conversation structure in Redis
- Verify tool results queued after tool execution
- Check `cleanup_conversation_history()` for errors

**"Two consecutive assistant messages"**:
- Check if turn was created mid-execution
- Verify needs_new_turn logic checks task_is_stopped
- Check if tool results added to wrong turn


---

## Future Considerations

### Potential Enhancements

**Turn Cleanup**:
- Currently all turns preserved indefinitely
- Could implement sliding window (keep last N turns)
- Would need to preserve turn summaries for context
- Trade-off: memory vs context window usage

**Queue Optimization**:
- Currently reloads current_turn_messages after each append
- Could batch all appends, reload once at end
- Minor performance improvement

**Error Recovery**:
- `cleanup_conversation_history()` inserts synthetic errors
- Could implement retry logic for failed tool executions
- Could allow user intervention for stuck tasks

**Parallel Child Execution**:
- Currently children execute independently
- Could implement coordination mechanisms
- Could share state between children via Redis

### Known Limitations

**No Automatic Retry**:
- `call_llm_api()` doesn't retry on throttling
- Caller must handle retry logic
- Could implement exponential backoff with retry

**No Turn Limit**:
- Tasks can run indefinitely
- No automatic cleanup of old tasks
- Could implement TTL or max turn limit

**No Conversation Compression**:
- Full conversation sent to Bedrock each time
- Could become expensive for long conversations
- Could implement summarization or compression

**Single Redis Instance**:
- All tasks share one Redis instance
- Could become bottleneck at scale
- Could implement sharding or clustering

---

## Conclusion

This documentation covers the complete architecture and operation of the UniCore agent system. Key takeaways:

1. **Process-per-task model** with Redis state storage
2. **Queue-based message handling** for consistency
3. **Turn-based conversation structure** with proper boundaries
4. **Parent-child task coordination** via completion messages
5. **Bedrock API compliance** via cleanup and validation

The system is designed for reliability and debuggability, with clear separation of concerns and comprehensive logging.


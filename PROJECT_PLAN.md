# UniCore Agent - PROJECT PLAN

## PROJECT OVERVIEW

UniCore Agent is an **ultra-minimal agentic system** using AWS Bedrock's Converse API with a strict fail-fast philosophy. The agent executes tools based on LLM decisions, with all error recovery handled agentically rather than programmatically.

### Core Architecture

- **Redis-based state management** - All conversation and task state stored in Redis
- **Process-based execution** - Each task runs as an independent Python process
- **Queue-based messaging** - All messages flow through Redis queues for reliability
- **Watchdog monitoring** - Central process manager monitors and kills tasks
- **Fail-fast philosophy** - Natural crashes with full tracebacks, no defensive coding
- **Centralized throttling coordination** - Multi-task throttling prevention with mandatory backoff
- **Dynamic model discovery** - Automatic discovery and short-name mapping for all Bedrock models
- **Module-level simplicity** - No unnecessary classes, minimal OOP overhead
- **Docker containerization** - Process isolation with host filesystem access and persistent workspace

---

## ABSOLUTE RULES - NO EXCEPTIONS

### 1. FAIL-FAST DEVELOPMENT PHILOSOPHY

**These rules are MANDATORY and ABSOLUTE:**

1. **ZERO ERROR HANDLING** - The agent must NEVER include try/except blocks, error checking, or fallback logic, EXCEPT for:
   - **Throttling exceptions** (ThrottlingException, TooManyRequestsException, ServiceUnavailable)
   - **Expected subprocess behavior** (TimeoutExpired when SIGTERM doesn't terminate process)
   - **Inherently stochastic processes** (LLM JSON parsing with retry loops)
   - **OS-level process checks** (ProcessLookupError when verifying dead PIDs)

2. **IMMEDIATE FAILURE** - Any imperfection in logic or state MUST result in immediate program termination with a full traceback.

3. **NO DEFENSIVE CODING** - Do not check for None, validate inputs, or handle edge cases. Let the program crash naturally.

4. **AGENT-DRIVEN ERROR RECOVERY** - When errors occur, they are returned to the LLM as tool results. The LLM decides how to proceed - this is the ONLY error handling mechanism.

5. **MAXIMUM COMPACTNESS** - Code must be as compact as possible without sacrificing readability. Use single-line imports, minimal variable names, and condensed logic where appropriate.

6. **NO STATE VALIDATION** - Never validate state before use. If state is corrupted, the program must crash immediately.

7. **NO UNNECESSARY CLASSES** - Use module-level code for single-instance daemons. Classes only when polymorphism or multiple instances are needed.

### 2. FORBIDDEN ANTIPATTERNS

**NEVER include these in production code:**

- ❌ State Validation - `if x:`, `if not x:`, `if x in y:` before accessing
- ❌ None Checking - `if x is None:` before using
- ❌ Input Validation - `isinstance()`, `len()` checks before accessing
- ❌ Error Messages - Custom error messages instead of natural crashes
- ❌ Graceful Degradation - `return False`, `return None`, `sys.exit()` with messages
- ❌ Default Values - Providing fallbacks for missing data (except `or` pattern for optional params)
- ❌ Try/Except Blocks - Except for the four allowed cases above
- ❌ Unnecessary Classes - Single-instance daemons should use module-level code

### 3. ALLOWED PATTERNS

**These are the ONLY acceptable patterns:**

- ✅ Direct access - `x['key']` (let KeyError reveal issues)
- ✅ Direct indexing - `sys.argv[1]` (let IndexError reveal issues)
- ✅ Natural crashes - Let TypeError, AttributeError, etc. propagate
- ✅ Throttling exception handling - ONLY for AWS rate limiting
- ✅ Expected subprocess exceptions - ONLY TimeoutExpired in kill sequence
- ✅ LLM JSON retry loops - ONLY for inherently stochastic parsing
- ✅ OS-level process exceptions - ONLY when checking dead PIDs
- ✅ Optional parameter defaults - `param = param or default_value`
- ✅ Polling loop existence checks - `if x:` when polling for resource creation

---

## QUEUE-BASED MESSAGE SYSTEM

### Architecture

All messages flow through Redis queues to ensure zero message loss and proper conversation format:

```
Any Source → queue_message_for_task() → Redis Queue → process_task_queue() → Conversation
```

### Queue Message Structure

```json
{
  "type": "user" | "completion" | "tool_result",
  "content": "message text" | tool_result_object,
  "sender_id": task_id | null,
  "tool_use_id": "toolUseId" (only for tool_result),
  "timestamp": 1234567890.123
}
```

### Key Functions

- **`queue_message_for_task()`** - Adds message to task's input queue
- **`process_task_queue()`** - Processes all queued messages, matches tool results with tool uses, maintains Bedrock conversation format

### Benefits

✅ Fixes child completion race conditions
✅ Enables true task persistence (pause/resume for months)
✅ Clean separation between inbox and history
✅ Unified interface for all message sources
✅ Graceful interruption handling with dummy errors for missing tool results
✅ Maintains strict Bedrock format (toolResults first, user messages after)

---

## TASK STATE MANAGEMENT

### Realtime Task Monitoring

The system provides OS-level verification of task status and health:

**`get_task_tree_status(parent_task_id=None, include_cpu=True)`**

Returns realtime status by:
1. Verifying PIDs against OS with `os.kill(pid, 0)`
2. Measuring CPU usage via `psutil`
3. Tracking CPU activity to detect stopped vs. active tasks
4. Extracting last tool use from conversation history
5. Distinguishing between "in API call" vs. "stuck after tool"

### Status Information

```python
{
    'task_id': {
        'status': 'stopped'|'running',
        'pid': int|None,
        'cpu_percent': float|None,
        'cpu_idle_seconds': float|None,
        'last_tool_use': {
            'tool_name': str,
            'tool_input': dict,
            'started_at': float,
            'elapsed_seconds': float
        } | {'in_api_call': {...}} | None
    }
}
```

### API Call Tracking

Tasks set `task_api_call:{task_id}` in Redis before Bedrock API calls and delete after completion. This distinguishes "waiting on LLM" from "stuck after tool execution".

### Process Cleanup

All tasks use try/finally to ensure Redis state is cleaned up even on abnormal exit.

---

## DOCKER IMPLEMENTATION

### Architecture

- **Base Image**: NVIDIA CUDA 12.6 with Ubuntu 24.04, Python 3.12
- **Host Redis**: Redis runs on host at localhost:6379
- **Container**: Runs web_ui.py and agent processes
- **Network**: Host network (container uses host's Redis)
- **GPU**: NVIDIA GPU access via --gpus all

### Mounts

- `/mnt/host` → `/` (host root, read-only)
- `/mnt/persistent` → `/work/agent_mount` (read-write workspace)

### Files

- **Dockerfile** - Container definition
- **launch** - Build and run script with mounts and environment
- **start_agent.sh** - Container entrypoint
- **requirements.txt** - Python dependencies
- **.dockerignore** - Exclude __pycache__ and .git
- **.env** - Environment variables (AWS/Anthropic credentials)

### Usage

```bash
cd /work/unicore
./launch
```

### Protection Level

✅ Process isolation
✅ Read-only host filesystem access
✅ Persistent workspace at /mnt/persistent
✅ Container can be killed without affecting host
✅ GPU access for future tools
✅ Environment variables loaded from .env
⚠️ Agent still runs on original code (Phase 2 would add code separation)

---

## CONVERSATION FORMAT COMPLIANCE

### Valid Bedrock Format

```
Turn N:
  Msg 0: User [text]
  Msg 1: Assistant [text + toolUse blocks]
  Msg 2: User [toolResult blocks only]
  Msg 3: User [text]  ← Multiple user messages allowed
  Msg 4: User [text]  ← after toolResults
```

### How System Maintains It

1. **Tool results always first** after assistant toolUse
2. **User messages always after** tool results
3. **All in same turn** until assistant responds
4. **process_task_queue()** matches tool results with tool uses and creates dummy errors for missing results

---

## KEY IMPLEMENTATION DETAILS

### Interruption Handling

When a process is killed mid-tool-execution:
1. Completed tool results remain in queue
2. On resume, `process_task_queue()` matches available results
3. Missing tool results get dummy errors: `{"error": "Tool execution interrupted or failed to complete"}`
4. LLM sees exactly what happened and can decide how to proceed

### Task Tree Operations

- **`get_child_tree(task_id, r)`** - Recursively finds all descendants
- **`get_last_tool_use(task_id, r)`** - Extracts most recent tool from conversation
- **`check_process_alive(pid, task_id)`** - Fast OS-level PID verification

### Performance

- Checking 327 tasks with psutil: ~50-100ms
- Checking task tree of 10 children: ~5-10ms
- CPU measurement is non-blocking (`interval=0`)
- Tree traversal caches results per call

---

## FUTURE ENHANCEMENTS

1. Add CLI tool for task monitoring
2. Add web UI endpoint to display tree status
3. Add prompt guidance for LLM to interpret task status
4. Phase 2 Docker: Separate agent code from host code

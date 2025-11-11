#!/usr/bin/env python3

"""Utility functions for MicroCore agents"""

import uuid
import redis
import time
import json
import copy
import os
from botocore.exceptions import ClientError as BotocoreClientError, ReadTimeoutError
import psutil
from pathlib import Path
import subprocess
import sys
from prompts import build_dynamic_system_prompt, build_static_system_prompt, transcribe

dead_statuses = [psutil.STATUS_DEAD, psutil.STATUS_STOPPED, psutil.STATUS_ZOMBIE]
good_statuses = [psutil.STATUS_RUNNING, psutil.STATUS_SLEEPING, psutil.STATUS_WAKING, psutil.STATUS_DISK_SLEEP, psutil.STATUS_IDLE]

def launch_task_agent(task_id=None, 
                model='sonnet45', 
                enable_recursion=True,
                messages=None, 
                parent_task_id=None, 
                base_name=None, 
                max_iterations=250, 
                start_process=True):
    """
    Launch agent process with queue-only message architecture
    
    Args:
        task_id: Optional existing task ID to resume
        model: Model ARN or short name
        enable_recursion: Whether to allow spawn_task tool
        messages: Initial messages (optional) - will be queued
        parent_task_id: Parent task ID if this is a child task
        base_name: Base name for new child tasks (1-3 words, required for child tasks)
    
    Returns:
        (pid, task_id) tuple
    """
    r = redis.Redis(decode_responses=True)

    pid = None
    
    # Check current status and pid and confirm pid is running
    existing_task = False
    if task_id:
        existing_task, pid, _ = check_task_activity(task_id)  
    else:
        task_id = generate_task_id(parent_task_id, base_name)
    
    model_arn = resolve_model(model, r)
    static_prompt = build_static_system_prompt(model_arn, parent_task_id=parent_task_id)
    messages = messages or []
    script_dir = Path(__file__).parent
    core_path = script_dir / 'core.py'
    command = f"{sys.executable} {core_path} {task_id}"
    
    if existing_task:
        print(f"[LAUNCHER] WARNING: False launch. Task {task_id} is already running")
        return pid, task_id
    
    # Check if conversation already exists (reactivating a stopped task)
    existing_conversation = r.json().get(f'task:{task_id}')
    
    if existing_conversation:
        # Reactivating existing task - don't overwrite conversation
        print(f"[LAUNCHER] Reactivating existing task {task_id}")
        task_data = r.json().get(f'task_data:{task_id}')
    else:
        # Create and save new task data
        print(f"[LAUNCHER] Creating new task {task_id}")
        task_data = {
            'task_id': task_id,
            'parent_task_id': parent_task_id,
            'model_name': model_arn,
            'static_system_prompt': static_prompt,
            'enable_recursion': enable_recursion,
            'created_at': time.time(),
            'status': 'stopped',
            'last_usage': {},
            'children': [],
            'command': command,
            'max_iterations': max_iterations,
            'process_started_at': time.time()
        }
        r.json().set(f'task_data:{task_id}', '$', task_data)
        conversation = [{'turn_number':0, 'messages':[], 'started_at': time.time()}]
        r.json().set(f'task:{task_id}', '$', conversation)
    
    num_queued = 0
    if messages:
        for msg in messages:
            text_contents = []
            if msg['role'] == 'user' and msg['content']:
                for item in msg['content']:
                    if 'text' in item:
                        text_contents.append(item['text'])
                for text_content in text_contents:
                    queue_message_for_task(task_id, 'user', text_content, sender_id=None, auto_launch=False)
                    num_queued += 1
            print(f"[LAUNCHER] Queued {num_queued} initial messages")

    # Get total messages in queue
    queued_messages = r.json().get(f'task_queue:{task_id}')
    
    if start_process and queued_messages:
        # NOW spawn process after task_data is ready
        process = subprocess.Popen(command, shell=True, preexec_fn=os.setsid)
        pid = process.pid
    
        # Update task_data with PID
        r.json().set(f'task_data:{task_id}', '$.pid', pid)
        print(f"[LAUNCHER] Launched {task_id} with PID {pid}")
    
    return pid, task_id

def generate_task_id(parent_task_id, base_name):
    """Generate task_id based on whether it's a root or child task"""
    if parent_task_id:
        if not base_name:
            raise ValueError("base_name is required for child tasks (1-3 words)")
        normalized_base = '_'.join(base_name.lower().split())
        return f"{normalized_base}_{uuid.uuid4().hex[:6]}"
    else:
        return f"conversation_{uuid.uuid4().hex[:6]}"

def resolve_model(model, r):
    """Resolve model short name to ARN"""
    if model.startswith('arn:') or model.startswith('us.') or model.startswith('eu.'):
        return model
    
    models = r.json().get('bedrock:converse:models')
    return models[model]['arn']

error_message = "Tool use was stopped by an error or a user interruption."

def cleanup_conversation_history(history):
    """
    Fixes conversation structure to comply with Bedrock API requirements.
    
    When consecutive assistant messages occur, look ahead to find the real
    tool results and insert them in the correct positions.
    """
    cleaned_history = []
    
    for turn in history:
        turn_messages = turn['messages']
        
        with open('/tmp/last_messages.jsonl', 'w') as f:
            f.write(json.dumps({'turn': turn_messages}) + '\n')
        
        # Collect all tool results by tool_use_id
        all_tool_results = {}
        for msg in turn_messages:
            if msg['role'] == 'user':
                for item in msg['content']:
                    if 'toolResult' in item:
                        tool_id = item['toolResult']['toolUseId']
                        all_tool_results[tool_id] = item
        
        # Build cleaned message list
        new_messages = []
        last_role = 'assistant'
        messages = copy.copy(turn_messages)
        
        for n, msg in enumerate(messages):
            timestamp = msg.get('timestamp')
            
            if msg['role'] == 'assistant' and last_role == 'user':
                new_messages.append(msg)
                last_role = 'assistant'
                
            elif msg['role'] == 'assistant' and last_role == 'assistant':
                # Consecutive assistant - insert user message with results from previous assistant
                prev_assistant = new_messages[-1]
                tool_ids_needed = [item['toolUse']['toolUseId'] for item in prev_assistant['content'] if 'toolUse' in item]
                
                if tool_ids_needed:
                    user_content = []
                    for tool_id in tool_ids_needed:
                        if tool_id in all_tool_results:
                            user_content.append(all_tool_results[tool_id])
                            all_tool_results[tool_id] = None
                        else:
                            user_content.append({'toolResult': {'toolUseId': tool_id, 'content': [{'text': error_message}]}})
                    
                    new_messages.append({'role': 'user', 'content': user_content, 'timestamp': timestamp})
                    last_role = 'user'
                
                new_messages.append(msg)
                last_role = 'assistant'
                
            elif msg['role'] == 'user':
                # Keep only unused tool results and all non-tool content
                unused_content = []
                for item in msg['content']:
                    if 'toolResult' in item:
                        tool_id = item['toolResult']['toolUseId']
                        if all_tool_results.get(tool_id) is not None:
                            unused_content.append(item)
                            all_tool_results[tool_id] = None
                    else:
                        unused_content.append(item)
                
                if unused_content:
                    msg['content'] = unused_content
                    new_messages.append(msg)
                    last_role = 'user'
                    
            else:
                print(n, f'[CORE] WARNING: catch-all for message of role {msg["role"]}, last role {last_role}')
                new_messages.append(msg)
        
        # Renumber messages
        for n, msg in enumerate(new_messages):
            msg['message_number'] = n
        
        cleaned_history.append({
            'turn_number': turn['turn_number'],
            'started_at': turn['started_at'],
            'messages': new_messages
        })
    
    return cleaned_history

def proactive_delay(model_arn, task_id):
    r = redis.Redis(decode_responses=True)
    task_data = r.json().get(f'task_data:{task_id}')
    is_alive, pid, _ = check_task_activity(task_id)
    if not is_alive:
        return False
    # Check for mandatory backoff
    throttle_state = r.get(f'throttle_state:{model_arn}')
    if throttle_state:
        state_data = json.loads(throttle_state)
        if state_data.get('mandatory_backoff'):
            backoff_time = random.uniform(20, 30)
            print(f"[CORE] Mandatory backoff for {model_arn}: {backoff_time:.1f}s")
            time.sleep(backoff_time)
            r.delete(f'throttle_state:{model_arn}')

            # Check status again after backoff
            is_alive, pid, _ = check_task_activity(task_id)
            if not is_alive:
                return False
    
    # Calculate proactive delay
    usage = task_data.get('last_usage', {})
    next_tokens = usage.get('inputTokens', 0) + usage.get('outputTokens', 0) + 500
    required_delay = max((next_tokens * 60) / 200000, 0.3)
    return required_delay

def is_turn_ending_message(message):
    """Check if message is from assistant with text but no tool use"""
    if message.get('role') != 'assistant':
        return False
    
    content = message.get('content', [])
    has_text = False
    has_tool_use = False
    
    for block in content:
        if 'text' in block:
            has_text = True
        if 'toolUse' in block:
            has_tool_use = True
    
    return has_text and not has_tool_use

def build_completion_message(child_task_id, child_conversation, success):
    """Build completion notification message for parent task"""
    total_turns = len(child_conversation)
    total_tool_iterations = 0
    final_message = None
    
    # Find the last assistant message
    for turn in reversed(child_conversation):
        for message in reversed(turn.get('messages', [])):
            if message['role'] == 'assistant':
                final_message = message['content']
                break
        if final_message:
            break
    
    # Count tool iterations
    for turn in child_conversation:
        for i, message in enumerate(turn.get('messages', [])):
            if message['role'] == 'assistant':
                next_idx = i + 1
                if next_idx < len(turn['messages']):
                    next_msg = turn['messages'][next_idx]
                    if (next_msg['role'] == 'user' and 
                        isinstance(next_msg.get('content'), list) and
                        any('toolResult' in str(content) for content in next_msg['content'])):
                        total_tool_iterations += 1
    
    # Extract text from final message
    final_text = ""
    if final_message:
        for content in final_message:
            if 'text' in content:
                final_text = content['text']
                break
    
    status = "completed successfully" if success else "failed"
    print(f"\n[CORE] Child task {child_task_id} completed with status: {status}")
    return (f"[SYSTEM] Child task {child_task_id} has {status}. "
            f"Ran {total_turns} turns with {total_tool_iterations} tool iterations. "
            f"You can continue the conversation by calling spawn_task with task_id='{child_task_id}' "
            f"and a new message.\n\nFinal response from child:\n{final_text}")

def build_llm_input(model_arn, bedrock_messages, full_system_prompt, tool_schemas):
    """
    Build input parameters for Bedrock Converse API.
    
    Args:
        model_arn: Model ARN to use
        bedrock_messages: List of messages in Bedrock format
        full_system_prompt: System prompt text
        tool_schemas: List of tool schemas (can be empty)
    
    Returns:
        Dictionary of input parameters for Bedrock Converse API
    """
    converse_params = {
        'modelId': model_arn,
        'messages': bedrock_messages,
        'system': [{"text": full_system_prompt}]
    }
    if tool_schemas:
        converse_params['toolConfig'] = {"tools": tool_schemas}
    return converse_params

def call_llm_api(bedrock, input_params, task_id, r, last_req_time, throttle_multiplier):
    """
    Call Bedrock Converse API with throttling exception handling.
    
    Args:
        bedrock: Boto3 Bedrock client
        input_params: Input parameters for Bedrock Converse API
        task_id: Current task ID
        r: Redis connection
        last_req_time: Timestamp of last request
        throttle_multiplier: Current throttle multiplier
    
    Returns:
        Tuple of (response, new_last_req_time, new_throttle_multiplier)
    """
    model_arn = input_params['modelId']  # Extract for pub/sub coordination
    required_delay = proactive_delay(model_arn, task_id)

    if required_delay == False:
        # Task is no longer active. TO DO: Work out a better way to handle this
        required_delay = 1.0
    
    print(f"[CORE] Waiting for {required_delay:.1f}s")
    if last_req_time:
        time.sleep(max(0, required_delay - (time.time() - last_req_time)))
    
    # Call Bedrock with throttling and timeout exception handling
    response_timer = time.time()
    try:
        response = bedrock.converse(**input_params)
        response_time = time.time() - response_timer
        print(f"[CORE] LLM API response time: {response_time:.1f}s")
        new_last_req_time = time.time()
        new_throttle_multiplier = max(1.0, throttle_multiplier * 0.9)
        r.publish(f'throttle_success:{model_arn}', json.dumps({'task_id': task_id, 'timestamp': time.time()}))

    except (ReadTimeoutError, BotocoreClientError) as e:
        # Determine error code for logging and filtering
        if isinstance(e, ReadTimeoutError):
            # This is a more serious timeout, so we need to back off longer
            error_code = 'ReadTimeoutError'
            extra_backoff = 60
        else:
            error_code = e.response['Error']['Code']
            extra_backoff = 30
            # Re-raise non-throttling BotocoreClientErrors immediately
            if error_code not in ['ThrottlingException', 'TooManyRequestsException', 'ServiceUnavailable']:
                filename = f'/tmp/llm_api_error_{uuid.uuid4().hex[:6]}.json'
                print(f"[CORE] ERROR: {error_code}, writing to {filename}")
                with open(filename, 'w') as f:
                    f.write(json.dumps(input_params))
                raise
        
        # Handle as throttling event
        print(f"[CORE] WARNING: {error_code}, treating as throttling event")
        r.publish(f'throttle_exception:{model_arn}', json.dumps({
            'task_id': task_id,
            'error_code': error_code,
            'timestamp': time.time()
        }))
        new_throttle_multiplier = min(3.0, throttle_multiplier * 1.5)
        backoff_time = required_delay * new_throttle_multiplier
        print(f"[CORE] Backing off {backoff_time:.1f}s, multiplier now {new_throttle_multiplier}")
        time.sleep(backoff_time + extra_backoff)

        # Retry the request recursively (not sure about this - test and decide later)
        #return call_llm_api(bedrock, input_params, task_id, r, required_delay, time.time(), new_throttle_multiplier)

    return response, new_last_req_time, new_throttle_multiplier

def check_task_activity(task_id):
    # Check if task is active by checking process status, and clean up Redis status if not
    r = redis.Redis(decode_responses=True)
    result = (False, None, None)
    needs_cleanup = True
    task_data = r.json().get(f'task_data:{task_id}')
    if task_data is not None:
        try:
            pid = task_data.get('pid')
        except:
            print(f"\n{task_id}\n\n{task_data}\n")
        if pid is not None:
            try:
                proc = psutil.Process(pid)
                cpu = proc.cpu_percent(interval=0)
                true_status = proc.status()
                cmdline_str = ' '.join(proc.cmdline())
                if true_status in good_statuses and 'core.py' in cmdline_str and task_id in cmdline_str:
                    result = (True, pid, cpu)
                    r.json().set(f'task_data:{task_id}', '$.status', 'running')
                    needs_cleanup = False
            except:
                pass
        
    if needs_cleanup and r.exists(f'task_data:{task_id}'):
        r.json().set(f'task_data:{task_id}', '$.pid', None)
        r.json().set(f'task_data:{task_id}', '$.status', 'stopped')
        r.publish(f'task_messages:{task_id}', json.dumps({"type": "process_ended"}))

    # TO DO: Add logic to clean up dead processes

    # RETURNS (is_alive, pid, cpu_percent)
    return result

def cleanup_task_statuses(r):
    # Mop up all incorrectly marked stopped tasks at launch
    task_keys = r.keys("task:*")

    for tk in task_keys:
        task_id = tk.lstrip("task").lstrip(':')
        check_task_activity(task_id)

    print(f"[CORE] Cleaned up {len(task_keys)} task statuses")

def get_last_tool_use(task_id, r):
    """Extract most recent tool use from conversation"""
    conversation = r.json().get(f'task:{task_id}')
    for turn in reversed(conversation):
        for msg in reversed(turn.get('messages', [])):
            if msg['role'] == 'assistant':
                for content_block in msg.get('content', []):
                    if 'toolUse' in content_block:
                        tool_use = content_block['toolUse']
                        return {
                            'tool_name': tool_use['name'],
                            'tool_input': tool_use['input'],
                            'started_at': msg.get('timestamp'),
                            'elapsed_seconds': time.time() - msg.get('timestamp', time.time())
                        }
    return None

def get_child_tree(task_id, r):
    """Recursively get all children of a task"""
    children = []
    task_data = r.json().get(f'task_data:{task_id}')
    for child_id in task_data.get('children', []):
        children.append(child_id)
        children.extend(get_child_tree(child_id, r))
    all_tasks = r.keys('task_data:*')
    for key in all_tasks:
        td = r.json().get(key)
        if td.get('parent_task_id') == task_id:
            cid = td['task_id']
            if cid not in children:
                children.append(cid)
                children.extend(get_child_tree(cid, r))
    return children

def queue_message_for_task(task_id: str, message_type: str, content: str, sender_id=None, tool_use_id=None, auto_launch=True):
    """Add message to task's input queue"""
    r = redis.Redis(decode_responses=True)
    
    queue_msg = {
        'type': message_type,
        'content': content,
        'sender_id': sender_id,
        'timestamp': time.time()
    }
    
    if tool_use_id:
        queue_msg['tool_use_id'] = tool_use_id
    
    queue_key = f'task_queue:{task_id}'
    existing = r.json().get(queue_key)
    if not existing:
        r.json().set(queue_key, '$', [])
    
    r.json().arrappend(queue_key, '$', queue_msg)

    is_running, _, _ = check_task_activity(task_id)
    print(f"[CORE] Message queued for task {task_id}. Task is running? {is_running}")
    if not is_running and auto_launch:
        print(f"[CORE] Launching task {task_id}")
        launch_task_agent(task_id, start_process=True)

   

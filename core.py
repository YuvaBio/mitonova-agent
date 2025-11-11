"""Core Agent Execution Loop - Reads state from Redis, executes turns, updates state"""

import boto3, json, time, sys, os, traceback, random
import redis
import os
from dotenv import load_dotenv
from botocore.exceptions import ClientError as BotocoreClientError
from pathlib import Path
import subprocess
from tools import TOOLS, TOOL_SCHEMAS
from decimal import Decimal
from utils import call_llm_api, queue_message_for_task, build_llm_input, generate_task_id, resolve_model, build_completion_message
from utils import cleanup_task_statuses, cleanup_conversation_history, check_task_activity, proactive_delay, launch_task_agent
from prompts import build_dynamic_system_prompt, build_static_system_prompt


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


load_dotenv()

# Throttling state
last_req_time = None
throttle_multiplier = 1.0

msg1 = "[SYSTEM] This is a single-iteration task. You may either respond via text to your parent task or perform one or more simultaneous tool uses, but you will not be able to respond or do further work after tool use "
msg2 = "[SYSTEM] This is a two-iteration task. You should use this initial iteration to perform you assigned task in one or more simultaneous tool calls, then use your second action to report your results. "
msg3 = "[SYSTEM] Warning: Iteration {iteration + 1} of {max_iterations}. Finish up your work and perform any final safety and/or hygiene operations and prepare to use your final iteration to report your results if successful, or to thoroughly document failures, any partial successes, and recommended next steps for the parent task."
msg4 = "[SYSTEM] Final iteration. Use this final operation to give the parent task your detailed final report rather than using tools."

def get_system_message(iteration, max_iterations):
    if max_iterations == 1:
        system_message = msg1
    elif max_iterations == 2 and iteration == 0:
        system_message = msg2
    elif max_iterations > 2 and max_iterations - iteration == 2:
        system_message = msg3
    elif iteration == max_iterations - 1:
        system_message = msg4
    else:
        system_message = None
    return system_message

def execute_tools(output, task_id):
    """Execute tools from assistant response and queue results"""
    
    for content_block in output['message']['content']:
        if 'toolUse' in content_block:
            tool_use = content_block['toolUse']
            tool_name = tool_use['name']
            tool_input = tool_use['input']
            tool_use_id = tool_use['toolUseId']
            
            print(f"[CORE] Executing tool: {tool_name}")
            
            try:
                result = TOOLS[tool_name](tool_input, task_id)
                
                tool_result = {
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "content": [{"text": json.dumps(result, cls=DecimalEncoder)}]
                    }
                }
            except Exception as e:
                error_msg = f"Tool execution failed: {type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
                print(f"[CORE] Tool error: {error_msg}")
                tool_result = {
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "content": [{"text": json.dumps({"error": error_msg})}],
                        "status": "error"
                    }
                }
            
            print(f"[CORE] Queueing tool result {tool_use_id} for {task_id}")
            queue_message_for_task(task_id, 'tool_result', tool_result, sender_id=task_id, tool_use_id=tool_use_id)

def dequeue_messages(task_id):
    """Send a message to a task by appending to conversation history"""
    r = redis.Redis(decode_responses=True)

    conversation = r.json().get(f'task:{task_id}')
    if conversation and isinstance(conversation, list) and len(conversation) > 0:
        current_turn_index = len(conversation) - 1
        current_turn = conversation[-1]
    else:
        current_turn_index = 0
        current_turn = {'turn':0, 'messages':[]}
    
    tool_results = []
    text_messages = []
    queue_messages = r.json().get(f'task_queue:{task_id}')
    if queue_messages:
        for msg in queue_messages:
            sample = str(msg.get('content'))[:100] + '...' if len(str(msg.get('content'))) > 100 else ""
            print(f"[CORE] Dequeued message {sample}")
            if isinstance(msg, dict):
                msg_type = msg.get('type')
                content = msg.get('content')
                if msg_type == 'tool_result':
                    tool_results.append(content)
                else:
                    text_messages.append(content)
            elif isinstance(msg, str):
                text_messages.append(msg)
        r.delete(f'task_queue:{task_id}')
    conv_key = f"task:{task_id}"
    conversation = r.json().get(conv_key)
    
    # Add tool results first as single user message (if any)
    if tool_results:
        message_number = len(current_turn['messages'])
        user_message = {
            "role": "user",
            "content": tool_results,
            "message_number": message_number,
            "timestamp": time.time()
        }
        r.json().arrappend(f'task:{task_id}', f'$[{current_turn_index}].messages', user_message)
    
    # Add text messages as separate user messages
    for message in text_messages:
        message_number = len(current_turn['messages'])
        user_message = {
            "role": "user",
            "content": [{"text": message}],
            "message_number": message_number,
            "timestamp": time.time()
        }
        r.json().arrappend(f'task:{task_id}', f'$[{current_turn_index}].messages', user_message)
    
    # Notify task via pub/sub
    r.publish(f"task_messages:{task_id}", json.dumps({"type": "new_message"}))

def execute_iteration(task_id, r, bedrock, system_message=None):
    """Execute one iteration of the agent loop"""
    global last_req_time, throttle_multiplier
    task_data = r.json().get(f'task_data:{task_id}')

    print(f"[CORE] Dequeueing messages for {task_id}")
    dequeue_messages(task_id)

    conversation = r.json().get(f'task:{task_id}')
    conversation = cleanup_conversation_history(conversation)
    
    current_turn_index = len(conversation) - 1
    current_turn = conversation[-1]
    message_number = len(current_turn['messages'])
    print(f"[CORE] Turn {current_turn_index} message {message_number} for {task_id}")
    
    messages = []
    for turn in conversation:
        for msg in turn['messages']:
            messages.append({'role': msg['role'], 'content': msg['content']})

    r.delete(f'task_queue:{task_id}')
    system_message = None
    
    # Build system prompt
    static_prompt = task_data['static_system_prompt']
    dynamic_prompt = build_dynamic_system_prompt(task_data, current_turn_index)
    full_system_prompt = static_prompt + dynamic_prompt
    
    model_arn = task_data['model_name']
    
    input_params = build_llm_input(model_arn, messages, full_system_prompt, TOOL_SCHEMAS)

    r.set(f'task_api_call:{task_id}', json.dumps({
        'started_at': time.time(),
        'turn': current_turn_index,
        'message_count': message_number
    }), ex=300)

    print(f"\n[CORE] Calling LLM API for {task_id} normal iteration")
    api_result = call_llm_api(
        bedrock, input_params, task_id, r, last_req_time, throttle_multiplier
    )

    r.delete(f'task_api_call:{task_id}')

    # Check if task was interrupted (call_llm_api returns False)
    if api_result == False:
        print(f"[CORE] Task {task_id} was interrupted, ending turn")
        return True  # Turn ending
    
    response, last_req_time, throttle_multiplier = api_result
    
    # Extract response
    output = response['output']
    stop_reason = response['stopReason']
    usage = response['usage']
        
    r.json().set(f'task_data:{task_id}', '$.last_usage', usage)
    print(f"[CORE] Stop reason: {stop_reason}")
    print(f"[CORE] Tokens - Input: {usage['inputTokens']}, Output: {usage['outputTokens']}")
    
    # Store assistant message
    assistant_message = {
        'role': 'assistant',
        'content': output['message']['content'],
        'message_number': message_number,
        'timestamp': time.time()
    }
    
    r.json().arrappend(f'task:{task_id}', f'$[{current_turn_index}].messages', assistant_message)

    if stop_reason == 'tool_use':
        print(f"[CORE] Executing tools for {task_id}")
        execute_tools(output, task_id)
    
    # Publish message notification
    r.publish(f'task_messages:{task_id}', json.dumps({
        'task_id': task_id,
        'turn_number': current_turn_index,
        'message_number': message_number,
        'message_type': 'assistant',
        'timestamp': time.time(),
        'stop_reason': stop_reason
    }))

    turn_ending = stop_reason not in ['tool_use', 'max_tokens']
    if turn_ending:
        print(f"[CORE] {task_id} TURN {current_turn_index} ENDING. Summarizing...")
        last_req_time, throttle_multiplier = summarize_and_store_turn(
            task_id, r, bedrock, current_turn_index, last_req_time, throttle_multiplier
        )

    return turn_ending

def summarize_and_store_turn(task_id, r, bedrock, current_turn_index, last_req_time, throttle_multiplier):
    """Generate and store a summary of the completed turn"""
    conversation = r.json().get(f'task:{task_id}')
    turn_data = conversation[current_turn_index]
    turn_messages = turn_data.get('messages', [])
    
    # Build messages for summarization
    summary_messages = [{
        'role': 'user',
        'content': [{
            'text': f"Summarize the work accomplished in this turn. Turn messages:\n\n{json.dumps(turn_messages, indent=2)}"
        }]
    }]
    
    # Get model from task_data
    task_data = r.json().get(f'task_data:{task_id}')
    model_arn = task_data['model_name']
    
    # Build system prompt for summarization
    system_prompt = "You are a concise summarizer. Summarize the key work accomplished and decisions made in the provided turn. Be brief and factual."
    
    # Build input params using existing utility
    input_params = build_llm_input(model_arn, summary_messages, system_prompt, [])
    
    # Call LLM API with minimal delay for summarization

    print(f"\n[CORE] Calling LLM API for {task_id} summary")
    api_result =  call_llm_api(
        bedrock, input_params, task_id, r, last_req_time, throttle_multiplier
    )

    if api_result == False:
        print(f"[CORE] WARNING: Turn summarization for {task_id} failed, possibly due to interruption")
        return last_req_time, throttle_multiplier
    else:
        response, new_last_req_time, new_throttle_multiplier = api_result
    
    # Extract summary text
    summary = response['output']['message']['content'][0]['text']
    
    # Store summary in turn data
    r.json().set(f'task:{task_id}', f'$[{current_turn_index}].turn_summary', summary)
    
    return new_last_req_time, new_throttle_multiplier

def notify_parent_of_completion(task_id, r):
    """Notify parent task when this child task completes"""
    task_data = r.json().get(f"task_data:{task_id}")
    parent_task_id = task_data.get("parent_task_id")
    if not parent_task_id:
        return
    conversation = r.json().get(f"task:{task_id}")
    completion_msg = build_completion_message(task_id, conversation, True)
    queue_message_for_task(parent_task_id, 'completion', completion_msg, sender_id=task_id)

def run_agent(task_id, max_iterations=250):
    """Main agent loop"""
    my_pid = os.getpid()
    bedrock = boto3.client('bedrock-runtime')
    r = redis.Redis(decode_responses=True)
    task_data = r.json().get(f'task_data:{task_id}')
    parent_task_id = task_data.get('parent_task_id')

    # Only root task is responsible for cleanup
    if parent_task_id is None:
        cleanup_task_statuses(r)

    # Check if task is still running
    status, pid, _ = check_task_activity(task_id)
    if status and pid == my_pid:
        print(f"[CORE] Task {task_id} is still running, exiting")
        return

    conversation = r.json().get(f'task:{task_id}')
    if conversation and isinstance(conversation, list):
        current_turn_index = len(conversation) - 1
    else:
        current_turn_index = 0

    print(f"\n[CORE] Starting turn {current_turn_index} for {task_id}")

    r.json().set(f'task_data:{task_id}', '$.pid', my_pid)
    
    # Subscribe to throttle state
    model_arn = task_data['model_name']
    pubsub = r.pubsub()
    pubsub.subscribe(f'throttle_state:{model_arn}')

    did_work = False
    
    for iteration in range(max_iterations):
        # Root task cleans up statuses each iteration
        if parent_task_id is None:
            cleanup_task_statuses(r)
        system_message = get_system_message(iteration, max_iterations)

        queue = r.json().get(f'task_queue:{task_id}')
        print(f"[CORE] Iteration {iteration}: queue length = {len(queue) if queue else 0}")
    
        if not queue or len(queue) == 0:
            print(f"[CORE] Breaking: queue empty at iteration {iteration}")
            break

        print(f"[CORE] Calling execute_iteration for iteration {iteration}")
        turn_ending = execute_iteration(task_id, r, bedrock, system_message)
        did_work = True
    
        print(f"[CORE] Iteration {iteration} complete: turn_ending={turn_ending}")
        
        if turn_ending:
            # Check if there are more messages in the queue; keep going if so, otherwise break
            queue = r.json().get(f'task_queue:{task_id}')
            if queue and len(queue) > 0:
                print(f"[CORE] Turn ended but queue has {len(queue)} messages, continuing...")
                continue
            print(f"[CORE] Breaking: turn ending at iteration {iteration}")
            break

    print(f"[CORE] Agent {task_id} finished")

    if did_work:
        notify_parent_of_completion(task_id, r)
        num_messages = len(r.json().get(f'task:{task_id}')[current_turn_index].get('messages', []))
        if task_data.get('pid') == my_pid:
            r.json().set(f'task_data:{task_id}', '$.pid', None)
            r.json().set(f'task_data:{task_id}', '$.status', 'stopped')
            r.publish(f'task_messages:{task_id}', json.dumps({
                'task_id': task_id,
                'turn_number': current_turn_index,
                'message_number': num_messages,
                'message_type': 'completion',
                'timestamp': time.time()
            }))
    
    r.delete(f'task_api_call:{task_id}')


if __name__ == '__main__':
    task_id = sys.argv[1]
    run_agent(task_id, max_iterations=250)
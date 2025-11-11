#!/usr/bin/env python3

"""Query task tool - Ask questions about a task's conversation and status"""

import sys
import os
import json
import boto3
import redis
from dotenv import load_dotenv

# Add parent directory to path to import utils
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import call_llm_api, build_llm_input, resolve_model, check_task_activity
from prompts import transcribe

load_dotenv()

QUERY_TASK_SPEC = {
    "toolSpec": {
        "name": "query_task",
        "description": "Ask a question about a task's conversation history and current status",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to query"
                    },
                    "question": {
                        "type": "string",
                        "description": "The question to ask about the task"
                    },
                    "model": {
                        "type": "string",
                        "description": "Model to use (default: sonnet45). Options: haiku35, sonnet35, sonnet45, opus4, opus41"
                    }
                },
                "required": ["task_id", "question"]
            }
        }
    }
}


def query_task_tool(params, task_id):
    """Query a task's conversation and status"""
    target_task_id = params['task_id']
    question = params['question']
    model = params.get('model', 'sonnet45')
    
    # Connect to Redis
    r = redis.Redis(host='localhost', port=6379, decode_responses=True)
    
    # Get task data
    task_data = r.json().get(f'task_data:{target_task_id}')
    if not task_data:
        return {"error": f"Task {target_task_id} not found"}
    
    # Get task status
    is_alive, pid, cpu_percent = check_task_activity(target_task_id)
    cpu_percent = cpu_percent or 0
    
    # Get conversation transcript
    transcript = transcribe(target_task_id, r, include_tool_details=True)

    status = "running" if is_alive else "stopped"
    
    # Build prompt
    prompt = f"""You are analyzing a task's conversation history and status.

Task ID: {target_task_id}
Current Status: {status}
PID: {pid}
CPU Usage: {cpu_percent:.1f}%

Conversation Transcript:
{transcript}

Question: {question}

Please answer the question based on the conversation transcript and task status above."""
    
    # Resolve model ARN
    model_arn = resolve_model(model, r)
    
    # Prepare messages for Bedrock
    bedrock_messages = [
        {
            "role": "user",
            "content": [{"text": prompt}]
        }
    ]
    
    # Build LLM input using build_llm_input
    input_params = build_llm_input(
        model_arn=model_arn,
        bedrock_messages=bedrock_messages,
        full_system_prompt="You are a helpful assistant analyzing task conversations.",
        tool_schemas=[],  
        #system_message=None
    )
    
    # Create Bedrock client
    bedrock = boto3.client('bedrock-runtime', region_name=os.getenv('AWS_REGION', 'us-east-1'))
    
    # Call LLM API
    response, _, _ = call_llm_api(
        bedrock=bedrock,
        input_params=input_params,
        task_id=task_id,  # Current task ID (the one calling this tool)
        r=r,
        last_req_time=None,
        throttle_multiplier=1.0
    )
    
    # Extract response text
    output = response['output']
    message_content = output['message']['content']
    
    response_text = ""
    for content_block in message_content:
        if 'text' in content_block:
            response_text = content_block['text']
            break
    
    return {
        "task_id": target_task_id,
        "status": status,
        "question": question,
        "answer": response_text,
        "model_used": model
    }

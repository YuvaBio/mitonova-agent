#!/usr/bin/env python3
import json, asyncio, time, socket, sys, subprocess, uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from pathlib import Path
import redis.asyncio as aioredis
from typing import Dict, Optional
import uuid
import redis

sys.path.insert(0, str(Path(__file__).parent))
from utils import queue_message_for_task
from utils import launch_task_agent

redis_client = aioredis.Redis(
    host='localhost', 
    port=6379, 
    db=0, 
    decode_responses=True,
    socket_keepalive=True,
    socket_keepalive_options={
        socket.TCP_KEEPIDLE: 60,
        socket.TCP_KEEPINTVL: 10,
        socket.TCP_KEEPCNT: 3
    },
    health_check_interval=30
)
active_websockets: Dict[str, WebSocket] = {}

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def root():
    return (Path(__file__).parent / "web_ui.html").read_text()

@app.get("/api/config")
async def get_config():
    """Get UI configuration"""
    import os
    return {
        "chat_heading": os.getenv("CHAT_HEADING", "Task Status")
    }

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/tasks")
async def list_tasks():
    """Get all tasks with their hierarchy"""
    task_keys = await redis_client.keys("task_data:*")
    tasks = []
    for key in task_keys:
        task_id = key.split(":", 1)[1]
        task_data = await redis_client.json().get(key, '$')
        if task_data and isinstance(task_data, list):
            task_data = task_data[0]
        if task_data and isinstance(task_data, dict):
            conversation = await redis_client.json().get(f"task:{task_id}", '$')
            if isinstance(conversation, list):
                conversation = conversation[0] if conversation else None
            if conversation:
                task_data['conversation'] = conversation
                tasks.append(task_data)
    return {"tasks": tasks}

@app.get("/api/task/{task_id}/conversation")
async def get_conversation(task_id: str):
    """Get full conversation history for a task"""
    conv_key = f"task:{task_id}"
    conversation = await redis_client.json().get(conv_key)
    if not conversation:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"conversation": conversation}

@app.post("/api/task/new")
async def create_task(data: dict):
    """Create a new task"""
    model = data.get('model', 'sonnet45')
    initial_message = data.get('message')
    task_id = None

    pid = None

    # Prepare initial messages if provided
    initial_messages = []
    if initial_message:
        initial_messages = [{
            'role': 'user',
            'content': [{'text': initial_message}],
            'message_number': 0,
            'timestamp': time.time()
        }]
    
    # Launch task with initial messages
    try:
        loop = asyncio.get_event_loop()
        pid, task_id = await loop.run_in_executor(
            None, 
            lambda: launch_task_agent(
                task_id=None, 
                model=model, 
                enable_recursion=True,
                messages=initial_messages,
                start_process = initial_messages is not None
            )
        )
    except Exception as e:
        error_msg = (f"{type(e).__name__}: {str(e)}")
        print(f"[WEB_SERVER] Error creating task: {error_msg}")
        raise HTTPException(status_code=500, detail=error_msg)
    
    return {"task_id": task_id, "pid": pid}


@app.post("/api/task/{task_id}/message")
async def post_message(task_id: str, data: dict):
    """Queue up a message to an existing task"""
    message = data.get('message', '')
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    
    sample = message[:100]
    print(f"[WEB_SERVER] Queueing message for task {task_id}: {sample}")
    queue_message_for_task(task_id, 'user', message, sender_id=None)
    pid = None
    
    return {"success": True, "pid": pid}

@app.post("/api/task/{task_id}/stop")
async def stop_task(task_id: str):
    """Stop a running task"""
    
    print(f"[WEB_SERVER] Stopping task {task_id}")
    await redis_client.publish("kill_requests", json.dumps({"task_id": task_id}))
    
    return {"success": True}

@app.websocket("/ws/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    """WebSocket for real-time task updates"""
    await websocket.accept()
    
    # Create fresh Redis connection for this websocket
    ws_redis = aioredis.Redis(
        host='localhost', 
        port=6379, 
        db=0, 
        decode_responses=True,
        socket_keepalive=True,
        health_check_interval=30
    )
    
    # Check if task exists
    task_data = await ws_redis.json().get(f"task_data:{task_id}")
    
    active_websockets[task_id] = websocket
    
    # Subscribe to task messages and state changes
    pubsub = ws_redis.pubsub()
    await pubsub.subscribe(f"task_messages:{task_id}", f"task_complete:{task_id}", f"task_state:{task_id}")
    
    try:
        # Send initial conversation state
        conversation = await ws_redis.json().get(f"task:{task_id}")
        if conversation:
            await websocket.send_json({"type": "conversation", "data": conversation})
        
        # Listen for updates
        async def listen_redis():
            try:
                async for message in pubsub.listen():
                    if message['type'] == 'message':
                        channel = message['channel']
                        if channel == f"task_state:{task_id}":
                            # State change notification
                            state_data = json.loads(message['data'])
                            await websocket.send_json({"type": "state", "status": state_data['status']})
                        else:
                            # Message update notification
                            conversation = await ws_redis.json().get(f"task:{task_id}")
                            await websocket.send_json({"type": "update", "data": conversation})
            except asyncio.CancelledError:
                pass
        
        async def listen_websocket():
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get('type') == 'stop':
                        await stop_task(task_id)
            except asyncio.CancelledError:
                pass
        
        # Run both listeners concurrently
        tasks = [asyncio.create_task(listen_redis()), asyncio.create_task(listen_websocket())]
        await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pubsub.unsubscribe(f"task_messages:{task_id}", f"task_complete:{task_id}", f"task_state:{task_id}")
        await pubsub.aclose()
        await ws_redis.aclose()
        if task_id in active_websockets:
            del active_websockets[task_id]

def find_available_port(start_port=8000):
    """Find next available port starting from start_port"""
    port = start_port
    while port < start_port + 100:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(('', port))
            sock.close()
            return port
        except OSError:
            port += 1
    raise RuntimeError("No available ports found")

if __name__ == "__main__":
    import uvicorn, os
    port = int(os.environ.get("UNICORE_PORT", 8000))
    print(f"Starting UniCore Web UI on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

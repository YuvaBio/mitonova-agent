#!/usr/bin/env python3
"""Google Custom Search API tool"""

import os
import requests

GOOGLE_SEARCH_SPEC = {
    "toolSpec": {
        "name": "google_search",
        "description": "Search the web using Google Custom Search API",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 10)"
                    }
                },
                "required": ["query"]
            }
        }
    }
}

def google_search_tool(params, task_id):
    """Search Google and return results"""
    query = params['query']
    limit = params.get('limit', 10)
    
    api_key = os.environ['GOOGLE_API_KEY']
    search_engine_id = os.environ['GOOGLE_SEARCH_ENGINE_ID']
    
    response = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params={
            'key': api_key,
            'cx': search_engine_id,
            'q': query,
            'num': min(limit, 10)
        }
    )
    data = response.json()
    
    items = data.get('items', [])
    total = int(data.get('searchInformation', {}).get('totalResults', '0'))
    
    results = [
        {
            "title": item.get('title', ''),
            "link": item.get('link', ''),
            "snippet": item.get('snippet', '')
        }
        for item in items
    ]
    
    return {
        "results": results,
        "total": total,
        "query": query
    }

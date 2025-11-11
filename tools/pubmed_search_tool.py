#!/usr/bin/env python3
"""PubMed and PubMed Central search via Elasticsearch"""

from elasticsearch import Elasticsearch

PUBMED_SEARCH_SPEC = {
    "toolSpec": {
        "name": "pubmed_search",
        "description": "Search PubMed and PubMed Central databases for scientific articles",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"},
                    "limit": {"type": "integer", "description": "Max results (default: 10)"}
                },
                "required": ["query"]
            }
        }
    }
}

def pubmed_search_tool(params, task_id):
    """Search PubMed and PMC"""
    query = params['query']
    limit = params.get('limit', 10)
    
    hosts = ["http://host.docker.internal:9200", "http://localhost:9200"]
    es = Elasticsearch(hosts, request_timeout=30)
    
    search_query = {"query_string": {"query": query}}
    search_results = es.search(index="pubmed,pubmedcentral", body={"query": search_query, "size": limit})
    
    hits = search_results["hits"]["hits"]
    total = search_results["hits"]["total"]["value"] if isinstance(search_results["hits"]["total"], dict) else search_results["hits"]["total"]
    
    results = []
    for hit in hits:
        source = hit["_source"]
        if "pmid" in source:
            results.append({
                "id": f"PMID:{source.get('pmid', 'unknown')}",
                "title": source.get("article_title", "No title"),
                "abstract": source.get("abstract", "No abstract"),
                "source": "PubMed"
            })
        elif "pmcid" in source:
            content = source.get("content", "")
            results.append({
                "id": str(source.get('pmcid', 'unknown')),
                "title": "Full text article",
                "abstract": content[:500] + "..." if len(content) > 500 else content,
                "source": "PubMed Central"
            })
        else:
            results.append({
                "id": hit["_id"],
                "title": "Unknown",
                "abstract": str(source),
                "source": hit["_index"]
            })
    
    return {
        "results": results,
        "total": total,
        "query": query
    }

#!/usr/bin/env python3

"""Tool registry - imports all tools and builds TOOLS dict and TOOL_SCHEMAS list"""

from .bash_tool import bash_tool, BASH_SPEC
from .spawn_task_tool import spawn_task_tool, SPAWN_TASK_SPEC
from .query_task_tool import query_task_tool, QUERY_TASK_SPEC
from .think_tool import think_tool, THINK_SPEC
from .google_search_tool import google_search_tool, GOOGLE_SEARCH_SPEC
from .pubmed_search_tool import pubmed_search_tool, PUBMED_SEARCH_SPEC
from .chembl_search_tool import chembl_search_tool, CHEMBL_SEARCH_SPEC

TOOLS = {
    'bash': bash_tool,
    'spawn_task': spawn_task_tool,
    'query_task': query_task_tool,
    'think': think_tool,
    'google_search': google_search_tool,
    'pubmed_search': pubmed_search_tool,
    'chembl_search': chembl_search_tool
}

TOOL_SCHEMAS = [
    BASH_SPEC,
    SPAWN_TASK_SPEC,
    QUERY_TASK_SPEC,
    THINK_SPEC,
    GOOGLE_SEARCH_SPEC,
    PUBMED_SEARCH_SPEC,
    CHEMBL_SEARCH_SPEC
]

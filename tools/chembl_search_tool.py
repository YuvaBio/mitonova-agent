#!/usr/bin/env python3
"""ChEMBL database search tool"""

import psycopg2

CHEMBL_SEARCH_SPEC = {
    "toolSpec": {
        "name": "chembl_search",
        "description": "Search ChEMBL database for compounds, targets, or other entities",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (compound name, ChEMBL ID, etc.)"},
                    "entity_type": {"type": "string", "description": "Entity type: compound, target, drug (default: compound)"},
                    "limit": {"type": "integer", "description": "Maximum results (default: 10)"}
                },
                "required": ["query"]
            }
        }
    }
}

def chembl_search_tool(params, task_id):
    """Search ChEMBL database"""
    query = params['query']
    entity_type = params.get('entity_type', 'compound')
    limit = params.get('limit', 10)
    
    conn = psycopg2.connect(host='localhost', dbname='chembl_35', user='agent', password='agent', port=5432)
    cursor = conn.cursor()
    
    search_term = f"%{query}%"
    
    if entity_type == 'compound':
        cursor.execute("""
            SELECT md.chembl_id, md.pref_name, cs.canonical_smiles,
                   cp.full_mwt, cp.alogp, cp.hba, cp.hbd, cp.psa
            FROM molecule_dictionary md
            LEFT JOIN compound_structures cs ON md.molregno = cs.molregno
            LEFT JOIN compound_properties cp ON md.molregno = cp.molregno
            LEFT JOIN molecule_synonyms ms ON md.molregno = ms.molregno
            WHERE md.chembl_id ILIKE %s
            OR md.pref_name ILIKE %s
            OR ms.synonyms ILIKE %s
            GROUP BY md.chembl_id, md.pref_name, cs.canonical_smiles,
                     cp.full_mwt, cp.alogp, cp.hba, cp.hbd, cp.psa
            LIMIT %s
        """, (search_term, search_term, search_term, limit))
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        cursor.execute("""
            SELECT COUNT(DISTINCT md.molregno)
            FROM molecule_dictionary md
            LEFT JOIN molecule_synonyms ms ON md.molregno = ms.molregno
            WHERE md.chembl_id ILIKE %s
            OR md.pref_name ILIKE %s
            OR ms.synonyms ILIKE %s
        """, (search_term, search_term, search_term))
        total = cursor.fetchone()[0]
        
    elif entity_type == 'target':
        cursor.execute("""
            SELECT td.chembl_id, td.pref_name, td.target_type, td.organism
            FROM target_dictionary td
            WHERE td.chembl_id ILIKE %s
            OR td.pref_name ILIKE %s
            LIMIT %s
        """, (search_term, search_term, limit))
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        cursor.execute("""
            SELECT COUNT(*)
            FROM target_dictionary td
            WHERE td.chembl_id ILIKE %s
            OR td.pref_name ILIKE %s
        """, (search_term, search_term))
        total = cursor.fetchone()[0]
        
    elif entity_type == 'drug':
        cursor.execute("""
            SELECT md.chembl_id, md.pref_name, cs.canonical_smiles,
                   md.max_phase, md.first_approval, md.oral
            FROM molecule_dictionary md
            LEFT JOIN compound_structures cs ON md.molregno = cs.molregno
            LEFT JOIN molecule_synonyms ms ON md.molregno = ms.molregno
            WHERE (md.chembl_id ILIKE %s
            OR md.pref_name ILIKE %s
            OR ms.synonyms ILIKE %s)
            AND md.max_phase >= 1
            GROUP BY md.chembl_id, md.pref_name, cs.canonical_smiles,
                     md.max_phase, md.first_approval, md.oral
            ORDER BY md.max_phase DESC
            LIMIT %s
        """, (search_term, search_term, search_term, limit))
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        cursor.execute("""
            SELECT COUNT(DISTINCT md.molregno)
            FROM molecule_dictionary md
            LEFT JOIN molecule_synonyms ms ON md.molregno = ms.molregno
            WHERE (md.chembl_id ILIKE %s
            OR md.pref_name ILIKE %s
            OR ms.synonyms ILIKE %s)
            AND md.max_phase >= 1
        """, (search_term, search_term, search_term))
        total = cursor.fetchone()[0]
    
    cursor.close()
    conn.close()
    
    results = [dict(zip(columns, row)) for row in rows]
    
    return {
        "results": results,
        "total": total,
        "query": query
    }

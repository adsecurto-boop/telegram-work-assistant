"""
MCP Schema Adapter for Gemini Function Calling.
Translates MCP JSON Schemas into Gemini-compatible Function Declarations.
"""
from typing import Any, Dict, List, Optional
from mcp_registry import ToolDescriptor


def sanitize_schema_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sanitize JSON Schema dictionary for Gemini tool calling.
    Removes unsupported keys like $schema, title, definitions, and normalizes types.
    """
    if not isinstance(schema, dict):
        return {"type": "STRING"}

    cleaned = {}
    
    # Copy allowed fields
    for key, val in schema.items():
        if key in ["$schema", "title", "$id", "additionalProperties", "default", "examples"]:
            continue
            
        if key == "type":
            if isinstance(val, str):
                cleaned["type"] = val.upper()
            elif isinstance(val, list):
                # Handle union types by taking the first non-null type
                types = [t.upper() for t in val if t != "null"]
                cleaned["type"] = types[0] if types else "STRING"
            else:
                cleaned["type"] = "STRING"
        elif key == "properties" and isinstance(val, dict):
            cleaned["properties"] = {
                prop_k: sanitize_schema_for_gemini(prop_v)
                for prop_k, prop_v in val.items()
            }
        elif key == "items" and isinstance(val, dict):
            cleaned["items"] = sanitize_schema_for_gemini(val)
        elif key == "required" and isinstance(val, list):
            cleaned["required"] = [str(r) for r in val]
        elif key == "enum" and isinstance(val, list):
            cleaned["enum"] = [str(e) for e in val]
        elif key in ["description", "format"]:
            cleaned[key] = str(val)

    if "type" not in cleaned:
        cleaned["type"] = "OBJECT"

    return cleaned


def tool_descriptor_to_gemini_declaration(tool: ToolDescriptor) -> Dict[str, Any]:
    """
    Convert a ToolDescriptor into a Gemini function declaration structure.
    """
    parameters = sanitize_schema_for_gemini(tool.input_schema)
    return {
        "name": tool.gemini_name,
        "description": tool.description or f"Tool {tool.canonical_id}",
        "parameters": parameters
    }


def convert_tools_for_gemini_config(tools: List[ToolDescriptor]) -> List[Dict[str, Any]]:
    """
    Convert a list of ToolDescriptors into a list of Gemini function declaration dicts.
    """
    return [tool_descriptor_to_gemini_declaration(t) for t in tools if t.enabled]

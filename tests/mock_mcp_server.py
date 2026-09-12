"""
Mock stdio JSON-RPC MCP server for unit/integration testing.
"""
import sys
import json

def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except Exception:
            continue

        method = req.get("method")
        msg_id = req.get("id")

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "MockMCPServer", "version": "1.0.0"}
                }
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "notifications/initialized":
            continue

        elif method == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        {
                            "name": "search_issues",
                            "description": "Search GitHub issues in repository",
                            "annotations": {"readOnlyHint": True, "openWorldHint": True},
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"}
                                },
                                "required": ["query"]
                            }
                        },
                        {
                            "name": "get_issue",
                            "description": "Get detailed GitHub issue information",
                            "annotations": {"readOnlyHint": True, "openWorldHint": True},
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "issue_number": {"type": "integer"}
                                },
                                "required": ["issue_number"]
                            }
                        },
                        {
                            "name": "create_issue",
                            "description": "Create a new GitHub issue",
                            "annotations": {"readOnlyHint": False, "idempotentHint": False},
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "body": {"type": "string"}
                                },
                                "required": ["title"]
                            }
                        }
                    ]
                }
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "tools/call":
            params = req.get("params", {})
            t_name = params.get("name")
            args = params.get("arguments", {})

            if t_name == "search_issues":
                res_content = [{"type": "text", "text": f"Found issue #61: Wayland screenshot issue matching '{args.get('query')}'"}]
                structured = {"items": [{"repository": "owner/telegram-work-assistant", "number": 61,
                                          "url": "https://github.com/owner/telegram-work-assistant/issues/61",
                                          "title": "Wayland screenshot issue", "state": "open",
                                          "user": {"login": "user_dev"}}]}
            elif t_name == "get_issue":
                num = args.get("issue_number", 61)
                res_content = [{"type": "text", "text": f"Issue #{num}: Blank screenshot under Wayland on Ubuntu 24. Created by user_dev."}]
                structured = {"repository": args.get("repository", "owner/telegram-work-assistant"),
                              "number": num, "url": f"https://github.com/owner/telegram-work-assistant/issues/{num}",
                              "title": "Blank screenshot under Wayland", "state": args.get("state", "open"),
                              "user": {"login": "user_dev"}, "labels": ["bug", "ubuntu"]}
            elif t_name == "create_issue":
                res_content = [{"type": "text", "text": f"Created GitHub issue #{99}: {args.get('title')}"}]
                structured = {"repository": args.get("repository", "owner/telegram-work-assistant"),
                              "number": 99, "state": "open", "title": args.get("title")}
            else:
                res_content = [{"type": "text", "text": f"Executed tool {t_name} with args {args}"}]
                structured = {"tool": t_name, "arguments": args}

            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": res_content,
                    "structuredContent": structured,
                    "isError": False
                }
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif msg_id is not None:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "Method not found"}
            }) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()

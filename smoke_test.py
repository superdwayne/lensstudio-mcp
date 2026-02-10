#!/usr/bin/env python3
"""Smoke test for the lens-mcp HTTP proxy server.

Starts a fake Lens Studio MCP HTTP server that mimics the built-in
MCP endpoint at localhost:50050/mcp, then exercises the LensBridge class:
auth flow, tools/list, tools/call, 401 retry, and dynamic tool registration.
"""

import asyncio
import json
import os
import sys
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

# ---------------------------------------------------------------------------
# Fake Lens Studio MCP HTTP server
# ---------------------------------------------------------------------------

FAKE_PORT = 50099  # Avoid conflicts with real Lens Studio
FAKE_TOKEN = "test-token-abc123"

# Fake tools that mimic what Lens Studio returns
FAKE_TOOLS = [
    {
        "name": "GetLensStudioSceneGraph",
        "description": "Get the scene hierarchy of the current project",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "CreateLensStudioSceneObject",
        "description": "Create a new scene object",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Object name"},
                "preset": {"type": "string", "description": "Preset name"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "GetLensStudioSceneObjectByName",
        "description": "Gets scene objects by name",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "DeleteLensStudioSceneObject",
        "description": "Delete a scene object by name or ID",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "CreateLensStudioAsset",
        "description": "Create a new asset",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "assetType": {"type": "string"},
            },
            "required": ["name", "assetType"],
        },
    },
    {
        "name": "SetLensStudioProperty",
        "description": "Set a property on a scene object or component",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "propertyName": {"type": "string"},
                "value": {},
            },
            "required": ["target", "propertyName", "value"],
        },
    },
    {
        "name": "QueryLensStudioKnowledgeBase",
        "description": "Search Lens Studio documentation",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
]

# Track whether auth should be invalidated for retry testing
_invalidate_next_token = False


class FakeLensHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests mimicking Lens Studio's MCP server."""

    def log_message(self, format, *args):
        pass  # Silence request logs

    def do_POST(self):
        global _invalidate_next_token

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
            return

        # Auth endpoint
        if self.path == "/mcp/request-auth":
            self._respond(200, {"token": FAKE_TOKEN})
            return

        # MCP endpoint — require auth
        if self.path == "/mcp":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != FAKE_TOKEN:
                # Simulate 401 for bad/missing token
                if _invalidate_next_token:
                    _invalidate_next_token = False
                self._respond(401, {"error": "Unauthorized"})
                return

            # Check if we should invalidate this token for the NEXT request
            if _invalidate_next_token:
                _invalidate_next_token = False
                self._respond(401, {"error": "Token expired"})
                return

            method = data.get("method", "")
            rid = data.get("id")
            params = data.get("params", {})

            if method == "tools/list":
                self._respond(200, {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"tools": FAKE_TOOLS},
                })
            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments", {})

                if tool_name == "GetLensStudioSceneGraph":
                    self._respond(200, {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [
                                {"type": "text", "text": json.dumps({
                                    "objects": [
                                        {"name": "Camera", "type": "Camera"},
                                        {"name": "Light", "type": "DirectionalLight"},
                                    ]
                                })}
                            ]
                        },
                    })
                elif tool_name == "CreateLensStudioSceneObject":
                    obj_name = tool_args.get("name", "")
                    if not obj_name:
                        self._respond(200, {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "result": {
                                "content": [{"type": "text", "text": "Error: name is required"}],
                                "isError": True,
                            },
                        })
                    else:
                        self._respond(200, {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "result": {
                                "content": [{"type": "text", "text": f"Created object: {obj_name}"}]
                            },
                        })
                elif tool_name == "GetLensStudioSceneObjectByName":
                    obj_name = tool_args.get("name", "Unknown")
                    self._respond(200, {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps({"objectUUID": "fake-uuid-" + obj_name, "name": obj_name})}]
                        },
                    })
                elif tool_name == "DeleteLensStudioSceneObject":
                    self._respond(200, {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": f"Deleted: {tool_args.get('objectUUID', '')}"}]
                        },
                    })
                elif tool_name == "CreateLensStudioAsset":
                    self._respond(200, {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": f"Created asset: {tool_args.get('name', '')} ({tool_args.get('assetType', '')})"}]
                        },
                    })
                elif tool_name == "SetLensStudioProperty":
                    self._respond(200, {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": f"Set {tool_args.get('propertyPath', '')} on {tool_args.get('objectUUID', '')}"}]
                        },
                    })
                elif tool_name == "QueryLensStudioKnowledgeBase":
                    self._respond(200, {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": f"Results for: {tool_args.get('query', '')}"}]
                        },
                    })
                else:
                    self._respond(200, {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                    })
            else:
                self._respond(200, {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })
            return

        self._respond(404, {"error": "Not found"})

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        payload = json.dumps(body).encode()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def run_tests():
    # Start fake HTTP server in a background thread
    server = HTTPServer(("127.0.0.1", FAKE_PORT), FakeLensHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Create a temp token file so we don't pollute the real one
    token_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="lens_mcp_token_"
    )
    token_file.close()

    # Patch environment before importing server module
    os.environ["LENS_MCP_PORT"] = str(FAKE_PORT)
    os.environ["LENS_MCP_TOKEN_FILE"] = token_file.name

    # Import server module with fake FastMCP
    import types
    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")

    class FakeApp:
        def __init__(self, name): self.name = name
        def tool(self, *a, **kw):
            def d(fn): return fn
            return d
        def run(self, **kw): pass

    fastmcp_mod.FastMCP = FakeApp
    server_mod.fastmcp = fastmcp_mod
    mcp_mod.server = server_mod
    sys.modules["mcp"] = mcp_mod
    sys.modules["mcp.server"] = server_mod
    sys.modules["mcp.server.fastmcp"] = fastmcp_mod

    from importlib.machinery import SourceFileLoader
    from importlib.util import spec_from_loader, module_from_spec

    loader = SourceFileLoader(
        "lens_mcp_server",
        os.path.join(os.path.dirname(__file__) or ".", "server.py"),
    )
    spec = spec_from_loader(loader.name, loader)
    mod = module_from_spec(spec)
    loader.exec_module(mod)

    bridge = mod.bridge
    passed = 0
    failed = 0

    def check(name, condition):
        nonlocal passed, failed
        if condition:
            print(f"  PASS  {name}")
            passed += 1
        else:
            print(f"  FAIL  {name}")
            failed += 1

    try:
        # ============================================================
        print("\n=== Authentication Tests ===")

        check("no token initially", bridge.token is None)

        # ensure_auth should request a new token
        await bridge.ensure_auth()
        check("ensure_auth obtains token", bridge.token == FAKE_TOKEN)

        # Token should be saved to file
        saved = json.loads(open(token_file.name).read())
        check("token saved to file", saved.get("authToken") == FAKE_TOKEN)

        # Status should show authenticated
        st = bridge.status()
        check("status shows authenticated", st["authenticated"] is True)
        check("status shows correct port", st["port"] == FAKE_PORT)

        # ============================================================
        print("\n=== Token Persistence Tests ===")

        # Create a new bridge and verify it loads the saved token
        bridge2 = mod.LensBridge(port=FAKE_PORT)
        check("new bridge has no token", bridge2.token is None)
        loaded = bridge2._load_token()
        check("load_token finds saved token", loaded is True)
        check("loaded token matches", bridge2.token == FAKE_TOKEN)
        await bridge2.close()

        # ============================================================
        print("\n=== tools/list Tests ===")

        tools = await bridge.list_tools()
        check("list_tools returns tools", len(tools) == 7)
        tool_names = {t["name"] for t in tools}
        check("GetSceneHierarchy in tools", "GetLensStudioSceneGraph" in tool_names)
        check("CreateLensStudioSceneObject in tools", "CreateLensStudioSceneObject" in tool_names)
        check("DeleteSceneObject in tools", "DeleteLensStudioSceneObject" in tool_names)

        # Tools cache should be populated
        check("tools cache populated", bridge._tools_cache is not None)
        check("tools cache count", len(bridge._tools_cache) == 7)

        # ============================================================
        print("\n=== tools/call Tests ===")

        # GetSceneHierarchy
        result = await bridge.call_tool("GetLensStudioSceneGraph", {})
        content = result.get("content", [])
        check("GetSceneHierarchy returns content", len(content) > 0)
        hierarchy = json.loads(content[0]["text"])
        check("hierarchy has objects", len(hierarchy.get("objects", [])) == 2)

        # CreateLensStudioSceneObject — success
        result = await bridge.call_tool(
            "CreateLensStudioSceneObject",
            {"name": "TestSphere", "preset": "SphereMeshObjectPreset"},
        )
        content = result.get("content", [])
        check("create object succeeds", "TestSphere" in content[0].get("text", ""))

        # CreateLensStudioSceneObject — validation error
        result = await bridge.call_tool(
            "CreateLensStudioSceneObject",
            {"name": ""},
        )
        check("empty name returns error", result.get("isError") is True)

        # DeleteLensStudioSceneObject (raw call with UUID)
        result = await bridge.call_tool("DeleteLensStudioSceneObject", {"objectUUID": "fake-uuid-TestSphere"})
        content = result.get("content", [])
        check("delete object succeeds", "fake-uuid-TestSphere" in content[0].get("text", ""))

        # Unknown tool
        result = await bridge.call_tool("NonexistentTool", {})
        check("unknown tool returns error", "error" in result)

        # ============================================================
        print("\n=== Scene Primitive Tests ===")

        # get_scene
        get_scene = mod.get_scene
        result = await get_scene()
        content = result.get("content", [])
        check("get_scene returns content", len(content) > 0)
        scene = json.loads(content[0]["text"])
        check("get_scene has objects", len(scene.get("objects", [])) == 2)

        # add_primitive — default cube
        add_primitive = mod.add_primitive
        result = await add_primitive()
        content = result.get("content", [])
        check("add_primitive default cube", "Cube" in content[0].get("text", ""))

        # add_primitive — sphere with custom name
        result = await add_primitive("sphere", "TestBall")
        content = result.get("content", [])
        check("add_primitive sphere named", "TestBall" in content[0].get("text", ""))

        # add_primitive — unknown shape
        result = await add_primitive("hexagon")
        check("add_primitive bad shape returns error", "error" in result)

        # create_object
        create_object = mod.create_object
        result = await create_object("MySphere", "SphereMeshObjectPreset")
        content = result.get("content", [])
        check("create_object succeeds", "MySphere" in content[0].get("text", ""))

        # create_object without preset
        result = await create_object("PlainObject")
        content = result.get("content", [])
        check("create_object without preset", "PlainObject" in content[0].get("text", ""))

        # delete_object (resolves name → UUID, then deletes by UUID)
        delete_object = mod.delete_object
        result = await delete_object("MySphere")
        content = result.get("content", [])
        check("delete_object succeeds", "fake-uuid-MySphere" in content[0].get("text", ""))

        # create_asset
        create_asset = mod.create_asset
        result = await create_asset("MyMaterial", "Material")
        content = result.get("content", [])
        check("create_asset succeeds", "MyMaterial" in content[0].get("text", ""))

        # set_property
        set_property = mod.set_property
        result = await set_property("fake-uuid-1234", "enabled", True, "boolean")
        content = result.get("content", [])
        check("set_property succeeds", "enabled" in content[0].get("text", ""))

        # query_knowledge_base
        query_knowledge_base = mod.query_knowledge_base
        result = await query_knowledge_base("how to add face tracking")
        content = result.get("content", [])
        check("query_knowledge_base succeeds", "face tracking" in content[0].get("text", ""))

        # ============================================================
        print("\n=== 401 Retry Tests ===")

        # Invalidate the token to simulate expiry
        bridge.token = "expired-token"
        # The request should fail with 401, then re-auth and succeed
        tools = await bridge.list_tools()
        check("401 retry re-authenticates", bridge.token == FAKE_TOKEN)
        check("401 retry succeeds", len(tools) == 7)

        # ============================================================
        print("\n=== Concurrent Request Tests ===")

        tasks = [
            bridge.call_tool("GetLensStudioSceneGraph", {})
            for _ in range(5)
        ]
        results = await asyncio.gather(*tasks)
        all_ok = all(
            "content" in r and len(r["content"]) > 0
            for r in results
        )
        check("5 concurrent requests all succeed", all_ok)

        # ============================================================
        print("\n=== Status After Operations ===")

        st = bridge.status()
        check("still authenticated", st["authenticated"] is True)
        check("cached tools count", st["cached_tools"] == 7)

    finally:
        await bridge.close()
        server.shutdown()
        # Clean up temp file
        try:
            os.unlink(token_file.name)
        except OSError:
            pass

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*40}\n")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    code = asyncio.run(run_tests())
    sys.exit(code)

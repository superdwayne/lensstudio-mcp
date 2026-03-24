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
FAKE_SKETCHFAB_PORT = 50098
FAKE_SKETCHFAB_TOKEN = "sketchfab-test-token"

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
                elif tool_name == "CreateSceneObjectFromPresetTool":
                    obj_name = tool_args.get("name", "Unnamed")
                    preset = tool_args.get("preset", "")
                    self._respond(200, {
                        "jsonrpc": "2.0", "id": rid,
                        "result": {"content": [{"type": "text", "text": json.dumps({"objectUUID": f"fake-uuid-{obj_name}", "name": obj_name, "preset": preset})}]},
                    })
                elif tool_name == "SetLensStudioParent":
                    self._respond(200, {
                        "jsonrpc": "2.0", "id": rid,
                        "result": {"content": [{"type": "text", "text": json.dumps({"message": f"Parented {tool_args.get('objectUUID', '')} to {tool_args.get('parentUUID', '')}"})}]},
                    })
                elif tool_name == "CreateAssetFromPresetTool":
                    asset_name = tool_args.get("name", "Unnamed")
                    self._respond(200, {
                        "jsonrpc": "2.0", "id": rid,
                        "result": {"content": [{"type": "text", "text": json.dumps({"assetUUID": f"fake-asset-uuid-{asset_name}", "name": asset_name})}]},
                    })
                elif tool_name == "GetLensStudioSceneObjectById":
                    obj_uuid = tool_args.get("objectUUID", "")
                    self._respond(200, {
                        "jsonrpc": "2.0", "id": rid,
                        "result": {"content": [{"type": "text", "text": json.dumps({"object": {"id": obj_uuid, "name": "FakeObj", "components": [{"type": "RenderMeshVisual", "id": f"comp-{obj_uuid}", "properties": {"id": f"comp-{obj_uuid}"}}]}})}]},
                    })
                elif tool_name == "GetLensStudioAssetsByName":
                    asset_name = tool_args.get("name", "Unknown")
                    self._respond(200, {
                        "jsonrpc": "2.0", "id": rid,
                        "result": {"content": [{"type": "text", "text": json.dumps({"assets": [{"id": f"fake-asset-{asset_name}", "name": asset_name}]})}]},
                    })
                elif tool_name in ("SearchLensStudioMusicLibrary", "InstallLicensedMusic"):
                    self._respond(200, {
                        "jsonrpc": "2.0", "id": rid,
                        "result": {"content": [{"type": "text", "text": json.dumps({"results": []})}]},
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
# Fake Sketchfab API server
# ---------------------------------------------------------------------------

FAKE_SKETCHFAB_RESULTS = [
    {
        "name": "Golden Crown",
        "uid": "abc123-crown",
        "thumbnails": {"images": [{"url": "https://example.com/thumb.jpg"}]},
        "user": {"displayName": "TestUser"},
        "license": {"slug": "cc-by-4.0"},
        "vertexCount": 5000,
        "isDownloadable": True,
    },
    {
        "name": "Silver Crown",
        "uid": "def456-crown",
        "thumbnails": {"images": [{"url": "https://example.com/thumb2.jpg"}]},
        "user": {"displayName": "AnotherUser"},
        "license": {"slug": "cc-by-sa-4.0"},
        "vertexCount": 3200,
        "isDownloadable": True,
    },
]


class FakeSketchfabHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests mimicking Sketchfab's Data API v3."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        # Fake download endpoint — no auth required (simulates signed URL)
        if self.path.startswith("/fake-download/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            fake_glb = b"glTF\x02\x00\x00\x00" + b"\x00" * 16
            self.send_header("Content-Length", str(len(fake_glb)))
            self.end_headers()
            self.wfile.write(fake_glb)
            return

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Token ") or auth[6:] != FAKE_SKETCHFAB_TOKEN:
            self._respond(403, {"detail": "Invalid token"})
            return

        if self.path.startswith("/v3/search"):
            self._respond(200, {"results": FAKE_SKETCHFAB_RESULTS})
        elif "/download" in self.path:
            # Return a fake download URL pointing back at our server
            self._respond(200, {
                "gltf": {
                    "url": f"http://127.0.0.1:{FAKE_SKETCHFAB_PORT}/fake-download/model.glb",
                    "size": 1024,
                }
            })
        else:
            self._respond(404, {"detail": "Not found"})

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

    # Start fake Sketchfab API server
    sketchfab_server = HTTPServer(("127.0.0.1", FAKE_SKETCHFAB_PORT), FakeSketchfabHandler)
    sketchfab_thread = Thread(target=sketchfab_server.serve_forever, daemon=True)
    sketchfab_thread.start()

    # Create a temp token file so we don't pollute the real one
    token_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="lens_mcp_token_"
    )
    token_file.close()

    # Create a temp model cache directory
    model_cache_dir = tempfile.mkdtemp(prefix="lens_mcp_models_")

    # Patch environment before importing server module
    os.environ["LENS_MCP_PORT"] = str(FAKE_PORT)
    os.environ["LENS_MCP_TOKEN_FILE"] = token_file.name
    os.environ["SKETCHFAB_API_TOKEN"] = FAKE_SKETCHFAB_TOKEN
    os.environ["LENS_MCP_MODEL_CACHE"] = model_cache_dir

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

    # Patch Sketchfab config to use fake server
    mod.SKETCHFAB_BASE_URL = f"http://127.0.0.1:{FAKE_SKETCHFAB_PORT}/v3"
    mod.sketchfab = mod.SketchfabClient(token=FAKE_SKETCHFAB_TOKEN)

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

        # ============================================================
        # PHASE 1: Hand Tracking Tests
        # ============================================================
        print("\n=== Phase 1: Hand Tracking Tests ===")

        result = mod.get_hand_landmarks()
        check("hand landmarks returns all", "landmarks" in result)
        check("hand landmarks count >= 21", len(result.get("landmarks", {})) >= 21)

        result = mod.get_hand_landmarks("thumb_tip")
        check("hand landmark specific", "thumb_tip" in result)

        result = mod.get_hand_landmarks("index-tip")
        check("hand landmark dashes normalized", "index_tip" in result)

        result = mod.get_hand_landmarks("nonexistent")
        check("hand landmark invalid", "error" in result)

        result = await mod.create_hand_anchor()
        check("create_hand_anchor succeeds", "message" in result)
        check("hand anchor has uuid", result.get("uuid") is not None)

        result = await mod.add_hand_element("Hand Anchor", "index_tip")
        check("add_hand_element succeeds", "message" in result)
        check("hand element position", result.get("position", {}).get("y") == 9)

        result = await mod.add_hand_element("Hand Anchor", "nonexistent_joint")
        check("add_hand_element invalid landmark", "error" in result)

        result = await mod.add_hand_element("Hand Anchor", "index_tip", shape="hexagon")
        check("add_hand_element invalid shape", "error" in result)

        result = await mod.create_gesture_trigger("pinch", "print('pinched!')")
        check("gesture trigger succeeds", "message" in result)
        check("gesture trigger enum", "HandGesture.Pinch" in result.get("gesture_enum", ""))

        result = await mod.create_gesture_trigger("invalid_gesture", "nope")
        check("gesture trigger invalid", "error" in result)

        # ============================================================
        # PHASE 2: Animation System Tests
        # ============================================================
        print("\n=== Phase 2: Animation System Tests ===")

        check("easing functions defined", len(mod.EASING_FUNCTIONS) >= 8)
        check("animatable properties defined", len(mod.ANIMATABLE_PROPERTIES) >= 10)

        result = await mod.create_tween("TestBall", "position_y", 0, 10, 1.0)
        check("create_tween succeeds", "message" in result)

        result = await mod.create_tween("TestBall", "scale", {"x": 1, "y": 1, "z": 1}, {"x": 2, "y": 2, "z": 2}, 0.5, easing="bounce")
        check("create_tween with easing", "message" in result)

        result = await mod.create_tween("TestBall", "nonexistent_prop", 0, 1, 1.0)
        check("create_tween invalid property", "error" in result)

        result = await mod.create_tween("TestBall", "position_y", 0, 1, 1.0, easing="invalid")
        check("create_tween invalid easing", "error" in result)

        seq = [
            {"object": "Ball", "property": "position_y", "to": 10, "duration": 1.0},
            {"object": "Ball", "property": "opacity", "to": 0, "duration": 0.5},
        ]
        result = await mod.create_animation_sequence(seq)
        check("animation sequence succeeds", "message" in result)

        result = await mod.create_animation_sequence([])
        check("animation sequence empty", "error" in result)

        result = await mod.animate_on_trigger("TestBall", "tap", "position_y", 10)
        check("animate_on_trigger succeeds", "message" in result)

        result = await mod.animate_on_trigger("TestBall", "nonexistent", "position_y", 10)
        check("animate_on_trigger invalid trigger", "error" in result)

        result = await mod.create_looping_animation("TestBall", "scale_x", [1, 2], 1.0)
        check("looping animation succeeds", "message" in result)

        result = await mod.create_looping_animation("TestBall", "scale_x", [1], 1.0)
        check("looping animation too few values", "error" in result)

        # ============================================================
        # PHASE 3: Particle System Tests
        # ============================================================
        print("\n=== Phase 3: Particle System Tests ===")

        check("particle presets defined", len(mod.PARTICLE_PRESETS) >= 7)
        check("sparkles preset exists", "sparkles" in mod.PARTICLE_PRESETS)

        result = await mod.create_particle_system("My Sparkles")
        check("create_particle_system default", "message" in result)
        check("particle has uuid", "uuid" in result)
        check("default preset is sparkles", result.get("preset") == "sparkles")

        result = await mod.create_particle_system("My Fire", preset="fire")
        check("create_particle_system fire", "message" in result)

        result = await mod.create_particle_system("Bad", preset="nonexistent")
        check("create_particle_system invalid", "error" in result)

        result = await mod.configure_particles("My Sparkles", emission_rate=100, gravity=-2.0)
        check("configure_particles succeeds", "message" in result)

        result = await mod.configure_particles("My Sparkles")
        check("configure_particles no props", "error" in result)

        result = await mod.attach_particles("My Sparkles", "TestBall")
        check("attach_particles succeeds", "message" in result)

        result = await mod.attach_particles("My Sparkles", "TestBall", offset={"x": 0, "y": 1, "z": 0})
        check("attach_particles with offset", "message" in result)

        result = await mod.create_particle_trail("TestBall", preset="hearts")
        check("create_particle_trail succeeds", "message" in result)

        result = await mod.create_particle_trail("TestBall", preset="invalid")
        check("create_particle_trail invalid", "error" in result)

        # ============================================================
        # PHASE 4: Body & World Tracking Tests
        # ============================================================
        print("\n=== Phase 4: Body & World Tracking Tests ===")

        result = mod.get_body_joints()
        check("body joints returns all", "joints" in result)
        check("body joints count >= 14", len(result.get("joints", {})) >= 14)

        result = mod.get_body_joints("left_shoulder")
        check("body joint specific", "left_shoulder" in result)

        result = mod.get_body_joints("nonexistent")
        check("body joint invalid", "error" in result)

        result = await mod.create_body_anchor()
        check("create_body_anchor succeeds", "message" in result)

        result = await mod.add_body_element("Body Anchor", "left_shoulder")
        check("add_body_element succeeds", "message" in result)

        result = await mod.add_body_element("Body Anchor", "nonexistent_joint")
        check("add_body_element invalid joint", "error" in result)

        result = await mod.create_world_tracker()
        check("create_world_tracker succeeds", "message" in result)

        result = await mod.create_world_tracker(mode="invalid")
        check("create_world_tracker invalid mode", "error" in result)

        result = await mod.place_in_world("TestBall", {"x": 0, "y": 1, "z": -5})
        check("place_in_world succeeds", "message" in result)

        result = await mod.enable_object_tracking("cat")
        check("enable_object_tracking succeeds", "message" in result)

        result = await mod.enable_object_tracking("invalid")
        check("enable_object_tracking invalid", "error" in result)

        # ============================================================
        # PHASE 5: Segmentation & Masking Tests
        # ============================================================
        print("\n=== Phase 5: Segmentation & Masking Tests ===")

        result = await mod.create_segmentation_mask("person")
        check("create_segmentation_mask person", "message" in result)

        result = await mod.create_segmentation_mask("hair")
        check("create_segmentation_mask hair", "message" in result)

        result = await mod.create_segmentation_mask("invalid")
        check("create_segmentation_mask invalid", "error" in result)

        result = await mod.apply_background_replacement(color="blue")
        check("apply_background_replacement succeeds", "message" in result)

        result = await mod.create_person_outline("white", 2.0)
        check("create_person_outline succeeds", "message" in result)

        result = await mod.apply_hair_color("red")
        check("apply_hair_color succeeds", "message" in result)

        result = await mod.apply_hair_color("invalid_color_xyz")
        check("apply_hair_color invalid", "error" in result)

        # ============================================================
        # PHASE 6: Scripting & Interactivity Tests
        # ============================================================
        print("\n=== Phase 6: Scripting & Interactivity Tests ===")

        result = await mod.create_script("MyScript")
        check("create_script basic", "message" in result)

        result = await mod.create_script("MyScript2", template="state_machine")
        check("create_script state_machine", "message" in result)

        result = await mod.create_script("MyScript3", template="invalid")
        check("create_script invalid template", "error" in result)

        result = await mod.create_script("Custom", code="print('hello');")
        check("create_script custom code", "message" in result)

        result = await mod.attach_script("TestBall", "MyScript")
        check("attach_script succeeds", "message" in result)

        result = await mod.create_tap_trigger("TestBall", "print('tapped!')")
        check("create_tap_trigger succeeds", "message" in result)

        result = await mod.create_state_machine(["idle", "active"], {"idle->active": {"trigger": "tap"}}, "idle")
        check("create_state_machine succeeds", "message" in result)

        result = await mod.create_state_machine(["idle"], {}, "idle")
        check("create_state_machine too few states", "error" in result)

        result = await mod.create_state_machine(["idle", "active"], {}, "missing")
        check("create_state_machine invalid initial", "error" in result)

        result = mod.generate_script_from_description("make something happen when I tap")
        check("generate_script_from_description", "script_code" in result)

        # ============================================================
        # PHASE 7: Audio & Voice Tests
        # ============================================================
        print("\n=== Phase 7: Audio & Voice Tests ===")

        result = await mod.add_audio("TestSound")
        check("add_audio succeeds", "message" in result)

        result = await mod.play_sound_on_trigger("TestSound", "tap")
        check("play_sound_on_trigger succeeds", "message" in result)

        result = await mod.play_sound_on_trigger("TestSound", "invalid")
        check("play_sound_on_trigger invalid trigger", "error" in result)

        result = await mod.apply_voice_effect("pitch_up")
        check("apply_voice_effect succeeds", "message" in result)

        result = await mod.apply_voice_effect("invalid_effect")
        check("apply_voice_effect invalid", "error" in result)

        result = await mod.sync_to_music_beat("TestBall", "scale")
        check("sync_to_music_beat succeeds", "message" in result)

        result = await mod.sync_to_music_beat("TestBall", "invalid_prop")
        check("sync_to_music_beat invalid prop", "error" in result)

        result = await mod.search_music_library("upbeat dance")
        check("search_music_library succeeds", "message" in result)

        result = await mod.install_licensed_music("track-123")
        check("install_licensed_music succeeds", isinstance(result, dict))

        # ============================================================
        # PHASE 8: Post-Processing Tests
        # ============================================================
        print("\n=== Phase 8: Post-Processing Tests ===")

        result = await mod.add_post_effect("bloom", 0.5)
        check("add_post_effect bloom", "message" in result)

        result = await mod.add_post_effect("invalid_effect")
        check("add_post_effect invalid", "error" in result)

        result = await mod.apply_color_grading("warm")
        check("apply_color_grading warm", "message" in result)

        result = await mod.apply_color_grading("invalid")
        check("apply_color_grading invalid", "error" in result)

        result = await mod.create_custom_lut("My LUT")
        check("create_custom_lut succeeds", "message" in result)

        result = await mod.add_screen_effect("light_leaks")
        check("add_screen_effect succeeds", "message" in result)

        result = await mod.add_screen_effect("effect", blend_mode="invalid")
        check("add_screen_effect invalid blend", "error" in result)

        # ============================================================
        # PHASE 9: Advanced Materials Tests
        # ============================================================
        print("\n=== Phase 9: Advanced Materials Tests ===")

        result = await mod.create_textured_material("TestMat", metallic=0.5, roughness=0.3)
        check("create_textured_material succeeds", "message" in result)

        result = await mod.create_textured_material("ColorMat", color="gold")
        check("create_textured_material with color", "message" in result)

        result = await mod.apply_texture("TestBall", "MyTexture")
        check("apply_texture succeeds", "message" in result)

        result = await mod.create_animated_texture("AnimTex", frame_count=10, fps=24)
        check("create_animated_texture succeeds", "message" in result)

        result = await mod.create_custom_shader("MyShader", "graph")
        check("create_custom_shader succeeds", "message" in result)

        result = await mod.create_custom_shader("Bad", "invalid_type")
        check("create_custom_shader invalid", "error" in result)

        result = await mod.set_material_property("TestMat", "passInfos.0.metallic", 0.8)
        check("set_material_property succeeds", "message" in result)

        # ============================================================
        # PHASE 10: Lens Recipes Tests
        # ============================================================
        print("\n=== Phase 10: Lens Recipes Tests ===")

        result = mod.list_lens_recipes()
        check("list_lens_recipes returns recipes", "recipes" in result)
        check("face_filter recipe exists", "face_filter" in result.get("recipes", {}))

        result = await mod.create_lens_from_recipe("face_filter")
        check("face_filter recipe succeeds", "message" in result)
        check("face_filter has components", result.get("components_created", 0) > 0)

        result = await mod.create_lens_from_recipe("hand_sparkles")
        check("hand_sparkles recipe succeeds", "message" in result)

        result = await mod.create_lens_from_recipe("background_replace", {"color": "green"})
        check("background_replace recipe succeeds", "message" in result)

        result = await mod.create_lens_from_recipe("world_object")
        check("world_object recipe succeeds", "message" in result)

        result = await mod.create_lens_from_recipe("beauty_filter")
        check("beauty_filter recipe succeeds", "message" in result)

        result = await mod.create_lens_from_recipe("invalid_recipe")
        check("invalid recipe returns error", "error" in result)

        result = await mod.create_face_filter_lens([
            {"landmark": "nose_tip", "shape": "sphere", "color": "red"},
            {"landmark": "left_ear_top", "shape": "cone", "color": "pink"},
        ])
        check("create_face_filter_lens succeeds", "message" in result)

        result = await mod.create_face_filter_lens([])
        check("create_face_filter_lens empty", "error" in result)

        result = await mod.create_try_on_lens("glasses")
        check("create_try_on_lens glasses", "message" in result)

        result = await mod.create_try_on_lens("invalid_type")
        check("create_try_on_lens invalid", "error" in result)

        result = await mod.create_game_lens("tap_targets")
        check("create_game_lens succeeds", "message" in result)

        result = await mod.create_game_lens("invalid_type")
        check("create_game_lens invalid", "error" in result)

        # ============================================================
        # 3D MODEL LIBRARY (Sketchfab) Tests
        # ============================================================
        print("\n=== 3D Model Library (Sketchfab) Tests ===")

        # search_3d_models — successful search
        result = await mod.search_3d_models("crown")
        check("search_3d_models returns results", "results" in result)
        check("search_3d_models message", "Found 2" in result.get("message", ""))
        results_list = result.get("results", [])
        check("search_3d_models count", len(results_list) == 2)
        check("search result has uid", results_list[0].get("uid") == "abc123-crown")
        check("search result has name", results_list[0].get("name") == "Golden Crown")
        check("search result has author", results_list[0].get("author") == "TestUser")
        check("search result has license", results_list[0].get("license") == "cc-by-4.0")
        check("search result has vertex_count", results_list[0].get("vertex_count") == 5000)
        check("search result has downloadable", results_list[0].get("downloadable") is True)
        check("search result has thumbnail", "example.com" in results_list[0].get("thumbnail", ""))

        # search_3d_models — missing API token
        original_token = mod.SKETCHFAB_API_TOKEN
        mod.SKETCHFAB_API_TOKEN = ""
        result = await mod.search_3d_models("crown")
        check("search without token returns error", "error" in result)
        check("search error mentions env var", "SKETCHFAB_API_TOKEN" in result.get("error", ""))
        mod.SKETCHFAB_API_TOKEN = original_token

        # import_3d_model — successful download and import
        # Mock ui_import_asset to avoid real macOS UI automation
        _original_ui_import = mod.ui_import_asset
        async def _mock_ui_import(path):
            return {"message": f"Imported {path}", "returncode": 0}
        mod.ui_import_asset = _mock_ui_import

        result = await mod.import_3d_model("abc123-crown", "TestCrown")
        check("import_3d_model returns message", "message" in result)
        check("import_3d_model has file_path", "TestCrown.glb" in result.get("file_path", ""))
        import pathlib
        check("import_3d_model file exists", pathlib.Path(result["file_path"]).exists())
        check("import_3d_model has import_result", "import_result" in result)

        mod.ui_import_asset = _original_ui_import

        # import_3d_model — missing UID
        result = await mod.import_3d_model("")
        check("import empty uid returns error", "error" in result)

        # import_3d_model — missing API token
        mod.SKETCHFAB_API_TOKEN = ""
        result = await mod.import_3d_model("abc123-crown")
        check("import without token returns error", "error" in result)
        mod.SKETCHFAB_API_TOKEN = original_token

    finally:
        await bridge.close()
        await mod.sketchfab.close()
        server.shutdown()
        sketchfab_server.shutdown()
        # Clean up temp files
        try:
            os.unlink(token_file.name)
        except OSError:
            pass
        try:
            import shutil
            shutil.rmtree(model_cache_dir, ignore_errors=True)
        except OSError:
            pass

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*40}\n")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    code = asyncio.run(run_tests())
    sys.exit(code)

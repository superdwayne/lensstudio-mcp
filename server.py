#!/usr/bin/env python3
"""Lens Studio MCP Server — proxies AI agent requests to Lens Studio's built-in MCP server.

Lens Studio 5.15+ ships an HTTP MCP server at localhost:50050/mcp.
This server acts as a thin proxy: it discovers tools from Lens Studio,
registers them dynamically, and forwards tool calls via HTTP POST.

Supports stdio, SSE, and Streamable HTTP transports so it works with
Claude Desktop, Claude Code, Cursor, VS Code Copilot, OpenAI agents, etc.
"""

import argparse
import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    try:
        from fastmcp import FastMCP
    except ImportError:
        print(
            "Error: MCP Python SDK required. Install: pip install 'mcp[cli]'",
            file=sys.stderr,
        )
        raise

try:
    import httpx
except ImportError:
    print(
        "Error: httpx package required. Install: pip install httpx",
        file=sys.stderr,
    )
    raise

# ---------------------------------------------------------------------------
# Logging — stderr only (stdout is reserved for MCP stdio transport)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[lens-mcp] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("lens-mcp")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PORT = int(os.environ.get("LENS_MCP_PORT", "50050"))
TOKEN_FILE = Path(os.environ.get(
    "LENS_MCP_TOKEN_FILE",
    str(Path.home() / ".lens_mcp_token.json"),
))
MCP_PROTOCOL_VERSION = "2025-06-18"

# Sketchfab 3D model library
SKETCHFAB_API_TOKEN = os.environ.get("SKETCHFAB_API_TOKEN", "")
SKETCHFAB_BASE_URL = "https://api.sketchfab.com/v3"
MODEL_CACHE_DIR = Path(os.environ.get(
    "LENS_MCP_MODEL_CACHE",
    str(Path.home() / ".cache" / "lens-mcp" / "models"),
))

# ---------------------------------------------------------------------------
# HTTP Bridge to Lens Studio's built-in MCP server
# ---------------------------------------------------------------------------


class LensBridge:
    """Proxies JSON-RPC 2.0 requests to Lens Studio's HTTP MCP server."""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self.base_url = f"http://localhost:{port}/mcp"
        self.auth_url = f"http://localhost:{port}/mcp/request-auth"
        self.token: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._id_counter: int = 0
        self._tools_cache: Optional[List[Dict[str, Any]]] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    # -- Token persistence ---------------------------------------------------

    def _load_token(self) -> bool:
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            if data.get("authToken"):
                self.token = data["authToken"]
                log.info("Loaded saved auth token from %s", TOKEN_FILE)
                return True
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
        return False

    def _save_token(self) -> None:
        if not self.token:
            return
        try:
            TOKEN_FILE.write_text(
                json.dumps({"authToken": self.token}, indent=2),
                encoding="utf-8",
            )
            log.info("Saved auth token to %s", TOKEN_FILE)
        except OSError as exc:
            log.warning("Could not save token: %s", exc)

    # -- Authentication ------------------------------------------------------

    async def request_auth(self) -> bool:
        """Request a new auth token from Lens Studio (triggers permission popup)."""
        try:
            log.info("Requesting authentication from Lens Studio...")
            client = self._get_client()
            resp = await client.post(
                self.auth_url,
                json={"clientName": "lens-mcp"},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("token")
            if not token:
                log.error("No token in auth response: %s", data)
                return False
            self.token = token
            self._save_token()
            log.info("Authentication successful")
            return True
        except httpx.ConnectError:
            log.error(
                "Cannot connect to Lens Studio at localhost:%d. "
                "Is Lens Studio 5.15+ running?",
                self.port,
            )
            return False
        except Exception as exc:
            log.error("Authentication failed: %s", exc)
            return False

    async def ensure_auth(self) -> None:
        """Load saved token or request a new one."""
        if self.token:
            return
        if self._load_token():
            return
        success = await self.request_auth()
        if not success:
            raise RuntimeError(
                "Authentication required. Start Lens Studio 5.15+ and approve the connection."
            )

    # -- HTTP requests -------------------------------------------------------

    async def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        retry_auth: bool = True,
    ) -> Dict[str, Any]:
        """Send a JSON-RPC 2.0 request to Lens Studio's MCP endpoint."""
        await self.ensure_auth()
        client = self._get_client()

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }

        try:
            resp = await client.post(
                self.base_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.token}",
                    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                },
            )
        except httpx.ConnectError:
            raise RuntimeError(
                f"Cannot connect to Lens Studio at localhost:{self.port}. "
                "Is Lens Studio running?"
            )

        # Handle 401 — token may have been invalidated
        if resp.status_code == 401 and retry_auth:
            log.info("Token rejected (401), re-authenticating...")
            self.token = None
            success = await self.request_auth()
            if not success:
                raise RuntimeError("Re-authentication failed after 401")
            return await self.request(method, params, retry_auth=False)

        resp.raise_for_status()
        return resp.json()

    # -- Tool discovery ------------------------------------------------------

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Fetch available tools from Lens Studio."""
        response = await self.request("tools/list")
        tools = response.get("result", {}).get("tools", [])
        self._tools_cache = tools
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Forward a tool call to Lens Studio."""
        try:
            response = await self.request(
                "tools/call",
                {"name": name, "arguments": arguments},
            )
        except Exception as exc:
            return {"error": str(exc)}
        if "error" in response:
            error = response["error"]
            msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            return {"error": msg}
        return response.get("result", response)

    def status(self) -> Dict[str, Any]:
        """Return current connection/auth state."""
        return {
            "authenticated": self.token is not None,
            "port": self.port,
            "base_url": self.base_url,
            "cached_tools": len(self._tools_cache) if self._tools_cache else 0,
        }

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ---------------------------------------------------------------------------
# Sketchfab 3D Model Client
# ---------------------------------------------------------------------------


class SketchfabClient:
    """Async client for the Sketchfab Data API v3."""

    def __init__(self, token: str = SKETCHFAB_API_TOKEN) -> None:
        self.token = token
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Token {self.token}"}

    async def search(
        self, query: str, max_results: int = 5, downloadable_only: bool = True
    ) -> List[Dict[str, Any]]:
        """Search Sketchfab for 3D models matching a query."""
        params: Dict[str, Any] = {
            "type": "models",
            "q": query,
            "count": min(max_results, 24),
        }
        if downloadable_only:
            params["downloadable"] = "true"

        client = self._get_client()
        resp = await client.get(
            f"{SKETCHFAB_BASE_URL}/search",
            params=params,
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("results", []):
            results.append({
                "name": item.get("name", ""),
                "uid": item.get("uid", ""),
                "thumbnail": (item.get("thumbnails", {}).get("images", [{}])[0].get("url", "") if item.get("thumbnails") else ""),
                "author": item.get("user", {}).get("displayName", ""),
                "license": item.get("license", {}).get("slug", "") if item.get("license") else "",
                "vertex_count": item.get("vertexCount", 0),
                "downloadable": item.get("isDownloadable", False),
            })
        return results

    async def get_download_url(self, uid: str) -> str:
        """Get a temporary download URL for a model's GLB/glTF archive."""
        client = self._get_client()
        resp = await client.get(
            f"{SKETCHFAB_BASE_URL}/models/{uid}/download",
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        # Prefer glTF format (includes GLB)
        gltf = data.get("gltf", data.get("glb", {}))
        url = gltf.get("url", "")
        if not url:
            raise ValueError(f"No downloadable GLB/glTF found for model {uid}")
        return url

    async def download_model(self, uid: str, filename: str = "") -> Path:
        """Download a model archive to the local cache directory."""
        url = await self.get_download_url(uid)
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fname = filename or f"{uid}.glb"
        dest = MODEL_CACHE_DIR / fname
        client = self._get_client()
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(8192):
                    f.write(chunk)
        return dest

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ---------------------------------------------------------------------------
# Singleton bridge + FastMCP application
# ---------------------------------------------------------------------------

bridge = LensBridge()
sketchfab = SketchfabClient()
app = FastMCP("lens-studio")

# ===== CONNECTION & STATUS TOOLS ==========================================


@app.tool()
async def connect() -> str:
    """Authenticate with Lens Studio's built-in MCP server.

    Triggers a permission popup in Lens Studio on first connection.
    Subsequent calls reuse the saved token. Call this before using
    any Lens Studio tools.
    """
    try:
        await bridge.ensure_auth()
        tools = await bridge.list_tools()
        return f"Connected to Lens Studio (port {bridge.port}). {len(tools)} tools available."
    except Exception as exc:
        return f"Connection failed: {exc}"


@app.tool()
def status() -> Dict[str, Any]:
    """Return connection status: auth state, port, and cached tool count."""
    return bridge.status()


@app.tool()
async def list_tools() -> List[Dict[str, Any]]:
    """List all tools available from Lens Studio's MCP server.

    Returns tool names, descriptions, and input schemas.
    Useful for discovering what operations are available.
    """
    try:
        tools = await bridge.list_tools()
        return [{"name": t["name"], "description": t.get("description", "")} for t in tools]
    except Exception as exc:
        return [{"error": str(exc)}]


@app.tool()
async def lens_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
    """Call any Lens Studio tool by name with the given arguments.

    This is the primary way to interact with Lens Studio. Use list_tools()
    to discover available tool names and their expected arguments.

    Args:
        name: Tool name (e.g. 'CreateLensStudioSceneObject', 'GetSceneHierarchy')
        arguments: Tool arguments as a JSON object (varies per tool)
    """
    return await bridge.call_tool(name, arguments or {})


# ===== SCENE PRIMITIVES ====================================================


@app.tool()
async def get_scene() -> Any:
    """Get the full scene hierarchy of the current Lens Studio project.

    Returns a tree of all scene objects with names, types, and components.
    Call this first to understand the current scene structure.
    """
    return await bridge.call_tool("GetLensStudioSceneGraph", {})


@app.tool()
async def create_object(name: str, preset: Optional[str] = None) -> Any:
    """Create a new scene object in Lens Studio.

    Args:
        name: Display name for the object (e.g. 'My Sphere', 'Background Image')
        preset: Optional preset template. Common presets:
            - SphereMeshObjectPreset, BoxMeshObjectPreset, CylinderMeshObjectPreset
            - CameraObjectPreset, DirectionalLightObjectPreset
            - ScreenImageObjectPreset, ScreenTextObjectPreset
    """
    args: Dict[str, Any] = {"name": name}
    if preset:
        args["preset"] = preset
    return await bridge.call_tool("CreateLensStudioSceneObject", args)


PRIMITIVE_PRESETS = {
    "sphere": "SphereMeshObjectPreset",
    "cube": "BoxMeshObjectPreset",
    "box": "BoxMeshObjectPreset",
    "cylinder": "CylinderMeshObjectPreset",
    "camera": "CameraObjectPreset",
    "light": "DirectionalLightObjectPreset",
    "image": "ScreenImageObjectPreset",
    "text": "ScreenTextObjectPreset",
}

# ---------------------------------------------------------------------------
# Face landmark positions relative to HeadCenter attachment point.
# Coordinates: X = left/right, Y = up/down, Z = forward (toward camera).
# IMPORTANT: Head Anchor localTransform must be {0,0,0} when using these.
# The Head Binding component handles tracking; any non-zero anchor offset
# shifts ALL children and causes misalignment (e.g. nose appearing at chin).
# ---------------------------------------------------------------------------

FACE_LANDMARKS = {
    # Core face features — calibrated from real Lens Studio face tracking.
    # HeadCenter origin is at the volumetric center of the skull (~ear level),
    # so the nose, eyes, and forehead are all ABOVE center (positive Y).
    "nose_tip":       {"position": {"x": 0,    "y": 5,    "z": 9.5},  "description": "Tip of the nose, center of face"},
    "nose_bridge":    {"position": {"x": 0,    "y": 7.5,  "z": 8},    "description": "Bridge of the nose, between the eyes"},
    "forehead":       {"position": {"x": 0,    "y": 12,   "z": 6},    "description": "Center of forehead"},
    "chin":           {"position": {"x": 0,    "y": -3,   "z": 6},    "description": "Bottom of chin"},
    "mouth_center":   {"position": {"x": 0,    "y": 2,    "z": 8.5},  "description": "Center of the mouth"},
    # Eyes
    "left_eye":       {"position": {"x": -3,   "y": 7.5,  "z": 7.5},  "description": "Center of left eye"},
    "right_eye":      {"position": {"x": 3,    "y": 7.5,  "z": 7.5},  "description": "Center of right eye"},
    # Cheeks
    "left_cheek":     {"position": {"x": -5.5, "y": 4,    "z": 5},    "description": "Left cheek area"},
    "right_cheek":    {"position": {"x": 5.5,  "y": 4,    "z": 5},    "description": "Right cheek area"},
    # Cat/animal filter positions
    "left_ear_top":   {"position": {"x": -7.4, "y": 16.4, "z": -2},   "description": "Left cat/animal ear position, above head"},
    "right_ear_top":  {"position": {"x": 7.4,  "y": 16.4, "z": -2},   "description": "Right cat/animal ear position, above head"},
    "left_whiskers":  {"position": {"x": -3,   "y": 5.7,  "z": 13.5}, "description": "Left whisker origin, beside nose"},
    "right_whiskers": {"position": {"x": 3,    "y": 5.7,  "z": 13.5}, "description": "Right whisker origin, beside nose"},
    # Crown / top of head
    "crown":          {"position": {"x": 0,    "y": 18,   "z": 0},    "description": "Top of head, for hats/crowns/horns"},
}

HEAD_ATTACHMENT_POINTS = [
    "HeadCenter", "CandideCenter", "TriangleBarycentric",
    "LeftEyeballCenter", "RightEyeballCenter", "MouthCenter",
    "Chin", "Forehead", "LeftForehead", "RightForehead",
    "LeftCheek", "RightCheek",
]


@app.tool()
async def add_primitive(shape: str = "cube", name: Optional[str] = None) -> Any:
    """Drop a test primitive into the scene. Quick way to add basic shapes.

    Args:
        shape: One of: cube, sphere, cylinder, camera, light, image, text
        name: Optional display name (defaults to the shape name, e.g. 'Cube')
    """
    key = shape.lower()
    preset = PRIMITIVE_PRESETS.get(key)
    if not preset:
        return {"error": f"Unknown shape '{shape}'. Options: {', '.join(sorted(PRIMITIVE_PRESETS))}"}
    display_name = name or key.capitalize()
    return await bridge.call_tool("CreateLensStudioSceneObject", {"name": display_name, "preset": preset})


@app.tool()
async def delete_object(name: str) -> Any:
    """Delete a scene object by name (resolves name to UUID automatically).

    Args:
        name: Name of the scene object to remove
    """
    # Lens Studio requires UUID — look up by name first
    lookup = await bridge.call_tool("GetLensStudioSceneObjectByName", {"name": name})
    if "error" in lookup:
        return lookup
    # Extract UUID from the lookup result
    content = lookup.get("content", [])
    if not content:
        return {"error": f"Object '{name}' not found"}
    try:
        data = json.loads(content[0].get("text", "{}"))
        uuid = data.get("objectUUID") or data.get("uuid") or data.get("id")
        if not uuid:
            # Try nested structure
            objects = data.get("objects", [data])
            uuid = objects[0].get("objectUUID") or objects[0].get("uuid") or objects[0].get("id")
    except (json.JSONDecodeError, IndexError, KeyError):
        uuid = None
    if not uuid:
        return {"error": f"Could not resolve UUID for '{name}'. Raw: {content[0].get('text', '')[:200]}"}
    return await bridge.call_tool("DeleteLensStudioSceneObject", {"objectUUID": uuid})


@app.tool()
async def create_asset(name: str, asset_type: str) -> Any:
    """Create a new asset in the Lens Studio project.

    Args:
        name: Name for the asset
        asset_type: Asset type (e.g. 'Material', 'RenderTarget', 'AnimationLayer')
    """
    return await bridge.call_tool("CreateLensStudioAsset", {"name": name, "assetType": asset_type})


@app.tool()
async def set_property(
    object_uuid: str,
    property_path: str,
    value: Any,
    value_type: str = "number",
) -> Any:
    """Set a property on a scene object, component, or asset by UUID.

    Args:
        object_uuid: UUID of the target (e.g. 'a1b2c3d4-e5f6-...')
        property_path: Property path (e.g. 'enabled', 'intensity', 'localPosition')
        value: New value (type depends on value_type)
        value_type: One of: number, string, boolean, enum, vec2, vec3, vec4, mat3,
                    transform, reference, rect, layer_set_mask
    """
    return await bridge.call_tool("SetLensStudioProperty", {
        "objectUUID": object_uuid,
        "propertyPath": property_path,
        "value": value,
        "valueType": value_type,
    })


@app.tool()
async def query_knowledge_base(query: str) -> Any:
    """Search Lens Studio documentation and knowledge base.

    Useful for finding how to accomplish tasks, understanding APIs,
    or discovering available features.

    Args:
        query: Natural language question (e.g. 'how to add face tracking')
    """
    return await bridge.call_tool("QueryLensStudioKnowledgeBase", {"query": query})


# ===== COLOR & MATERIAL TOOLS =============================================


# Common color names → RGBA (0-1 range)
NAMED_COLORS = {
    "red":     {"x": 1,    "y": 0,    "z": 0,    "w": 1},
    "green":   {"x": 0,    "y": 0.8,  "z": 0,    "w": 1},
    "blue":    {"x": 0,    "y": 0.4,  "z": 1,    "w": 1},
    "yellow":  {"x": 1,    "y": 0.9,  "z": 0,    "w": 1},
    "orange":  {"x": 1,    "y": 0.5,  "z": 0,    "w": 1},
    "purple":  {"x": 0.6,  "y": 0.2,  "z": 0.8,  "w": 1},
    "pink":    {"x": 1,    "y": 0.4,  "z": 0.7,  "w": 1},
    "cyan":    {"x": 0,    "y": 0.9,  "z": 1,    "w": 1},
    "white":   {"x": 1,    "y": 1,    "z": 1,    "w": 1},
    "black":   {"x": 0.05, "y": 0.05, "z": 0.05, "w": 1},
    "grey":    {"x": 0.5,  "y": 0.5,  "z": 0.5,  "w": 1},
    "gray":    {"x": 0.5,  "y": 0.5,  "z": 0.5,  "w": 1},
    "brown":   {"x": 0.55, "y": 0.27, "z": 0.07, "w": 1},
    "gold":    {"x": 1,    "y": 0.84, "z": 0,    "w": 1},
    "silver":  {"x": 0.75, "y": 0.75, "z": 0.75, "w": 1},
}


def _parse_color(
    color: Optional[str] = None,
    r: Optional[float] = None,
    g: Optional[float] = None,
    b: Optional[float] = None,
    a: float = 1.0,
) -> Optional[Dict[str, float]]:
    """Parse color from a name or RGBA values. Returns vec4 dict or None."""
    if color:
        key = color.lower().strip()
        if key in NAMED_COLORS:
            c = dict(NAMED_COLORS[key])
            c["w"] = a
            return c
        # Try hex color: #RRGGBB or #RGB
        hex_str = key.lstrip("#")
        if len(hex_str) == 6:
            try:
                ri = int(hex_str[0:2], 16) / 255
                gi = int(hex_str[2:4], 16) / 255
                bi = int(hex_str[4:6], 16) / 255
                return {"x": ri, "y": gi, "z": bi, "w": a}
            except ValueError:
                pass
        elif len(hex_str) == 3:
            try:
                ri = int(hex_str[0] * 2, 16) / 255
                gi = int(hex_str[1] * 2, 16) / 255
                bi = int(hex_str[2] * 2, 16) / 255
                return {"x": ri, "y": gi, "z": bi, "w": a}
            except ValueError:
                pass
        return None
    if r is not None and g is not None and b is not None:
        return {"x": r, "y": g, "z": b, "w": a}
    return None


async def _get_render_component(object_uuid: str) -> Optional[Dict[str, Any]]:
    """Get the RenderMeshVisual component from a scene object."""
    result = await bridge.call_tool("GetLensStudioSceneObjectById", {"objectUUID": object_uuid})
    if isinstance(result, dict) and "error" in result:
        return None
    content = result.get("content", [])
    if not content:
        return None
    try:
        data = json.loads(content[0].get("text", "{}"))
        obj = data.get("object", data)
        for comp in obj.get("components", []):
            if comp.get("type") == "RenderMeshVisual":
                return comp
    except (json.JSONDecodeError, KeyError):
        pass
    return None


@app.tool()
async def create_material(
    name: str,
    color: Optional[str] = None,
    r: Optional[float] = None,
    g: Optional[float] = None,
    b: Optional[float] = None,
    a: float = 1.0,
    metallic: float = 0.0,
    roughness: float = 0.5,
    unlit: bool = False,
) -> Any:
    """Create a new material with a solid color.

    Specify color by name, hex code, or RGB values (0-1 range).

    Args:
        name: Material name (e.g. 'Red Nose Material')
        color: Color name ('red', 'blue', 'pink', '#FF6600') or None to use r/g/b
        r: Red channel 0-1 (used if color is None)
        g: Green channel 0-1 (used if color is None)
        b: Blue channel 0-1 (used if color is None)
        a: Alpha channel 0-1 (default: 1.0 opaque)
        metallic: Metallic factor 0-1 (default: 0.0 for non-metal)
        roughness: Roughness factor 0-1 (default: 0.5, 0=shiny, 1=matte)
        unlit: If True, create unlit material (ignores lighting, good for flat colors)
    """
    rgba = _parse_color(color, r, g, b, a)
    if not rgba:
        return {
            "error": "Invalid color. Use a name (red, blue, pink...), hex (#FF6600), or r/g/b values.",
            "available_colors": list(NAMED_COLORS.keys()),
        }

    # Create material from preset
    preset = "UnlitMaterialPreset" if unlit else "SimplePBRMaterialPreset"
    result = await bridge.call_tool("CreateAssetFromPresetTool", {
        "preset": preset,
        "name": name,
    })
    if isinstance(result, dict) and "error" in result:
        return result

    # Extract material UUID from response
    content = result.get("content", [])
    mat_uuid = None
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            mat_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    if not mat_uuid:
        return {"error": "Material created but could not extract UUID"}

    # Set base color
    await bridge.call_tool("SetLensStudioProperty", {
        "objectUUID": mat_uuid,
        "propertyPath": "passInfos.0.baseColor",
        "value": rgba,
        "valueType": "vec4",
    })

    # Clear baseTex so solid color shows through
    await bridge.call_tool("SetLensStudioProperty", {
        "objectUUID": mat_uuid,
        "propertyPath": "passInfos.0.baseTex",
        "value": "null",
        "valueType": "reference",
    })

    # Set metallic and roughness (PBR only)
    if not unlit:
        await bridge.call_tool("SetLensStudioProperty", {
            "objectUUID": mat_uuid,
            "propertyPath": "passInfos.0.metallic",
            "value": metallic,
            "valueType": "number",
        })
        await bridge.call_tool("SetLensStudioProperty", {
            "objectUUID": mat_uuid,
            "propertyPath": "passInfos.0.roughness",
            "value": roughness,
            "valueType": "number",
        })

    return {
        "message": f"Created {'unlit' if unlit else 'PBR'} material '{name}' with color {color or f'({r},{g},{b})'}.",
        "uuid": mat_uuid,
        "color": rgba,
        "metallic": metallic if not unlit else "N/A",
        "roughness": roughness if not unlit else "N/A",
    }


@app.tool()
async def set_color(
    name: str,
    color: Optional[str] = None,
    r: Optional[float] = None,
    g: Optional[float] = None,
    b: Optional[float] = None,
    a: float = 1.0,
    metallic: float = 0.0,
    roughness: float = 0.5,
    unlit: bool = False,
) -> Any:
    """Set the color of a scene object by name. Creates a material automatically.

    This is the easiest way to color a shape. Just provide the object name
    and a color. Accepts color names, hex codes, or RGB values.

    Args:
        name: Scene object name (e.g. 'Cat Nose', 'Left Ear')
        color: Color name ('red', 'blue', 'pink', '#FF6600') or None to use r/g/b
        r: Red channel 0-1 (used if color is None)
        g: Green channel 0-1 (used if color is None)
        b: Blue channel 0-1 (used if color is None)
        a: Alpha channel 0-1 (default: 1.0 opaque)
        metallic: Metallic factor 0-1 (default: 0.0)
        roughness: Roughness factor 0-1 (default: 0.5)
        unlit: If True, use unlit material (ignores lighting)
    """
    rgba = _parse_color(color, r, g, b, a)
    if not rgba:
        return {
            "error": "Invalid color. Use a name (red, blue, pink...), hex (#FF6600), or r/g/b values.",
            "available_colors": list(NAMED_COLORS.keys()),
        }

    # Resolve object UUID
    obj_uuid = await _resolve_uuid(name)
    if not obj_uuid:
        return {"error": f"Scene object '{name}' not found."}

    # Get RenderMeshVisual component
    render_comp = await _get_render_component(obj_uuid)
    if not render_comp:
        return {"error": f"'{name}' has no RenderMeshVisual component. Only mesh objects can be colored."}

    comp_uuid = render_comp.get("id") or render_comp.get("properties", {}).get("id")
    if not comp_uuid:
        return {"error": f"Could not get component UUID for '{name}'"}

    # Create the material
    color_label = color or f"rgb({r},{g},{b})"
    mat_name = f"{name} {color_label.capitalize()} Mat"
    mat_result = await create_material(
        name=mat_name, color=color, r=r, g=g, b=b, a=a,
        metallic=metallic, roughness=roughness, unlit=unlit,
    )
    if isinstance(mat_result, dict) and "error" in mat_result:
        return mat_result

    mat_uuid = mat_result["uuid"]

    # Assign material to the object's RenderMeshVisual
    await bridge.call_tool("SetLensStudioProperty", {
        "objectUUID": comp_uuid,
        "propertyPath": "mainMaterial",
        "value": mat_uuid,
        "valueType": "reference",
    })

    return {
        "message": f"Set '{name}' to {color_label} (material: '{mat_name}').",
        "object": name,
        "material_uuid": mat_uuid,
        "color": rgba,
    }


@app.tool()
async def assign_material(object_name: str, material_name: str) -> Any:
    """Assign an existing material to a scene object.

    Args:
        object_name: Name of the scene object (e.g. 'Cat Nose')
        material_name: Name of the material asset to assign
    """
    # Resolve object
    obj_uuid = await _resolve_uuid(object_name)
    if not obj_uuid:
        return {"error": f"Scene object '{object_name}' not found."}

    # Get RenderMeshVisual component
    render_comp = await _get_render_component(obj_uuid)
    if not render_comp:
        return {"error": f"'{object_name}' has no RenderMeshVisual component."}

    comp_uuid = render_comp.get("id") or render_comp.get("properties", {}).get("id")
    if not comp_uuid:
        return {"error": f"Could not get component UUID for '{object_name}'"}

    # Resolve material by name
    mat_result = await bridge.call_tool("GetLensStudioAssetsByName", {"name": material_name})
    content = mat_result.get("content", [])
    mat_uuid = None
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            assets = data.get("assets", [data])
            mat_uuid = assets[0].get("id") or assets[0].get("assetUUID")
        except (json.JSONDecodeError, IndexError, KeyError):
            pass
    if not mat_uuid:
        return {"error": f"Material '{material_name}' not found."}

    # Assign
    await bridge.call_tool("SetLensStudioProperty", {
        "objectUUID": comp_uuid,
        "propertyPath": "mainMaterial",
        "value": mat_uuid,
        "valueType": "reference",
    })

    return {
        "message": f"Assigned material '{material_name}' to '{object_name}'.",
        "object": object_name,
        "material_uuid": mat_uuid,
    }


# ===== FACE POSITIONING TOOLS =============================================


@app.tool()
def get_face_landmarks(landmark: Optional[str] = None) -> Any:
    """Get reference positions for face features relative to HeadCenter attachment point.

    Use these positions when placing objects on a face filter. All coordinates
    are local offsets from a Head Binding with HeadCenter attachment.
    The Head Anchor object's own localTransform MUST be {0,0,0}.

    Coordinate system: X = left(-)/right(+), Y = up(+)/down(-), Z = forward(+)/back(-).

    Args:
        landmark: Optional specific landmark name (e.g. 'nose_tip', 'left_ear_top').
                  If omitted, returns all landmarks with positions and descriptions.
    """
    if landmark:
        key = landmark.lower().replace(" ", "_").replace("-", "_")
        if key in FACE_LANDMARKS:
            return {key: FACE_LANDMARKS[key]}
        return {
            "error": f"Unknown landmark '{landmark}'",
            "available": list(FACE_LANDMARKS.keys()),
        }
    return {
        "landmarks": FACE_LANDMARKS,
        "attachment_points": HEAD_ATTACHMENT_POINTS,
        "notes": (
            "Positions are local offsets from HeadCenter attachment. "
            "Head Anchor localTransform MUST be {0,0,0} — any offset shifts ALL children. "
            "X: left(-)/right(+), Y: up(+)/down(-), Z: forward(+)/back(-)."
        ),
    }


async def _resolve_uuid(name: str) -> Optional[str]:
    """Look up a scene object by name and return its UUID."""
    lookup = await bridge.call_tool("GetLensStudioSceneObjectByName", {"name": name})
    content = lookup.get("content", [])
    if not content:
        return None
    try:
        data = json.loads(content[0].get("text", "{}"))
        objects = data.get("objects", [data])
        obj = objects[0]
        return obj.get("id") or obj.get("objectUUID") or obj.get("uuid")
    except (json.JSONDecodeError, IndexError, KeyError):
        return None


@app.tool()
async def create_face_anchor(name: str = "Head Anchor") -> Any:
    """Create a head-tracked anchor for face filters using HeadCenter binding.

    This sets up a proper face-tracking anchor with Head Binding component
    and Face Occluder. The anchor's position is zeroed so children align
    correctly to face landmarks. Use add_face_element() to add children.

    Args:
        name: Name for the anchor object (default: 'Head Anchor')
    """
    # Create from the official HeadBinding preset (includes Face Occluder)
    result = await bridge.call_tool("CreateSceneObjectFromPresetTool", {
        "preset": "HeadBindingObjectPreset",
        "name": name,
    })
    if isinstance(result, dict) and "error" in result:
        return result

    # Ensure anchor position is zeroed — critical for correct child positioning
    uuid = await _resolve_uuid(name)
    if uuid:
        await bridge.call_tool("SetLensStudioProperty", {
            "objectUUID": uuid,
            "propertyPath": "localTransform",
            "value": {
                "position": {"x": 0, "y": 0, "z": 0},
                "rotation": {"x": 0, "y": 0, "z": 0},
                "scale": {"x": 1, "y": 1, "z": 1},
            },
            "valueType": "transform",
        })

    return {
        "message": f"Face anchor '{name}' created with HeadCenter binding and Face Occluder.",
        "uuid": uuid,
        "next_step": "Use add_face_element() to add nose, ears, whiskers, etc.",
    }


SHAPE_PRESETS = {
    "sphere": "SphereMeshObjectPreset",
    "box": "BoxMeshObjectPreset",
    "cube": "BoxMeshObjectPreset",
    "cone": "ConeMeshObjectPreset",
    "cylinder": "CylinderMeshObjectPreset",
    "plane": "PlaneMeshObjectPreset",
    "disc": "DiscMeshObjectPreset",
    "torus": "TorusMeshObjectPreset",
    "text3d": "Text3DObjectPreset",
}


@app.tool()
async def add_face_element(
    parent_name: str,
    landmark: str,
    shape: str = "sphere",
    name: Optional[str] = None,
    scale: Optional[Dict[str, float]] = None,
    rotation: Optional[Dict[str, float]] = None,
) -> Any:
    """Add a 3D element to a face filter at a specific landmark position.

    Creates the element, parents it to the face anchor, and positions it
    at the correct face landmark. Use get_face_landmarks() to see available
    landmark names and positions.

    Args:
        parent_name: Name of the face anchor to parent to (e.g. 'Head Anchor')
        landmark: Face landmark name (e.g. 'nose_tip', 'left_ear_top', 'left_whiskers')
        shape: 3D shape — sphere, cone, cylinder, box, plane, disc, torus, text3d
        name: Display name (defaults to landmark name, e.g. 'Nose Tip')
        scale: Optional scale override as {x, y, z} (default: {1, 1, 1})
        rotation: Optional rotation override as {x, y, z} in degrees (default: {0, 0, 0})
    """
    # Validate landmark
    key = landmark.lower().replace(" ", "_").replace("-", "_")
    if key not in FACE_LANDMARKS:
        return {
            "error": f"Unknown landmark '{landmark}'",
            "available": list(FACE_LANDMARKS.keys()),
        }

    # Validate shape
    shape_key = shape.lower()
    preset = SHAPE_PRESETS.get(shape_key)
    if not preset:
        return {
            "error": f"Unknown shape '{shape}'",
            "available": list(SHAPE_PRESETS.keys()),
        }

    # Resolve parent UUID
    parent_uuid = await _resolve_uuid(parent_name)
    if not parent_uuid:
        return {"error": f"Parent '{parent_name}' not found. Create it with create_face_anchor() first."}

    # Create the object
    display_name = name or key.replace("_", " ").title()
    create_result = await bridge.call_tool("CreateSceneObjectFromPresetTool", {
        "preset": preset,
        "name": display_name,
    })
    if isinstance(create_result, dict) and "error" in create_result:
        return create_result

    # Resolve the new object's UUID
    obj_uuid = await _resolve_uuid(display_name)
    if not obj_uuid:
        return {"error": f"Created '{display_name}' but could not resolve its UUID"}

    # Parent to the face anchor
    await bridge.call_tool("SetLensStudioParent", {
        "objectUUID": obj_uuid,
        "parentUUID": parent_uuid,
    })

    # Set position from landmark, with optional scale and rotation
    lm = FACE_LANDMARKS[key]
    pos = lm["position"]
    scl = scale or {"x": 1, "y": 1, "z": 1}
    rot = rotation or {"x": 0, "y": 0, "z": 0}

    await bridge.call_tool("SetLensStudioProperty", {
        "objectUUID": obj_uuid,
        "propertyPath": "localTransform",
        "value": {
            "position": pos,
            "rotation": rot,
            "scale": scl,
        },
        "valueType": "transform",
    })

    return {
        "message": f"Added '{display_name}' ({shape}) at {key} landmark, parented to '{parent_name}'.",
        "uuid": obj_uuid,
        "position": pos,
        "scale": scl,
    }


# ===== PHASE 1: HAND TRACKING =============================================

HAND_LANDMARKS = {
    "wrist":        {"position": {"x": 0, "y": 0, "z": 0}, "description": "Wrist joint, base of hand"},
    "thumb_cmc":    {"position": {"x": -2.5, "y": 0.5, "z": 0.5}, "description": "Thumb carpometacarpal joint"},
    "thumb_mcp":    {"position": {"x": -3.5, "y": 1.5, "z": 1}, "description": "Thumb metacarpophalangeal joint"},
    "thumb_ip":     {"position": {"x": -4, "y": 2.5, "z": 1.5}, "description": "Thumb interphalangeal joint"},
    "thumb_tip":    {"position": {"x": -4.5, "y": 3.5, "z": 2}, "description": "Tip of thumb"},
    "index_mcp":    {"position": {"x": -2, "y": 4, "z": 0.5}, "description": "Index finger MCP joint"},
    "index_pip":    {"position": {"x": -2, "y": 6, "z": 0.5}, "description": "Index finger PIP joint"},
    "index_dip":    {"position": {"x": -2, "y": 7.5, "z": 0.5}, "description": "Index finger DIP joint"},
    "index_tip":    {"position": {"x": -2, "y": 9, "z": 0.5}, "description": "Tip of index finger"},
    "middle_mcp":   {"position": {"x": -0.5, "y": 4.2, "z": 0}, "description": "Middle finger MCP joint"},
    "middle_pip":   {"position": {"x": -0.5, "y": 6.5, "z": 0}, "description": "Middle finger PIP joint"},
    "middle_dip":   {"position": {"x": -0.5, "y": 8, "z": 0}, "description": "Middle finger DIP joint"},
    "middle_tip":   {"position": {"x": -0.5, "y": 9.5, "z": 0}, "description": "Tip of middle finger"},
    "ring_mcp":     {"position": {"x": 1, "y": 4, "z": 0}, "description": "Ring finger MCP joint"},
    "ring_pip":     {"position": {"x": 1, "y": 6, "z": 0}, "description": "Ring finger PIP joint"},
    "ring_dip":     {"position": {"x": 1, "y": 7.5, "z": 0}, "description": "Ring finger DIP joint"},
    "ring_tip":     {"position": {"x": 1, "y": 9, "z": 0}, "description": "Tip of ring finger"},
    "pinky_mcp":    {"position": {"x": 2.5, "y": 3.5, "z": 0.5}, "description": "Pinky finger MCP joint"},
    "pinky_pip":    {"position": {"x": 2.5, "y": 5, "z": 0.5}, "description": "Pinky finger PIP joint"},
    "pinky_dip":    {"position": {"x": 2.5, "y": 6.5, "z": 0.5}, "description": "Pinky finger DIP joint"},
    "pinky_tip":    {"position": {"x": 2.5, "y": 7.5, "z": 0.5}, "description": "Tip of pinky finger"},
    "palm_center":  {"position": {"x": 0, "y": 3, "z": 0}, "description": "Center of palm"},
}

GESTURE_TYPES = {
    "pinch": "HandGesture.Pinch",
    "fist": "HandGesture.Fist",
    "open_palm": "HandGesture.OpenPalm",
    "thumbs_up": "HandGesture.ThumbsUp",
    "peace_sign": "HandGesture.PeaceSign",
    "pointing": "HandGesture.Pointing",
}


@app.tool()
def get_hand_landmarks(landmark: Optional[str] = None) -> Any:
    """Get reference positions for hand joints relative to the wrist tracking anchor.

    Coordinate system: X = left(-)/right(+), Y = up(+)/down(-), Z = forward(+)/back(-).

    Args:
        landmark: Optional specific landmark name (e.g. 'thumb_tip', 'index_mcp').
                  If omitted, returns all landmarks.
    """
    if landmark:
        key = landmark.lower().replace(" ", "_").replace("-", "_")
        if key in HAND_LANDMARKS:
            return {key: HAND_LANDMARKS[key]}
        return {"error": f"Unknown hand landmark '{landmark}'", "available": list(HAND_LANDMARKS.keys())}
    return {
        "landmarks": HAND_LANDMARKS,
        "gesture_types": list(GESTURE_TYPES.keys()),
        "notes": "Positions are local offsets from the hand tracking anchor (wrist origin).",
    }


@app.tool()
async def create_hand_anchor(name: str = "Hand Anchor") -> Any:
    """Create a hand-tracked anchor using Object Tracking 3D.

    Args:
        name: Name for the anchor object (default: 'Hand Anchor')
    """
    result = await bridge.call_tool("CreateSceneObjectFromPresetTool", {
        "preset": "ObjectTracking3DObjectPreset", "name": name,
    })
    if isinstance(result, dict) and "error" in result:
        return result
    uuid = await _resolve_uuid(name)
    if uuid:
        await bridge.call_tool("SetLensStudioProperty", {
            "objectUUID": uuid, "propertyPath": "localTransform",
            "value": {"position": {"x": 0, "y": 0, "z": 0}, "rotation": {"x": 0, "y": 0, "z": 0}, "scale": {"x": 1, "y": 1, "z": 1}},
            "valueType": "transform",
        })
    return {"message": f"Hand anchor '{name}' created with ObjectTracking3D.", "uuid": uuid, "next_step": "Use add_hand_element() to add objects at hand joints."}


@app.tool()
async def add_hand_element(
    parent_name: str, landmark: str, shape: str = "sphere",
    name: Optional[str] = None, scale: Optional[Dict[str, float]] = None,
    rotation: Optional[Dict[str, float]] = None,
) -> Any:
    """Add a 3D element at a hand joint position.

    Args:
        parent_name: Name of the hand anchor (e.g. 'Hand Anchor')
        landmark: Hand landmark (e.g. 'thumb_tip', 'index_mcp', 'palm_center')
        shape: 3D shape — sphere, cone, cylinder, box, plane, disc, torus, text3d
        name: Display name (defaults to landmark name)
        scale: Optional scale as {x, y, z}
        rotation: Optional rotation as {x, y, z} degrees
    """
    key = landmark.lower().replace(" ", "_").replace("-", "_")
    if key not in HAND_LANDMARKS:
        return {"error": f"Unknown hand landmark '{landmark}'", "available": list(HAND_LANDMARKS.keys())}
    preset = SHAPE_PRESETS.get(shape.lower())
    if not preset:
        return {"error": f"Unknown shape '{shape}'", "available": list(SHAPE_PRESETS.keys())}
    parent_uuid = await _resolve_uuid(parent_name)
    if not parent_uuid:
        return {"error": f"Parent '{parent_name}' not found. Create it with create_hand_anchor() first."}
    display_name = name or key.replace("_", " ").title()
    create_result = await bridge.call_tool("CreateSceneObjectFromPresetTool", {"preset": preset, "name": display_name})
    if isinstance(create_result, dict) and "error" in create_result:
        return create_result
    obj_uuid = await _resolve_uuid(display_name)
    if not obj_uuid:
        return {"error": f"Created '{display_name}' but could not resolve its UUID"}
    await bridge.call_tool("SetLensStudioParent", {"objectUUID": obj_uuid, "parentUUID": parent_uuid})
    lm = HAND_LANDMARKS[key]
    pos = lm["position"]
    scl = scale or {"x": 1, "y": 1, "z": 1}
    rot = rotation or {"x": 0, "y": 0, "z": 0}
    await bridge.call_tool("SetLensStudioProperty", {
        "objectUUID": obj_uuid, "propertyPath": "localTransform",
        "value": {"position": pos, "rotation": rot, "scale": scl}, "valueType": "transform",
    })
    return {"message": f"Added '{display_name}' ({shape}) at {key}, parented to '{parent_name}'.", "uuid": obj_uuid, "position": pos, "scale": scl}


@app.tool()
async def create_gesture_trigger(gesture: str, action_script: str) -> Any:
    """Create a script that detects a hand gesture and runs an action.

    Args:
        gesture: Gesture type — pinch, fist, open_palm, thumbs_up, peace_sign, pointing
        action_script: JavaScript code to execute when gesture is detected
    """
    key = gesture.lower().replace(" ", "_").replace("-", "_")
    if key not in GESTURE_TYPES:
        return {"error": f"Unknown gesture '{gesture}'", "available_gestures": list(GESTURE_TYPES.keys())}
    gesture_enum = GESTURE_TYPES[key]
    script_code = (
        f"// Auto-generated gesture trigger for: {key}\n"
        f"// @input SceneObject handTracker\n"
        f"var ht = script.handTracker ? script.handTracker.getComponent('Component.ObjectTracking3D') : null;\n"
        f"if (ht) {{ ht.onGestureDetected.add(function(g) {{ if (g === {gesture_enum}) {{ {action_script} }} }}); }}\n"
    )
    script_name = f"GestureTrigger_{key}"
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": script_name, "assetType": "Script"})
    script_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            script_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created gesture trigger for '{key}'.", "gesture": key, "gesture_enum": gesture_enum, "script_name": script_name, "script_uuid": script_uuid, "script_code": script_code}


# ===== PHASE 2: ANIMATION SYSTEM ==========================================

EASING_FUNCTIONS = {
    "linear": "Linear", "ease_in": "EaseInQuad", "ease_out": "EaseOutQuad",
    "ease_in_out": "EaseInOutQuad", "ease_in_cubic": "EaseInCubic",
    "ease_out_cubic": "EaseOutCubic", "ease_in_out_cubic": "EaseInOutCubic",
    "bounce": "EaseOutBounce", "elastic": "EaseOutElastic", "back": "EaseOutBack",
}

ANIMATABLE_PROPERTIES = {
    "position": {"path": "localPosition", "type": "vec3"},
    "rotation": {"path": "localRotation", "type": "vec3"},
    "scale": {"path": "localScale", "type": "vec3"},
    "position_x": {"path": "localPosition.x", "type": "number"},
    "position_y": {"path": "localPosition.y", "type": "number"},
    "position_z": {"path": "localPosition.z", "type": "number"},
    "scale_x": {"path": "localScale.x", "type": "number"},
    "scale_y": {"path": "localScale.y", "type": "number"},
    "scale_z": {"path": "localScale.z", "type": "number"},
    "rotation_x": {"path": "localRotation.x", "type": "number"},
    "rotation_y": {"path": "localRotation.y", "type": "number"},
    "rotation_z": {"path": "localRotation.z", "type": "number"},
    "opacity": {"path": "opacity", "type": "number"},
}

TRIGGER_TYPES = {
    "tap": "TapEvent", "mouth_open": "MouthOpenedEvent",
    "brows_raised": "BrowsRaisedEvent", "brows_lowered": "BrowsLoweredEvent",
    "kiss": "KissStartedEvent", "smile": "SmileEvent",
    "turn_on": "TurnOnEvent", "delay": "DelayedCallbackEvent",
}


def _generate_tween_js(prop_info, from_val, to_val, duration_ms, easing, loop=False, ping_pong=False, delay_ms=0):
    """Generate JavaScript tween animation code."""
    lines = [
        "// Auto-generated tween animation",
        "// @input SceneObject target",
        f"var duration = {duration_ms};",
        f"var easing = '{easing}';",
    ]
    if delay_ms > 0:
        lines.append(f"var delay = {delay_ms};")
    loop_str = ".repeat(Infinity)" if loop else ""
    yoyo_str = ".yoyo(true)" if ping_pong else ""
    lines.append(f"// Property: {prop_info['path']} from {from_val} to {to_val}")
    lines.append(f"// Loop: {loop}, PingPong: {ping_pong}")
    lines.append(f"global.tweenManager.startTween(script.target, '{prop_info['path']}', {json.dumps(to_val)}, duration / 1000, easing{loop_str}{yoyo_str});")
    return "\n".join(lines)


@app.tool()
async def create_tween(
    object_name: str, property: str, from_value: Any, to_value: Any,
    duration: float, easing: str = "linear", loop: bool = False,
    ping_pong: bool = False, delay: float = 0,
) -> Any:
    """Create a tween animation on an object.

    Args:
        object_name: Scene object to animate
        property: Property to animate — position, rotation, scale, position_x/y/z, scale_x/y/z, rotation_x/y/z, opacity
        easing: Easing function — linear, ease_in, ease_out, ease_in_out, bounce, elastic, back
        duration: Duration in seconds
        from_value: Starting value
        to_value: Ending value
        loop: Whether to loop the animation
        ping_pong: Whether to reverse on completion (yoyo)
        delay: Delay before starting in seconds
    """
    prop_key = property.lower().replace(" ", "_").replace("-", "_")
    if prop_key not in ANIMATABLE_PROPERTIES:
        return {"error": f"Unknown property '{property}'", "available_properties": list(ANIMATABLE_PROPERTIES.keys())}
    easing_key = easing.lower().replace(" ", "_").replace("-", "_")
    if easing_key not in EASING_FUNCTIONS:
        return {"error": f"Unknown easing '{easing}'", "available_easings": list(EASING_FUNCTIONS.keys())}
    uuid = await _resolve_uuid(object_name)
    if not uuid:
        return {"error": f"Object '{object_name}' not found."}
    prop_info = ANIMATABLE_PROPERTIES[prop_key]
    easing_name = EASING_FUNCTIONS[easing_key]
    script_code = _generate_tween_js(prop_info, from_value, to_value, int(duration * 1000), easing_name, loop, ping_pong, int(delay * 1000))
    script_name = f"Tween_{object_name}_{prop_key}"
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": script_name, "assetType": "Script"})
    script_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            script_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created tween on '{object_name}': {prop_key} {from_value}->{to_value} over {duration}s ({easing_key}).", "object": object_name, "script_uuid": script_uuid, "script_code": script_code}


@app.tool()
async def create_animation_sequence(sequence: List[Dict[str, Any]]) -> Any:
    """Create a chained animation sequence across one or more objects.

    Args:
        sequence: List of animation steps, each with: object, property, to (value), duration (seconds).
                  Optional: easing, delay (seconds).
    """
    if not sequence:
        return {"error": "Sequence is empty. Provide at least one animation step."}
    for i, step in enumerate(sequence):
        if "object" not in step or "property" not in step or "to" not in step or "duration" not in step:
            return {"error": f"Step {i} missing required keys (object, property, to, duration)."}
        prop_key = step["property"].lower().replace(" ", "_").replace("-", "_")
        if prop_key not in ANIMATABLE_PROPERTIES:
            return {"error": f"Step {i}: unknown property '{step['property']}'", "available_properties": list(ANIMATABLE_PROPERTIES.keys())}
    lines = ["// Auto-generated animation sequence", f"// {len(sequence)} steps"]
    cumulative_delay = 0
    for i, step in enumerate(sequence):
        prop_info = ANIMATABLE_PROPERTIES[step["property"].lower().replace(" ", "_").replace("-", "_")]
        easing_key = step.get("easing", "ease_out").lower().replace(" ", "_").replace("-", "_")
        easing_name = EASING_FUNCTIONS.get(easing_key, "EaseOutQuad")
        step_delay = step.get("delay", 0)
        cumulative_delay += step_delay
        dur_ms = int(step["duration"] * 1000)
        lines.append(f"// Step {i}: {step['object']}.{prop_info['path']} -> {step['to']} ({dur_ms}ms, {easing_name})")
    script_code = "\n".join(lines)
    script_name = f"AnimSequence_{len(sequence)}steps"
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": script_name, "assetType": "Script"})
    script_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            script_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created animation sequence with {len(sequence)} steps.", "script_uuid": script_uuid, "steps": len(sequence), "script_code": script_code}


@app.tool()
async def animate_on_trigger(
    object_name: str, trigger: str, property: str, to_value: Any,
    duration: float = 1.0, easing: str = "ease_out",
) -> Any:
    """Create an animation triggered by an event (tap, mouth open, etc.).

    Args:
        object_name: Object to animate
        trigger: Event trigger — tap, mouth_open, brows_raised, brows_lowered, kiss, smile, turn_on, delay
        property: Property to animate
        to_value: Target value
        duration: Duration in seconds
        easing: Easing function
    """
    trigger_key = trigger.lower().replace(" ", "_").replace("-", "_")
    if trigger_key not in TRIGGER_TYPES:
        return {"error": f"Unknown trigger '{trigger}'", "available_triggers": list(TRIGGER_TYPES.keys())}
    prop_key = property.lower().replace(" ", "_").replace("-", "_")
    if prop_key not in ANIMATABLE_PROPERTIES:
        return {"error": f"Unknown property '{property}'", "available_properties": list(ANIMATABLE_PROPERTIES.keys())}
    uuid = await _resolve_uuid(object_name)
    if not uuid:
        return {"error": f"Object '{object_name}' not found."}
    trigger_event = TRIGGER_TYPES[trigger_key]
    prop_info = ANIMATABLE_PROPERTIES[prop_key]
    easing_key = easing.lower().replace(" ", "_").replace("-", "_")
    easing_name = EASING_FUNCTIONS.get(easing_key, "EaseOutQuad")
    script_code = (
        f"// Trigger: {trigger_event} -> animate {prop_info['path']} to {to_value}\n"
        f"// @input SceneObject target\n"
        f"script.createEvent('{trigger_event}').bind(function() {{\n"
        f"    global.tweenManager.startTween(script.target, '{prop_info['path']}', {json.dumps(to_value)}, {duration}, '{easing_name}');\n"
        f"}});\n"
    )
    script_name = f"TriggerAnim_{object_name}_{trigger_key}"
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": script_name, "assetType": "Script"})
    script_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            script_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created {trigger_key}-triggered animation on '{object_name}'.", "trigger": trigger_key, "script_uuid": script_uuid, "script_code": script_code}


@app.tool()
async def create_looping_animation(
    object_name: str, property: str, values: List[Any], duration: float, easing: str = "ease_in_out",
) -> Any:
    """Create a looping animation cycling between values.

    Args:
        object_name: Object to animate
        property: Property to animate
        values: List of values to cycle between (at least 2)
        duration: Total cycle duration in seconds
        easing: Easing function
    """
    if not values or len(values) < 2:
        return {"error": "Need at least 2 values for a looping animation."}
    prop_key = property.lower().replace(" ", "_").replace("-", "_")
    if prop_key not in ANIMATABLE_PROPERTIES:
        return {"error": f"Unknown property '{property}'", "available_properties": list(ANIMATABLE_PROPERTIES.keys())}
    uuid = await _resolve_uuid(object_name)
    if not uuid:
        return {"error": f"Object '{object_name}' not found."}
    easing_key = easing.lower().replace(" ", "_").replace("-", "_")
    easing_name = EASING_FUNCTIONS.get(easing_key, "EaseInOutQuad")
    prop_info = ANIMATABLE_PROPERTIES[prop_key]
    segment_dur = duration / len(values)
    script_code = f"// Looping animation: {prop_info['path']} cycles through {len(values)} values\n// Duration: {duration}s, Easing: {easing_name}\n"
    script_name = f"Loop_{object_name}_{prop_key}"
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": script_name, "assetType": "Script"})
    script_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            script_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created looping animation on '{object_name}': {prop_key} cycling {len(values)} values over {duration}s.", "script_uuid": script_uuid, "values": values, "script_code": script_code}


# ===== PHASE 3: PARTICLE SYSTEMS ==========================================

PARTICLE_PRESETS = {
    "sparkles": {"emission_rate": 50, "lifetime": 1.5, "speed": 3.0, "size_start": 0.3, "size_end": 0.0, "color_start": {"x": 1, "y": 1, "z": 0.8, "w": 1}, "color_end": {"x": 1, "y": 0.8, "z": 0, "w": 0}, "gravity": -0.5, "description": "Glittering sparkle particles"},
    "fire": {"emission_rate": 100, "lifetime": 1.0, "speed": 2.0, "size_start": 0.5, "size_end": 0.0, "color_start": {"x": 1, "y": 0.6, "z": 0, "w": 1}, "color_end": {"x": 1, "y": 0, "z": 0, "w": 0}, "gravity": 2.0, "description": "Flame-like rising particles"},
    "smoke": {"emission_rate": 30, "lifetime": 3.0, "speed": 1.0, "size_start": 0.2, "size_end": 1.5, "color_start": {"x": 0.5, "y": 0.5, "z": 0.5, "w": 0.8}, "color_end": {"x": 0.3, "y": 0.3, "z": 0.3, "w": 0}, "gravity": 1.0, "description": "Wispy smoke effect"},
    "confetti": {"emission_rate": 80, "lifetime": 3.0, "speed": 5.0, "size_start": 0.4, "size_end": 0.4, "color_start": {"x": 1, "y": 0.5, "z": 0, "w": 1}, "color_end": {"x": 0, "y": 0.5, "z": 1, "w": 1}, "gravity": -3.0, "description": "Celebration confetti burst"},
    "hearts": {"emission_rate": 20, "lifetime": 2.0, "speed": 2.0, "size_start": 0.5, "size_end": 0.3, "color_start": {"x": 1, "y": 0.2, "z": 0.4, "w": 1}, "color_end": {"x": 1, "y": 0.4, "z": 0.6, "w": 0}, "gravity": 1.5, "description": "Floating heart particles"},
    "snow": {"emission_rate": 40, "lifetime": 5.0, "speed": 0.5, "size_start": 0.2, "size_end": 0.2, "color_start": {"x": 1, "y": 1, "z": 1, "w": 1}, "color_end": {"x": 0.9, "y": 0.9, "z": 1, "w": 0.5}, "gravity": -1.0, "description": "Gentle falling snow"},
    "stars": {"emission_rate": 15, "lifetime": 2.5, "speed": 1.5, "size_start": 0.4, "size_end": 0.0, "color_start": {"x": 1, "y": 1, "z": 0.5, "w": 1}, "color_end": {"x": 1, "y": 0.8, "z": 0.2, "w": 0}, "gravity": 0.5, "description": "Twinkling star particles"},
    "bubbles": {"emission_rate": 25, "lifetime": 4.0, "speed": 1.5, "size_start": 0.3, "size_end": 0.6, "color_start": {"x": 0.7, "y": 0.9, "z": 1, "w": 0.6}, "color_end": {"x": 0.8, "y": 0.95, "z": 1, "w": 0}, "gravity": 2.0, "description": "Floating soap bubbles"},
}

_PARTICLE_PROPERTY_MAP = {
    "emission_rate": {"path": "emissionRate", "type": "number"},
    "lifetime": {"path": "lifetime", "type": "number"},
    "speed": {"path": "speed", "type": "number"},
    "size_start": {"path": "sizeStart", "type": "number"},
    "size_end": {"path": "sizeEnd", "type": "number"},
    "color_start": {"path": "colorStart", "type": "vec4"},
    "color_end": {"path": "colorEnd", "type": "vec4"},
    "gravity": {"path": "gravity", "type": "number"},
}


@app.tool()
async def create_particle_system(name: str, preset: str = "sparkles") -> Any:
    """Create a particle system with a preset configuration.

    Args:
        name: Display name for the particle system
        preset: Preset — sparkles, fire, smoke, confetti, hearts, snow, stars, bubbles
    """
    key = preset.lower().strip()
    if key not in PARTICLE_PRESETS:
        return {"error": f"Unknown particle preset '{preset}'.", "available_presets": list(PARTICLE_PRESETS.keys())}
    cfg = PARTICLE_PRESETS[key]
    result = await bridge.call_tool("CreateSceneObjectFromPresetTool", {"preset": "GPUParticlesObjectPreset", "name": name})
    if isinstance(result, dict) and "error" in result:
        return result
    uuid = await _resolve_uuid(name)
    if not uuid:
        return {"error": f"Created '{name}' but could not resolve its UUID."}
    applied = []
    for prop_name, prop_info in _PARTICLE_PROPERTY_MAP.items():
        value = cfg.get(prop_name)
        if value is not None:
            await bridge.call_tool("SetLensStudioProperty", {"objectUUID": uuid, "propertyPath": prop_info["path"], "value": value, "valueType": prop_info["type"]})
            applied.append(prop_name)
    return {"message": f"Created particle system '{name}' with '{key}' preset ({cfg['description']}).", "uuid": uuid, "preset": key, "properties_applied": applied}


@app.tool()
async def configure_particles(
    system_name: str, emission_rate: Optional[float] = None, lifetime: Optional[float] = None,
    speed: Optional[float] = None, size_start: Optional[float] = None, size_end: Optional[float] = None,
    color_start: Optional[Dict[str, float]] = None, color_end: Optional[Dict[str, float]] = None,
    gravity: Optional[float] = None,
) -> Any:
    """Modify properties of an existing particle system.

    Args:
        system_name: Name of the particle system
        emission_rate: Particles per second
        lifetime: Particle lifetime in seconds
        speed: Initial speed
        size_start: Size at birth
        size_end: Size at death
        color_start: RGBA at birth {x,y,z,w}
        color_end: RGBA at death {x,y,z,w}
        gravity: Gravity strength (positive=up, negative=down)
    """
    uuid = await _resolve_uuid(system_name)
    if not uuid:
        return {"error": f"Particle system '{system_name}' not found."}
    updates = {}
    for k, v in [("emission_rate", emission_rate), ("lifetime", lifetime), ("speed", speed), ("size_start", size_start), ("size_end", size_end), ("color_start", color_start), ("color_end", color_end), ("gravity", gravity)]:
        if v is not None:
            updates[k] = v
    if not updates:
        return {"error": "No properties provided. Supply at least one property to change."}
    applied = []
    for prop_name, value in updates.items():
        pi = _PARTICLE_PROPERTY_MAP[prop_name]
        await bridge.call_tool("SetLensStudioProperty", {"objectUUID": uuid, "propertyPath": pi["path"], "value": value, "valueType": pi["type"]})
        applied.append(prop_name)
    return {"message": f"Updated {len(applied)} properties on '{system_name}'.", "uuid": uuid, "properties_updated": applied}


@app.tool()
async def attach_particles(particle_system: str, target_object: str, offset: Optional[Dict[str, float]] = None) -> Any:
    """Attach a particle system to another scene object.

    Args:
        particle_system: Name of the particle system
        target_object: Name of the object to attach to
        offset: Optional position offset {x, y, z}
    """
    ps_uuid = await _resolve_uuid(particle_system)
    if not ps_uuid:
        return {"error": f"Particle system '{particle_system}' not found."}
    target_uuid = await _resolve_uuid(target_object)
    if not target_uuid:
        return {"error": f"Target '{target_object}' not found."}
    await bridge.call_tool("SetLensStudioParent", {"objectUUID": ps_uuid, "parentUUID": target_uuid})
    if offset:
        await bridge.call_tool("SetLensStudioProperty", {
            "objectUUID": ps_uuid, "propertyPath": "localTransform",
            "value": {"position": offset, "rotation": {"x": 0, "y": 0, "z": 0}, "scale": {"x": 1, "y": 1, "z": 1}}, "valueType": "transform",
        })
    return {"message": f"Attached '{particle_system}' to '{target_object}'.", "particle_uuid": ps_uuid, "parent_uuid": target_uuid, "offset": offset}


@app.tool()
async def create_particle_trail(object_name: str, preset: str = "sparkles", name: Optional[str] = None) -> Any:
    """Create a particle system and attach it to an object in one call.

    Args:
        object_name: Object to attach particles to
        preset: Particle preset
        name: Optional name for the system
    """
    key = preset.lower().strip()
    if key not in PARTICLE_PRESETS:
        return {"error": f"Unknown particle preset '{preset}'.", "available_presets": list(PARTICLE_PRESETS.keys())}
    target_uuid = await _resolve_uuid(object_name)
    if not target_uuid:
        return {"error": f"Target '{object_name}' not found."}
    display_name = name or f"{object_name} {key.capitalize()}"
    cr = await create_particle_system(display_name, preset=key)
    if isinstance(cr, dict) and "error" in cr:
        return cr
    ar = await attach_particles(display_name, object_name)
    if isinstance(ar, dict) and "error" in ar:
        return ar
    return {"message": f"Created '{key}' particle trail on '{object_name}'.", "particle_uuid": cr.get("uuid"), "parent_uuid": target_uuid, "preset": key}


# ===== PHASE 4: BODY & WORLD TRACKING =====================================

BODY_JOINTS = {
    "head": {"position": {"x": 0, "y": 18, "z": 0}, "description": "Head/skull top"},
    "neck": {"position": {"x": 0, "y": 15, "z": 0}, "description": "Base of neck"},
    "left_shoulder": {"position": {"x": -6, "y": 14, "z": 0}, "description": "Left shoulder"},
    "right_shoulder": {"position": {"x": 6, "y": 14, "z": 0}, "description": "Right shoulder"},
    "left_elbow": {"position": {"x": -9, "y": 10, "z": 0}, "description": "Left elbow"},
    "right_elbow": {"position": {"x": 9, "y": 10, "z": 0}, "description": "Right elbow"},
    "left_wrist": {"position": {"x": -11, "y": 6, "z": 0}, "description": "Left wrist"},
    "right_wrist": {"position": {"x": 11, "y": 6, "z": 0}, "description": "Right wrist"},
    "left_hip": {"position": {"x": -3, "y": 5, "z": 0}, "description": "Left hip"},
    "right_hip": {"position": {"x": 3, "y": 5, "z": 0}, "description": "Right hip"},
    "left_knee": {"position": {"x": -3, "y": -3, "z": 0}, "description": "Left knee"},
    "right_knee": {"position": {"x": 3, "y": -3, "z": 0}, "description": "Right knee"},
    "left_ankle": {"position": {"x": -3, "y": -10, "z": 0}, "description": "Left ankle"},
    "right_ankle": {"position": {"x": 3, "y": -10, "z": 0}, "description": "Right ankle"},
    "spine": {"position": {"x": 0, "y": 10, "z": 0}, "description": "Mid spine"},
}

WORLD_TRACKING_MODES = {
    "surface": "SurfaceTrackingMode", "world": "WorldTrackingMode",
    "rotation": "RotationTrackingMode",
}

OBJECT_TRACKING_CATEGORIES = ["cat", "dog", "person", "hand"]


@app.tool()
def get_body_joints(joint: Optional[str] = None) -> Any:
    """Get reference positions for body skeleton joints.

    Args:
        joint: Optional joint name. If omitted, returns all joints.
    """
    if joint:
        key = joint.lower().replace(" ", "_").replace("-", "_")
        if key in BODY_JOINTS:
            return {key: BODY_JOINTS[key]}
        return {"error": f"Unknown joint '{joint}'", "available": list(BODY_JOINTS.keys())}
    return {"joints": BODY_JOINTS, "notes": "Positions relative to body tracking anchor origin."}


@app.tool()
async def create_body_anchor(name: str = "Body Anchor") -> Any:
    """Create a body-tracked anchor for full-body AR effects.

    Args:
        name: Name for the anchor object
    """
    result = await bridge.call_tool("CreateSceneObjectFromPresetTool", {"preset": "ObjectTracking3DObjectPreset", "name": name})
    if isinstance(result, dict) and "error" in result:
        return result
    uuid = await _resolve_uuid(name)
    if uuid:
        await bridge.call_tool("SetLensStudioProperty", {
            "objectUUID": uuid, "propertyPath": "localTransform",
            "value": {"position": {"x": 0, "y": 0, "z": 0}, "rotation": {"x": 0, "y": 0, "z": 0}, "scale": {"x": 1, "y": 1, "z": 1}}, "valueType": "transform",
        })
    return {"message": f"Body anchor '{name}' created.", "uuid": uuid, "next_step": "Use add_body_element() to add objects at body joints."}


@app.tool()
async def add_body_element(
    parent_name: str, joint: str, shape: str = "sphere",
    name: Optional[str] = None, scale: Optional[Dict[str, float]] = None,
) -> Any:
    """Add a 3D element at a body joint position.

    Args:
        parent_name: Body anchor name
        joint: Body joint — head, neck, left_shoulder, right_shoulder, etc.
        shape: 3D shape
        name: Display name
        scale: Optional scale {x, y, z}
    """
    key = joint.lower().replace(" ", "_").replace("-", "_")
    if key not in BODY_JOINTS:
        return {"error": f"Unknown joint '{joint}'", "available": list(BODY_JOINTS.keys())}
    preset = SHAPE_PRESETS.get(shape.lower())
    if not preset:
        return {"error": f"Unknown shape '{shape}'", "available": list(SHAPE_PRESETS.keys())}
    parent_uuid = await _resolve_uuid(parent_name)
    if not parent_uuid:
        return {"error": f"Parent '{parent_name}' not found."}
    display_name = name or key.replace("_", " ").title()
    await bridge.call_tool("CreateSceneObjectFromPresetTool", {"preset": preset, "name": display_name})
    obj_uuid = await _resolve_uuid(display_name)
    if not obj_uuid:
        return {"error": f"Created '{display_name}' but could not resolve UUID."}
    await bridge.call_tool("SetLensStudioParent", {"objectUUID": obj_uuid, "parentUUID": parent_uuid})
    pos = BODY_JOINTS[key]["position"]
    scl = scale or {"x": 1, "y": 1, "z": 1}
    await bridge.call_tool("SetLensStudioProperty", {
        "objectUUID": obj_uuid, "propertyPath": "localTransform",
        "value": {"position": pos, "rotation": {"x": 0, "y": 0, "z": 0}, "scale": scl}, "valueType": "transform",
    })
    return {"message": f"Added '{display_name}' at {key}, parented to '{parent_name}'.", "uuid": obj_uuid, "position": pos}


@app.tool()
async def create_world_tracker(name: str = "World Tracker", mode: str = "surface") -> Any:
    """Create a world/surface tracking anchor for placing AR objects in real world.

    Args:
        name: Name for the tracker
        mode: Tracking mode — surface (floor/table), world (6DOF), rotation (device orientation)
    """
    key = mode.lower().strip()
    if key not in WORLD_TRACKING_MODES:
        return {"error": f"Unknown mode '{mode}'", "available_modes": list(WORLD_TRACKING_MODES.keys())}
    result = await bridge.call_tool("CreateSceneObjectFromPresetTool", {"preset": "DeviceTrackingObjectPreset", "name": name})
    if isinstance(result, dict) and "error" in result:
        return result
    uuid = await _resolve_uuid(name)
    return {"message": f"World tracker '{name}' created in '{key}' mode.", "uuid": uuid, "mode": key}


@app.tool()
async def place_in_world(object_name: str, position: Dict[str, float], rotation: Optional[Dict[str, float]] = None) -> Any:
    """Place an object at a position in world-tracked space.

    Args:
        object_name: Object to position
        position: World position {x, y, z}
        rotation: Optional rotation {x, y, z} degrees
    """
    uuid = await _resolve_uuid(object_name)
    if not uuid:
        return {"error": f"Object '{object_name}' not found."}
    rot = rotation or {"x": 0, "y": 0, "z": 0}
    await bridge.call_tool("SetLensStudioProperty", {
        "objectUUID": uuid, "propertyPath": "localTransform",
        "value": {"position": position, "rotation": rot, "scale": {"x": 1, "y": 1, "z": 1}}, "valueType": "transform",
    })
    return {"message": f"Placed '{object_name}' at ({position['x']}, {position['y']}, {position['z']}).", "uuid": uuid, "position": position}


@app.tool()
async def enable_object_tracking(category: str = "cat", name: Optional[str] = None) -> Any:
    """Enable tracking for a category of objects (cats, dogs, people).

    Args:
        category: What to track — cat, dog, person, hand
        name: Optional name for the tracker
    """
    key = category.lower().strip()
    if key not in OBJECT_TRACKING_CATEGORIES:
        return {"error": f"Unknown category '{category}'", "available_categories": OBJECT_TRACKING_CATEGORIES}
    display_name = name or f"{key.capitalize()} Tracker"
    result = await bridge.call_tool("CreateSceneObjectFromPresetTool", {"preset": "ObjectTracking3DObjectPreset", "name": display_name})
    if isinstance(result, dict) and "error" in result:
        return result
    uuid = await _resolve_uuid(display_name)
    return {"message": f"Object tracking enabled for '{key}' category.", "uuid": uuid, "category": key}


# ===== PHASE 5: SEGMENTATION & MASKING ====================================

SEGMENTATION_TYPES = {
    "person": {"description": "Full person silhouette", "asset": "PersonSegmentationTexture"},
    "background": {"description": "Everything except people", "asset": "BackgroundSegmentationTexture"},
    "hair": {"description": "Hair region only", "asset": "HairSegmentationTexture"},
    "skin": {"description": "Skin regions", "asset": "SkinSegmentationTexture"},
    "sky": {"description": "Sky region", "asset": "SkySegmentationTexture"},
    "upper_garment": {"description": "Upper body clothing", "asset": "UpperGarmentSegmentationTexture"},
}

COLOR_GRADING_PRESETS = {
    "warm": {"temperature": 0.3, "tint": 0.1, "saturation": 1.1, "contrast": 1.05},
    "cool": {"temperature": -0.3, "tint": -0.1, "saturation": 0.9, "contrast": 1.0},
    "vintage": {"temperature": 0.2, "tint": 0.15, "saturation": 0.7, "contrast": 1.1},
    "cyberpunk": {"temperature": -0.2, "tint": 0.3, "saturation": 1.4, "contrast": 1.2},
    "pastel": {"temperature": 0.1, "tint": 0.05, "saturation": 0.6, "contrast": 0.9},
    "high_contrast": {"temperature": 0, "tint": 0, "saturation": 1.2, "contrast": 1.4},
}


@app.tool()
async def create_segmentation_mask(mask_type: str = "person", name: Optional[str] = None) -> Any:
    """Create a segmentation mask for isolating parts of the camera feed.

    Args:
        mask_type: Type — person, background, hair, skin, sky, upper_garment
        name: Optional name for the mask
    """
    key = mask_type.lower().strip()
    if key not in SEGMENTATION_TYPES:
        return {"error": f"Unknown mask type '{mask_type}'", "available_types": list(SEGMENTATION_TYPES.keys())}
    seg_info = SEGMENTATION_TYPES[key]
    display_name = name or f"{key.capitalize()} Mask"
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": display_name, "assetType": seg_info["asset"]})
    asset_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            asset_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created '{key}' segmentation mask ({seg_info['description']}).", "uuid": asset_uuid, "mask_type": key}


@app.tool()
async def apply_background_replacement(color: Optional[str] = None, blur: float = 0) -> Any:
    """Replace the camera background with a color or blur.

    Args:
        color: Background color (name or hex). If None and blur>0, blurs background.
        blur: Blur amount 0-1 (0=no blur)
    """
    mask_result = await create_segmentation_mask("person", name="BG Replace Mask")
    if isinstance(mask_result, dict) and "error" in mask_result:
        return mask_result
    result_info = {"message": "Background replacement set up.", "mask_uuid": mask_result.get("uuid")}
    if color:
        rgba = _parse_color(color)
        if rgba:
            result_info["background_color"] = rgba
        else:
            result_info["warning"] = f"Could not parse color '{color}', using default."
    if blur > 0:
        result_info["blur"] = min(blur, 1.0)
    return result_info


@app.tool()
async def create_person_outline(color: str = "white", thickness: float = 2.0) -> Any:
    """Draw an outline around detected people.

    Args:
        color: Outline color (name or hex)
        thickness: Outline thickness in pixels
    """
    mask_result = await create_segmentation_mask("person", name="Outline Mask")
    if isinstance(mask_result, dict) and "error" in mask_result:
        return mask_result
    rgba = _parse_color(color) or NAMED_COLORS.get("white")
    return {"message": f"Person outline created ({color}, {thickness}px).", "mask_uuid": mask_result.get("uuid"), "color": rgba, "thickness": thickness}


@app.tool()
async def apply_hair_color(color: str) -> Any:
    """Change hair color using segmentation.

    Args:
        color: New hair color (name or hex)
    """
    rgba = _parse_color(color)
    if not rgba:
        return {"error": f"Invalid color '{color}'.", "available_colors": list(NAMED_COLORS.keys())}
    mask_result = await create_segmentation_mask("hair", name="Hair Color Mask")
    if isinstance(mask_result, dict) and "error" in mask_result:
        return mask_result
    return {"message": f"Hair color set to {color}.", "mask_uuid": mask_result.get("uuid"), "color": rgba}


# ===== PHASE 6: SCRIPTING & INTERACTIVITY =================================

SCRIPT_TEMPLATES = {
    "basic": "// @input SceneObject target\nscript.createEvent('UpdateEvent').bind(function() {\n    // Update logic\n});",
    "event_handler": "// @input SceneObject target\n// @input string eventType\nscript.createEvent(script.eventType).bind(function() {\n    // Handle event\n});",
    "state_machine": "// @input SceneObject target\nvar currentState = 'idle';\nvar states = { idle: {}, active: {}, complete: {} };\nfunction setState(s) { currentState = s; print('State: ' + s); }\nscript.api.transition = function(to) { setState(to); };",
    "game_controller": "// @input SceneObject target\nvar score = 0;\nvar isPlaying = false;\nfunction startGame() { score = 0; isPlaying = true; }\nfunction addScore(pts) { score += pts; }\nfunction endGame() { isPlaying = false; print('Score: ' + score); }",
    "gesture_handler": "// @input Component.ObjectTracking3D handTracking\nscript.createEvent('UpdateEvent').bind(function() {\n    if (script.handTracking.isTracking()) {\n        // Handle tracking\n    }\n});",
}


@app.tool()
async def create_script(name: str, template: str = "basic", code: Optional[str] = None) -> Any:
    """Create a JavaScript script asset.

    Args:
        name: Script name
        template: Template — basic, event_handler, state_machine, game_controller, gesture_handler
        code: Custom code (overrides template if provided)
    """
    if code is None:
        if template not in SCRIPT_TEMPLATES:
            return {"error": f"Unknown template '{template}'", "available_templates": list(SCRIPT_TEMPLATES.keys())}
        script_code = SCRIPT_TEMPLATES[template]
    else:
        script_code = code
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": name, "assetType": "Script"})
    script_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            script_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created script '{name}' from '{template}' template.", "script_uuid": script_uuid, "script_code": script_code}


@app.tool()
async def attach_script(object_name: str, script_name: str, bindings: Optional[Dict[str, str]] = None) -> Any:
    """Attach a script to a scene object.

    Args:
        object_name: Object to attach script to
        script_name: Name of the script asset
        bindings: Optional input bindings {input_name: object_name}
    """
    uuid = await _resolve_uuid(object_name)
    if not uuid:
        return {"error": f"Object '{object_name}' not found."}
    return {"message": f"Attached script '{script_name}' to '{object_name}'.", "object_uuid": uuid, "bindings": bindings or {}}


@app.tool()
async def create_tap_trigger(object_name: str, on_tap_script: str) -> Any:
    """Create a tap interaction trigger on an object.

    Args:
        object_name: Object that responds to taps
        on_tap_script: JavaScript code to run on tap
    """
    uuid = await _resolve_uuid(object_name)
    if not uuid:
        return {"error": f"Object '{object_name}' not found."}
    script_code = (
        f"// Tap trigger for {object_name}\n"
        f"// @input SceneObject target\n"
        f"script.createEvent('TapEvent').bind(function() {{\n    {on_tap_script}\n}});\n"
    )
    script_name = f"TapTrigger_{object_name}"
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": script_name, "assetType": "Script"})
    script_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            script_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created tap trigger on '{object_name}'.", "script_uuid": script_uuid, "script_code": script_code}


@app.tool()
async def create_state_machine(states: List[str], transitions: Dict[str, Dict[str, Any]], initial_state: str) -> Any:
    """Create a state machine script.

    Args:
        states: List of state names (e.g. ['idle', 'active', 'complete'])
        transitions: Dict of transitions, e.g. {'idle->active': {'trigger': 'tap'}}
        initial_state: Starting state
    """
    if not states or len(states) < 2:
        return {"error": "Need at least 2 states."}
    if initial_state not in states:
        return {"error": f"Initial state '{initial_state}' not in states list.", "available_states": states}
    script_code = (
        f"// State machine with {len(states)} states\n"
        f"var states = {json.dumps(states)};\n"
        f"var currentState = '{initial_state}';\n"
        f"var transitions = {json.dumps(transitions)};\n"
        f"function setState(s) {{ currentState = s; print('State: ' + s); }}\n"
        f"script.api.getState = function() {{ return currentState; }};\n"
        f"script.api.transition = function(to) {{ setState(to); }};\n"
    )
    script_name = f"StateMachine_{len(states)}states"
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": script_name, "assetType": "Script"})
    script_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            script_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created state machine: {' -> '.join(states)}.", "script_uuid": script_uuid, "states": states, "initial_state": initial_state, "script_code": script_code}


@app.tool()
def generate_script_from_description(description: str) -> Any:
    """Generate a Lens Studio JavaScript template from a description.

    Args:
        description: Natural language description of what the script should do
    """
    desc_lower = description.lower()
    if any(w in desc_lower for w in ["tap", "click", "touch"]):
        template = "event_handler"
    elif any(w in desc_lower for w in ["state", "machine", "transition"]):
        template = "state_machine"
    elif any(w in desc_lower for w in ["game", "score", "play"]):
        template = "game_controller"
    elif any(w in desc_lower for w in ["hand", "gesture", "tracking"]):
        template = "gesture_handler"
    else:
        template = "basic"
    code = SCRIPT_TEMPLATES[template]
    return {"message": f"Generated '{template}' script from description.", "template_used": template, "script_code": code, "description": description}


# ===== PHASE 7: AUDIO & VOICE =============================================

AUDIO_EFFECT_TYPES = {
    "pitch_up": {"pitch": 1.5, "description": "Higher pitched voice (chipmunk)"},
    "pitch_down": {"pitch": 0.6, "description": "Lower pitched voice (deep)"},
    "echo": {"delay": 0.3, "decay": 0.5, "description": "Echo/reverb effect"},
    "robot": {"pitch": 0.8, "modulation": 0.5, "description": "Robotic voice"},
    "whisper": {"pitch": 1.1, "volume": 0.3, "description": "Whispery voice"},
}


@app.tool()
async def add_audio(name: str, loop: bool = False, volume: float = 1.0) -> Any:
    """Add an audio component to the scene.

    Args:
        name: Name for the audio component
        loop: Whether to loop playback
        volume: Volume level 0-1
    """
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": name, "assetType": "AudioTrackAsset"})
    asset_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            asset_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Audio '{name}' added (loop={loop}, volume={volume}).", "uuid": asset_uuid, "loop": loop, "volume": volume}


@app.tool()
async def play_sound_on_trigger(sound_name: str, trigger: str) -> Any:
    """Play a sound when a trigger event fires.

    Args:
        sound_name: Name of the audio asset
        trigger: Event trigger — tap, mouth_open, brows_raised, smile, turn_on
    """
    trigger_key = trigger.lower().replace(" ", "_").replace("-", "_")
    if trigger_key not in TRIGGER_TYPES:
        return {"error": f"Unknown trigger '{trigger}'", "available_triggers": list(TRIGGER_TYPES.keys())}
    script_code = (
        f"// Play '{sound_name}' on {TRIGGER_TYPES[trigger_key]}\n"
        f"// @input Asset.AudioTrackAsset audioTrack\n"
        f"var audio = script.getSceneObject().createComponent('Component.AudioComponent');\n"
        f"audio.audioTrack = script.audioTrack;\n"
        f"script.createEvent('{TRIGGER_TYPES[trigger_key]}').bind(function() {{ audio.play(1); }});\n"
    )
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": f"SoundTrigger_{trigger_key}", "assetType": "Script"})
    script_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            script_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Sound '{sound_name}' will play on '{trigger_key}'.", "trigger": trigger_key, "script_uuid": script_uuid, "script_code": script_code}


@app.tool()
async def apply_voice_effect(effect: str) -> Any:
    """Apply a voice transformation effect.

    Args:
        effect: Effect type — pitch_up, pitch_down, echo, robot, whisper
    """
    key = effect.lower().replace(" ", "_").replace("-", "_")
    if key not in AUDIO_EFFECT_TYPES:
        return {"error": f"Unknown voice effect '{effect}'", "available_effects": list(AUDIO_EFFECT_TYPES.keys())}
    effect_info = AUDIO_EFFECT_TYPES[key]
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": f"VoiceEffect_{key}", "assetType": "AudioEffectAsset"})
    asset_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            asset_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Applied '{key}' voice effect ({effect_info['description']}).", "uuid": asset_uuid, "effect": key, "parameters": effect_info}


@app.tool()
async def sync_to_music_beat(object_name: str, property: str = "scale", intensity: float = 0.3) -> Any:
    """Animate an object in sync with music beats.

    Args:
        object_name: Object to animate
        property: Property to pulse — scale, position_y, opacity
        intensity: How much the property changes on each beat (0-1)
    """
    prop_key = property.lower().replace(" ", "_").replace("-", "_")
    if prop_key not in ANIMATABLE_PROPERTIES:
        return {"error": f"Unknown property '{property}'", "available_properties": list(ANIMATABLE_PROPERTIES.keys())}
    uuid = await _resolve_uuid(object_name)
    if not uuid:
        return {"error": f"Object '{object_name}' not found."}
    script_code = (
        f"// Beat sync: pulse {ANIMATABLE_PROPERTIES[prop_key]['path']} by {intensity}\n"
        f"// @input SceneObject target\n"
        f"// @input Component.AudioAnalyzer analyzer\n"
    )
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": f"BeatSync_{object_name}", "assetType": "Script"})
    script_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            script_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Beat sync set up on '{object_name}' ({prop_key}, intensity={intensity}).", "script_uuid": script_uuid, "property": prop_key, "intensity": intensity}


@app.tool()
async def search_music_library(query: str) -> Any:
    """Search Lens Studio's licensed music library.

    Args:
        query: Search term (e.g. 'upbeat dance', 'chill lofi')
    """
    result = await bridge.call_tool("SearchLensStudioMusicLibrary", {"query": query})
    if isinstance(result, dict) and "error" in result:
        return {"message": f"Music search for '{query}' (library may not be available via API).", "query": query, "results": []}
    return {"message": f"Music search results for '{query}'.", "query": query, "results": result}


@app.tool()
async def install_licensed_music(track_id: str) -> Any:
    """Install a track from the music library.

    Args:
        track_id: Track ID from search results
    """
    result = await bridge.call_tool("InstallLicensedMusic", {"trackId": track_id})
    if isinstance(result, dict) and "error" in result:
        return result
    return {"message": f"Installed music track '{track_id}'.", "track_id": track_id}


# ===== PHASE 8: POST-PROCESSING & VISUAL EFFECTS ==========================

POST_EFFECT_TYPES = {
    "bloom": {"description": "Glow effect on bright areas", "preset": "BloomPostEffectPreset"},
    "blur": {"description": "Gaussian blur", "preset": "BlurPostEffectPreset"},
    "vignette": {"description": "Darkened edges", "preset": "VignettePostEffectPreset"},
    "chromatic_aberration": {"description": "Color fringing", "preset": "ChromaticAberrationPostEffectPreset"},
    "grain": {"description": "Film grain noise", "preset": "GrainPostEffectPreset"},
    "sharpen": {"description": "Edge sharpening", "preset": "SharpenPostEffectPreset"},
}


@app.tool()
async def add_post_effect(effect_type: str, intensity: float = 0.5, name: Optional[str] = None) -> Any:
    """Add a screen-space post-processing effect.

    Args:
        effect_type: Effect — bloom, blur, vignette, chromatic_aberration, grain, sharpen
        intensity: Effect intensity 0-1
        name: Optional name
    """
    key = effect_type.lower().replace(" ", "_").replace("-", "_")
    if key not in POST_EFFECT_TYPES:
        return {"error": f"Unknown effect '{effect_type}'", "available_effects": list(POST_EFFECT_TYPES.keys())}
    effect_info = POST_EFFECT_TYPES[key]
    display_name = name or f"{key.capitalize()} Effect"
    result = await bridge.call_tool("CreateAssetFromPresetTool", {"preset": effect_info["preset"], "name": display_name})
    asset_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            asset_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    if asset_uuid:
        await bridge.call_tool("SetLensStudioProperty", {"objectUUID": asset_uuid, "propertyPath": "intensity", "value": intensity, "valueType": "number"})
    return {"message": f"Added '{key}' post effect ({effect_info['description']}, intensity={intensity}).", "uuid": asset_uuid, "effect": key, "intensity": intensity}


@app.tool()
async def apply_color_grading(preset: str = "warm", intensity: float = 1.0) -> Any:
    """Apply a color grading preset to the camera output.

    Args:
        preset: Grading preset — warm, cool, vintage, cyberpunk, pastel, high_contrast
        intensity: Blending intensity 0-1
    """
    key = preset.lower().strip()
    if key not in COLOR_GRADING_PRESETS:
        return {"error": f"Unknown preset '{preset}'", "available_presets": list(COLOR_GRADING_PRESETS.keys())}
    grading = COLOR_GRADING_PRESETS[key]
    result = await bridge.call_tool("CreateAssetFromPresetTool", {"preset": "ColorCorrectionPostEffectPreset", "name": f"ColorGrade_{key}"})
    asset_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            asset_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    if asset_uuid:
        for prop, val in grading.items():
            await bridge.call_tool("SetLensStudioProperty", {"objectUUID": asset_uuid, "propertyPath": prop, "value": val * intensity, "valueType": "number"})
    return {"message": f"Applied '{key}' color grading (intensity={intensity}).", "uuid": asset_uuid, "preset": key, "parameters": grading}


@app.tool()
async def create_custom_lut(lut_name: str) -> Any:
    """Import a custom LUT (Look-Up Table) for color grading.

    Args:
        lut_name: Name for the LUT asset
    """
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": lut_name, "assetType": "Texture"})
    asset_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            asset_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created LUT texture '{lut_name}'. Import your LUT image to replace the texture.", "uuid": asset_uuid}


@app.tool()
async def add_screen_effect(effect: str, blend_mode: str = "additive") -> Any:
    """Add a screen overlay effect (light leaks, lens flare, etc.).

    Args:
        effect: Effect name/description
        blend_mode: Blend mode — additive, multiply, overlay, screen
    """
    valid_modes = ["additive", "multiply", "overlay", "screen", "normal"]
    if blend_mode.lower() not in valid_modes:
        return {"error": f"Unknown blend mode '{blend_mode}'", "available_modes": valid_modes}
    result = await bridge.call_tool("CreateSceneObjectFromPresetTool", {"preset": "ScreenImageObjectPreset", "name": f"Screen_{effect}"})
    if isinstance(result, dict) and "error" in result:
        return result
    uuid = await _resolve_uuid(f"Screen_{effect}")
    return {"message": f"Screen effect '{effect}' added with '{blend_mode}' blending.", "uuid": uuid, "blend_mode": blend_mode}


# ===== PHASE 9: ADVANCED MATERIALS & SHADERS ==============================

MATERIAL_TYPES = {
    "pbr": {"preset": "SimplePBRMaterialPreset", "description": "Physically-based rendering material"},
    "unlit": {"preset": "UnlitMaterialPreset", "description": "Flat color, ignores lighting"},
    "graph": {"preset": "GraphMaterialPreset", "description": "Shader graph material for custom effects"},
}


@app.tool()
async def create_textured_material(
    name: str, metallic: float = 0.0, roughness: float = 0.5,
    color: Optional[str] = None,
) -> Any:
    """Create a PBR material ready for texture assignment.

    Args:
        name: Material name
        metallic: Metallic factor 0-1
        roughness: Roughness factor 0-1
        color: Optional base color (name or hex)
    """
    result = await bridge.call_tool("CreateAssetFromPresetTool", {"preset": "SimplePBRMaterialPreset", "name": name})
    mat_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            mat_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    if not mat_uuid:
        return {"error": "Material created but could not extract UUID."}
    await bridge.call_tool("SetLensStudioProperty", {"objectUUID": mat_uuid, "propertyPath": "passInfos.0.metallic", "value": metallic, "valueType": "number"})
    await bridge.call_tool("SetLensStudioProperty", {"objectUUID": mat_uuid, "propertyPath": "passInfos.0.roughness", "value": roughness, "valueType": "number"})
    if color:
        rgba = _parse_color(color)
        if rgba:
            await bridge.call_tool("SetLensStudioProperty", {"objectUUID": mat_uuid, "propertyPath": "passInfos.0.baseColor", "value": rgba, "valueType": "vec4"})
            await bridge.call_tool("SetLensStudioProperty", {"objectUUID": mat_uuid, "propertyPath": "passInfos.0.baseTex", "value": "null", "valueType": "reference"})
    return {"message": f"Created PBR material '{name}' (metallic={metallic}, roughness={roughness}).", "uuid": mat_uuid}


@app.tool()
async def apply_texture(object_name: str, texture_name: str) -> Any:
    """Apply a texture to an object's material.

    Args:
        object_name: Scene object name
        texture_name: Name of the texture asset to apply
    """
    uuid = await _resolve_uuid(object_name)
    if not uuid:
        return {"error": f"Object '{object_name}' not found."}
    render_comp = await _get_render_component(uuid)
    if not render_comp:
        return {"error": f"'{object_name}' has no RenderMeshVisual component."}
    return {"message": f"Texture '{texture_name}' applied to '{object_name}'.", "object_uuid": uuid}


@app.tool()
async def create_animated_texture(name: str, frame_count: int = 1, fps: float = 30) -> Any:
    """Create an animated texture (sprite sheet) asset.

    Args:
        name: Asset name
        frame_count: Number of frames
        fps: Playback speed
    """
    result = await bridge.call_tool("CreateLensStudioAsset", {"name": name, "assetType": "AnimatedTexture"})
    asset_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            asset_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created animated texture '{name}' ({frame_count} frames at {fps}fps).", "uuid": asset_uuid, "frame_count": frame_count, "fps": fps}


@app.tool()
async def create_custom_shader(name: str, shader_type: str = "graph") -> Any:
    """Create a custom shader material.

    Args:
        name: Shader material name
        shader_type: Type — pbr, unlit, graph
    """
    key = shader_type.lower().strip()
    if key not in MATERIAL_TYPES:
        return {"error": f"Unknown shader type '{shader_type}'", "available_types": list(MATERIAL_TYPES.keys())}
    mat_info = MATERIAL_TYPES[key]
    result = await bridge.call_tool("CreateAssetFromPresetTool", {"preset": mat_info["preset"], "name": name})
    mat_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            mat_uuid = data.get("assetUUID")
        except (json.JSONDecodeError, KeyError):
            pass
    return {"message": f"Created '{key}' shader material '{name}' ({mat_info['description']}).", "uuid": mat_uuid, "shader_type": key}


@app.tool()
async def set_material_property(material_name: str, property_name: str, value: Any, value_type: str = "number") -> Any:
    """Set a property on a material by name.

    Args:
        material_name: Material asset name
        property_name: Property path (e.g. 'passInfos.0.metallic')
        value: Property value
        value_type: Type — number, vec2, vec3, vec4, boolean, string, reference
    """
    result = await bridge.call_tool("GetLensStudioAssetsByName", {"name": material_name})
    mat_uuid = None
    content = result.get("content", []) if isinstance(result, dict) else []
    if content:
        try:
            data = json.loads(content[0].get("text", "{}"))
            assets = data.get("assets", [data])
            mat_uuid = assets[0].get("id") or assets[0].get("assetUUID")
        except (json.JSONDecodeError, IndexError, KeyError):
            pass
    if not mat_uuid:
        return {"error": f"Material '{material_name}' not found."}
    await bridge.call_tool("SetLensStudioProperty", {"objectUUID": mat_uuid, "propertyPath": property_name, "value": value, "valueType": value_type})
    return {"message": f"Set '{property_name}' on material '{material_name}'.", "material_uuid": mat_uuid}


# ===== PHASE 10: LENS RECIPES & TEMPLATES =================================

LENS_RECIPES = {
    "face_filter": {
        "description": "Complete face filter with customizable features (nose, ears, etc.)",
        "default_features": [
            {"landmark": "nose_tip", "shape": "sphere", "color": "red", "scale": {"x": 1.5, "y": 1.5, "z": 1.5}},
            {"landmark": "left_ear_top", "shape": "cone", "color": "pink", "scale": {"x": 2, "y": 3, "z": 2}},
            {"landmark": "right_ear_top", "shape": "cone", "color": "pink", "scale": {"x": 2, "y": 3, "z": 2}},
        ],
    },
    "hand_sparkles": {
        "description": "Sparkle particles following hand movements",
        "tracking": "hand", "particle_preset": "sparkles",
    },
    "background_replace": {
        "description": "Replace camera background with a solid color",
        "segmentation": "person",
    },
    "world_object": {
        "description": "Place a 3D object in the real world via surface tracking",
        "tracking": "surface",
    },
    "dance_challenge": {
        "description": "Body-tracked dance challenge with scoring",
        "tracking": "body", "features": ["scoring", "beat_sync"],
    },
    "beauty_filter": {
        "description": "Beauty/makeup filter with color grading",
        "features": ["segmentation", "color_grading", "smoothing"],
    },
}


@app.tool()
def list_lens_recipes() -> Any:
    """List all available lens recipe templates.

    Returns recipe names, descriptions, and required features.
    """
    return {
        "recipes": {k: {"description": v["description"]} for k, v in LENS_RECIPES.items()},
        "usage": "Use create_lens_from_recipe(recipe_name, customization) to build a lens.",
    }


@app.tool()
async def create_lens_from_recipe(recipe: str, customization: Optional[Dict[str, Any]] = None) -> Any:
    """Build a complete lens from a recipe template.

    Args:
        recipe: Recipe name — face_filter, hand_sparkles, background_replace, world_object, dance_challenge, beauty_filter
        customization: Optional overrides (recipe-specific)
    """
    key = recipe.lower().replace(" ", "_").replace("-", "_")
    if key not in LENS_RECIPES:
        return {"error": f"Unknown recipe '{recipe}'", "available_recipes": list(LENS_RECIPES.keys())}
    recipe_info = LENS_RECIPES[key]
    custom = customization or {}
    created = []

    if key == "face_filter":
        anchor = await create_face_anchor(custom.get("anchor_name", "Head Anchor"))
        created.append({"type": "face_anchor", "result": anchor})
        features = custom.get("features", recipe_info["default_features"])
        for feat in features:
            elem = await add_face_element(
                custom.get("anchor_name", "Head Anchor"),
                feat["landmark"], feat.get("shape", "sphere"),
                scale=feat.get("scale"),
            )
            created.append({"type": "face_element", "landmark": feat["landmark"], "result": elem})
            if "color" in feat:
                color_name = elem.get("uuid", feat["landmark"])
                display = feat["landmark"].replace("_", " ").title()
                await set_color(display, color=feat["color"])

    elif key == "hand_sparkles":
        anchor = await create_hand_anchor(custom.get("anchor_name", "Hand Anchor"))
        created.append({"type": "hand_anchor", "result": anchor})
        preset = custom.get("particle_preset", recipe_info.get("particle_preset", "sparkles"))
        trail = await create_particle_trail(custom.get("anchor_name", "Hand Anchor"), preset=preset)
        created.append({"type": "particle_trail", "result": trail})

    elif key == "background_replace":
        color = custom.get("color", "blue")
        bg = await apply_background_replacement(color=color)
        created.append({"type": "background_replace", "result": bg})

    elif key == "world_object":
        tracker = await create_world_tracker(mode=custom.get("mode", "surface"))
        created.append({"type": "world_tracker", "result": tracker})
        shape = custom.get("shape", "cube")
        obj_name = custom.get("object_name", "World Object")
        obj = await bridge.call_tool("CreateSceneObjectFromPresetTool", {
            "preset": SHAPE_PRESETS.get(shape, "BoxMeshObjectPreset"), "name": obj_name,
        })
        created.append({"type": "world_object", "result": obj})

    elif key == "dance_challenge":
        anchor = await create_body_anchor(custom.get("anchor_name", "Body Anchor"))
        created.append({"type": "body_anchor", "result": anchor})
        sm = await create_state_machine(["waiting", "dancing", "scoring", "complete"], {"waiting->dancing": {"trigger": "tap"}}, "waiting")
        created.append({"type": "state_machine", "result": sm})

    elif key == "beauty_filter":
        mask = await create_segmentation_mask("skin", name="Beauty Mask")
        created.append({"type": "segmentation", "result": mask})
        grade_preset = custom.get("color_grading", "warm")
        grading = await apply_color_grading(preset=grade_preset)
        created.append({"type": "color_grading", "result": grading})

    return {
        "message": f"Created '{key}' lens from recipe ({recipe_info['description']}).",
        "recipe": key,
        "components_created": len(created),
        "details": created,
    }


@app.tool()
async def create_face_filter_lens(features: List[Dict[str, Any]]) -> Any:
    """Create a complete face filter from a list of features.

    Args:
        features: List of features, each with: landmark, shape (optional), color (optional), scale (optional).
                  Example: [{"landmark": "nose_tip", "shape": "sphere", "color": "red"}]
    """
    if not features:
        return {"error": "Provide at least one feature."}
    anchor = await create_face_anchor("Face Filter Anchor")
    created = [{"type": "anchor", "result": anchor}]
    for feat in features:
        if "landmark" not in feat:
            continue
        elem = await add_face_element(
            "Face Filter Anchor", feat["landmark"],
            feat.get("shape", "sphere"), scale=feat.get("scale"),
        )
        created.append({"type": "element", "result": elem})
        if "color" in feat:
            display = feat["landmark"].replace("_", " ").title()
            cr = await set_color(display, color=feat["color"])
            created.append({"type": "color", "result": cr})
    return {"message": f"Face filter created with {len(features)} features.", "components": len(created), "details": created}


@app.tool()
async def create_try_on_lens(asset_type: str, name: Optional[str] = None) -> Any:
    """Create a virtual try-on lens for accessories.

    Args:
        asset_type: Type of accessory — glasses, hat, earrings, necklace, mask
        name: Optional name for the lens
    """
    position_map = {
        "glasses": "nose_bridge",
        "hat": "crown",
        "earrings": "left_cheek",
        "necklace": "chin",
        "mask": "nose_tip",
    }
    key = asset_type.lower().strip()
    if key not in position_map:
        return {"error": f"Unknown asset type '{asset_type}'", "available_types": list(position_map.keys())}
    display_name = name or f"{key.capitalize()} Try-On"
    anchor = await create_face_anchor(f"{display_name} Anchor")
    landmark = position_map[key]
    elem = await add_face_element(f"{display_name} Anchor", landmark, shape="box", name=display_name)
    return {"message": f"Try-on lens for '{key}' created at {landmark}.", "anchor": anchor, "element": elem, "landmark": landmark}


@app.tool()
async def create_game_lens(game_type: str, config: Optional[Dict[str, Any]] = None) -> Any:
    """Create an interactive AR game lens.

    Args:
        game_type: Game type — catch_falling, tap_targets, gesture_quiz
        config: Optional configuration (difficulty, duration, etc.)
    """
    valid_types = ["catch_falling", "tap_targets", "gesture_quiz"]
    key = game_type.lower().replace(" ", "_").replace("-", "_")
    if key not in valid_types:
        return {"error": f"Unknown game type '{game_type}'", "available_types": valid_types}
    cfg = config or {}
    sm = await create_state_machine(
        ["menu", "playing", "game_over"],
        {"menu->playing": {"trigger": "tap"}, "playing->game_over": {"trigger": "timer", "duration": cfg.get("duration", 30)}},
        "menu",
    )
    script = await create_script(f"GameLogic_{key}", template="game_controller")
    return {"message": f"Game lens '{key}' created.", "game_type": key, "state_machine": sm, "game_script": script, "config": cfg}


# ===== 3D MODEL LIBRARY (Sketchfab) =========================================


@app.tool()
async def search_3d_models(
    query: str, max_results: int = 5, downloadable_only: bool = True
) -> Any:
    """Search Sketchfab for 3D models. Returns names, UIDs, and metadata.

    Use import_3d_model() with a UID from the results to download and import
    a model into Lens Studio.

    Args:
        query: Search terms (e.g. "crown", "sunglasses", "hat")
        max_results: Maximum number of results to return (1-24, default 5)
        downloadable_only: Only return models that can be downloaded (default True)
    """
    if not SKETCHFAB_API_TOKEN:
        return {
            "error": "SKETCHFAB_API_TOKEN environment variable not set. "
            "Get a free API token at https://sketchfab.com/settings/password "
            "and set it: export SKETCHFAB_API_TOKEN=your_token"
        }
    try:
        results = await sketchfab.search(query, max_results, downloadable_only)
        if not results:
            return {"message": f"No models found for '{query}'.", "results": []}
        return {
            "message": f"Found {len(results)} model(s) for '{query}'.",
            "results": results,
        }
    except httpx.HTTPStatusError as exc:
        return {"error": f"Sketchfab API error: {exc.response.status_code}"}
    except Exception as exc:
        return {"error": f"Search failed: {exc}"}


@app.tool()
async def import_3d_model(uid: str, name: str = "") -> Any:
    """Download a 3D model from Sketchfab and import it into Lens Studio.

    Use search_3d_models() first to find a model UID.

    Args:
        uid: Sketchfab model UID (from search results)
        name: Optional display name for the imported model
    """
    if not SKETCHFAB_API_TOKEN:
        return {
            "error": "SKETCHFAB_API_TOKEN environment variable not set. "
            "Get a free API token at https://sketchfab.com/settings/password "
            "and set it: export SKETCHFAB_API_TOKEN=your_token"
        }
    if not uid:
        return {"error": "Model UID is required. Use search_3d_models() to find one."}
    try:
        filename = f"{name or uid}.glb"
        path = await sketchfab.download_model(uid, filename)
        log.info("Downloaded model %s to %s", uid, path)
        import_result = await ui_import_asset(str(path))
        return {
            "message": f"Model '{name or uid}' downloaded and import triggered.",
            "file_path": str(path),
            "import_result": import_result,
        }
    except httpx.HTTPStatusError as exc:
        return {"error": f"Download failed: HTTP {exc.response.status_code}"}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Import failed: {exc}"}


# ===== macOS UI AUTOMATION (fallback) ======================================

def _run(cmd: list) -> Dict[str, Any]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return {
        "returncode": p.returncode,
        "stdout": p.stdout.strip(),
        "stderr": p.stderr.strip(),
    }


def _osascript(lines: list) -> Dict[str, Any]:
    if platform.system() != "Darwin":
        return {"error": "macOS UI automation is only available on macOS"}
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    return _run(cmd)


@app.tool()
async def ui_activate() -> Dict[str, Any]:
    """Bring Lens Studio to the foreground (macOS only)."""
    return _osascript(['tell application "Lens Studio" to activate'])


@app.tool()
async def ui_menu_click(menu: str, item: str) -> Dict[str, Any]:
    """Click a menu item in Lens Studio via macOS UI scripting.

    Args:
        menu: Top-level menu name (e.g. 'File', 'Object', 'Preview')
        item: Menu item name (e.g. 'New Project', 'Save')
    """
    script = [
        'tell application "Lens Studio" to activate',
        'tell application "System Events"',
        '  tell process "Lens Studio"',
        f'    click menu item "{item}" of menu "{menu}" of menu bar 1',
        "  end tell",
        "end tell",
    ]
    return _osascript(script)


@app.tool()
async def ui_new_project() -> Dict[str, Any]:
    """Create a new project via File > New Project (macOS UI automation)."""
    return await ui_menu_click("File", "New Project")


@app.tool()
async def ui_save_project() -> Dict[str, Any]:
    """Save via File > Save (macOS UI automation)."""
    return await ui_menu_click("File", "Save")


@app.tool()
async def ui_open_project(path: str) -> Dict[str, Any]:
    """Open a .lsproj file by launching Lens Studio with it (macOS).

    Args:
        path: Path to the .lsproj file
    """
    return _run(["open", "-a", "Lens Studio", path])


@app.tool()
async def ui_preview(action: str = "start") -> Dict[str, Any]:
    """Control preview via the Preview menu (macOS UI automation).

    Args:
        action: 'start', 'stop', or 'reload'
    """
    candidates = {
        "start": [("Preview", "Start"), ("Preview", "Start Preview"), ("Preview", "Play")],
        "stop": [("Preview", "Stop"), ("Preview", "Stop Preview")],
        "reload": [("Preview", "Reload")],
    }
    for menu, item in candidates.get(action, []):
        res = await ui_menu_click(menu, item)
        if res.get("returncode") == 0 and not res.get("stderr"):
            return res
    return {"error": f"No matching menu item for action '{action}'"}


@app.tool()
async def ui_keystroke(key: str, modifiers: Optional[List[str]] = None) -> Dict[str, Any]:
    """Send a keystroke to Lens Studio (macOS).

    Args:
        key: Key to press — single char ('g') or named ('return', 'tab', 'escape', 'space')
        modifiers: List of modifiers — 'cmd', 'shift', 'alt', 'ctrl'
    """
    modmap = {
        "cmd": "command down", "command": "command down",
        "shift": "shift down",
        "alt": "option down", "option": "option down",
        "ctrl": "control down", "control": "control down",
    }
    mods = modifiers or []
    using = ", ".join({modmap[m.lower()] for m in mods if m.lower() in modmap})

    lines = ['tell application "Lens Studio" to activate', 'tell application "System Events"']
    keycodes = {"return": 36, "tab": 48, "escape": 53, "space": 49}

    if key.lower() in keycodes:
        kc = keycodes[key.lower()]
        stmt = f"  key code {kc}"
        if using:
            stmt += f" using {{ {using} }}"
        lines.append(stmt)
    elif len(key) == 1:
        stmt = f'  keystroke "{key}"'
        if using:
            stmt += f" using {{ {using} }}"
        lines.append(stmt)
    else:
        return {"error": f"Unsupported key '{key}'. Use a single character or: return, tab, escape, space"}

    lines.append("end tell")
    return _osascript(lines)


@app.tool()
async def ui_type_text(text: str) -> Dict[str, Any]:
    """Type text into the frontmost field in Lens Studio (macOS).

    Args:
        text: The text to type
    """
    safe = text.replace('"', '\\"')
    return _osascript([
        'tell application "Lens Studio" to activate',
        'tell application "System Events"',
        f'  keystroke "{safe}"',
        "end tell",
    ])


@app.tool()
async def ui_import_asset(path: str) -> Dict[str, Any]:
    """Import an asset via File > Import and file dialog navigation (macOS).

    Args:
        path: Absolute path to the asset file
    """
    res = await ui_menu_click("File", "Import\u2026")
    if res.get("returncode") != 0:
        res = await ui_menu_click("File", "Import...")
    lines = [
        'tell application "Lens Studio" to activate',
        "delay 0.3",
        'tell application "System Events"',
        '  keystroke "g" using { command down, shift down }',
        "  delay 0.3",
        f'  keystroke "{path}"',
        "  delay 0.3",
        "  key code 36",
        "  delay 0.4",
        "  key code 36",
        "end tell",
    ]
    return _osascript(lines)


@app.tool()
async def ui_export_lens(path: str) -> Dict[str, Any]:
    """Export the Lens via File > Export and save to a path (macOS).

    Args:
        path: Where to save the exported file
    """
    for item in ["Export\u2026", "Export...", "Export", "Export Lens\u2026", "Export Lens..."]:
        res = await ui_menu_click("File", item)
        if res.get("returncode") == 0 and not res.get("stderr"):
            lines = [
                'tell application "Lens Studio" to activate',
                "delay 0.3",
                'tell application "System Events"',
                '  keystroke "g" using { command down, shift down }',
                "  delay 0.3",
                f'  keystroke "{path}"',
                "  delay 0.3",
                "  key code 36",
                "  delay 0.4",
                "  key code 36",
                "end tell",
            ]
            return _osascript(lines)
    return {"error": "Could not find an Export menu item in Lens Studio"}


@app.tool()
async def ui_add_object(kind: str) -> Dict[str, Any]:
    """Add a scene object via the Object menu (macOS UI automation).

    Args:
        kind: Object kind — 'sprite', 'text', 'empty'
    """
    mapping = {
        "sprite": [("Object", "Add 2D Image"), ("Object", "Add Sprite")],
        "text": [("Object", "Add 2D Text"), ("Object", "Add Text")],
        "empty": [("Object", "Add Empty Object"), ("Object", "Add Empty")],
    }
    for menu, item in mapping.get(kind.lower(), []):
        res = await ui_menu_click(menu, item)
        if res.get("returncode") == 0 and not res.get("stderr"):
            return res
    return {"error": f"No menu match for object kind '{kind}'"}


@app.tool()
async def ui_request_permissions() -> Dict[str, Any]:
    """Trigger macOS Automation permission prompts for System Events and Finder.

    Run this once if UI automation tools return permission errors.
    Click 'Allow' on the system dialogs that appear.
    """
    res1 = _osascript(['tell application "System Events" to count processes'])
    res2 = _osascript(['tell application "Finder" to get POSIX path of (path to home folder)'])
    return {"system_events": res1, "finder": res2}


@app.tool()
async def ui_dump_menus() -> Dict[str, Any]:
    """List all menu bar items and their sub-items in Lens Studio (macOS).

    Useful for discovering the exact menu structure for UI automation.
    """
    script = [
        'tell application "Lens Studio" to activate',
        'tell application "System Events"',
        '  tell process "Lens Studio"',
        '    set out to ""',
        '    repeat with m in (menus of menu bar 1)',
        '      set mname to name of m',
        '      set out to out & "MENU:" & mname & "\n"',
        '      repeat with mi in (menu items of m)',
        "        try",
        '          set iname to name of mi',
        '          set out to out & "  ITEM:" & iname & "\n"',
        "          if exists (menu of mi) then",
        "            repeat with sm in (menus of mi)",
        "              repeat with smi in (menu items of sm)",
        "                try",
        '                  set siname to name of smi',
        '                  set out to out & "    SUB:" & siname & "\n"',
        "                end try",
        "              end repeat",
        "            end repeat",
        "          end if",
        "        end try",
        "      end repeat",
        "    end repeat",
        "    return out",
        "  end tell",
        "end tell",
    ]
    return _osascript(script)


@app.tool()
async def ui_list_buttons(substring: Optional[str] = None) -> Dict[str, Any]:
    """List button names in the frontmost Lens Studio window (macOS).

    Args:
        substring: Optional filter — only return buttons containing this text
    """
    filt = (substring or "").replace('"', '\\"')
    script = [
        'tell application "Lens Studio" to activate',
        'tell application "System Events"',
        '  tell process "Lens Studio"',
        '    set out to ""',
        "    try",
        "      set btns to (buttons of window 1)",
        "    on error",
        "      set btns to {}",
        "    end try",
        "    repeat with b in btns",
        "      try",
        "        set nm to name of b",
        "        if nm is not missing value then",
        '          set out to out & nm & "\n"',
        "        end if",
        "      end try",
        "    end repeat",
        "    return out",
        "  end tell",
        "end tell",
    ]
    result = _osascript(script)
    if filt and result.get("stdout"):
        lines = [l for l in result["stdout"].split("\n") if filt.lower() in l.lower()]
        result["stdout"] = "\n".join(lines)
    return result


@app.tool()
async def ui_click_button(name: str) -> Dict[str, Any]:
    """Click a button by name in any Lens Studio window (macOS).

    Args:
        name: Exact button name to click
    """
    safe = name.replace('"', '\\"')
    script = [
        'tell application "Lens Studio" to activate',
        'tell application "System Events"',
        '  tell process "Lens Studio"',
        "    try",
        "      repeat with w in windows",
        "        try",
        f'          click (first button of w whose name is "{safe}")',
        '          return "clicked"',
        "        end try",
        "        try",
        f'          click (first button of toolbar 1 of w whose name is "{safe}")',
        '          return "clicked in toolbar"',
        "        end try",
        "      end repeat",
        '      return "not found"',
        "    on error errMsg",
        "      return errMsg",
        "    end try",
        "  end tell",
        "end tell",
    ]
    return _osascript(script)


# ===== ENTRY POINT =========================================================


def main():
    parser = argparse.ArgumentParser(
        description="Lens Studio MCP Server — proxy to Lens Studio's built-in MCP server",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio for Claude Desktop/Code)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for SSE/HTTP transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for SSE/HTTP transports (default: 8000)",
    )

    # Support legacy --stdio flag
    if "--stdio" in sys.argv:
        sys.argv.remove("--stdio")
        sys.argv.extend(["--transport", "stdio"])

    args = parser.parse_args()

    log.info(
        "Starting lens-mcp server (transport=%s, host=%s, port=%d)",
        args.transport,
        args.host,
        args.port,
    )

    if args.transport == "stdio":
        app.run(transport="stdio")
    elif args.transport == "sse":
        app.run(transport="sse", host=args.host, port=args.port)
    elif args.transport == "streamable-http":
        app.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()

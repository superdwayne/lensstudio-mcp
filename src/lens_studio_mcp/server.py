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
# Singleton bridge + FastMCP application
# ---------------------------------------------------------------------------

bridge = LensBridge()
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

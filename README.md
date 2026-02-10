# lens-studio-mcp

An MCP (Model Context Protocol) server that connects **any AI coding agent** to **Lens Studio** in real-time. Tell your AI to build an AR lens and watch it happen live.

No plugins required — works with Lens Studio 5.15+'s built-in MCP endpoint.

Compatible with: **Claude Desktop**, **Claude Code**, **Cursor**, **Windsurf**, **VS Code Copilot**, **OpenAI agents**, and any MCP-compatible client.

## How It Works

```
AI Agent  ←─ MCP (stdio/SSE/HTTP) ─→  lens-studio-mcp  ←─ HTTP JSON-RPC ─→  Lens Studio (port 50050)
```

Lens Studio 5.15+ ships with a built-in HTTP MCP server at `localhost:50050/mcp`. This package is a thin proxy that:

1. Discovers all tools from Lens Studio's API
2. Registers them as MCP tools with friendly wrappers
3. Forwards tool calls via HTTP and handles authentication
4. Adds convenience tools for face filters, materials, colors, and macOS UI automation

## Installation

### From PyPI (recommended)

```bash
pip install lens-studio-mcp
```

### From source

```bash
git clone https://github.com/superdwayne/lens-studio-mcp.git
cd lens-studio-mcp
pip install -e .
```

## Quick Start

### 1. Open Lens Studio

Launch **Lens Studio 5.15+** and open or create a project. The built-in MCP server starts automatically on port 50050.

### 2. Configure your AI agent

Add `lens-studio-mcp` to your editor's MCP configuration:

**Windsurf** (`.windsurf/mcp.json` or project `.mcp.json`):
```json
{
  "mcpServers": {
    "lens-studio": {
      "command": "lens-studio-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "lens-studio": {
      "command": "lens-studio-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

**Cursor** (`.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "lens-studio": {
      "command": "lens-studio-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

**SSE transport** (for remote/web clients):
```bash
lens-studio-mcp --transport sse --port 8000
```

**Streamable HTTP transport**:
```bash
lens-studio-mcp --transport streamable-http --port 8000
```

### 3. Start building

The first time you connect, Lens Studio shows a permission popup — click **Allow**. After that, the auth token is cached and reused automatically.

Now just tell your AI agent what to build:

> "Create a face filter with a red clown nose and colorful wig"

## Tools

### Connection & Status
| Tool | Description |
|------|-------------|
| `connect()` | Authenticate with Lens Studio (triggers permission popup on first use) |
| `status()` | Connection state, port, cached tool count |
| `list_tools()` | List all available Lens Studio tools |
| `lens_tool(name, arguments)` | Call any Lens Studio tool directly by name |

### Scene
| Tool | Description |
|------|-------------|
| `get_scene()` | Full scene hierarchy with names, types, components |
| `create_object(name, preset?)` | Create a scene object (optional preset template) |
| `delete_object(name)` | Remove a scene object by name |
| `add_primitive(shape, name?)` | Quick-add: sphere, cube, cylinder, camera, light, image, text |
| `set_property(uuid, path, value, type)` | Set any property on an object/component/asset |
| `create_asset(name, type)` | Create a new asset (Material, RenderTarget, etc.) |

### Materials & Colors
| Tool | Description |
|------|-------------|
| `create_material(name, color?, r?, g?, b?)` | Create a PBR or unlit material with a solid color |
| `set_color(object_name, color?)` | Set an object's color by name (auto-creates material) |
| `assign_material(object_name, material_name)` | Assign an existing material to an object |

### Face Filters
| Tool | Description |
|------|-------------|
| `create_face_anchor(name?)` | Create a head-tracked anchor with Face Occluder |
| `add_face_element(parent, landmark, shape?)` | Add 3D element at a face landmark position |
| `get_face_landmarks(landmark?)` | Reference positions for nose, eyes, ears, cheeks, etc. |

### Knowledge Base
| Tool | Description |
|------|-------------|
| `query_knowledge_base(query)` | Search Lens Studio docs and knowledge base |

### macOS UI Automation
Fallback tools for operations not covered by the API:

`ui_activate`, `ui_menu_click`, `ui_new_project`, `ui_save_project`, `ui_open_project`, `ui_preview`, `ui_keystroke`, `ui_type_text`, `ui_import_asset`, `ui_export_lens`, `ui_add_object`, `ui_dump_menus`, `ui_list_buttons`, `ui_click_button`, `ui_request_permissions`

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LENS_MCP_PORT` | `50050` | Lens Studio's MCP server port |
| `LENS_MCP_TOKEN_FILE` | `~/.lens_mcp_token.json` | Path to cached auth token |

## Requirements

- **Python 3.10+**
- **Lens Studio 5.15+** (running on the same machine)
- macOS recommended (UI automation tools are macOS-only, but all API tools work cross-platform)

## Security

- Lens Studio's MCP server binds to `localhost` only — not accessible from the network.
- Auth tokens are stored locally at `~/.lens_mcp_token.json`.
- The SSE/HTTP transport also binds to `127.0.0.1` by default.

## License

MIT

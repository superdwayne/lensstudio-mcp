# Lens Studio MCP Server

An MCP (Model Context Protocol) server that connects **any AI agent** to **Lens Studio** in real-time. Tell your AI to build an AR lens and watch it happen live.

No plugins required — works with Lens Studio 5.15+'s built-in MCP endpoint.

Compatible with: **Claude Desktop**, **Claude Code**, **Cursor**, **Windsurf**, **VS Code Copilot**, **OpenAI agents**, and any MCP-compatible client.

---

## How It Works

```
AI Agent  <── MCP (stdio/SSE/HTTP) ──>  lens-studio-mcp  <── HTTP JSON-RPC ──>  Lens Studio (port 50050)
```

Lens Studio 5.15+ ships with a built-in HTTP MCP server at `localhost:50050/mcp`. This server is a proxy that:

1. Discovers all tools from Lens Studio's API
2. Registers them as MCP tools with friendly wrappers
3. Forwards tool calls via HTTP and handles authentication
4. Adds **81 high-level tools** for face filters, materials, animation, particles, scripting, and more

---

## Prerequisites

- **Python 3.10+**
- **Lens Studio 5.15+** (running on the same machine)
- macOS recommended (UI automation tools are macOS-only, but all API tools work cross-platform)

---

## Installation

### Step 1: Clone the repo

```bash
git clone https://github.com/superdwayne/lensstudio-mcp.git
cd lensstudio-mcp
```

### Step 2: Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 3: Verify the server runs

```bash
python server.py --help
```

You should see the available transport options (`stdio`, `sse`, `streamable-http`).

---

## Connecting to Your AI Agent

### Option A: Claude Desktop (automatic installer)

Run the included installer script — it automatically adds `lens-studio` to your Claude Desktop config:

```bash
python install_claude_config.py
```

Then **restart Claude Desktop**. The Lens Studio MCP server will appear in your available tools.

### Option B: Claude Desktop (manual)

Open your Claude Desktop config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Add the `lens-studio` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "lens-studio": {
      "command": "/FULL/PATH/TO/lensstudio-mcp/.venv/bin/python",
      "args": [
        "/FULL/PATH/TO/lensstudio-mcp/server.py",
        "--transport", "stdio"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

> Replace `/FULL/PATH/TO/lensstudio-mcp` with the actual path where you cloned the repo.

Restart Claude Desktop after saving.

### Option C: Claude Code

Add to your project's `.mcp.json` or run:

```bash
claude mcp add lens-studio -- /FULL/PATH/TO/lensstudio-mcp/.venv/bin/python /FULL/PATH/TO/lensstudio-mcp/server.py --transport stdio
```

Or create a `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "lens-studio": {
      "command": "/FULL/PATH/TO/lensstudio-mcp/.venv/bin/python",
      "args": [
        "/FULL/PATH/TO/lensstudio-mcp/server.py",
        "--transport", "stdio"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

### Option D: Cursor

Create `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "lens-studio": {
      "command": "/FULL/PATH/TO/lensstudio-mcp/.venv/bin/python",
      "args": [
        "/FULL/PATH/TO/lensstudio-mcp/server.py",
        "--transport", "stdio"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

### Option E: Windsurf

Add to `.windsurf/mcp.json` or your project `.mcp.json`:

```json
{
  "mcpServers": {
    "lens-studio": {
      "command": "/FULL/PATH/TO/lensstudio-mcp/.venv/bin/python",
      "args": [
        "/FULL/PATH/TO/lensstudio-mcp/server.py",
        "--transport", "stdio"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

### Option F: SSE Transport (remote/web clients)

For clients that connect over HTTP instead of stdio:

```bash
source .venv/bin/activate
python server.py --transport sse --port 8000
```

Then point your client to `http://localhost:8000/sse`.

### Option G: Streamable HTTP Transport

```bash
source .venv/bin/activate
python server.py --transport streamable-http --port 8000
```

---

## First Run

1. **Open Lens Studio 5.15+** and open or create a project
2. **Start your AI agent** (Claude Desktop, Cursor, etc.)
3. On first connection, Lens Studio shows a **permission popup** — click **Allow**
4. The auth token is cached at `~/.lens_mcp_token.json` and reused automatically
5. Start prompting your AI!

### Example prompts

```
"Create a face filter with a red clown nose and cat ears"

"Add a sparkle particle effect to both hands"

"Apply a vintage color grading effect to the camera"

"Search for a crown 3D model and import it"

"Make the nose scale up when I open my mouth"
```

---

## Optional: Sketchfab 3D Model Search

To enable searching and importing 3D models from [Sketchfab](https://sketchfab.com):

1. Get a free API token at [sketchfab.com/settings/password](https://sketchfab.com/settings/password)
2. Add the token to your MCP config's `env` block:

```json
{
  "mcpServers": {
    "lens-studio": {
      "command": "/FULL/PATH/TO/lensstudio-mcp/.venv/bin/python",
      "args": [
        "/FULL/PATH/TO/lensstudio-mcp/server.py",
        "--transport", "stdio"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1",
        "SKETCHFAB_API_TOKEN": "your_token_here"
      }
    }
  }
}
```

---

## Available Tools (81)

### Connection & Status
| Tool | Description |
|------|-------------|
| `connect()` | Authenticate with Lens Studio (triggers permission popup on first use) |
| `status()` | Connection state, port, cached tool count |
| `list_tools()` | List all available Lens Studio tools |
| `lens_tool(name, arguments)` | Call any Lens Studio tool directly by name |

### Scene Management
| Tool | Description |
|------|-------------|
| `get_scene()` | Full scene hierarchy with names, types, components |
| `create_object(name, preset?)` | Create a scene object (optional preset template) |
| `delete_object(name)` | Remove a scene object by name |
| `add_primitive(shape, name?)` | Quick-add: sphere, cube, cylinder, camera, light, image, text |
| `set_property(uuid, path, value, type)` | Set any property on an object/component/asset |
| `create_asset(name, type)` | Create a new asset (Material, RenderTarget, etc.) |
| `query_knowledge_base(query)` | Search Lens Studio docs and knowledge base |

### Materials & Colors
| Tool | Description |
|------|-------------|
| `create_material(name, color?)` | Create a PBR or unlit material with a solid color |
| `set_color(object_name, color?)` | Set an object's color by name (auto-creates material) |
| `assign_material(object, material)` | Assign an existing material to an object |
| `create_textured_material(name)` | Create a PBR material ready for textures |
| `apply_texture(object, texture)` | Apply a texture to an object's material |
| `create_animated_texture(name)` | Create a sprite sheet texture |
| `create_custom_shader(name)` | Create a custom shader material |
| `set_material_property(material, prop, value)` | Set a property on a material |

### Face Tracking
| Tool | Description |
|------|-------------|
| `create_face_anchor(name?)` | Create a head-tracked anchor with Face Occluder |
| `add_face_element(parent, landmark, shape?)` | Add 3D element at a face landmark |
| `get_face_landmarks(landmark?)` | Reference positions for nose, eyes, ears, etc. |

### Hand Tracking
| Tool | Description |
|------|-------------|
| `create_hand_anchor(name?)` | Create a hand-tracked anchor |
| `add_hand_element(parent, landmark, shape?)` | Add 3D element at a hand joint |
| `get_hand_landmarks(landmark?)` | Reference positions for all hand joints |
| `create_gesture_trigger(gesture, script)` | Trigger script on pinch, fist, open palm, etc. |

### Body Tracking
| Tool | Description |
|------|-------------|
| `create_body_anchor(name?)` | Create a body-tracked anchor |
| `add_body_element(parent, joint, shape?)` | Add 3D element at a body joint |
| `get_body_joints(joint?)` | Reference positions for all body joints |

### World Tracking
| Tool | Description |
|------|-------------|
| `create_world_tracker(name?, mode?)` | Surface, world (6DOF), or rotation tracking |
| `place_in_world(object, position)` | Position an object in world space |
| `enable_object_tracking(category)` | Track cats, dogs, people, or hands |

### Animation
| Tool | Description |
|------|-------------|
| `create_tween(object, property, from, to, duration)` | Animate a property over time |
| `create_animation_sequence(sequence)` | Chain multiple animations |
| `animate_on_trigger(object, trigger, property, to)` | Animate on tap, mouth open, smile, etc. |
| `create_looping_animation(object, property, values, duration)` | Loop between values |

### Particles
| Tool | Description |
|------|-------------|
| `create_particle_system(name, preset?)` | Sparkles, fire, smoke, confetti, hearts, snow, stars, bubbles |
| `configure_particles(system, ...)` | Tune emission rate, lifetime, speed, size, color, gravity |
| `attach_particles(system, target)` | Attach particles to an object |
| `create_particle_trail(object, preset?)` | Create and attach particles in one call |

### Segmentation
| Tool | Description |
|------|-------------|
| `create_segmentation_mask(type)` | Person, background, hair, skin, sky, upper garment |
| `apply_background_replacement(color?, blur?)` | Replace or blur the background |
| `create_person_outline(color, thickness)` | Draw an outline around people |
| `apply_hair_color(color)` | Change hair color |

### Scripting
| Tool | Description |
|------|-------------|
| `create_script(name, template?, code?)` | Create a JavaScript script asset |
| `attach_script(object, script)` | Attach a script to a scene object |
| `create_tap_trigger(object, script)` | Run code when an object is tapped |
| `create_state_machine(states, transitions, initial)` | Create a state machine |
| `generate_script_from_description(description)` | AI-generate a script from natural language |

### Audio
| Tool | Description |
|------|-------------|
| `add_audio(name, loop?, volume?)` | Add an audio component |
| `play_sound_on_trigger(sound, trigger)` | Play sound on tap, smile, etc. |
| `apply_voice_effect(effect)` | Pitch up/down, echo, robot, whisper |
| `sync_to_music_beat(object, property?, intensity?)` | Animate in sync with music |
| `search_music_library(query)` | Search licensed music |
| `install_licensed_music(track_id)` | Install a track from the library |

### Post-Processing
| Tool | Description |
|------|-------------|
| `add_post_effect(type, intensity?)` | Bloom, blur, vignette, chromatic aberration, grain, sharpen |
| `apply_color_grading(preset, intensity?)` | Warm, cool, vintage, cyberpunk, pastel, high contrast |
| `create_custom_lut(name)` | Import a custom color LUT |
| `add_screen_effect(effect, blend_mode?)` | Light leaks, lens flare overlays |

### Lens Recipes
| Tool | Description |
|------|-------------|
| `list_lens_recipes()` | List available recipe templates |
| `create_lens_from_recipe(recipe)` | Build a complete lens from a template |
| `create_face_filter_lens(features)` | Create a face filter from a feature list |
| `create_try_on_lens(asset_type)` | Virtual try-on for glasses, hats, earrings, etc. |
| `create_game_lens(game_type)` | Interactive AR game (catch, tap, gesture quiz) |

### 3D Model Library (Sketchfab)
| Tool | Description |
|------|-------------|
| `search_3d_models(query)` | Search Sketchfab for downloadable 3D models |
| `import_3d_model(uid)` | Download and import a model into Lens Studio |

### macOS UI Automation (fallback)
For operations not available via API: `ui_activate`, `ui_menu_click`, `ui_new_project`, `ui_save_project`, `ui_open_project`, `ui_preview`, `ui_keystroke`, `ui_type_text`, `ui_import_asset`, `ui_export_lens`, `ui_add_object`, `ui_dump_menus`, `ui_list_buttons`, `ui_click_button`, `ui_request_permissions`

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LENS_MCP_PORT` | `50050` | Lens Studio's MCP server port |
| `LENS_MCP_TOKEN_FILE` | `~/.lens_mcp_token.json` | Path to cached auth token |
| `SKETCHFAB_API_TOKEN` | *(none)* | Sketchfab API token for 3D model search/import |
| `SKETCHFAB_BASE_URL` | `https://api.sketchfab.com/v3` | Sketchfab API base URL |
| `LENS_MCP_MODEL_CACHE` | `~/.cache/lens-mcp/models/` | Cache directory for downloaded 3D models |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Connection refused" | Make sure Lens Studio 5.15+ is open with a project loaded |
| "401 Unauthorized" | Lens Studio was restarted — delete `~/.lens_mcp_token.json` and reconnect |
| Permission popup doesn't appear | Check Lens Studio is in the foreground and not blocked by a dialog |
| UI automation tools fail | macOS only — run `ui_request_permissions` to grant Accessibility access |
| Sketchfab tools return error | Set `SKETCHFAB_API_TOKEN` in your MCP config's `env` block |

---

## Running Tests

```bash
source .venv/bin/activate
python -m pytest smoke_test.py -v
```

All 160 tests run against a mock Lens Studio server — no real Lens Studio instance needed.

---

## Security

- Lens Studio's MCP server binds to `localhost` only — not accessible from the network
- Auth tokens are stored locally at `~/.lens_mcp_token.json`
- SSE/HTTP transports bind to `127.0.0.1` by default
- Sketchfab API token is read from environment variables, never hardcoded

## License

MIT

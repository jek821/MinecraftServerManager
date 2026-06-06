# Minecraft Server Manager

Web UI for running and managing Minecraft world saves on a single Linux host. One open port handles the game, the admin interface, and resource-pack downloads.

## Requirements

- **Linux** (uses `/proc` for CPU/RAM stats and `du` for world sizes)
- **Python 3.10+**
- **Java** (for the Minecraft server jar)
- A **Paper** jar for chunk pre-generation (vanilla works for normal play)

## Quick start

```bash
# Clone the repo, then:
MC_PASSWORD="your-password" SERVER_NAME="My Server" ./webapp/run.sh
```

Open `http://<your-server-ip>:25565` in a browser and log in with `MC_PASSWORD`.

Put your server jar at `jars/server.jar` (Paper recommended). World saves live in `worldFiles/<world-name>/`.

### First-time setup

1. Start the app and open the web UI.
2. Upload or generate a world, or copy an existing save into `worldFiles/`.
3. Set **Server Host** in Settings to the IP or hostname players use to connect.
4. Activate a world, then start the server from the HUD.

## How the port works

Only **25565** needs to be open on your firewall. A small TCP multiplexer inspects each connection and routes it:

| Traffic | Routed to |
|---------|-----------|
| Minecraft protocol | Internal MC port (default 25566) |
| HTTP(S) resource-pack requests | Internal pack server (17892) |
| Web UI (HTTP) | Flask app (17891) |

The game server binds to the internal port; players still connect on 25565.

```
Players / browser  →  :25565 (public)
                         ├─ Minecraft  →  :25566
                         ├─ Pack HTTP  →  :17892
                         └─ Web UI     →  :17891
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MC_PASSWORD` | `admin` | Web UI login password. **Change this.** |
| `SERVER_NAME` | `MC` | Shown on the login page |
| `SECRET_KEY` | auto-generated | Flask session key (stored in `.secret_key` if unset) |

## Directory layout

```
MinecraftServerManager/
├── webapp/           # Flask app, static UI, port proxy
├── worldFiles/       # World saves (gitignored)
├── jars/             # server.jar (gitignored)
├── config.json       # Active world, JVM args, server host (gitignored)
└── server-icon.png   # Optional 64×64 list icon (gitignored)
```

Each world can have its own `paintings/` folder for custom painting images.

## Features

- **Worlds** — upload, download, rename, delete, generate, activate
- **Server** — start/stop, logs, MOTD, whitelist, JVM args
- **Pre-gen** — Chunky-based chunk pre-generation (Paper only; stop the server first)
- **Custom paintings** — upload images per world; builds resource pack + data pack automatically
- **RCON** — console, op/deop, give paintings to players
- **Server icon** — upload a global icon applied to all worlds

## Pre-generation notes

- Stop the managed server before starting pre-gen.
- Requires a **Paper** jar in `jars/server.jar`.
- Works on worlds you have already played — Chunky skips existing chunks.
- Use **Cancel Pre-gen** in the modal; do not use Stop Server (it is blocked during pre-gen anyway).
- Suggested radius: 1,000–2,000 for testing; max 10,000.

Re-running pre-gen on the same center and radius after a completed job is safe but usually finishes almost immediately.

## Security

This is a **single-password** admin tool, not multi-user hosting software.

- Set a strong `MC_PASSWORD` before exposing the host.
- Login allows **10 failed attempts per IP**, then a **10-hour lockout**. Lockouts are in-memory and reset when the app restarts.
- The web UI is served on the same port as Minecraft. Anyone who can reach `:25565` can attempt to log in.
- RCON passwords are stored in each world's `server.properties`.
- Intended for private servers (friends, homelab). Review your firewall and exposure before putting it on the public internet.

## Limitations

- Linux only for now
- Single admin account (no per-user permissions)
- No Docker or systemd unit included
- Pre-gen and world generation spawn temporary background Java processes
- Bleeding-edge Minecraft versions may log resource-pack format warnings; paintings still work in most cases

## Development

`run.sh` creates `webapp/.venv` and installs `requirements.txt` on first run. For local dev, same script:

```bash
MC_PASSWORD=test SERVER_NAME=DEV ./webapp/run.sh
```

If you change dependencies, reinstall with:

```bash
webapp/.venv/bin/pip install -r webapp/requirements.txt
```

Dependencies: Flask, requests, Pillow.

## License

No license file yet — add one before distributing publicly (MIT is a common choice).

# Agent Tower Windows

[简体中文 README](README.md)

Turn Feishu into a unified command tower for Codex, Claude Code, WorkBuddy, Qoder CLI, and DeepSeek Harness. Start jobs from a phone, watch their progress, and receive results in Feishu topics.

This repository is based on [wowofighting/agent-tower](https://gitee.com/wowofighting/agent-tower) (Apache-2.0) with added Windows support. The upstream project targets macOS; see [NOTICE](NOTICE) for the list of modifications.

## Platform status

The upstream project targets macOS. This repository adds Windows adapters for Codex and DSH jobs, explicitly authorized desktop conversations, and Feishu text/image exchange. macOS continues to use `launchd`; Windows provides PowerShell setup, runtime checks, and optional user-logon startup. See [Windows setup and desktop control](README.md#windows--飞书手机端) for the verified scope and limitations.

Desktop control adds `#attach`, `#sessions`, `#status`, and `#leave`, with configured short aliases. Authorized Codex conversations can be opened automatically when not yet loaded. Existing DSH conversations use their authenticated local Host; the optional Host export plugin maintains its private connection file. Local credentials, runtime state, logs, and received images are excluded from Git.

## What it does

- Runs one daemon with two loops: remote control and progress broadcasting.
- Starts and resumes Codex, Claude Code, WorkBuddy, Qoder, and DeepSeek Harness jobs.
- Streams session updates into Feishu topics and supports topic replies for supported native questions.
- Provides job management commands, read-only session watching, retries, delivery persistence, diagnostics, and retention controls.
- Includes an optional independent watchdog for detecting a stalled daemon before recovery is explicitly enabled.

## Quick start on Windows

```powershell
git clone https://gitee.com/june-zheng/agent-tower-windows.git
cd agent-tower-windows

# 1. Create a Feishu enterprise app and enable im:message / im:chat /
#    im:message.group_msg plus long-connection event delivery, then add the bot to a group.
# 2. Install Python 3.14, uv, Node.js and the Codex CLI, then complete Codex login.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-windows.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-windows.ps1 -Start
```

After startup, send `#help` in the Feishu group, then put the returned `open_id`
into `config/ctl.conf` and `chat_id` into `config/feishu-app.json`, and restart the
tower. See [Windows setup and desktop control](README.md#windows--飞书手机端) for the
full procedure and security boundaries.

## Quick start on macOS

```bash
git clone https://gitee.com/june-zheng/agent-tower-windows.git ~/agent-tower-windows
cd ~/agent-tower-windows

# Create a Feishu enterprise app, enable im:message and im:chat,
# and configure long connection event delivery.
cp config/feishu-app.example.json config/feishu-app.json
# Fill in the app credentials. Leave chat_id empty initially if needed.

# Create the virtual environment, install dependencies, and register launchd.
./scripts/install.sh

# Send #help in the Feishu group, then add your open_id to allow_ou
# in config/ctl.conf and run the installer again.
tail logs/ctl.log
```

The installed service is managed by `launchd`. After a code update, restart it with:

```bash
launchctl kickstart -k gui/$(id -u)/com.$(id -un).agent-tower
```

## Feishu commands

| Command | Purpose |
| --- | --- |
| `#new <directory> <prompt>` | Start a Codex job |
| `#cnew <directory> <prompt>` | Start a Claude Code job |
| `#wnew <directory> <prompt>` | Start a WorkBuddy job |
| `#qnew <directory> <prompt>` | Start a Qoder job |
| `#dnew <project> <prompt>` | Start a DeepSeek Harness job |
| `#go <job-id> <instruction>` | Continue a job |
| `#goal <job-id> <goal>` | Run a bounded goal loop |
| `#stop` / `#jobs` / `#model` / `#title` | Stop, list, switch, or rename jobs |
| `#scan` / `#watch` / `#unwatch` | Discover and manage read-only session watching |
| `#health` / `#deliveries` | Inspect runtime health and persistent delivery state |
| `#help` / `#help all` | Show command help |

Commands that create or control jobs are protected by the configured allowlist and permission mode. Watching a session does not grant permission to start, resume, or stop it.

## Repository layout

```text
scripts/
  tower.py             Main daemon entry point
  tower_ctl.py         Remote control and job lifecycle
  tower_sync.py        Session scanning and Feishu broadcasting
  tower_watchdog.py    Optional stalled-process watchdog
  install.sh           Idempotent macOS installer and launchd setup
config/
  *.example            Safe configuration templates
launchd/
  *.plist.example      launchd templates
docs/testing/          Offline tests and verification boundaries
```

Instance configuration, credentials, logs, state, and local runtime files are intentionally excluded from version control. Copy the example configuration after cloning and keep secrets out of the repository.

## Requirements

- macOS with Python 3.12 or newer
- `lark-oapi`, `requests`, and `setproctitle` (installed by `install.sh`)
- The Agent CLIs you intend to control: `codex`, `claude`, WorkBuddy's `codebuddy`, `qoderclicn`, and optionally `dsh`
- A Feishu enterprise app with the required messaging permissions and long connection events

Qoder's login state can be reused from the local Qoder installation. Qoder sessions can also be watched without the CLI executable when their transcripts are available locally. See the Chinese README and the testing notes for the full engine-specific behavior and limits.

## Testing

Run the offline regression suite from the repository root:

```bash
.venv/bin/python scripts/run_tests.py
```

The suite uses an isolated temporary directory, disables real sync, and does not copy local Feishu credentials or runtime state. A passing suite does not prove live Feishu authentication or end-to-end delivery.

## Security and operations

- Keep `config/feishu-app.json`, local allowlists, tokens, logs, and session state out of Git.
- The optional watchdog is only a template until it is explicitly registered.
- Delivery state is persisted for retry and diagnostics; failed payloads follow the configured retention policy.
- Read the testing notes in [`docs/testing/`](docs/testing/) before enabling new engine integrations or recovery behavior.

## License

Apache-2.0. See [LICENSE](LICENSE). This repository is based on
[wowofighting/agent-tower](https://gitee.com/wowofighting/agent-tower); the list of
modifications is in [NOTICE](NOTICE).

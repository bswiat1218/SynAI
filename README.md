# SynAI // terminal UI

## Architecture documentation

The authoritative SynAI 2.0 architecture baseline, trust boundaries,
protocol contracts, ADRs and phase compatibility matrix are in
[docs/architecture/synai-2.0.md](docs/architecture/synai-2.0.md). Distributed
Agent Task execution and the Sandbox Broker are planned, not implemented.

**SynAI** is a bold **'80s cyberpunk arcade Textual TUI** for coding with local or remote Ollama models: near-black violet panels, readable light text, cyan/hot-pink digital borders, and a retro masthead by default. Select a model, write a prompt, stream its answer, inspect provider-emitted reasoning, and reopen saved conversations. Native tools let eligible models read/edit a workspace, create files in any language, fetch HTTP resources, and run tests/builds/git inside a restricted non-root container or, with explicit consent, directly on the Linux host.

This replaces the old benchmark CLI. `list-models`, `run`, and `run-all` are no longer commands. Existing `prompts/` and `results/` files are preserved, but are not automatically imported, run, or sent to the model.

## Develop and launch from source

SynAI is a **work in progress**, not a published release or distribution.
Run it from this source checkout using **Python 3.12+**, Linux, and an interactive
terminal. The bundled editor targets **Linux x86_64/glibc 2.34+**.
Neovim and mini.nvim assets are included in the repository. Ollama can run
locally or at a trusted remote endpoint. GTK and a container runtime are not
required for chat-only startup.

For development in VS Code, open a terminal in this checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/synai
```

`.venv/bin/python -m synai` is an equivalent launcher; `.venv/bin/python app.py`
remains a source-checkout compatibility launcher. Runtime code lives under
`synai`; generic internal imports such as `from config import Settings` are not
supported APIs. `--help`, `--version`, and `--print-editor-image-recipe` exit
without initializing storage or contacting Ollama. The version is a development
identifier, not a published release.

After updating the checkout, rerun `.venv/bin/python -m pip install -e .` when
dependencies or console scripts change. Editable installation is for development,
not a release build. This does not delete private conversations or preferences
in `~/.synai`.

### Moving a source checkout to a Linux server

Copy the complete checkout, including `.git`, bundled editor assets, `prompts/`
and `results/`. Recreate `.venv` and `web/node_modules` on the destination rather
than relying on dependencies copied from another machine. Stop SynAI before
copying `~/.synai` separately; preserve its private file/directory permissions
and make sure the destination files belong to the account running SynAI.
Do not overwrite an existing destination checkout or data directory without
reviewing and backing it up first.

Saved conversations contain absolute workspace paths. Managed workspaces must
match their new location under `~/.synai/conversations`; external host workspaces
must be copied separately and rebound to their destination paths before use.
Keep recorded message provenance unchanged. Saved Ollama endpoints are also
preserved: confirm that the server can reach the configured inference machine.

For an SSH alias named `home-server` and a checkout at `~/synai`, launch with:

```sh
ssh -t home-server 'cd ~/synai && .venv/bin/synai'
```

The TUI needs an interactive terminal but not a desktop. Its separate GTK editor
still requires a usable graphical display; ordinary headless SSH is insufficient.
The browser application now provides authenticated Ollama chat, persistent
conversation history, a Slate Dark Project Command Center, project metadata and
activity views, and device/sandbox status. Chat remains tool-disabled and does
not access source files, execute tasks, or apply patches. The original Phase
13B read-only foundation is described in [web deployment and shared-data
ownership](docs/phase-13b-web.md); see [Phase 13D browser chat](docs/phase-13d-web-chat.md)
for browser capabilities and limitations, and [Phase 13E Client Agent](docs/phase-13e-client-agent.md)
for Linux pairing, local workspace authorization, and explicitly consented
immutable snapshots. Remote access requires HTTPS, and
the web service and TUI cannot own the same data root concurrently.

SynAI opens on its main menu. Set your Ollama server in **CONNECTION SETTINGS** if needed, then choose **NEW CONVERSATION**, review its environment, choose **NEXT**, select a model from the shared connection, and choose **CREATE CONVERSATION**. Use **CONVERSATIONS** to reopen or delete saved chats. Highlight a row with the arrows and press **Enter** or **OPEN** to restore its execution environment. Launching, configuring drafts, refreshing models, and browsing the menu do not create empty histories.

In a conversation, type your prompt in the composer, then choose **SEND** or press **Ctrl+Enter / Ctrl+S**. Without a ready approved execution environment and a native-tool-capable model, this is chat-only: the model cannot execute tools. **Escape / STOP** cancels the active turn, **F2** opens the main menu to start a new conversation, and **Ctrl+Q** quits. Enter inserts newlines in the composer. Ctrl+N no longer creates a conversation.

### Keyboard-first quick start

1. On the main menu, use arrows to highlight **CONNECTION SETTINGS** and press **Enter**. The endpoint field is initially highlighted, but is not yet editable.
2. Press **Enter** to edit it. Type your trusted Ollama URL, such as `http://192.168.1.10:11434`, then press **Enter** again to retain the draft. Navigate to **SAVE CONNECTION** and press **Enter**. Review any endpoint-change confirmation; saving the form is separate from finishing field editing.
3. Choose **CANCEL / Escape** to leave the connection editor, then open **NEW CONVERSATION**. Highlight configuration pages with Up/Down; each page opens immediately. Right enters its controls. Leave the default sandbox mode for isolated tools, or deliberately select host mode as described below.
4. Navigate to **NEXT** and press **Enter**. On model selection, press **Enter** to open the dropdown, use Up/Down to highlight a model, and press **Enter** to confirm it. Tab leaves the dropdown; highlight **CREATE CONVERSATION** and press **Enter**.
5. In the conversation, type a prompt and press **Ctrl+S** to send. Creating a sandbox-mode conversation does not start a container: without separately approved tool setup, it remains chat-only.
6. Press **F2** for conversation actions, **CONVERSATION DETAILS** for live status and full paths, or **F1** for Help. After returning to chat, **F3** focuses the composer.

For every menu, arrows navigate without activating actions. Tab/Shift+Tab can also move between controls, and leave native lists. Enter activates the highlighted control. Escape first closes a dropdown or finishes field editing; only a subsequent Escape closes the containing menu.

### Main menu and environment setup

Press **F2** to open the compact dashboard. **F2 / RETURN TO CHAT / Escape** returns without discarding your draft and restores your previous focus. With no open chat, Escape leaves you on the dashboard. During a model response the dashboard remains accessible for status and **STOP RESPONSE**, but model, conversation, connection and execution changes are locked. Approvals and setup/handoff operations cannot be bypassed with F2.

The main menu groups actions by purpose:

- **Conversations**: **New Conversation** and the combined **Conversations** browser. New Conversation reviews settings before model selection. The browser opens the highlighted row and deletes checked rows after confirmation. Each new draft starts with launch defaults and does not start a container.
- **Current Conversation** (only when one is open): model selection/capabilities and execution status, **Settings**, **Conversation Details**, **Disconnect / Remove** (or **Disconnect Host Tools**) and **Stop Response** while streaming.
- **Connection**: **Connection Settings** edits the application-wide Ollama endpoint and provider idle timeout; **Refresh Models** retries discovery after connecting Ollama or changing installed models. These controls appear only here. Offline startup still allows new-conversation settings, browsing and help.
- **Application**: **Themes**, **Quit SynAI**, and **Retry Legacy Cleanup** only after a cleanup failure. Press **F1** for the **Help / Shortcuts** overlay instead of looking for a menu item.

There is no conversation sidebar: a small read-only model/execution/workspace summary leaves more room for the conversation and logs. All navigation, model selection and execution controls live in F2. Host mode hides container-only details and clearly reports whether tools are authorized.

All workflows support keyboard operation; mouse support is optional. Dashboard groups share one page with two columns (stacked on smaller terminals), keyboard-scrollable content and visible focus. **Conversation Details** opens a read-only overlay with full workspace/storage paths, live execution status, runtime/image and tool/resource limits, keeping F2 compact. It remains available during streaming and does not enable tools or stop the response. PageUp/PageDown scroll its contents; **CLOSE / Escape** returns to the same dashboard control.

Menus use aligned action rows and scrollable content with fixed footer actions. Layouts support 80×24, a compact 60×20 fallback and wider terminals.

| Keys | Action |
| --- | --- |
| F1 | Open Help / Shortcuts from chat or menus; Escape closes it |
| F2 | Open dashboard / return to chat |
| F3 / F4 / F5 / F6 | Focus composer / conversation / reasoning / tool activity (conversation screen only) |
| F7 | Open or focus the current conversation's desktop workspace editor |
| F8 | Select all prompt text when the composer is focused |
| Tab / Shift+Tab | Next / previous control; focused controls scroll into view |
| Arrows on menu controls | Move focus according to the menu layout; no wrapping at edges |
| Enter / Space | Activate focused buttons; Enter chooses a model or opens a highlighted conversation |
| Enter in a single-line field | Start editing; Enter or Escape finishes editing and retains the draft without saving |
| Arrows inside an editing field / open dropdown / list | Move the text cursor or choose entries; Tab/Shift+Tab leaves the control |
| PageUp / PageDown in menus | Scroll details; in configuration, scroll the active form |
| PageUp/PageDown, Home/End (and arrows in logs) | Scroll focused logs |
| Space in Conversations | Toggle the highlighted row's deletion checkbox |
| Ctrl+Enter / Ctrl+S / Ctrl+J | Send; plain Enter in the composer inserts a newline |
| Escape | Cancel a confirmation/menu, or stop a turn in the conversation |
| Ctrl+P / Ctrl+Q | Open the theme picker / ownership-aware quit |
| Alt+N / Alt+C / Alt+M / Alt+S in F2 | New / Conversations / focus model selector / Settings |
| Alt+O / Alt+R / Alt+D / Alt+H / Alt+X in F2 | Connection / Refresh / Disconnect / Help / Stop response |

Use F3 to return to typing after scrolling logs. Pane shortcuts do not change focus inside a modal. Permissions require a deliberate confirmation; approval dialogs initially focus **Deny**.

### Optional Neovim workspace editor

**F7**, or **F2 / WORKSPACE EDITOR**, opens a separate SynAI-owned Linux desktop window and requests maximization. It contains a persistent file/folder tree, Neovim with **mini.nvim**, and a resizable interactive terminal below the editor. The desktop window manager controls maximization and focus requests. A local graphical display is required; SSH/headless sessions without a usable display get an explicit error.

The editor is optional: normal SynAI startup does not import GTK or start Neovim.
SynAI includes pinned Neovim **0.11.5**, its runtime, and mini.nvim **v0.16.0**.
Neither needs a system installation or a plugin directory in your home folder.
On Debian/Ubuntu, install the remaining desktop integration explicitly:

```sh
sudo apt-get install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91
```

SynAI checks its current Python and then `/usr/bin/python3` for Python 3.9+, GTK3/VTE, and a reachable display. The child entry point can use system Python independently of SynAI's virtual environment; do not recreate your existing virtual environment just to expose GTK. SynAI itself still requires Python 3.12+.

For **host-mode** editing, SynAI verifies and extracts its packaged editor into
private temporary storage before opening the window. Startup and remote editor
commands use that exact binary, not `nvim` on PATH. Nothing is downloaded or
installed on launch. The selected environment needs Linux x86_64, glibc 2.34+,
`libgcc_s.so.1`, and executable temporary storage with room for the runtime and
at least 32 MiB of additional space. ARM64 and Alpine/musl are not supported by
this bundled editor runtime; incompatibility is reported rather than hidden.

`SYNAI_MINI_PATH` is an optional host-only plugin-directory override. Leave it
unset to use the bundled mini.nvim. An invalid override is an explicit error.
The host terminal uses `$SHELL`, or `/bin/sh` when unset. Missing or non-executable
shells are errors, not an environment fallback.

For **sandbox-mode** editing, **both Neovim and the terminal run inside the
conversation's validated, attached sandbox**, with its numeric non-root user,
`/workspace` working directory, and existing resource limits. SynAI streams the
same packaged runtime into private `/tmp` storage there. A compatible ordinary
Python sandbox does not need Neovim or mini.nvim preinstalled. The default
`python:3.12-slim` image is compatible when it targets x86_64/glibc 2.34+.
For an explicit Debian-based example, build the minimal recipe:

```sh
mkdir -p synai-editor-build
synai --print-editor-image-recipe > synai-editor-build/Dockerfile
docker build -t synai-editor:local synai-editor-build
```

For Podman, use `podman build` instead. Set this image in the conversation's
sandbox settings, create or attach the sandbox through the normal approved
workflow, then press F7. There are no extra workspace mounts, automatic container
starts, plugin downloads, or installs on editor launch. Existing sandbox limits
are not increased; insufficient temporary space or a non-executable `/tmp`
blocks launch. A missing, mismatched, incompatible, or unavailable sandbox
blocks launch; it **never opens a host shell instead**.

Use a dedicated build directory containing only the exported Dockerfile, never
your project or private conversation storage as a build context. Recipe printing
does not run Docker/Podman. The canonical recipe lives in the source checkout at
`synai/editor/sandbox-editor.Dockerfile`.

Opening the editor requires deliberate consent. Manual editing and interactive shell commands are **human-driven operations, not AI tool calls**: they do not receive per-command approvals or the noninteractive tool helper's command timeout/output budget. Host mode is prominently labeled **HOST // NOT ISOLATED** and has your account's access. Sandbox mode retains the selected container's restrictions. Opening the editor does not authorize AI host tools.

- Only one window is open per SynAI instance. F7 presents that window again.
- **Ctrl+Alt+1 / 2 / 3** focus the file tree / Neovim / terminal. **Ctrl+Alt+R** refreshes the tree; **F5** also refreshes while the tree has focus. Arrows expand/navigate folders; Enter opens a file. Out-of-workspace symlinks and symlinked directories are not traversed. Folders exceeding 2,000 entries report an error rather than silently truncating.
- mini.nvim supplies text objects, surround/comment editing, auto-pairs, completion, statusline, and buffer tabs. This is not mini.files: the persistent tree is part of the desktop wrapper. Normal Neovim editing keys and commands remain available.
- The resolved SynAI palette is applied to the window, Neovim highlights, and terminal colors. Theme previews update live; cancelling a preview restores the original. Only SynAI's dedicated Neovim configuration loads; your normal config is unchanged, and workspace-local configuration/modelines do not execute.
- Close the editor before switching conversations, changing environment settings, disconnecting its sandbox, or deleting its conversation. Model and theme changes remain available.
- Window close and SynAI quit offer **Save / Discard / Cancel** for modified buffers. Unnamed buffers require a workspace save destination. Save failures retain the editor. Active editor-owned terminal jobs require confirmation before termination.
- A child crash or unexpected parent disconnect reports or retains a private recovery directory under `/tmp/synai-editor-*` in the selected environment. Swap files are kept there on abnormal exit. Recover with Neovim's `-r` support before manually removing that specific directory. Parent disconnect lets the remaining window offer save/cancel instead of killing dirty buffers. Container removal destroys its temporary recovery data.
- Cleanup targets editor-owned process sessions only. Programs that deliberately create a new session/detach can survive; inspect and stop specific surviving processes yourself, or remove an owned sandbox through the normal workflow.

No sidebar rename/delete UI, automatic language-server installation, or plugin-manager UI is included.

Editor tests follow the existing unittest suite. Bundled headless Neovim tests
run normally, without any external editor installation:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_editor*.py'
```

For graphical tests, use system Python with GTK and a desktop, or `xvfb-run`:

```sh
SYNAI_TEST_DESKTOP=1 \
  xvfb-run -a .venv/bin/python -m unittest discover -s tests -p 'test_editor.py'
SYNAI_TEST_DESKTOP=1 \
  xvfb-run -a /usr/bin/python3 -m unittest discover -s tests -p 'test_editor_desktop.py'
```

Set `SYNAI_TEST_WINDOW_MANAGER=/usr/bin/xfwm4` when available to verify the actual
maximized window state under Xvfb. Only after approving temporary container
creation, set `SYNAI_TEST_EDITOR_IMAGE=python:3.12-slim-bookworm` (already pulled)
to also exercise restricted sandbox Neovim and desktop PTYs without an
editor-ready image. Tests remove the containers they create, never unrelated
containers.

Arrow navigation skips hidden and disabled controls and scrolls the destination into view. Within a scrollable menu body, it prefers controls in that body before moving out to footer actions, so offscreen fields are not skipped. In configuration, focusing a page button immediately opens that page, whether reached with arrows, Tab or mouse. Page focus preserves drafts and never saves settings or approves an action.

Menu arrows never directly operate scrollbars or scroll containers, including unused directions inside a field or list. **PageUp/PageDown** scroll menu details; focusing an offscreen control automatically reveals it. All focused buttons use a consistent double theme-primary border and neutral surface, without underlining text, including destructive actions and configuration page buttons. An unfocused active configuration page has a solid border instead. Conversation-log scrolling is unchanged.

Menu labels omit shortcut hints, but existing shortcuts still work; their full reference is in **F1 Help / Shortcuts**. Repeated F1 does not stack overlays. Escape returns to the previous screen and focus without discarding drafts or stopping a response. F1 is blocked over pending approvals/sandbox handoff and while a menu is opening, saving, deleting or configuring tools, so Help cannot obscure a required decision or race a screen transition. Alt+H remains a dashboard-only Help alias.

**Every conversation owns a saved execution environment**, stored in its history JSON. New chats review a managed sandbox workspace or existing host project and settings before choosing a model; canceling a draft leaves the active chat, provider and prompt draft unchanged. All conversations use the same application connection, which can be edited from the main menu even when offline.

For an existing conversation, choose **SETTINGS** in F2, then **SAVE SETTINGS**. Invalid settings are rejected before saving. The model and messages are retained. Changing workspace after messages exist requires confirmation that approved tools may work in a different directory. Declining leaves the environment unchanged. Workspace selection remains in configuration and cannot bypass this flow. Disconnect any attached sandbox before editing.

Conversation configuration has four sidebar pages:

| Page | Contents |
| --- | --- |
| Overview | Current form values, unsaved-change indicator and execution status |
| Execution | Sandbox/host mode first, then container image/runtime or host warning and tool setup |
| Workspace | Read-only managed sandbox path, or selectable existing host project |
| Limits | Command timeout, output bytes, tool-call budget and container memory/CPU/PID limits |

Focus a sidebar page with arrows, Tab or mouse to open it immediately. Right enters that page's first available control; Left from a non-editing field or closed dropdown returns to its page button. Each page scrolls independently. PageUp/PageDown scroll the active form even when a sidebar page or footer button has focus. Switching pages preserves all draft values and never contacts Ollama or starts a sandbox. The shared **SAVE SETTINGS** (or **NEXT** for new chats) validates every page, including hidden fields. An invalid field opens its page and starts editing. The status line tracks unsaved edits across pages; **CANCEL / CLOSE / Escape** outside an editing field discards unsaved edits. **BACK** from model selection returns to the preserved new-conversation draft. Sandbox actions use saved settings only, so save changes first.

Single-line fields begin in navigation mode: typing, paste and deletion do not change them just because they are highlighted. **Enter** starts editing and selects the value; Left/Right then move the cursor, while Up/Down do not scroll the menu. The editing border uses the theme's secondary accent, distinct from the primary navigation highlight. **Enter** or **Escape** ends editing, retaining the draft without saving or closing the form. Tab transfers focus and ends editing. Opening Help or switching away from the terminal preserves an active edit. The multiline chat composer remains directly editable.

Closed dropdowns participate in arrow navigation without opening. **Enter** opens the dropdown and focuses its options; arrows highlight, **Enter** confirms and **Escape** cancels and returns focus. Space does not open dropdowns. Returning from another application restores valid menu focus; if focus is missing, an arrow restores navigation without Tab. A recovery Enter restores focus only, avoiding accidental activation. Terminal focus reports are used when available; keys intercepted by VS Code or the terminal cannot be handled by SynAI.

During new-conversation model discovery, or if discovery fails or finds no models, the unavailable dropdown stays disabled and **BACK** remains a safe navigation target.

Reopening a chat restores its workspace, image/runtime and limits, including after restarting SynAI, but **does not restore an old provider connection**. Missing workspaces, offline endpoints and unavailable models are reported; history remains viewable without silently choosing another model or workspace. Execution CLI/environment values are defaults for new conversations; saved execution environments take precedence on resume.

Connection settings and the selected theme are stored privately in `~/.synai/settings.json`. Connection precedence is independent for endpoint and idle timeout: **explicit CLI flag > environment variable > saved setting > default**. Launch overrides are not automatically saved. Saving through the UI applies immediately and persists, but explicit launch overrides win again on the next restart. The editor shows the effective launch sources and saved values. Invalid/unreadable settings are reported rather than overwritten.

Changing the shared endpoint requires confirmation that retained messages from any conversation may be sent to the new server. Opening a conversation recorded on a different endpoint also warns before activation. Saved models remain selected; if one is unavailable, deliberately choose a replacement in the chat model selector or change the connection. Model changes require confirmation, retain the same conversation and workspace, and never create a new chat.

### Themes

Press **Ctrl+P**, or choose **THEMES** in F2, to open the same dedicated theme picker. This replaces the general command palette at those entry points. The active theme appears first; the remaining registered themes are sorted by name and labeled light/dark.

- **Up/Down** highlights and previews a theme across all panels, menus, the composer, existing messages, reasoning and tool telemetry. Previewing does not save preferences.
- **Enter** in the theme list, or **APPLY**, confirms and saves the highlighted theme, then closes the picker.
- **CANCEL / Escape** restores the exact theme active before opening the picker, without changing saved settings. This also preserves an originally unsaved theme from a previous write failure.
- If saving fails, the picker stays open, shows the error and offers **RETRY**. The preview stays visible but is not saved; Cancel still restores the original theme.
- **Tab / Shift+Tab** moves between the native list and footer actions. **F1** opens Help without ending the preview. Closing the picker restores the invoking screen and focus, including active field editing and drafts.

The picker remains available during model responses; previewing or confirming does not stop streaming or change conversation data. It is blocked over approvals, sandbox handoff and active saving/opening/deleting/setup transitions. Repeated Ctrl+P does not stack pickers. Resizing or switching away from the terminal preserves the active preview.

SynAI cyberpunk is the first-run default and remains selectable as `synai-cyberpunk`; confirmed theme choices are remembered after restarting. A missing saved theme is reported and uses cyberpunk rather than silently overwriting the saved setting. Theme saves preserve the saved connection settings and do not persist temporary CLI/environment connection overrides.

SynAI text uses the selected theme's normal foreground, including reasoning, telemetry, errors, headings and disabled labels. Accents decorate borders; text-bearing panels, titles and buttons use the theme's original neutral backgrounds. Warnings/errors retain explicit wording and bold emphasis; focus, selection and disabled states remain distinct without dimming labels. Theme palettes are not automatically contrast-corrected: some original palettes, such as Solarized, have lower contrast on certain panels. Choose another theme if its original foreground/background combination is difficult to read.

When switching conversations with a sandbox attached, choose **CANCEL SWITCH**, **LEAVE RUNNING AND DISCONNECT**, or **REMOVE OWNED CONTAINER AND DISCONNECT**. Removal is offered only for containers owned by this app; attached containers are never removed. The old runtime handles removal before the new runtime is activated. Leaving a container running retains its ID with the old chat for later reattachment. Workspace files are never deleted.

Saving/restoring configuration does not pull images, run commands or create/attach containers. Each switched/resumed sandbox chat starts disconnected; open its configuration to explicitly create or attach a sandbox with approval. Host chats request fresh host-access consent on opening. Runtime consent is not saved. Setup actions appear only for an existing conversation, not an unsaved new-chat draft. Save any edited environment fields before using these actions.

**Ctrl+S** is an alternative send shortcut; **Ctrl+J** also sends for terminals that encode Ctrl+Enter as a line feed. These shortcuts work while the composer is focused. Plain Enter always inserts a newline. Some terminals cannot distinguish Ctrl+Enter from Enter, or VS Code may intercept it. If Ctrl+Enter still inserts a newline, use Ctrl+S or open VS Code's **Preferences: Open Keyboard Shortcuts (JSON)** and add:

```json
{
  "key": "ctrl+enter",
  "command": "workbench.action.terminal.sendSequence",
  "when": "terminalFocus",
  "args": { "text": "\u001b[13;5u" }
}
```

This makes VS Code forward an explicit Ctrl+Enter sequence. The binding applies to all focused integrated terminals, so omit/remove it if you prefer Ctrl+S or need another terminal application to keep its own behavior.

The center shows the conversation, the right pane shows reasoning and tool activity. Narrow terminals stack these panes; short terminals use a compact layout with scrolling rather than collapsing the prompt editor. The F2 dashboard and configuration pages remain scrollable. Your terminal controls the font; the app uses text decoration, not graphical pixel fonts or flashing effects. The interface streams actual provider reasoning only when the model emits it; it does not invent or expose unavailable hidden reasoning.

### Model labels and tool activity

Replies are labeled with the exact model that generated them (including tags such as `qwen3:4b`), rather than a generic assistant label. Changing models does not relabel older replies.

**Tool Activity** shows readable requests and outcomes: target files/directories, commands and working directories, successes, failures, denials, timeouts and output-limit notices. Command results include exit code, elapsed time, and separate **STDOUT / STDERR** sections. File reads/writes/patches and HTTP responses have bounded previews; directory results show names and directory/symlink markers. Approval dialogs still contain the authoritative edit diff.

Activity timestamps show local clock time; original UTC timestamps, raw native tool arguments/results and full bounded output stay unchanged in JSON history. Shortened previews are labeled. Older saved chats use the same readable view; unrecognized or malformed entries are explicitly shown as unformatted rather than silently dropped.

The Python project and development launch command are named `synai`.
Conversation storage lives under `~/.synai`; see the legacy cleanup instructions
below. Developers should rerun `.venv/bin/python -m pip install -e .` to refresh
editable package metadata and console scripts after updating.

## Remote Windows Ollama

Endpoint precedence is `--ollama-url`, then `OLLAMA_URL`, then `OLLAMA_HOST`, then `http://localhost:11434`.

```sh
export OLLAMA_HOST=http://192.168.1.10:11434
.venv/bin/python app.py
# Or override for one launch:
.venv/bin/python app.py --ollama-url http://192.168.1.10:11434
```

Replace the address with your inference PC's LAN IP. Windows Ollama must listen on an interface reachable from Linux; restart it after changing Windows `OLLAMA_HOST`, and allow port 11434 only on your trusted LAN. Never expose the unauthenticated service publicly.

Use the F2 dashboard's **REFRESH MODELS** after downloading or deleting models. `/api/show` discovers native tool support; models without advertised native tool capability remain **chat-only**. Unknown capability metadata is not treated as permission to parse commands from text. Model reasoning uses Ollama's model-specific defaults.

## Enable coding tools

Choose **Sandbox (Docker/Podman)** or **Host (no sandbox)** under **Execution environment** on configuration's **Execution** page. This choice is saved per conversation. Sandbox is the default; a failed sandbox never falls back to host execution.

### Host execution (no container required)

1. On **Execution**, select **Host (no sandbox)**. Image/runtime and memory/CPU/PID settings are retained for later container use but disabled/inactive in host mode.
2. On **Workspace**, click **CHOOSE DIRECTORY**. Use the arrow keys and **Enter** (or click a folder) to browse; **UP** and **HOME** navigate without typing. Hidden directories are included. **CHOOSE DIRECTORY** selects the current folder; **CANCEL / Escape** leaves your draft unchanged. Selection updates the draft only; save it to apply. Root, your entire home directory and directories exposing private SynAI storage cannot be selected. This external project is never deleted by conversation deletion. In sandbox mode the managed path stays read-only and no picker is offered.
3. Choose **SAVE SETTINGS**, or **NEXT** and **CREATE CONVERSATION** for a new conversation.
4. Read the **ENABLE HOST TOOLS // NO SANDBOX** warning. Confirm to enable tools for this opening of this conversation. Denial keeps the saved host mode but leaves tools disabled; chat/history remain available.
5. To enable later, reopen configuration's **Execution** page and click **ENABLE HOST TOOLS**. Each terminal, modifying-file and network action still requires individual approval; the approval details identify **HOST EXECUTION // NOT ISOLATED**.

Host tools run as your non-root Linux account in separate Python helper subprocesses. No Docker/Podman daemon is required. Shell commands start in the workspace but are **not confined to it**: they can access, modify or transmit anything your account can access. SynAI refuses root, enables Linux no-new-privileges before tool execution and rejects direct sudo/escalation requests; these are **not an isolation guarantee** and do not remove existing group/daemon privileges. Use a dedicated account without sudo or container-daemon privileges for untrusted work.

Host mode retains command timeout, combined output bounds, tool-call budgets, bounded workspace-relative file tools, edit previews and SHA preconditions. It does **not** enforce container memory/CPU/PID limits, read-only-root isolation or a tmpfs disk limit. Commands receive a minimal environment, with no inherited credential variables and a private temporary HOME/TMPDIR cleaned after the action. Host-installed executables on PATH remain available; put persistent dependencies in a workspace-local virtual environment rather than the temporary home.

**DISCONNECT HOST TOOLS** immediately revokes authorization without deleting files or changing saved mode. Reopening a host chat or changing its workspace requires fresh consent. Returning from a menu does not. Saving limits alone retains existing consent. Switching back to sandbox mode revokes host consent; creating/attaching a container remains an explicit approved action. Disconnect an attached container before changing its mode.

### Sandbox execution

Docker is the default container runtime (`--runtime podman` selects Podman). Its daemon must be available to your user **without host sudo**. The UI, not the model, manages containers.

1. Create a conversation with sandbox mode selected. **Workspace** displays the read-only path `~/.synai/conversations/<conversation-id>/workspace`. It starts empty and is created only when you commit the conversation, not while browsing drafts.
2. On **Execution**, set an image you trust with **Python 3.12+**, `/bin/sh`, and any required language toolchains. The default `python:3.12-slim` supplies Python only; it does not include arbitrary JavaScript/Go/Rust compilers, git or every package manager. Save the environment.
3. Open F2 **SETTINGS**, select **Execution**, then activate **CREATE SANDBOX**, inspect the exact managed workspace/image/network warning, and approve once. The app pulls the image and creates a container. It uses your non-root numeric UID/GID (at least 1000), drops all capabilities, enables no-new-privileges, sets CPU/memory/PID limits, and makes the root filesystem read-only. Only that conversation's managed workspace is bound at `/workspace`, plus a bounded private `/tmp`. SynAI source, the conversation JSON, other chats, home and the data root are not mounted.
4. If the workspace is not writable to that UID, setup fails with guidance; the app does not recursively change ownership or use sudo. Prepare permissions yourself.
5. Start a conversation and ask the model to inspect/edit/test the project. Each modifying, terminal or HTTP action displays an **ALLOW ONCE / DENY** dialog. Writes/patches show a diff and reject changes if the file changed after approval.

Alternatively, open the conversation's configuration **Execution** page, provide an existing container ID/name and click **ATTACH SANDBOX**. The app checks the same restrictions and the exact matching managed workspace mount before enabling tools. An old container mounted to SynAI source or an external project cannot attach. It refuses privileged/root containers, additional mounts, devices, capability additions, host namespaces or missing limits. Attached containers are **never removed by this app**.

**DISCONNECT / REMOVE OWNED** requires confirmation: it deletes only the currently app-owned container, not workspace files; attached containers are just detached. On quit, you can remove an owned container or leave it running and later reattach by ID. After a crash, any owned container may remain running: use your runtime to inspect/remove that specific container yourself. No unrelated container cleanup is performed.

### Tool permissions and limits

- Workspace `list_files` / `read_file`: automatic after environment activation, bounded reads within the selected workspace (`/workspace` in containers); traversal and symlinks are rejected.
- `write_file`, `patch_file`, `delete_file`: individual approval; edits are confined to workspace text files. Directory deletion is not supported.
- `terminal`: individual approval for the exact noninteractive command and cwd. Allows tests/builds/git; direct sudo/escalation requests are rejected. Runs in the selected container or directly on the host, with its filesystem/network access.
- `fetch_url`: approved HTTP(S) fetch in the selected environment; bounded output with explicit truncation.
- Sandbox mode exposes no model-controlled host shell or Docker socket. Host mode has the account's access, including any existing daemon privileges; no app-provided administration tools, root user, browser automation or plugins are added.

Default command limits: **60 seconds**, **1 MiB combined stdout/stderr**, **20 tool calls per user turn**. A turn pauses for approval before a bounded tool-budget extension. Cancellation/timeout terminates the active helper/command process group and preserves partial results. Noninteractive commands are supported; persistent daemons, terminal editors and full PTY sessions are not.

Commands that deliberately detach from their process group are not supported and may survive cancellation. In sandbox mode, remove the owned container to terminate all its remaining processes. In host mode there is no container cleanup boundary: inspect and stop specific surviving processes yourself. Do not use the agent to launch background services.

In sandbox mode, the read-only runtime means system-wide installs will not work. Supply tools in your image, or use approved non-root commands to install dependencies into the workspace, such as a project virtual environment. The model is not allowed to work around missing tools with sudo.

## History and recovery

Each conversation has a private folder outside the application source:

```text
~/.synai/
  settings.json            # global connection and selected theme
  conversations/
    <conversation-id>/
      conversation.json
      workspace/           # allocated for sandbox mode
  legacy-cleanup.json      # one-time cleanup receipt, when applicable
```

`conversation.json` contains the execution environment, selected model/workspace binding, origin endpoint, messages, reasoning, native tool requests/results, approvals, activity and limits. Each new assistant message records its actual model, provider and endpoint, including partial or failed responses; this metadata is not sent as part of the provider message format. Only `workspace/` is mounted into a sandbox; metadata and application preferences stay outside. **F2 / CONVERSATION DETAILS** shows the storage folder and current workspace. Host-only chats need no managed workspace until switching to sandbox. Switching modes does not copy an external project into the managed workspace or delete previous generated files. A previously allocated workspace that goes missing is reported, not silently reconstructed.

Choose **CONVERSATIONS** in F2, highlight a row and press **Enter / OPEN**. Its saved execution environment is restored automatically while the global connection stays active. Interrupted messages are marked and outstanding tools receive interruption results; **pending actions are never replayed**. Reattach a validated container bound to that workspace for sandbox tools, or give fresh host-access consent for host tools. Missing models/history errors are shown explicitly. Use F2's model selector for a confirmed model change, Settings for workspace edits, and Connection Settings for endpoint edits. History context is not silently truncated; if Ollama rejects a long context, start a new conversation.

New histories use schema version 5, requiring managed-layout sandbox paths and an explicit execution mode. Existing managed schema-4 histories remain readable and upgrade when saved: their endpoint and provider timeout are preserved as historical metadata, not restored as connection settings. Older replies receive legacy attribution from the recorded conversation model/endpoint; request-level details not previously recorded cannot be reconstructed. No bulk history migration occurs. Authorization is never restored from history, even if prior host approvals were recorded.

**DELETE SELECTED** in the **CONVERSATIONS** browser permanently deletes each checked conversation's **entire managed folder**, including generated code and all files in its managed workspace. External host project directories are never deleted. Disconnect an attached sandbox first; a saved running container or unavailable runtime blocks deletion until the workspace is demonstrably unused. Corrupt metadata with an existing workspace must be repaired before destructive deletion so container use can be checked.

To delete chats, press **F2**, then **CONVERSATIONS**. Check rows with **Space** or click (checked deletion markers turn red), choose **DELETE SELECTED**, review the folder paths and generated-file loss warning, then **DELETE PERMANENTLY**. Opening uses the highlighted row independently of checked rows; **Enter opens and does not toggle a checkbox**. **CLOSE / Escape** returns to the main menu. Escape during a confirmation cancels that operation. Failures remain checked for retry; no unrelated directory or container is cleaned up. Deleting an inactive chat preserves your current draft.

### Resetting existing conversations

Old flat version-1/2/3 histories are **not imported or resumed**. At first startup, SynAI offers an explicit cleanup of histories found in the previous default directory (`~/.local/share/local-coding-agent/history`) and any old `AGENT_HISTORY_DIR` override. Review the exact file list before **DELETE OLD HISTORIES**. Declining preserves everything and dismisses the automatic offer.

Old workspace/project/source files are always preserved. Container removal requires a separate confirmation and proof of current app ownership. Old random ownership tokens were not saved across launches, so many old containers cannot be verified: they remain untouched, with their runtime/ID listed for manual inspection. A further confirmation is required to delete their history references while leaving those containers running. Removal failures preserve affected histories for retry. **RETRY LEGACY CLEANUP** appears after a failed cleanup.

`--history-dir` is retired and produces an actionable error. `AGENT_HISTORY_DIR` no longer redirects active storage; it is considered only for confirmed legacy cleanup. The active data location is fixed at `~/.synai`.

## Configuration

```sh
synai --help
synai --command-timeout 120 --tool-budget 30 --output-bytes 2097152
```

| Setting | Flag | Environment / default |
|---|---|---|
| Global Ollama endpoint | `--ollama-url` | `OLLAMA_URL`, `OLLAMA_HOST`, saved setting, localhost |
| Global provider stream idle timeout | `--request-timeout` | `AGENT_REQUEST_TIMEOUT`, legacy `BENCHMARK_REQUEST_TIMEOUT`, saved setting, 1200 seconds |
| Conversation storage | Fixed location | `~/.synai` |
| Container runtime | `--runtime` | `AGENT_RUNTIME`, docker |
| Default trusted image | `--image` | `AGENT_IMAGE`, python:3.12-slim |
| Command timeout | `--command-timeout` | 60 seconds |
| Captured command output | `--output-bytes` | 1048576 bytes |
| Tool calls per turn | `--tool-budget` | 20 |
| Execution mode | Per-conversation UI only | sandbox |

Managed containers default to 1 GiB RAM, 2 CPUs, 128 PIDs and a 256 MiB temporary filesystem. Memory/CPU/PID policy is saved per conversation and editable in its environment; temporary-filesystem size remains fixed. Existing containers must also have explicit limits. Streamed reply text is capped at 4 MiB per assistant round; the UI shortens very large text for responsiveness and labels that shortening. Full bounded text remains in history.

## Safety boundary

**Host mode has no OS isolation.** Workspace restrictions on file tools and command-text checks do not constrain arbitrary generated programs, prevent filesystem races, or remove account/group privileges. No-new-privileges prevents acquiring privileges via setuid/file-capability execution, but it does not block existing access, daemon APIs, resource exhaustion or all possible escalation paths. Do not treat the app's no-sudo policy as a host security boundary. Approve only commands you trust and keep credentials outside the account/workspace wherever possible.

The sandbox is **risk reduction, not a security guarantee**. The selected workspace is accessible to approved commands, and network access may transmit its data. Only trust container images you control; malicious images, kernel/runtime bugs and unsafe external configuration are outside the app's guarantees. Removing the word `sudo` alone does not prevent escalation: the app enforces non-root execution, dropped capabilities and no-new-privileges and fails closed for tool setup. Use a dedicated account/rootless runtime where feasible and keep credentials out of the workspace.

Managed mounts exclude the host SynAI source/install directory and conversation metadata. This does not hide source deliberately copied into a workspace, baked into a selected image, pasted into prompts or served over a reachable network endpoint. Use trusted images and do not publish sensitive source through external services. The selected workspace is accessible to approved commands, and network access may transmit its data. Only trust container images you control; malicious images, kernel/runtime bugs and unsafe external configuration are outside the app's guarantees. Removing the word `sudo` alone does not prevent escalation: the app enforces non-root execution, dropped capabilities and no-new-privileges and fails closed for tool setup. Use a dedicated account/rootless runtime where feasible and keep credentials out of the workspace.

Terminal tools are more powerful than strict file tools. They can modify any writable workspace file and use network destinations without domain allowlisting once that **specific command** is approved. Approval is not proof that a command is safe; inspect commands and diffs before allowing them.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Default tests mock provider/container operations, use Textual's headless test runner and run real host helpers in disposable temporary workspaces (with a loopback HTTP test server). To explicitly opt into pulling a test image and creating/removing one disposable non-root Docker container in a temporary workspace:

```sh
AGENT_CONTAINER_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

Integration tests do not modify unrelated containers or existing project workspaces.

For the focused UI consistency checks:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_ui_consistency.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_theme_picker.py' -v
```

These checks cover modal alignment, complete footer labels and long directory paths at 60x20, 80x24, 100x36 and 140x45; arrow-only dashboard reachability; Enter-gated field editing and paste; Help/refocus and resize ownership; safe focus during failed model discovery; active-form paging; and read-only details during streaming. Related navigation, theme, history, directory-picker and approval suites run with the full command above. Headless rendering does not verify key forwarding by VS Code or a particular terminal; use F1 to check shortcuts interactively after launch.

Theme-picker checks additionally verify preview/cancel without preference writes, explicit confirmation, restart persistence, failed-save retry, rollback to an unsaved original theme, rapid preview transitions, modal guards, field focus restoration and streaming continuity.

## Maintaining bundled editor assets

Pinned Neovim and mini.nvim archives and their notices live under
`synai/editor/vendor`. `runtime.json` records exact versions, upstream URLs,
lengths and SHA-256 checksums. Normal application startup never downloads
editor assets. To explicitly restore the pinned files during development:

```sh
.venv/bin/python scripts/prepare_editor_assets.py
# Or restore from previously downloaded files without network access:
.venv/bin/python scripts/prepare_editor_assets.py --from-directory /path/to/assets
```

Review upstream licenses, checksums, binary compatibility and extracted size
before updating these assets. The editor needs glibc 2.34+, `libgcc_s.so.1`,
and executable temporary storage. Keep it within the sandbox's existing
256 MiB tmpfs with room for swaps/recovery. Run the editor tests after changes.

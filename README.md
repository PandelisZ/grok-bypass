# Grok CLI with OpenAI GPT-5.5

This repo documents a supported way to run the Grok CLI against an
OpenAI-compatible provider instead of the default xAI-hosted Grok backend.

The useful setup is an isolated `GROK_HOME` plus a tiny launcher:

- `~/.grok-openai/config.toml` contains only the OpenAI model config.
- `~/.local/bin/grok-openai` sets `GROK_HOME=~/.grok-openai` and launches Grok.
- `OPENAI_API_KEY` stays in your shell environment. It is not stored in this repo.

This does not patch the Grok binary or bypass xAI entitlements. It uses Grok's
custom model/provider configuration path.

## Install

From this repo:

```bash
mkdir -p ~/.grok-openai ~/.local/bin
cp config/grok-openai.config.toml ~/.grok-openai/config.toml
cp bin/grok-openai ~/.local/bin/grok-openai
chmod +x ~/.local/bin/grok-openai
```

Make sure `OPENAI_API_KEY` is set:

```bash
export OPENAI_API_KEY="sk-..."
```

For a persistent zsh setup:

```bash
printf '\nexport OPENAI_API_KEY="sk-..."\n' >> ~/.zshrc
```

## Use

Launch the TUI through the OpenAI-only home:

```bash
grok-openai
```

Useful variants:

```bash
grok-openai --cwd ~/w/my-repo
grok-openai --no-alt-screen
grok-openai -p "say hi"
```

The TUI should show the normal start menu (`New worktree`, `Resume session`,
`Quit`) rather than the SuperGrok subscription screen.

## What The Launcher Does

`bin/grok-openai` is intentionally small:

```sh
export GROK_HOME="${GROK_OPENAI_HOME:-$HOME/.grok-openai}"
exec "$HOME/.local/bin/grok" \
  -m gpt-5.5 \
  --no-memory \
  --disable-web-search \
  "$@"
```

The separate `GROK_HOME` matters because the normal `~/.grok` directory can
contain xAI login state and TUI defaults that still route into Grok-specific
subscription checks. Keeping OpenAI in `~/.grok-openai` avoids that state.

## Verify

Check model discovery:

```bash
grok-openai models
```

Expected shape:

```text
You are not authenticated.

Default model: gpt-5.5

Available models:
  * gpt-5.5 (default)
  - grok-build
```

Run a headless inference through Grok:

```bash
grok-openai -p "Reply exactly wrapper-responses-ok" --verbatim --output-format plain
```

Expected stdout:

```text
wrapper-responses-ok
```

Known caveat with `grok 0.1.210`: after the successful primary response, the
binary may emit a non-fatal stderr line for an internal fallback request using
model id `grok-build`:

```text
responses API error ... The requested model 'grok-build' does not exist.
```

The command still exits `0` and stdout contains the OpenAI `gpt-5.5` response.
The config keeps `[model.grok-build]` pointed at OpenAI's Responses endpoint so
this fallback does not use the xAI proxy, but this binary version still sends
`grok-build` as the request model id for that internal call.

Confirm OpenAI Responses directly:

```bash
curl -sS https://api.openai.com/v1/responses \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","input":"Reply exactly direct-responses-ok","max_output_tokens":256}' \
  | jq -r '.output_text // (.output[]?.content[]?.text) // .error.message // .'
```

Expected stdout:

```text
direct-responses-ok
```

## Subagents

Subagents are enabled in `config/grok-openai.config.toml`:

```toml
[subagents]
enabled = true
default_model = "gpt-5.5"
```

The wrapper does not pass `--no-subagents`, so parent and child sessions can use
OpenAI `gpt-5.5`.

Verified with a forced `explore` subagent:

```bash
grok-openai \
  -p "Use the task tool exactly once to ask an explore subagent to answer with exactly the text subagent-openai-ok. Then return exactly what the subagent said." \
  --verbatim \
  --output-format streaming-json
```

Evidence from the saved session:

```text
tool: spawn_subagent
subagent_type: explore
subagent_id: 019e28d9-c3f6-7043-92de-655d90ddf96a
parent model: gpt-5.5-2026-04-23
child model: gpt-5.5-2026-04-23
child output: subagent-openai-ok
```

## Codex OAuth Bridge

`bridge/codex_bridge.py` is a tiny local OpenAI-compatible HTTP server backed by
the same ChatGPT OAuth Responses path used by the Codex CLI. It reads your local
Codex auth from `~/.codex/auth.json`, sends requests to
`https://chatgpt.com/backend-api/codex/responses`, and refreshes expired access
tokens using the stored refresh token. Tokens are never printed by the bridge.

Install the bridge helpers:

```bash
mkdir -p ~/.grok-codex-bridge ~/.local/bin ~/.local/share/grok-codex-bridge
cp config/grok-codex-bridge.config.toml ~/.grok-codex-bridge/config.toml
cp bridge/*.py ~/.local/share/grok-codex-bridge/
cp bin/codex-bridge ~/.local/bin/codex-bridge
cp bin/grok-codex-bridge ~/.local/bin/grok-codex-bridge
chmod +x ~/.local/share/grok-codex-bridge/codex_bridge.py \
  ~/.local/bin/codex-bridge \
  ~/.local/bin/grok-codex-bridge
```

Start the local bridge in one terminal:

```bash
codex-bridge --port 11435
```

Point Grok at it in another terminal:

```bash
grok-codex-bridge
```

Headless smoke test:

```bash
grok-codex-bridge \
  -p "Reply exactly grok-codex-bridge-ok" \
  --verbatim \
  --output-format plain
```

Expected stdout:

```text
grok-codex-bridge-ok
```

Direct bridge checks:

```bash
curl -sS http://127.0.0.1:11435/v1/models | jq .

curl -sS http://127.0.0.1:11435/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"codex-gpt-5.5","input":"Reply exactly bridge-ok"}' \
  | jq -r '.output_text // .error.message // .'
```

Notes:

- The bridge binds to `127.0.0.1` by default.
- It implements the small subset Grok needs: `/v1/models`, `/v1/responses`,
  and `/v1/chat/completions`.
- `CODEX_BRIDGE_UPSTREAM_MODEL` controls the underlying Codex model; default is
  `gpt-5.5`.
- `CODEX_HOME` controls which Codex auth store is used; default is `~/.codex`.

## Files In This Repo

- `bin/grok-openai` - reusable launcher script.
- `bin/codex-bridge` - local HTTP bridge launcher.
- `bin/grok-codex-bridge` - Grok launcher pointed at the local Codex bridge.
- `config/grok-openai.config.toml` - OpenAI-only Grok home config template.
- `config/grok-codex-bridge.config.toml` - Grok config template for the Codex bridge.
- `bridge/codex_bridge.py` - local OpenAI-compatible HTTP bridge.
- `bridge/codex_auth.py` - Codex OAuth token loading and refresh.
- `bridge/codex_wire.py` - request/response shaping for Codex Responses.
- `custom-inference-provider.md` - detailed notes on Grok custom providers.
- `grok-reverse-engineering-notes.md` - reverse-engineering notes and boundaries.

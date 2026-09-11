# hermes-devin

Use [Devin](https://devin.ai) / Windsurf Cascade models in
[Hermes Agent](https://hermes-agent.nousresearch.com) — a model-provider plugin
that speaks the Cascade Connect-RPC + protobuf API directly. No gateway process,
no Node/Bun, no third-party dependencies: just Python stdlib.

Protocol implementation ported from
[devin-gateway](https://github.com/CaiJingLong/devin-gateway). Requires an active
Devin or Windsurf subscription — this plugin does not provide an account or quota.

## Install

```bash
hermes plugins install vaayne/hermes-devin
```

Or manually — drop the directory at `$HERMES_HOME/plugins/model-providers/devin/`
(any name works for the installed-plugins path; `plugin.yaml` declares
`kind: model-provider`).

## Sign in

Three ways to get a `devin-session-token$…` credential, in resolution order:

1. **OAuth login (recommended)** — ships with the plugin, stdlib-only:

   ```bash
   python3 ~/.hermes/plugins/hermes-devin/login.py
   # --paste for SSH/headless, --print to just print, --status to check
   ```

   Opens a browser for Devin sign-in, exchanges the callback for a token, and
   writes `DEVIN_API_KEY=…` to `~/.hermes/.env`.

2. **Devin CLI** — if you've run `devin auth login`, you're done: the plugin reads
   `~/.local/share/devin/credentials.toml` (`windsurf_api_key`) automatically.

3. **Manual** — put `DEVIN_API_KEY=<token>` in `~/.hermes/.env` yourself.
   `~/.devin-gateway/token` is also picked up.

`DEVIN_BASE_URL` overrides the API endpoint (default `https://server.codeium.com`).

## Use

```bash
hermes --provider devin -m claude-opus-4-8-high
hermes model          # interactive picker, lists the live Cascade catalog
```

Aliases: `devin-gateway`, `windsurf`, `cascade`.

## Notes

- The model list comes from the live `GetCliModelConfigs` RPC; a bundled snapshot
  is the offline fallback. Unknown Cascade UIDs pass through verbatim.
- Reasoning effort is encoded in the model id (`-low` / `-medium` / `-high` /
  `-xhigh` / `-max` / `-none`), e.g. `claude-opus-4-8-high`.
- Auxiliary tasks (compression, summarization) default to `gpt-5-4-mini-low`.
- Tool-call argument fragments are buffered and emitted once, complete, at the
  end of each response.

## Layout

| File | Role |
| --- | --- |
| `__init__.py` | `ProviderProfile` registration, credential bridging, model discovery |
| `_proto.py` | protobuf wire codec + Cascade message shapes |
| `_cascade.py` | Connect-RPC transport (`GetUserJwt`, `GetChatMessage`, `GetCliModelConfigs`) |
| `_client.py` | OpenAI-compatible client shim + message conversion |
| `login.py` | OAuth PKCE login flow (port of devin-gateway's login CLI) |

## License

MIT — see [LICENSE](LICENSE). Protocol derived from
[devin-gateway](https://github.com/CaiJingLong/devin-gateway) (MIT, CaiJingLong).

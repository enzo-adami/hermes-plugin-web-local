# hermes-plugin-web-local

Keyless **local web extract** provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
It fetches pages directly from your machine with `httpx` and reduces them to readable
text with [`trafilatura`](https://github.com/adbar/trafilatura) (Apache-2.0). No API key,
no third-party extraction service. It only extracts: pair it with any search provider
(`ddgs`, `searxng`, `brave-free`, ...).

## Install

```bash
hermes plugins install https://github.com/enzo-adami/hermes-plugin-web-local
```

Then select it for extraction in `config.yaml`:

```yaml
web:
  extract_backend: local
  search_backend: ddgs   # keep an explicit search backend
```

Until NousResearch/hermes-agent skips extract-only providers during search auto-detection,
keep `web.search_backend` explicit. Otherwise an always-available extract-only provider
can be picked for search.

## What it does (disclosure)

- **Network**: requests only the URLs the agent asks to extract, from this machine.
  It contacts no other service, sends no telemetry and stores no credentials.
- **Safety**: the SSRF check (`tools.url_safety`) and the website policy
  (`tools.website_policy`) run again on **every** redirect hop. Redirects are followed
  manually, at most 5 hops. Bodies are capped at 10 MB.
- **Time**: each URL is waited at most 60 s. A batch stays inside the caller's
  `web.extract_timeout` (120 s by default) minus a small margin, so completed pages are
  kept when later ones hang. A body that trickles in slowly is cut at the deadline.
  Threads are not cancelled: a blocked DNS lookup or read can outlive the wait.
- **Content**: HTML goes through trafilatura's in-memory API only (its own
  `fetch_url` is never called). `text/plain` is returned as is. An unknown charset
  falls back to UTF-8 instead of failing.

## Troubleshooting: `trafilatura is unavailable`

The plugin declares `trafilatura` in `plugin.yaml`, but that only records the
requirement — it does not install it into the environment your agent process is
running. A Hermes install can hold several `environments/*/venv` directories, and
`hermes pm repair` repairs the *recorded* one, which is not necessarily the one
the running gateway uses. Symptom: `hermes pm repair` prints
"dependency environment repaired" and every extract still fails.

Find the interpreter that actually matters, then install into it:

```bash
# the interpreter of the running process
python -c "import sys; print(sys.executable)"

# every env on the box, to see which one lacks the module
for v in ~/.hermes/installs/*/environments/*/venv; do
  printf '%s: ' "$v"; "$v/bin/python" -c "import trafilatura; print('ok')" 2>&1 | tail -1
done

# uv-managed venvs usually have no pip
uv pip install --python <that python> 'trafilatura>=2.0,<3'
# or, in a venv with pip
<that python> -m pip install 'trafilatura>=2.0,<3'
```

No restart is needed for a pure-Python dependency: the next extract imports it.
Since core silently falls back to a keyless extractor when a backend fails, check
`~/.hermes/logs/errors.log` for `keyless rescue` to be sure the plugin was the one
that served the page.

## Tests

Run with the Python of a Hermes install, so that `agent` and `tools` can be imported. You can also
point `HERMES_AGENT_REPO` at a hermes-agent checkout:

```bash
HERMES_AGENT_REPO=~/.hermes/hermes-agent python -m pytest tests -q
```

## Origin

This plugin was first proposed in-tree as NousResearch/hermes-agent#90188. It is published here as a
standalone plugin, following the project's policy for third-party integrations.

## License

MIT

# lmstudio-to-litellm

Keeps a LiteLLM model list in sync with LM Studio downloads. A single-file Python service that runs as a Docker container, polls the LM Studio API and the LiteLLM management API, and reconciles the two.

## What it does each cycle

1. Reads `GET {LMS_BASE_URL}/api/v1/models` from LM Studio (only entries with `"type": "llm"`).
2. Reads `GET /model/info` from LiteLLM and keeps only managed entries — those whose `model_name` starts with `MODEL_PREFIX`, or that carry the `MODEL_TAG` tag. The LM Studio key is stored in each entry's `litellm_params.model`.
3. Adds models present in LM Studio but missing in LiteLLM (`POST /model/new`).
4. Deletes managed models no longer present in LM Studio (`POST /model/delete`).
5. Refreshes changed entries via `PATCH /model/{id}/update`: the full LM Studio model info is stored under `model_info.lmstudio_info`, and `model_name` is reset to `{MODEL_PREFIX}{key}` if it has drifted (e.g. after a prefix change).

Safety rule: if the LM Studio side cannot be read, the whole cycle is skipped and nothing is ever deleted.

By default it runs forever, one cycle every `POLL_INTERVAL` seconds; pass `--once` to run a single cycle and exit (for testing).

To remove **all** managed models in one shot — even those still present in LM Studio — run `python3 main.py --once --delete-all`. Without `--yes` it only lists the targets and deletes nothing; with `--yes` it deletes them.

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` in this directory and fill in real values — or fetch the example straight from GitHub:

```sh
curl -o .env https://raw.githubusercontent.com/davidassigbi/lmstudio-to-litellm/main/.env.example
```

`.env` is gitignored (chmod 600) and never committed or baked into the image. Docker Compose reads it automatically, and standalone runs use `--env-file .env`.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `LMS_BASE_URL` | yes | — | LM Studio native API base URL (no `/v1` suffix), e.g. `http://192.168.1.50:1234` |
| `LMS_API_TOKEN` | no | *(empty)* | Optional LM Studio API token; sent as Bearer auth on LM Studio requests when set |
| `LITELLM_BASE_URL` | yes | — | LiteLLM proxy base URL, e.g. `http://localhost:4000` |
| `LITELLM_MASTER_KEY` | yes | — | Master key for the management endpoints (`/model/new`, `/model/info`, `/model/delete`) |
| `LITELLM_CREDENTIAL_NAME` | yes | — | Name of the LiteLLM credential holding the LM Studio API token |
| `HTTP_TIMEOUT` | no | `60` | Seconds per HTTP request |
| `MODEL_TAG` | no | `lmstudio-to-litellm` | Tag applied to managed models; also used for matching |
| `MODEL_PREFIX` | no | *(empty)* | Prefix on managed model names; also used for matching |
| `POLL_INTERVAL` | no | `300` | Seconds between cycles |
| `LOG_LEVEL` | no | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, ...) |

At least one of `MODEL_PREFIX` and `MODEL_TAG` must be non-empty — with both empty there is no way to identify managed models, so the service exits on startup.

## Running (dev)

Builds the image locally and mounts this directory into `/app`, so edits to `main.py` are live without rebuilding:

```sh
docker compose -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.dev.yml exec lmstudio-to-litellm sh
docker compose -f docker-compose.dev.yml exec lmstudio-to-litellm python3 main.py --once   # one cycle and exit
```

## Running (prod)

Runs the published registry image; no local build, no volume mount. Set `LMSTUDIO_LITELLM_IMAGE` to your published image:

```sh
docker compose -f docker-compose.prod.yml up -d
```

## Running without compose

**With a `.env` file (day-to-day):**
```sh
curl -o .env https://raw.githubusercontent.com/davidassigbi/lmstudio-to-litellm/main/.env.example
docker run -d --name lmstudio-to-litellm --restart unless-stopped \
  --env-file .env ghcr.io/davidassigbi/lmstudio-to-litellm:latest
```

**With env vars inlined (no file at all):** only the four required vars shown; optional ones have defaults in `main.py`. Inline secrets land in your shell history, so prefer `.env` for day-to-day use:
```sh
docker run -d --name lmstudio-to-litellm --restart unless-stopped \
  -e LMS_BASE_URL=http://<windows-ip>:1234 \
  -e LITELLM_BASE_URL=http://localhost:4000 \
  -e LITELLM_MASTER_KEY=sk-... \
  -e LITELLM_CREDENTIAL_NAME=lms-token \
  ghcr.io/davidassigbi/lmstudio-to-litellm:latest
```


## Files

- `main.py` — the sync service (stdlib + `requests`)
- `Dockerfile` — `python:3.12-slim`, unprivileged user, outbound-only HTTP
- `requirements.txt` — runtime dependencies
- `docker-compose.dev.yml` / `docker-compose.prod.yml` — dev/prod compose files

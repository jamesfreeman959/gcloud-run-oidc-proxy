# gcloud-run-oidc-proxy

A small HTTP proxy that **mints/caches a Google Cloud Run ID token** (OIDC) from a **mounted service account JSON key** and forwards requests to your Cloud Run service with `Authorization: Bearer <token>` injected.

This is useful when you want an OpenAI-compatible client (e.g. LiteLLM) to talk to a **private Cloud Run** endpoint without teaching the client how to mint GCP ID tokens.

Architecture:

```
Client (LiteLLM) -> token-proxy -> Cloud Run (authenticated via ID token)
```

## What it does
- Accepts inbound HTTP requests on `:8080` and forwards them to `CLOUD_RUN_URL`.
- Mints a **Cloud Run ID token** using `GOOGLE_APPLICATION_CREDENTIALS` (service account JSON key).
- Caches the token and refreshes it near expiry.
- Supports **streaming (SSE)** passthrough for OpenAI-style `stream=true` responses.

## Configuration
Required environment variables:
- **`CLOUD_RUN_URL`**: Your Cloud Run service URL (e.g. `https://your-service-xyz.a.run.app`)
- **`GOOGLE_APPLICATION_CREDENTIALS`**: Path inside the container to the mounted service account JSON (e.g. `/etc/proxy/service-account.json`)

Optional:
- **`HTTP_TIMEOUT_S`**: Upstream timeout (default `600`)
- **`TOKEN_REFRESH_SKEW_S`**: Refresh token this many seconds before expiry (default `60`)

## Run locally (Docker Compose)
1. Create a real `.env` from the example:

```bash
cp .env.example .env
```

2. Edit `.env` and set `CLOUD_RUN_URL` to your real Cloud Run URL.

3. Put your real service account key somewhere **not committed** (example path used by compose):
- `./secrets/service-account.json`

4. Start the proxy:

```bash
docker compose up --build
```

The proxy listens on `http://localhost:8080`.

Health check:

```bash
curl http://localhost:8080/healthz
```

## LiteLLM configuration
Configure LiteLLM (or any OpenAI-compatible client) to point at the proxy:
- **API Base**: `http://token-proxy:8080` (inside compose) or `http://localhost:8080` (from host)
- **API Key**: any dummy value (auth happens at the proxy)

## Test with curl (before integrating anything)
These examples assume your Cloud Run service implements **OpenAI-compatible** endpoints (for example `/v1/chat/completions`).

From your host machine (after `docker compose up`), run a **non-streaming** test:

```bash
curl -sS http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dummy' \
  -d '{
    "model": "your-model-name",
    "messages": [{"role":"user","content":"Say hello in one short sentence."}],
    "temperature": 0
  }'
```

If your upstream supports **streaming**, run a streaming test (prints tokens/events as they arrive):

```bash
curl -N http://localhost:8080/v1/chat/completions?stream=true \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -H 'Authorization: Bearer dummy' \
  -d '{
    "model": "your-model-name",
    "messages": [{"role":"user","content":"Write a 10-word sentence about testing proxies."}],
    "temperature": 0,
    "stream": true
  }'
```

Notes:
- The `Authorization: Bearer dummy` header is optional for the proxy; it will be replaced when forwarding upstream.
- If you get a `401/403` from Cloud Run, double-check `CLOUD_RUN_URL` and that the service account has permission to invoke the service.

## Publishing to GHCR
This repo includes a GitHub Actions workflow that builds and publishes a container image to GHCR:
- On pushes to `main`: tags `latest`
- On tags like `v1.2.3`: tags `v1.2.3`

Image name:
- `ghcr.io/<owner>/<repo>`

## Security notes
- This repo intentionally contains **no real URLs or credentials**.
- Do **not** commit `.env` or any service account JSON key. The provided `.gitignore` blocks common secret file patterns.
- Prefer short-lived credentials where possible; a long-lived JSON key should be treated as sensitive and rotated appropriately.
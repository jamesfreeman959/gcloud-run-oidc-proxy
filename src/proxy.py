import os
import time
from typing import Dict, Iterable, Optional

import google.auth.transport.requests
import google.oauth2.service_account
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse


app = FastAPI(title="gcloud-run-oidc-proxy")


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _strip_trailing_slash(url: str) -> str:
    return url[:-1] if url.endswith("/") else url


_CLOUD_RUN_URL = _strip_trailing_slash(_require_env("CLOUD_RUN_URL"))
_SA_KEY_FILE = _require_env("GOOGLE_APPLICATION_CREDENTIALS")
_HTTP_TIMEOUT_S = _float_env("HTTP_TIMEOUT_S", 600.0)

# Cache token until close to expiry. This is conservative and avoids refresh storms.
_TOKEN_REFRESH_SKEW_S = _int_env("TOKEN_REFRESH_SKEW_S", 60)

_cached_creds: Optional[google.oauth2.service_account.IDTokenCredentials] = None
_cached_token: Optional[str] = None
_cached_token_exp: Optional[int] = None  # epoch seconds


def _base_headers(request: Request) -> Dict[str, str]:
    # Note: FastAPI/Starlette headers are case-insensitive, but we normalize to simple dict.
    incoming = dict(request.headers)

    # Hop-by-hop headers should never be forwarded. See RFC 7230 §6.1.
    hop_by_hop = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        # Always replace any inbound Authorization with our minted ID token.
        # (Avoid duplicates due to header casing differences.)
        "authorization",
    }

    return {k: v for k, v in incoming.items() if k.lower() not in hop_by_hop}


def _response_headers(upstream_headers: httpx.Headers) -> Dict[str, str]:
    hop_by_hop = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }

    out: Dict[str, str] = {}
    for k, v in upstream_headers.items():
        if k.lower() in hop_by_hop:
            continue
        out[k] = v
    return out


def _get_or_refresh_creds() -> google.oauth2.service_account.IDTokenCredentials:
    global _cached_creds
    if _cached_creds is None:
        _cached_creds = (
            google.oauth2.service_account.IDTokenCredentials.from_service_account_file(
                _SA_KEY_FILE,
                target_audience=_CLOUD_RUN_URL,
            )
        )
    return _cached_creds


def _get_id_token() -> str:
    global _cached_token, _cached_token_exp

    now = int(time.time())
    if (
        _cached_token is not None
        and _cached_token_exp is not None
        and now < (_cached_token_exp - _TOKEN_REFRESH_SKEW_S)
    ):
        return _cached_token

    creds = _get_or_refresh_creds()
    req = google.auth.transport.requests.Request()
    creds.refresh(req)

    token = creds.token
    if not token:
        raise RuntimeError("Failed to mint ID token (empty token).")

    exp = getattr(creds, "expiry", None)
    if exp is not None:
        _cached_token_exp = int(exp.timestamp())
    else:
        # Fallback: refresh on every request if expiry is unavailable.
        _cached_token_exp = None

    _cached_token = token
    return token


def _looks_like_streaming_request(request: Request, body_bytes: bytes) -> bool:
    # OpenAI-style streaming often uses stream=true or Accept: text/event-stream.
    q = request.query_params.get("stream")
    if q is not None and q.lower() in {"1", "true", "yes"}:
        return True

    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept.lower():
        return True

    # If content-type is json, do a cheap substring scan to avoid json parsing.
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype and b'"stream"' in body_bytes:
        # Heuristic: ensure it isn't stream:false
        if b'"stream":true' in body_bytes.replace(b" ", b""):
            return True

    return False


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy(request: Request, path: str):
    body = await request.body()
    stream_request = _looks_like_streaming_request(request, body)

    token = _get_id_token()
    headers = _base_headers(request)
    headers["Authorization"] = f"Bearer {token}"

    upstream_url = f"{_CLOUD_RUN_URL}/{path}" if path else _CLOUD_RUN_URL

    timeout = httpx.Timeout(_HTTP_TIMEOUT_S)

    # Use a streaming request so we can relay SSE without buffering.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream(
            method=request.method,
            url=upstream_url,
            params=request.query_params,
            headers=headers,
            content=body if body else None,
        ) as upstream:
            status_code = upstream.status_code
            resp_headers = _response_headers(upstream.headers)
            media_type = upstream.headers.get("content-type")

            should_stream = stream_request or (
                media_type is not None and "text/event-stream" in media_type.lower()
            )

            if should_stream:
                async def _aiter_bytes() -> Iterable[bytes]:
                    async for chunk in upstream.aiter_bytes():
                        if chunk:
                            yield chunk

                return StreamingResponse(
                    _aiter_bytes(),
                    status_code=status_code,
                    headers=resp_headers,
                    media_type=media_type,
                )

            content = await upstream.aread()
            return Response(
                content=content,
                status_code=status_code,
                headers=resp_headers,
                media_type=media_type,
            )

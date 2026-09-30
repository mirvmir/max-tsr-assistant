"""Technical HTTP surface only; user commands are handled through the durable inbox."""
from __future__ import annotations

from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from tsr.adapters.max.ingress import parse_update, persist_update, secret_value, verify_secret


class StatusResponse(BaseModel):
    status: str


_UPDATE_SCHEMA = {
    'type': 'object', 'required': ['update_type', 'timestamp'],
    'properties': {'update_type': {'type': 'string', 'maxLength': 64},
                   'timestamp': {'type': 'integer', 'format': 'int64', 'description': 'Unix milliseconds'}},
    'additionalProperties': True,
    'description': 'MAX Update. Required event fields are validated; irrelevant fields are discarded before persistence.',
}


def create_app(settings, db, application=None) -> FastAPI:
    app = FastAPI(title='MAX TSR technical API', version='0.1.0', openapi_version='3.1.0',
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.application = application
    ready_key = APIKeyHeader(name='X-Readiness-Secret', auto_error=False, scheme_name='ReadinessSecret')

    @app.get('/health/live', response_model=StatusResponse, operation_id='health_live')
    async def live():
        return StatusResponse(status='live')

    @app.get('/health/ready', response_model=StatusResponse, operation_id='health_ready',
             responses={401: {'description': 'Invalid readiness credential'},
                        503: {'description': 'Readiness credential not configured, or DB/worker/release/secrets/subscription/restore gate unavailable. Authenticated degraded response: {"status":"degraded"}.'}})
    async def ready(supplied: str | None = Security(ready_key)):
        expected = secret_value(settings.readiness_secret)
        if not expected:
            raise HTTPException(status_code=503, detail='not_ready')
        if not verify_secret(supplied, expected):
            raise HTTPException(status_code=401, detail='unauthorized')
        try:
            from tsr.adapters.max.readiness import collect_readiness
            health = await run_in_threadpool(collect_readiness, settings, db, application)
            healthy = health.status == "ready"
        except Exception:
            healthy = False
        if not healthy:
            return JSONResponse(status_code=503,content={'status':'degraded'})
        return StatusResponse(status='ready')

    @app.post('/webhooks/max', response_model=StatusResponse, operation_id='receive_max_update',
              responses={400: {'description': 'Invalid MAX update'}, 401: {'description': 'Invalid webhook secret'},
                         413: {'description': 'Body exceeds configured byte limit'},
                         415: {'description': 'JSON content type required'},
                         503: {'description': 'Durable commit unavailable; sender must retry'}},
              openapi_extra={'requestBody': {'required': True, 'content': {'application/json': {'schema': _UPDATE_SCHEMA}}},
                             'parameters': [{'in': 'header', 'name': 'X-Max-Bot-Api-Secret', 'required': True,
                                             'schema': {'type': 'string'}, 'description': 'Secret registered with MAX subscription'}]})
    async def webhook(request: Request):
        expected = secret_value(settings.max_webhook_secret)
        if not expected:
            raise HTTPException(status_code=503, detail='not_ready')
        supplied = request.headers.getlist('X-Max-Bot-Api-Secret')
        if len(supplied) != 1 or not verify_secret(supplied[0], expected):
            raise HTTPException(status_code=401, detail='unauthorized')
        if request.headers.get('content-type', '').split(';', 1)[0].lower() != 'application/json' or request.headers.get('content-encoding', 'identity') != 'identity':
            raise HTTPException(status_code=415, detail='unsupported_media_type')
        length = request.headers.get('content-length')
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                raise HTTPException(status_code=400, detail='invalid_update') from None
            if size < 0:
                raise HTTPException(status_code=400, detail='invalid_update')
            if size > settings.max_body_bytes:
                raise HTTPException(status_code=413, detail='body_too_large')
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > settings.max_body_bytes:
                raise HTTPException(status_code=413, detail='body_too_large')
            body.extend(chunk)
        try:
            update = parse_update(bytes(body), max_text_chars=getattr(settings, 'max_input_chars', 4000))
        except ValueError:
            raise HTTPException(status_code=400, detail='invalid_update') from None
        try:
            await run_in_threadpool(persist_update, db, settings, update, datetime.now(timezone.utc))
        except Exception:
            # Neither raw MAX data nor exceptions/DSNs are returned to the sender.
            raise HTTPException(status_code=503, detail='temporarily_unavailable') from None
        return StatusResponse(status='accepted')

    return app

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from invoker import command_registry
from fastapi import WebSocket, WebSocketDisconnect, Form, File, UploadFile, Depends
from auth.token import verify_token
from logger import logger
from sockets.socket_registry import socket_registry
from sockets.room_state import get_sockets_in_room
from fastapi.responses import JSONResponse
from inspect import signature, Parameter
from typing import get_type_hints, List
import inspect
from utils.refresh_available_commands import sync_command_registry_to_db


router = APIRouter()
auth_scheme = HTTPBearer()

sync_command_registry_to_db(command_registry=command_registry)


async def maybe_await(func, *args, **kwargs):
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def get_user_id(credentials: HTTPAuthorizationCredentials = Depends(auth_scheme)):
    token_payload = verify_token(credentials.credentials)
    user_id = token_payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user_id


def build_file_upload_endpoint(cmd):
    schema_fields = cmd.schema.model_fields if cmd.schema else {}
    hints = get_type_hints(cmd.schema)
    is_multi = getattr(cmd, "multi_file", False)

    async def endpoint_template(**kwargs):
        payload_fields = {k: v for k, v in kwargs.items() if k in schema_fields}
        file_or_files = kwargs.get("files" if is_multi else "file")
        user_id = kwargs.get("user_id") if "user_id" in kwargs else None
        if cmd.require_auth and user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")

        payload = cmd.schema(**payload_fields) if cmd.schema else None

        if cmd.require_auth:
            return await cmd.execute(payload, file_or_files, user_id)
        else:
            return await cmd.execute(payload, file_or_files)

    params = []

    for field_name, field in schema_fields.items():
        field_type = hints[field_name]
        default = Form(...) if field.is_required() else Form(field.default)
        params.append(
            Parameter(field_name, Parameter.POSITIONAL_OR_KEYWORD, default=default, annotation=field_type)
        )

    if is_multi:
        params.append(Parameter("files", Parameter.POSITIONAL_OR_KEYWORD, default=File(...), annotation=List[UploadFile]))
    else:
        params.append(Parameter("file", Parameter.POSITIONAL_OR_KEYWORD, default=File(...), annotation=UploadFile))

    if cmd.require_auth:
        params.append(
            Parameter("user_id", Parameter.POSITIONAL_OR_KEYWORD, default=Depends(get_user_id), annotation=str)
        )

    endpoint_template.__signature__ = signature(endpoint_template).replace(parameters=params)
    return endpoint_template


for command in command_registry:
    schema = command.schema
    endpoint_name = command.name
    http_method = command.method.upper()


    def generate_endpoint(cmd):
        """
        Build the FastAPI endpoint for a command.

        - For file_upload commands: use the existing builder.
        - For GET + schema: parse the model from QUERY PARAMS via Depends().
          (Avoids GET-with-body and the 'loc=["body"]' 422.)
        - For non-GET + schema: accept the model as request body (default FastAPI).
        - For auth: inject user_id via Depends(get_user_id) as before.
        """
        if cmd.type == "file_upload":
            return build_file_upload_endpoint(cmd)

        has_schema = cmd.schema is not None
        method = cmd.method.upper()
        is_get = method == "GET"

        if cmd.require_auth:
            if not has_schema:
                async def endpoint(user_id: str = Depends(get_user_id)):
                    # No payload; pass user_id only
                    return await maybe_await(cmd.execute, {"user_id": user_id})
            else:
                if is_get:
                    # READ SCHEMA FROM QUERY PARAMS
                    async def endpoint(
                            payload: cmd.schema = Depends(),  # <--- key change
                            user_id: str = Depends(get_user_id),
                    ):
                        if hasattr(payload, "user_id"):
                            setattr(payload, "user_id", user_id)
                        return await maybe_await(cmd.execute, payload, user_id=user_id)
                else:
                    # READ SCHEMA FROM REQUEST BODY (POST/PUT/etc)
                    async def endpoint(
                            payload: cmd.schema,
                            user_id: str = Depends(get_user_id),
                    ):
                        if hasattr(payload, "user_id"):
                            setattr(payload, "user_id", user_id)
                        return await maybe_await(cmd.execute, payload, user_id=user_id)
        else:
            if not has_schema:
                async def endpoint():
                    return await maybe_await(cmd.execute, None)
            else:
                if is_get:
                    # READ SCHEMA FROM QUERY PARAMS
                    async def endpoint(payload: cmd.schema = Depends()):  # <--- key change
                        return await maybe_await(cmd.execute, payload)
                else:
                    # READ SCHEMA FROM REQUEST BODY
                    async def endpoint(payload: cmd.schema):
                        return await maybe_await(cmd.execute, payload)

        return endpoint


    route_kwargs = {
        "path": f"/{endpoint_name}",
        "endpoint": generate_endpoint(command),
        "methods": [http_method],
        "name": endpoint_name,
        "summary": f"{endpoint_name} Command",
        "response_model": dict,
    }

    if not command.require_auth:
        route_kwargs["openapi_extra"] = {"security": []}

    router.add_api_route(**route_kwargs)


for room_name, socket_handler in socket_registry.items():
    route_path = f"/ws/{room_name}"

    def make_emit(room):
        async def emit(payload: dict):
            for ws in get_sockets_in_room(room):
                try:
                    await ws.send_json(payload)
                except Exception:
                    pass
        return emit

    socket_handler.emit = make_emit(room_name)

    async def websocket_endpoint(websocket: WebSocket, room=room_name, handler=socket_handler):
        token = websocket.query_params.get("token")
        if not token:
            await websocket.close(code=1008)
            return

        try:
            payload = verify_token(token)
            user = {"user_id": payload.get("user_id")}

            if not handler.authorize(websocket, user):
                await websocket.close(code=1008)
                return

            await websocket.accept()
            from sockets.room_state import add_socket_to_room, remove_socket_from_room
            add_socket_to_room(room, websocket)

            await handler.on_connect(websocket, user)

            while True:
                raw = await websocket.receive_json()
                await handler.on_message(raw, websocket, user)

        except WebSocketDisconnect:
            logger.info(f"[WebSocket:{room}] disconnected")
        except Exception as e:
            logger.info(f"[WebSocket:{room}] error:", e)
            await websocket.close(code=1008)
        finally:
            from sockets.room_state import remove_socket_from_room
            remove_socket_from_room(room, websocket)
            await handler.on_disconnect(websocket)

    router.add_api_websocket_route(route_path, websocket_endpoint, name=room_name)


@router.get("/ws/progress-docs", tags=["WebSocket"])
def websocket_docs():
    return JSONResponse({
        "info": "WebSocket Endpoint: ws://localhost:8000/ws/progress",
        "auth": "Use Bearer token as ?token=...",
        "room": "Use path param to join (e.g., /ws/{room})",
        "example": "ws://localhost:8000/ws/progress?token=eyJhbGciOiJIUzI1Ni...",
        "message_format": {
            "job_id": "string",
            "status": "processing|done|error",
            "message": "string",
            "percent": "int (optional)"
        }
    })

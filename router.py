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
from typing import get_type_hints, List, get_origin, get_args

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
    """
    Build an endpoint for commands that upload files.
    This inspects:
      - cmd.schema (pydantic model fields -> Form(...))
      - cmd.execute signature -> for UploadFile / List[UploadFile] params create File(...) params
      - cmd.require_auth -> inject user_id via Depends(get_user_id)
    Also continues to support older 'multi_file' boolean (keeps backward compatibility).
    """
    schema_fields = cmd.schema.model_fields if cmd.schema else {}
    schema_hints = get_type_hints(cmd.schema) if cmd.schema else {}
    # We'll inspect cmd.execute signature to find file params automatically
    exec_sig = inspect.signature(cmd.execute)
    exec_params = list(exec_sig.parameters.values())

    # Determine which parameters of execute are the 'payload' and which are file params.
    # Heuristic:
    # - If cmd.schema exists, assume the first non-self parameter corresponds to payload
    # - Otherwise payload may be absent
    # We'll skip 'self' and find subsequent params
    file_param_infos = []  # list of tuples (name, is_list)
    # build a set of schema field names to avoid collisions
    schema_names = set(schema_fields.keys())

    # Start scanning after 'self' — exclude 'self' explicitly if present
    for p in exec_params:
        if p.name == "self":
            continue
        # Skip the first payload param if schema is present (it should be the payload)
        # Convention: execute(self, payload, thumbnail: UploadFile=None, ...)
        # So if schema exists, assume first non-self param is payload and skip it
        break

    # We'll do a more robust pass: look at parameters except 'self' and the first one if matches schema payload name
    exec_param_iter = [p for p in exec_params if p.name != "self"]
    # If cmd.schema exists and the first exec param is likely the payload, drop it:
    if cmd.schema and len(exec_param_iter) >= 1:
        # The first param name is usually 'payload' or similar; we remove it from file detection
        exec_param_iter = exec_param_iter[1:]

    # Now inspect remaining params for UploadFile / List[UploadFile]
    for p in exec_param_iter:
        ann = p.annotation
        # Handle typing.List[UploadFile], list[UploadFile], etc.
        is_list = False
        try:
            origin = get_origin(ann)
            if origin in (list, List):
                args = get_args(ann)
                if args and args[0] is UploadFile:
                    is_list = True
                    file_param_infos.append((p.name, True))
                    continue
            # direct UploadFile annotation
            if ann is UploadFile:
                file_param_infos.append((p.name, False))
                continue
            # if annotation is typing.Any or missing, try to infer by name (fallback)
        except Exception:
            pass
        # fallback by annotation string name (helps if annotations are forwarded or as string)
        an_str = getattr(ann, "__name__", str(ann)).lower() if ann is not None else ""
        if "uploadfile" in an_str or "upload_file" in an_str:
            # can't tell list vs single — check default or name
            # treat names plural (ending with 's' or 'images') as list
            if p.name.endswith("s") or p.name in ("images", "files"):
                file_param_infos.append((p.name, True))
            else:
                file_param_infos.append((p.name, False))

    # Backwards compatibility: if cmd.multi_file True and no file_param_infos found, maintain old behavior
    if getattr(cmd, "multi_file", False) and not file_param_infos:
        file_param_infos = [("files", True)]
    if not file_param_infos:
        # Fallback to legacy single 'file' param
        file_param_infos = [("file", False)]

    # Build the dynamic endpoint function
    async def endpoint_template(**kwargs):
        # Extract payload fields for building the Pydantic model
        payload_fields = {k: v for k, v in kwargs.items() if k in schema_fields}
        # Extract file args by name
        files_kwargs = {name: kwargs.get(name) for (name, _) in file_param_infos}
        # Determine user_id if injected
        user_id = kwargs.get("user_id") if "user_id" in kwargs else None

        # auth check
        if cmd.require_auth and user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")

        # build payload model instance
        payload = cmd.schema(**payload_fields) if cmd.schema else None

        # Prepare to call execute. We need to pass payload and the file params in the correct order
        # Build arglist in the same order as execute signature (skipping self)
        call_args = []
        for p in exec_params:
            if p.name == "self":
                continue
            # if first param expected payload and we have payload, append it
            if cmd.schema and p == exec_params[0]:
                call_args.append(payload)
                continue
            # if this param is one of our file params, append corresponding value
            if p.name in files_kwargs:
                call_args.append(files_kwargs[p.name])
                continue
            # if param is user_id and we have user_id, append it
            if p.name == "user_id" and ("user_id" in kwargs or cmd.require_auth):
                call_args.append(user_id)
                continue
            # otherwise, try to source from kwargs (e.g., optional args) or use None
            call_args.append(kwargs.get(p.name))

        # call execute (maybe async)
        if cmd.require_auth:
            return await maybe_await(cmd.execute, *call_args)
        else:
            return await maybe_await(cmd.execute, *call_args)

    # Build parameters list: first the schema form fields
    params = []
    for field_name, field in schema_fields.items():
        field_type = schema_hints.get(field_name, str)
        default = Form(...) if field.is_required() else Form(field.default)
        params.append(Parameter(field_name, Parameter.POSITIONAL_OR_KEYWORD, default=default, annotation=field_type))

    # Then add the file parameters discovered
    for name, is_list in file_param_infos:
        if is_list:
            params.append(Parameter(name, Parameter.POSITIONAL_OR_KEYWORD, default=File(None), annotation=List[UploadFile]))
        else:
            params.append(Parameter(name, Parameter.POSITIONAL_OR_KEYWORD, default=File(None), annotation=UploadFile))

    # Inject user_id via dependency if required
    if cmd.require_auth:
        params.append(Parameter("user_id", Parameter.POSITIONAL_OR_KEYWORD, default=Depends(get_user_id), annotation=str))

    # Replace the endpoint function signature
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


    group_name = getattr(command, "group", None) or "Default"

    route_kwargs = {
        "path": f"/{endpoint_name}",
        "endpoint": generate_endpoint(command),
        "methods": [http_method],
        "name": endpoint_name,
        "summary": f"{endpoint_name} Command",
        "response_model": dict,
        # put the command group into tags so Swagger groups endpoints
        "tags": [group_name],
    }

    # If route is open (no require_auth) keep your current openapi_extra behavior
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

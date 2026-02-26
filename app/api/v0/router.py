from __future__ import annotations

import inspect
from inspect import Parameter, signature
from typing import List, Optional, get_args, get_origin, get_type_hints

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.settings import settings
from auth.internal_token import verify_internal_token
from auth.token import verify_token
from commands.invoker import command_registry
from utils.refresh_available_commands import sync_command_registry_to_db

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("app.api.v0.router")
except Exception:
    import logging

    logger = logging.getLogger(__name__)

router = APIRouter()
auth_scheme = HTTPBearer(auto_error=False)

sync_command_registry_to_db(command_registry=command_registry)


async def maybe_await(func, *args, **kwargs):
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _command_auth_mode(cmd) -> str:
    return (getattr(cmd, "auth_mode", None) or "user").strip().lower()


def _verify_user_token(token: str) -> str:
    payload = verify_token(token)
    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    return str(user_id)


def _verify_internal_token(token: str, cmd) -> str:
    required_scopes = set(getattr(cmd, "internal_scopes", None) or [])

    internal_payload = verify_internal_token(
        token,
        secret=(settings.internal_auth_secret or "").strip(),
        required_scopes=required_scopes,
    )
    sub = (internal_payload.get("sub") or "").strip() or "service"
    return f"internal:{sub}"


def make_get_user_id(cmd):
    mode = _command_auth_mode(cmd)

    def _dep(credentials: Optional[HTTPAuthorizationCredentials] = Depends(auth_scheme)) -> str:
        token = (getattr(credentials, "credentials", None) or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="Missing token")

        if mode == "internal":
            return _verify_internal_token(token, cmd)

        if mode == "either":
            try:
                return _verify_internal_token(token, cmd)
            except Exception:
                return _verify_user_token(token)

        return _verify_user_token(token)

    return _dep


def build_file_upload_endpoint(cmd):
    schema_fields = cmd.schema.model_fields if cmd.schema else {}
    schema_hints = get_type_hints(cmd.schema) if cmd.schema else {}

    exec_sig = inspect.signature(cmd.execute)
    exec_params = list(exec_sig.parameters.values())

    file_param_infos = []
    exec_param_iter = [p for p in exec_params if p.name != "self"]
    if cmd.schema and len(exec_param_iter) >= 1:
        exec_param_iter = exec_param_iter[1:]

    for p in exec_param_iter:
        ann = p.annotation
        try:
            origin = get_origin(ann)
            if origin in (list, List):
                args = get_args(ann)
                if args and args[0] is UploadFile:
                    file_param_infos.append((p.name, True))
                    continue
            if ann is UploadFile:
                file_param_infos.append((p.name, False))
                continue
        except Exception:
            pass

        an_str = getattr(ann, "__name__", str(ann)).lower() if ann is not None else ""
        if "uploadfile" in an_str or "upload_file" in an_str:
            if p.name.endswith("s") or p.name in ("images", "files"):
                file_param_infos.append((p.name, True))
            else:
                file_param_infos.append((p.name, False))

    if getattr(cmd, "multi_file", False) and not file_param_infos:
        file_param_infos = [("files", True)]
    if not file_param_infos:
        file_param_infos = [("file", False)]

    async def endpoint_template(**kwargs):
        payload_fields = {k: v for k, v in kwargs.items() if k in schema_fields}
        files_kwargs = {name: kwargs.get(name) for (name, _) in file_param_infos}
        user_id = kwargs.get("user_id") if "user_id" in kwargs else None

        if cmd.require_auth and user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")

        payload = cmd.schema(**payload_fields) if cmd.schema else None

        call_args = []
        for p in exec_params:
            if p.name == "self":
                continue
            if cmd.schema and p == exec_params[0]:
                call_args.append(payload)
                continue
            if p.name in files_kwargs:
                call_args.append(files_kwargs[p.name])
                continue
            if p.name == "user_id":
                call_args.append(user_id)
                continue
            call_args.append(kwargs.get(p.name))

        return await maybe_await(cmd.execute, *call_args)

    params = []
    for field_name, field in schema_fields.items():
        field_type = schema_hints.get(field_name, str)
        default = Form(...) if field.is_required() else Form(field.default)
        params.append(
            Parameter(
                field_name,
                Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=field_type,
            )
        )

    for name, is_list in file_param_infos:
        if is_list:
            params.append(
                Parameter(
                    name,
                    Parameter.POSITIONAL_OR_KEYWORD,
                    default=File(None),
                    annotation=List[UploadFile],
                )
            )
        else:
            params.append(
                Parameter(
                    name,
                    Parameter.POSITIONAL_OR_KEYWORD,
                    default=File(None),
                    annotation=UploadFile,
                )
            )

    if cmd.require_auth:
        params.append(
            Parameter(
                "user_id",
                Parameter.POSITIONAL_OR_KEYWORD,
                default=Depends(make_get_user_id(cmd)),
                annotation=str,
            )
        )

    endpoint_template.__signature__ = signature(endpoint_template).replace(parameters=params)
    return endpoint_template


def build_command_endpoint(cmd):
    has_schema = cmd.schema is not None
    method = cmd.method.upper()
    is_get = method == "GET"

    async def endpoint_template(**kwargs):
        user_id = kwargs.get("user_id")
        payload = kwargs.get("payload")

        if cmd.require_auth and not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        if has_schema and payload is not None and hasattr(payload, "user_id"):
            setattr(payload, "user_id", user_id)

        if not has_schema and cmd.require_auth:
            return await maybe_await(cmd.execute, {"user_id": user_id})

        if not has_schema:
            return await maybe_await(cmd.execute, None)

        if cmd.require_auth:
            return await maybe_await(cmd.execute, payload, user_id=user_id)
        return await maybe_await(cmd.execute, payload)

    params = []

    if has_schema:
        if is_get:
            params.append(
                Parameter(
                    "payload",
                    Parameter.POSITIONAL_OR_KEYWORD,
                    default=Depends(),
                    annotation=cmd.schema,
                )
            )
        else:
            params.append(
                Parameter(
                    "payload",
                    Parameter.POSITIONAL_OR_KEYWORD,
                    default=Parameter.empty,
                    annotation=cmd.schema,
                )
            )

    if cmd.require_auth:
        params.append(
            Parameter(
                "user_id",
                Parameter.POSITIONAL_OR_KEYWORD,
                default=Depends(make_get_user_id(cmd)),
                annotation=str,
            )
        )

    endpoint_template.__signature__ = signature(endpoint_template).replace(parameters=params)
    return endpoint_template


for command in command_registry:
    endpoint_name = command.name
    http_method = command.method.upper()

    endpoint = build_file_upload_endpoint(command) if command.type == "file_upload" else build_command_endpoint(command)

    group_name = getattr(command, "group", None) or "Default"

    route_kwargs = {
        "path": f"/{endpoint_name}",
        "endpoint": endpoint,
        "methods": [http_method],
        "name": endpoint_name,
        "summary": f"{endpoint_name} Command",
        "response_model": dict,
        "tags": [group_name],
    }

    if not command.require_auth:
        route_kwargs["openapi_extra"] = {"security": []}

    router.add_api_route(**route_kwargs)


__all__ = ["router"]

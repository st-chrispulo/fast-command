from __future__ import annotations

from typing import Literal, Optional, Type

from fastapi import HTTPException
from pydantic import BaseModel

from auth.guard import check_permission


class BaseCommand:
    name: str
    schema: Optional[Type[BaseModel]] = None
    require_auth: bool = True
    auth_mode: Literal["user", "internal", "either"] = "user"
    method: str = "POST"
    type: str = "json"
    multi_file: bool = False

    def run(self, payload: BaseModel, *args, **kwargs) -> dict:
        raise NotImplementedError

    def _is_internal_user_id(self, user_id: Optional[str]) -> bool:
        return bool(user_id) and str(user_id).startswith("internal:")

    def _enforce_auth(self, user_id: Optional[str]) -> str:
        if not self.require_auth:
            return str(user_id or "")

        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        user_id_str = str(user_id)
        is_internal = self._is_internal_user_id(user_id_str)

        if self.auth_mode == "user" and is_internal:
            raise HTTPException(status_code=401, detail="User token required")

        if self.auth_mode == "internal" and not is_internal:
            raise HTTPException(status_code=401, detail="Internal token required")

        if not is_internal:
            check_permission(user_id=user_id_str, command=self.name)

        return user_id_str

    def execute(self, *args, **kwargs) -> dict:
        payload = None
        user_id = None

        if self.type == "file_upload":
            payload = kwargs.get("payload")
            user_id = kwargs.get("user_id")
            user_id = self._enforce_auth(user_id)

            file_data = kwargs.get("file") or kwargs.get("files")
            return self.run(payload, file_data, user_id)

        if args:
            payload = args[0]
            if isinstance(payload, dict):
                user_id = payload.get("user_id")
            else:
                user_id = getattr(payload, "user_id", None)

        user_id = user_id or kwargs.get("user_id")
        self._enforce_auth(user_id)

        return self.run(payload)

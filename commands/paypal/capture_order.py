import os, requests
from fastapi import HTTPException
from pydantic import BaseModel, field_validator
from commands.base_command import BaseCommand
from auth.db import SessionLocal
from .create_order import get_access_token, PAYPAL_BASE

class CaptureOrderPayload(BaseModel):
    order_id: str
    @field_validator("order_id")
    @classmethod
    def validate_order_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("order_id must not be empty")
        return v

class CapturePayPalOrderCommand(BaseCommand):
    name = "paypal/capture_order"
    schema = CaptureOrderPayload

    def run(self, payload: CaptureOrderPayload):
        db = SessionLocal()
        try:
            token = get_access_token()
            r = requests.post(
                f"{PAYPAL_BASE}/v2/checkout/orders/{payload.order_id}/capture",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={},
                timeout=20,
            )
            if r.status_code != 201:
                raise HTTPException(502, f"capture failed: {r.text}")
            data = r.json()
            pu = (data.get("purchase_units") or [{}])[0]
            caps = (pu.get("payments") or {}).get("captures") or []
            capture_id = caps[0]["id"] if caps else None
            amount = (caps[0].get("amount") if caps else {}) or {}
            return {
                "status": data.get("status"),
                "order_id": data.get("id"),
                "capture_id": capture_id,
                "amount": amount,
                "raw": data,
            }
        finally:
            db.close()

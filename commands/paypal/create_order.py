import os, base64, requests
from fastapi import HTTPException
from pydantic import BaseModel, field_validator
from commands.base_command import BaseCommand
from auth.db import SessionLocal

PAYPAL_BASE = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}[os.getenv("PAYPAL_MODE", "sandbox")]

CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
SECRET = os.getenv("PAYPAL_SECRET", "")
BASE_URL = os.getenv("BASE_URL", "https://your-backend.example.com")

PRICES = {
    "checkout_199": {"value": "199.00", "description": "MVP Builder – Full Checkout"},
    "repack_19": {"value": "19.00", "description": "Token Repack"},
}

class CreateOrderPayload(BaseModel):
    sku: str
    @field_validator("sku")
    @classmethod
    def validate_sku(cls, v: str) -> str:
        v = v.strip()
        if v not in PRICES:
            raise ValueError("invalid sku")
        return v

def get_access_token() -> str:
    auth = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    r = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    if r.status_code != 200:
        raise HTTPException(502, f"paypal auth failed: {r.text}")
    return r.json()["access_token"]


class CreatePayPalOrderCommand(BaseCommand):
    name = "paypal/create_order"
    schema = CreateOrderPayload

    def run(self, payload: CreateOrderPayload):
        print(CLIENT_ID, SECRET, BASE_URL)
        db = SessionLocal()
        try:
            token = get_access_token()
            price = PRICES[payload.sku]
            body = {
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "amount": {"currency_code": "USD", "value": price["value"]},
                        "description": price["description"],
                        "custom_id": payload.sku,
                    }
                ],
                "application_context": {
                    "brand_name": os.getenv("BRAND_NAME", "Navales Beacon"),
                    "landing_page": "LOGIN",
                    "user_action": "PAY_NOW",
                    "return_url": f"{BASE_URL}/paypal/return",
                    "cancel_url": f"{BASE_URL}/paypal/cancel",
                },
            }
            r = requests.post(
                f"{PAYPAL_BASE}/v2/checkout/orders",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=20,
            )
            if r.status_code != 201:
                raise HTTPException(502, f"create order failed: {r.text}")
            data = r.json()
            approve = next((l["href"] for l in data.get("links", []) if l.get("rel") == "approve"), None)
            return {"order_id": data["id"], "approve_url": approve, "status": data.get("status"), "raw": data}
        finally:
            db.close()

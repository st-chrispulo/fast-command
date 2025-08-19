from commands.base_command import BaseCommand
from auth.db import SessionLocal
from sqlalchemy import text


class GetAllListingsCommand(BaseCommand):
    name = "listing/all"
    schema = None
    require_auth = True

    def run(self, payload=None):
        db = SessionLocal()
        try:
            result = db.execute(text("SELECT * FROM tbl_listings ORDER BY created_at DESC"))
            listings = [dict(row._mapping) for row in result]
            return {"status": "success", "data": listings}
        finally:
            db.close()

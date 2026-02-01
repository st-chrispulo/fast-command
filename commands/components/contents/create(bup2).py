# content_create.py
import os, re, json, mimetypes
from uuid import uuid4
from fastapi import UploadFile
from typing import Optional, List
from commands.base_command import BaseCommand
from pydantic import BaseModel, field_validator
from uuid import UUID
from integrations.gcs.gcs import get_gcs  # <-- use your singleton

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")

def make_uuid_name(filename: str, default_stem: str) -> str:
    name = filename or ""
    stem, ext = os.path.splitext(name)
    stem = (stem or default_stem).strip()
    stem = _SAFE_CHARS_RE.sub("_", stem) or default_stem
    ext = (ext or "").lower().lstrip(".") or "bin"
    return f"{stem}.{uuid4()}.{ext}"

_FALLBACK_MIME = {
    ".webp": "image/webp",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
}

def _resolve_content_type(upload: UploadFile) -> str:
    if getattr(upload, "content_type", None):
        return upload.content_type
    name = getattr(upload, "filename", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext in _FALLBACK_MIME:
        return _FALLBACK_MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


class CreateCompContentsPayload(BaseModel):
    name: str
    description: Optional[str] = None
    template_id: Optional[UUID] = None
    created_by: Optional[int] = None
    updated_by: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _name_trim(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("description", mode="before")
    @classmethod
    def _strip_optional(cls, v):
        return v.strip() if isinstance(v, str) else v


class ContentCreateCommand(BaseCommand):
    name = "content_create"
    schema = CreateCompContentsPayload
    require_auth = True
    method = "post"
    type = "file_upload"

    base_folder = "comp_contents"  # becomes part of dest_prefix inside GCS helper

    MAX_IMAGE_MB = 50
    MAX_FILE_MB = 200
    IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
    ANY_FILE_TYPES = None  # allow any

    async def execute(
        self,
        payload: CreateCompContentsPayload,
        thumbnail_file: Optional[UploadFile] = None,
        images_files: Optional[List[UploadFile]] = None,
        attachment_file: Optional[UploadFile] = None,
        user_id: Optional[str] = None,
    ):
        gcs = get_gcs()

        content_id = str(uuid4())
        dest_prefix = f"{self.base_folder}/{content_id}"

        uploaded = {"thumbnail": None, "images": [], "attachment": None}

        async def _read_and_check(f: UploadFile, max_mb: int, allowed: Optional[set]):
            if f is None:
                return b""
            data = await f.read()
            f.file.seek(0)
            ct = _resolve_content_type(f)
            if len(data) > max_mb * 1024 * 1024:
                raise ValueError(f"File '{f.filename}' exceeds {max_mb}MB limit")
            if allowed is not None and ct not in allowed:
                raise ValueError(f"Unsupported content type '{ct}' for '{f.filename}'")
            return data, ct  # we return ct so we don't recompute

        # Thumbnail
        if thumbnail_file:
            _, thumb_ct = await _read_and_check(thumbnail_file, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
            thumb_name = make_uuid_name(thumbnail_file.filename, "thumbnail")
            # store under a subfolder
            key_prefix = f"{dest_prefix}/thumbnail"
            res = gcs.upload_fileobj(
                fileobj=thumbnail_file.file,
                filename=thumb_name,
                dest_prefix=key_prefix,
                public=False,
                content_type=thumb_ct,
            )
            uploaded["thumbnail"] = {
                "ok": res["ok"],
                "bucket": res["bucket"],
                "key": res["key"],
                "https_url": f"https://storage.googleapis.com/{res['bucket']}/{res['key']}",
                "public_url": res.get("public_url"),
            }

        # Images
        if images_files:
            for idx, img in enumerate(images_files):
                _, img_ct = await _read_and_check(img, self.MAX_IMAGE_MB, self.IMAGE_TYPES)
                img_name = make_uuid_name(img.filename, f"img{idx:03d}")
                key_prefix = f"{dest_prefix}/images"
                res = gcs.upload_fileobj(
                    fileobj=img.file,
                    filename=img_name,
                    dest_prefix=key_prefix,
                    public=False,
                    content_type=img_ct,
                )
                uploaded["images"].append({
                    "ok": res["ok"],
                    "bucket": res["bucket"],
                    "key": res["key"],
                    "https_url": f"https://storage.googleapis.com/{res['bucket']}/{res['key']}",
                    "public_url": res.get("public_url"),
                })

        # Attachment
        if attachment_file:
            _, att_ct = await _read_and_check(attachment_file, self.MAX_FILE_MB, self.ANY_FILE_TYPES)
            att_name = make_uuid_name(attachment_file.filename, "file")
            key_prefix = f"{dest_prefix}/files"
            res = gcs.upload_fileobj(
                fileobj=attachment_file.file,
                filename=att_name,
                dest_prefix=key_prefix,
                public=False,
                content_type=att_ct,
            )
            uploaded["attachment"] = {
                "ok": res["ok"],
                "bucket": res["bucket"],
                "key": res["key"],
                "https_url": f"https://storage.googleapis.com/{res['bucket']}/{res['key']}",
                "public_url": res.get("public_url"),
            }

        return {
            "status": "ok",
            "content_id": content_id,
            "name": payload.name,
            "description": payload.description,
            "template_id": str(payload.template_id) if payload.template_id else None,
            "created_by": payload.created_by,
            "updated_by": payload.updated_by,
            "gcs": uploaded,
        }

if __name__ == "__main__":
    """
    Self-contained smoke test for ContentCreateCommand using GCS singleton.
    - Creates tiny sample files under ./sample_data if missing
    - Builds UploadFile objects (thumbnail, images[], attachment)
    - Calls the command and prints returned GCS links
    Requirements:
      - .env with at least GCS_BUCKET set, or set env vars in shell
      - If using a service account: set GCS_SA_JSON_PATH (or GOOGLE_APPLICATION_CREDENTIALS)
    """
    import os
    import io
    import asyncio
    import mimetypes
    import zipfile
    from pprint import pprint
    from fastapi import UploadFile

    # ---- Local sample assets (auto-generated if missing) ----
    SAMPLE_DIR = "./sample_data"
    THUMBNAIL_PATH = os.path.join(SAMPLE_DIR, "thumb.png")
    IMAGE_PATHS = [
        os.path.join(SAMPLE_DIR, "img1.png"),
        os.path.join(SAMPLE_DIR, "img2.png"),
    ]
    ATTACHMENT_PATH = os.path.join(SAMPLE_DIR, "file.zip")

    def _ensure_dir(p):
        os.makedirs(p, exist_ok=True)

    def _write_min_png(path: str):
        # 1x1 transparent PNG
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc``\x00\x00\x00\x02\x00\x01"
            b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        with open(path, "wb") as f:
            f.write(png_bytes)

    def _write_min_zip(path: str):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("readme.txt", "Sample attachment file.\n")
        with open(path, "wb") as f:
            f.write(buf.getvalue())

    def ensure_test_assets():
        _ensure_dir(SAMPLE_DIR)
        if not os.path.isfile(THUMBNAIL_PATH):
            _write_min_png(THUMBNAIL_PATH)
        for p in IMAGE_PATHS:
            if not os.path.isfile(p):
                _write_min_png(p)
        if not os.path.isfile(ATTACHMENT_PATH):
            _write_min_zip(ATTACHMENT_PATH)

    # ---- UploadFile builder (no content_type kwarg; set attribute manually) ----
    def build_uploadfile(path: str) -> UploadFile:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Missing test file: {path}")
        f = open(path, "rb")  # keep open until after upload
        uf = UploadFile(filename=os.path.basename(path), file=f)
        try:
            ct, _ = mimetypes.guess_type(path)
            uf.content_type = ct or "application/octet-stream"
        except Exception:
            pass
        return uf

    # Ensure assets exist
    ensure_test_assets()

    # Optional: set envs here (or rely on .env read by gcs.py)
    # os.environ.setdefault("GCS_BUCKET", "your-bucket-name")
    # os.environ.setdefault("GCS_SA_JSON_PATH", r"C:\path\to\service-account.json")

    # Build payload (non-file fields)
    sample_payload = {
        "name": "Sample Component (GCS singleton test)",
        "description": "Smoke test via __main__",
        "template_id": None,
        "created_by": 1,
        "updated_by": 1,
    }
    payload = CreateCompContentsPayload(**sample_payload)

    # Prepare UploadFile instances
    thumbnail_file = build_uploadfile(THUMBNAIL_PATH)
    images_files = [build_uploadfile(p) for p in IMAGE_PATHS]
    attachment_file = build_uploadfile(ATTACHMENT_PATH)

    # Run the command
    cmd = ContentCreateCommand()
    setattr(cmd, "user_id", 1)

    async def _run():
        return await cmd.execute(
            payload=payload,
            thumbnail_file=thumbnail_file,
            images_files=images_files,
            attachment_file=attachment_file,
            user_id="1",
        )

    try:
        result = asyncio.run(_run())
        pprint(result)
        print("\n✅ ContentCreateCommand + GCS helper smoke test finished.")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
    finally:
        # Close open file handles
        try:
            if thumbnail_file and thumbnail_file.file and not thumbnail_file.file.closed:
                thumbnail_file.file.close()
        except Exception:
            pass
        for uf in images_files or []:
            try:
                if uf and uf.file and not uf.file.closed:
                    uf.file.close()
            except Exception:
                pass
        try:
            if attachment_file and attachment_file.file and not attachment_file.file.closed:
                attachment_file.file.close()
        except Exception:
            pass

# gcs.py
"""
GCS helper (singleton) with multi-file upload.
- Uses google-cloud-storage (official SDK)
- Reads config from .env (or GOOGLE_APPLICATION_CREDENTIALS)
- Default destination prefix: interations/gcs  (stored as 'interations/gcs' in GCS)
"""

import os
import json
import mimetypes
import logging
from pathlib import Path
from datetime import timedelta
from typing import Iterable, List, Dict, Optional, Any

from dotenv import load_dotenv

# google libs
from google.cloud import storage
from google.oauth2 import service_account
from google.api_core.exceptions import NotFound, Forbidden

# load .env early
load_dotenv()

# app logger (use your app logger if available)
try:
    from logger import logger  # your existing logger module
except Exception:
    logger = logging.getLogger("gcs")
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(h)
    logger.setLevel(logging.INFO)


class GCS:
    """
    Singleton Google Cloud Storage client.

    Environment variables (preferred):
      - GCS_BUCKET              (required)
      - GCS_SA_JSON_PATH        (optional) path to service account JSON OR raw JSON string
      - GOOGLE_APPLICATION_CREDENTIALS (optional) standard ADC env var (path)
      - GCS_PROJECT_ID          (optional)
      - GCS_BASE_PREFIX         (optional; default 'interations/gcs')
      - GCS_PUBLIC_DEFAULT      (optional; 'true'/'false', default 'false')
    """

    _instance: Optional["GCS"] = None

    @classmethod
    def instance(cls) -> "GCS":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        # Config
        self.bucket_name = os.getenv("GCS_BUCKET", "").strip()
        if not self.bucket_name:
            # do not silently default here — make it clear to the developer
            raise RuntimeError("GCS_BUCKET is required (set in environment or .env).")

        self.project_id = os.getenv("GCS_PROJECT_ID") or None
        self.base_prefix = (os.getenv("GCS_BASE_PREFIX") or "interations/gcs").strip("/")
        self.public_default = os.getenv("GCS_PUBLIC_DEFAULT", "false").lower() == "true"

        # Initialize client (robustly load credentials)
        creds = None
        try:
            creds = self._load_credentials()
            if creds:
                logger.info("GCS: using explicit service account credentials")
                self.client = storage.Client(credentials=creds, project=self.project_id)
            else:
                # creds None => let storage.Client pick up ADC / GOOGLE_APPLICATION_CREDENTIALS
                logger.info("GCS: no explicit SA JSON provided; using ADC / GOOGLE_APPLICATION_CREDENTIALS if available")
                self.client = storage.Client(project=self.project_id)
        except Exception as e:
            logger.exception("GCS: failed to initialize client: %s", e)
            # re-raise so startup fails fast
            raise

        # Validate bucket access
        try:
            self.bucket = self.client.bucket(self.bucket_name)
            # do a light sanity check but avoid raising for 403/404 quietly
            if not self.sanity():
                logger.warning("GCS: sanity check failed for bucket '%s' (NotFound or Forbidden)", self.bucket_name)
        except Exception:
            logger.exception("GCS: error obtaining bucket '%s'", self.bucket_name)
            raise

        self._initialized = True
        logger.info("GCS initialized: bucket=%s base_prefix=%s", self.bucket_name, self.base_prefix)

    # -------------------------
    # Credential loading helpers
    # -------------------------
    def _load_credentials(self):
        """
        Attempts to load credentials in this order:
          1) GCS_SA_JSON_PATH env var:
               - if value begins with '{' treat as raw JSON string and parse
               - else treat as a path: expanduser(), normalize slashes, resolve
          2) GOOGLE_APPLICATION_CREDENTIALS (path) or ADC (let storage.Client handle it) -> return None
        Returns google.oauth2.service_account.Credentials or None
        """
        env_name = "GCS_SA_JSON_PATH"
        raw = os.getenv(env_name) or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or None
        if not raw:
            return None

        s = raw.strip()
        # If raw JSON content passed directly in env
        if s.startswith("{") and s.endswith("}"):
            try:
                info = json.loads(s)
                creds = service_account.Credentials.from_service_account_info(info)
                return creds
            except Exception as e:
                logger.exception("GCS: failed parsing JSON from %s", env_name)
                raise

        # Otherwise treat as a file path. Normalize path separators (accept forward slashes)
        try:
            # Replace backslashes with forward slashes to handle Windows in .envs that lose escapes
            normalized = s.replace("\\", "/")
            p = Path(normalized).expanduser().resolve()
            if not p.exists():
                raise FileNotFoundError(f"Service account file not found: {p}")
            creds = service_account.Credentials.from_service_account_file(str(p))
            return creds
        except Exception as e:
            logger.exception("GCS: failed loading service account from path '%s': %s", raw, e)
            raise

    # -------------------------
    # Utilities
    # -------------------------
    @staticmethod
    def _ctype(path_or_key: str) -> str:
        ctype, _ = mimetypes.guess_type(path_or_key)
        return ctype or "application/octet-stream"

    def _build_key(self, dest_prefix: Optional[str], filename: str) -> str:
        """
        Compose a stable GCS object key.

        dest_prefix:
          - if provided and absolute-like (starts with '/'), we strip leading slash
          - if None -> use base_prefix
        Result is "<dest_prefix>/<filename>" or "<base_prefix>/<filename>"
        """
        if dest_prefix:
            dp = str(dest_prefix).strip("/")
            if dp:
                return f"{dp}/{filename}"
        return f"{self.base_prefix}/{filename}"

    # -------------------------
    # Health
    # -------------------------
    def sanity(self) -> bool:
        """Return True if bucket is reachable with current credentials."""
        try:
            # This triggers an RPC — keep it lightweight; exceptions will be handled by caller
            self.client.get_bucket(self.bucket_name)
            return True
        except (NotFound, Forbidden) as e:
            logger.warning("GCS sanity check: bucket not reachable: %s", e)
            return False
        except Exception as e:
            logger.exception("GCS sanity check error: %s", e)
            return False

    # -------------------------
    # Single upload (local path)
    # -------------------------
    def upload_file(
        self,
        local_path: str,
        dest_prefix: Optional[str] = None,
        public: Optional[bool] = None,
    ) -> Dict[str, Any]:
        filename = os.path.basename(local_path)
        key = self._build_key(dest_prefix, filename)
        blob = self.bucket.blob(key)
        blob.content_type = self._ctype(local_path)
        logger.debug("GCS.upload_file uploading %s -> %s", local_path, key)
        blob.upload_from_filename(local_path)

        make_public = self.public_default if public is None else public
        if make_public:
            blob.make_public()

        return {
            "ok": True,
            "bucket": self.bucket_name,
            "key": key,
            "content_type": blob.content_type,
            "size": os.path.getsize(local_path),
            "public_url": blob.public_url if make_public else None,
        }

    # -------------------------
    # Batch upload (local paths)
    # -------------------------
    def upload_files(
        self,
        files: Iterable[str],
        dest_prefix: Optional[str] = None,
        public: Optional[bool] = None,
        stop_on_error: bool = False,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for path in files:
            try:
                result = self.upload_file(path, dest_prefix=dest_prefix, public=public)
                results.append(result)
            except Exception as e:
                err = {"ok": False, "file": path, "error": str(e)}
                results.append(err)
                logger.exception("GCS.upload_files error for %s: %s", path, e)
                if stop_on_error:
                    raise
        return results

    # -------------------------
    # Signed URLs
    # -------------------------
    def signed_get_url(self, key: str, expires_seconds: int = 900) -> str:
        blob = self.bucket.blob(key)
        return blob.generate_signed_url(version="v4", expiration=timedelta(seconds=expires_seconds), method="GET")

    def signed_put_url(
        self,
        key: str,
        content_type: Optional[str] = None,
        expires_seconds: int = 900,
    ) -> Dict[str, Any]:
        blob = self.bucket.blob(key)
        ctype = content_type or self._ctype(key)
        url = blob.generate_signed_url(version="v4", expiration=timedelta(seconds=expires_seconds), method="PUT", content_type=ctype)
        return {"url": url, "required_headers": {"Content-Type": ctype}}

    # -------------------------
    # Upload file-like object
    # -------------------------
    def upload_fileobj(
        self,
        fileobj,
        filename: str,
        dest_prefix: Optional[str] = None,
        public: Optional[bool] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upload a file-like object to GCS.
        - filename is used to build the key under dest_prefix/base_prefix
        - content_type best-effort resolved if not provided
        Returns: { ok, bucket, key, content_type, public_url? }
        """
        key = self._build_key(dest_prefix, filename)
        blob = self.bucket.blob(key)
        blob.content_type = content_type or self._ctype(filename)
        logger.debug("GCS.upload_fileobj starting upload key=%s content_type=%s", key, blob.content_type)

        try:
            try:
                fileobj.seek(0)
            except Exception:
                pass
            blob.upload_from_file(fileobj, rewind=True, content_type=blob.content_type)
            make_public = self.public_default if public is None else public
            if make_public:
                blob.make_public()
            logger.debug("GCS.upload_fileobj finished upload key=%s", key)
            return {"ok": True, "bucket": self.bucket_name, "key": key, "content_type": blob.content_type, "public_url": blob.public_url if make_public else None}
        except Exception as e:
            logger.exception("GCS.upload_fileobj failed for key=%s: %s", key, e)
            raise

    def read_text(self, key: str, encoding: str = "utf-8") -> str:
        """
        Read a GCS object as text using server-side credentials.
        key is the object key inside the bucket (e.g. 'uploads/.../file.js')
        """
        k = str(key).lstrip("/")
        blob = self.bucket.blob(k)
        data = blob.download_as_bytes()
        try:
            return data.decode(encoding)
        except Exception:
            # fallback
            return data.decode("utf-8", errors="replace")


# convenience accessor
def get_gcs() -> GCS:
    return GCS.instance()

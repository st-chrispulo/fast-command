# gcs_upload.py  (Py3.9-safe)
import argparse
import mimetypes
import os
from datetime import timedelta
from typing import Optional, Dict

from google.cloud import storage


def upload_file_to_gcs(
    bucket_name: str,
    source_path: str,
    destination_name: Optional[str] = None,
    make_public: bool = False,
    signed_url_expires_sec: Optional[int] = None,
    project: Optional[str] = None,
) -> Dict[str, str]:
    """
    Uploads `source_path` to `gs://bucket_name/destination_name`.
    Returns a dict with gs_uri and either public_url, signed_url, or https_url.
    """
    if not os.path.isfile(source_path):
        raise FileNotFoundError("Not found: {}".format(source_path))

    destination_name = destination_name or os.path.basename(source_path)

    client = storage.Client(project=project)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(destination_name)

    ctype, _ = mimetypes.guess_type(source_path)
    blob.content_type = ctype or "application/octet-stream"

    blob.upload_from_filename(source_path)

    result: Dict[str, str] = {
        "gs_uri": "gs://{}/{}".format(bucket_name, destination_name),
        "gcs_console": (
            "https://console.cloud.google.com/storage/browser/_details/"
            "{}/{}".format(bucket_name, destination_name)
        ),
    }

    if make_public:
        blob.make_public()
        result["public_url"] = blob.public_url
    elif signed_url_expires_sec:
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=int(signed_url_expires_sec)),
            method="GET",
        )
        result["signed_url"] = url
    else:
        result["https_url"] = "https://storage.googleapis.com/{}/{}".format(
            bucket_name, destination_name
        )

    return result


def main():
    p = argparse.ArgumentParser(description="Upload a file to GCS and return a URL.")
    p.add_argument("--bucket", required=True, help="Target bucket name")
    p.add_argument("--file", required=True, help="Local file path to upload")
    p.add_argument("--dest", help="Destination object name (defaults to file name)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--public", action="store_true", help="Make object public and return its public URL")
    g.add_argument("--signed", type=int, metavar="SECONDS", help="Return a V4 signed URL valid for N seconds")
    p.add_argument("--project", help="Optional GCP project ID")
    args = p.parse_args()

    info = upload_file_to_gcs(
        bucket_name=args.bucket,
        source_path=args.file,
        destination_name=args.dest,
        make_public=args.public,
        signed_url_expires_sec=args.signed,
        project=args.project,
    )

    print(
        info.get("public_url")
        or info.get("signed_url")
        or info.get("https_url")
        or info["gs_uri"]
    )


if __name__ == "__main__":
    main()

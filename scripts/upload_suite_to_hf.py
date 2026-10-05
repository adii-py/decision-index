#!/usr/bin/env python3
"""Upload the staged hub-upload/ directory to a private Hugging Face dataset."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from huggingface_hub import HfApi, create_repo, upload_folder


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="adi060/decision-index-suite-0.2", help="HF dataset repo id")
    ap.add_argument("--folder", default="hub-upload", help="staged suite directory")
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--skip-create", action="store_true", help="repo already exists on huggingface.co")
    args = ap.parse_args()

    folder = Path(args.folder)
    required = ("selected-rows.jsonl.gz", "added-rows.jsonl.gz", "excluded-questions.json", "manifest.json")
    missing = [name for name in required if not (folder / name).exists()]
    if missing:
        raise SystemExit(f"missing in {folder}: {', '.join(missing)}; run scripts/prepare_hub_upload.py first")

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        raise SystemExit("set HF_TOKEN (write token) before running")

    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    api = HfApi(token=token)
    user = api.whoami()["name"]
    if not args.repo.startswith(f"{user}/"):
        print(f"warning: repo {args.repo} is not under your account ({user})")

    if not args.skip_create:
        create_repo(args.repo, repo_type="dataset", private=not args.public, exist_ok=True, token=token)
        print(f"repo ready: {args.repo}")
    print(f"uploading {folder} -> {args.repo} ({'public' if args.public else 'private'})")
    upload_folder(
        folder_path=str(folder),
        repo_id=args.repo,
        repo_type="dataset",
        token=token,
        commit_message="Decision Index 0.2.1 frozen suite (150759 scoreable rows)",
    )
    files = api.list_repo_files(args.repo, repo_type="dataset")
    print("uploaded files:", files)
    for name in required:
        if name not in files:
            raise SystemExit(f"upload incomplete: {name} missing on hub")
    if args.public:
        api.update_repo_settings(args.repo, private=False, repo_type="dataset")
        print(f"visibility: public (dashboard setup.sh needs no HF_TOKEN)")
    print("ok:", args.repo)


if __name__ == "__main__":
    main()

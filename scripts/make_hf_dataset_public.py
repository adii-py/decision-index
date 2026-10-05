#!/usr/bin/env python3
"""Set the hosted suite dataset to public so dashboard runners need no HF_TOKEN."""

import argparse
import os
import sys

from huggingface_hub import HfApi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="adi060/decision-index-suite-0.2")
    args = ap.parse_args()

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        raise SystemExit("set HF_TOKEN (write token for the dataset owner) before running")

    api = HfApi(token=token)
    api.update_repo_settings(args.repo, private=False, repo_type="dataset")
    print(f"ok: {args.repo} is now public (anonymous read for setup.sh)")


if __name__ == "__main__":
    main()

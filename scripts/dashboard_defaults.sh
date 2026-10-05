# Shared dashboard defaults for setup.sh and run.sh (source, do not execute).
# Suite hosting is configured here — not on the eval-runner secret list.
#
# Private HF dataset: set DECISION_INDEX_HF_TOKEN below (read token) OR make the dataset
# public once: HF_TOKEN=hf_<write> python scripts/make_hf_dataset_public.py

DECISION_INDEX_SUITE_EDITION_DEFAULT="${DECISION_INDEX_SUITE_EDITION_DEFAULT:-0.2.1}"
DECISION_INDEX_SUITE_DATASET_DEFAULT="${DECISION_INDEX_SUITE_DATASET_DEFAULT:-adi060/decision-index-suite-0.2}"
# Read-only Hugging Face token (dataset read). Required while the dataset stays private.
DECISION_INDEX_HF_TOKEN="${DECISION_INDEX_HF_TOKEN:-}"

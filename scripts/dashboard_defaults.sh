# Shared dashboard defaults for setup.sh and run.sh (source, do not execute).
# Suite hosting is configured here — not on the eval-runner secret list.
#
# HF auth: optional when the dataset is public (current: adi060/decision-index-suite-0.2).
# Set DECISION_INDEX_HF_TOKEN only if you switch back to a private dataset.

DECISION_INDEX_SUITE_EDITION_DEFAULT="${DECISION_INDEX_SUITE_EDITION_DEFAULT:-0.2.1}"
DECISION_INDEX_SUITE_DATASET_DEFAULT="${DECISION_INDEX_SUITE_DATASET_DEFAULT:-adi060/decision-index-suite-0.2}"
# Optional read-only Hugging Face token (leave empty when the dataset is public).
DECISION_INDEX_HF_TOKEN="${DECISION_INDEX_HF_TOKEN:-}"

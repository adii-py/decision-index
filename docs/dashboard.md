# Decision Index — eval-ops dashboard (Runbook A)

Decision Index is a **non-agentic / API-scored** eval ([ONBOARDING_A_NEW_EVAL.md](../ONBOARDING_A_NEW_EVAL.md) §14.1). No Docker, no Artifact Registry, no harbor.

## Suite dataset (hosted)

**Live:** [`adi060/decision-index-suite-0.2`](https://huggingface.co/datasets/adi060/decision-index-suite-0.2) (private, 150,759 scoreable rows, edition 0.2.1 hashes verified).

Re-upload only if you rebuild locally:

The public id `multimodalart/decision-index-suite-0.2` does not exist. Build once locally, then upload a **private** Hugging Face dataset:

```sh
pip install -e ".[transformers,rebuild]"
export HF_HUB_DISABLE_XET=1

python -m decision_index suite rebuild --work work
python -m decision_index suite import \
    --edition 0.2.1 \
    --dir suite-0.2 \
    --rows work/artifacts/benchmark-suite/release-v2-rebuilt/selected-rows.jsonl.gz \
    --added-rows work/artifacts/benchmark-suite/release-v2-rebuilt/added-rows.jsonl.gz

python scripts/prepare_hub_upload.py \
    --edition 0.2.1 \
    --rows suite-0.2/selected-rows.jsonl.gz \
    --added-rows suite-0.2/added-rows.jsonl.gz \
    --out hub-upload

export HF_TOKEN=hf_<write-token>
python scripts/upload_suite_to_hf.py --repo adi060/decision-index-suite-0.2
```

Files on the dataset (≈79 MB): `selected-rows.jsonl.gz`, `added-rows.jsonl.gz`, `excluded-questions.json`, `manifest.json`.

## Register the eval (§5)

| Field | Value |
|-------|--------|
| `name` | Decision Index |
| `repo_url` | `https://github.com/adii-py/decision-index` |
| `commit_sha` | pin `main` (e.g. latest with `run.sh` / `input_param.json`) |
| `machine_type` | `n2-standard-4` (smoke); scale up for full runs |
| `input_params` | contents of [`input_param.json`](../input_param.json) |

## Runner secret (not in onboarding §3.6 — add for this eval)

| Secret | Scope | Purpose |
|--------|--------|---------|
| `HF_TOKEN` | eval runner / Secret Manager | read access to `adi060/decision-index-suite-0.2` |

Platform-injected secrets (`GRID_AI_API`, `GITHUB_TOKEN`) are not enough for suite download.

## `setup.sh` downloads the suite (HF approach)

On dashboard runs (`EVAL_RUNNER_WORK_DIR` set), **`setup.sh` runs before `run.sh`** and:

1. Installs Python 3.12 + the kit (`uv venv`, `pip install -e .`)
2. Downloads the frozen suite from Hugging Face into `suite-0.2/` using `HF_TOKEN`
3. Verifies hashes (edition **0.2.1**)

Defaults (override via runner env if needed):

| Env | Default |
|-----|---------|
| `SUITE_DATASET` / `DECISION_INDEX_SUITE_DATASET` | `adi060/decision-index-suite-0.2` |
| `DECISION_INDEX_SUITE_EDITION` | `0.2.1` |

`run.sh` still accepts `--suite-dataset` per run; if `setup.sh` missed the download, `run.sh` retries with the same HF token.

Size: **~79 MB** download, **~800 MB** unpacked, **150,759** scoreable rows.

## First smoke (§14.1 A8)

| Field | Value |
|-------|--------|
| `model` | any Validate alias (form required; ignored when `engine=http`) |
| `model_alpha` | systemone model id (e.g. `xor-1.2`) |
| `suite_dataset` | `adi060/decision-index-suite-0.2` |
| `engine` | `http` |
| `base_url` | `https://grid.ai.juspay.net` |
| `task_range` | `0-9` |
| `resume` | `false` |

Pass: `${EVAL_RUNNER_OUTPUT_DIR}/${eval_run_id}_results.json` with numeric Decision Index and `additional.status: scored`.

## Full run

Clear `task_range`. Expect ~150k requests; use `delay_s` 0.5 (≈120 RPM).

## Engine (F34)

| `engine` | Endpoint | When |
|----------|----------|------|
| `http` | `{origin}/v1/systemone` | systemone models (xor, etc.) — **default** |
| `grid` | `{origin}/v1/chat/completions` | chat models on Grid |
| `random` | none | pipeline check only |

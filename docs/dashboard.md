# Decision Index — eval-ops dashboard (Runbook A)

Decision Index is a **non-agentic / API-scored** eval ([ONBOARDING_A_NEW_EVAL.md](../ONBOARDING_A_NEW_EVAL.md) §14.1). No Docker, no Artifact Registry, no harbor.

## Check configuration locally (before touching the dashboard)

```sh
./scripts/verify_dashboard_config.sh
```

This validates `setup.sh`, `run.sh`, `input_param.json`, contract tests, and (when Hub allows) a suite download. It prints the **commit SHA** and **registration fields** to paste into the eval row.

The dashboard database is authoritative for `input_params`; the in-repo [`input_param.json`](../input_param.json) is the reference copy — keep them in sync.

## Suite dataset (hosted in scripts, not the dashboard form)

**Live:** [`adi060/decision-index-suite-0.2`](https://huggingface.co/datasets/adi060/decision-index-suite-0.2) (150,759 scoreable rows, edition 0.2.1 hashes verified).

Defaults live in [`scripts/dashboard_defaults.sh`](../scripts/dashboard_defaults.sh) (sourced by `setup.sh` and `run.sh`):

| Setting | Default |
|---------|---------|
| `DECISION_INDEX_SUITE_DATASET_DEFAULT` | `adi060/decision-index-suite-0.2` |
| `DECISION_INDEX_SUITE_EDITION_DEFAULT` | `0.2.1` |

**No `suite_dataset` form field** and **no eval-runner `HF_TOKEN` secret** are required when the Hub dataset is public or anonymously readable. For a private dataset, add `HF_TOKEN` to repo `.env` for local dev only (`.env` is gitignored and not cloned on the dashboard VM — make the dataset public or use a read token in CI).

Re-upload only if you rebuild locally:

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
| `commit_sha` | output of `./scripts/verify_dashboard_config.sh` (pin, do not float `main`) |
| `machine_type` | `n2-standard-4` (smoke); scale up for full runs |
| `input_params` | contents of [`input_param.json`](../input_param.json) |

Platform-injected secrets (`GRID_AI_API`, `GITHUB_TOKEN`) are enough for a standard run. Suite download is handled in `setup.sh`.

## `setup.sh` downloads the suite

On dashboard runs (`EVAL_RUNNER_WORK_DIR` set), **`setup.sh` runs before `run.sh`** and:

1. Installs Python 3.12 + the kit (`uv venv`, `pip install -e .`)
2. Downloads the frozen suite from Hugging Face into `suite-0.2/` using script defaults
3. Verifies hashes (edition **0.2.1**)

`run.sh` retries the same download if setup was skipped.

Size: **~79 MB** download, **~800 MB** unpacked, **150,759** scoreable rows.

## First smoke (§14.1 A8)

| Field | Value |
|-------|--------|
| `model` | any Validate alias (form required; ignored when `engine=http`) |
| `model_alpha` | systemone model id (e.g. `xor-1.2`) |
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

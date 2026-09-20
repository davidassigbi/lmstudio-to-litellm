"""lmstudio-to-litellm: keep a LiteLLM model list in sync with LM Studio downloads.

Each cycle adds missing models, deletes managed models that disappeared from
LM Studio, and refreshes changed metadata via PATCH /model/{id}/update.
Safety rule: if the LM Studio side cannot be read, the whole cycle is skipped
and nothing is ever deleted.

Pass --delete-all (with --once) to remove every managed model in one shot;
without --yes it only lists the targets and deletes nothing.
"""

import argparse
import json
import logging
import os
import sys
import time

import requests

LOG = logging.getLogger("lmstudio-to-litellm")

def _require_env(name):
    value = os.environ.get(name)
    if not value:
        sys.stderr.write(f"error: missing required environment variable {name}\n")
        sys.exit(1)
    return value


LMS_BASE_URL = _require_env("LMS_BASE_URL").rstrip("/")
LMS_API_TOKEN = os.environ.get("LMS_API_TOKEN", "")
LITELLM_BASE_URL = _require_env("LITELLM_BASE_URL").rstrip("/")
LITELLM_MASTER_KEY = _require_env("LITELLM_MASTER_KEY")
LITELLM_CREDENTIAL_NAME = _require_env("LITELLM_CREDENTIAL_NAME")
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "60"))
MODEL_TAG = os.environ.get("MODEL_TAG", "lmstudio-to-litellm")
MODEL_PREFIX = os.environ.get("MODEL_PREFIX", "")
if not MODEL_PREFIX and not MODEL_TAG:
    sys.stderr.write(
        "error: set MODEL_PREFIX or MODEL_TAG so managed models can be identified\n"
    )
    sys.exit(1)
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "300"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LITELLM_HEADERS = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
LMS_HEADERS = {"Authorization": f"Bearer {LMS_API_TOKEN}"} if LMS_API_TOKEN else None


def http_json(method, url, headers=None, body=None):
    """Perform an HTTP request; return (status_code, parsed_json_or_text)."""
    resp = requests.request(
        method,
        url,
        headers=headers,
        json=body,
        timeout=HTTP_TIMEOUT,
    )
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text


def parse_lms_models(payload):
    """Parse an LM Studio /api/v1/models payload into {key: info}.

    Raises RuntimeError when the payload does not have the expected shape so
    that run_cycle skips without deleting anything.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise RuntimeError(
            "unexpected LM Studio /api/v1/models response: expected {'models': [...]}"
        )
    return {v["key"]: v for v in payload["models"] if v["type"] == "llm"}


def lmstudio_models():
    """Return the LLM models currently in LM Studio as {key: info}."""
    status, payload = http_json(
        "GET", LMS_BASE_URL + "/api/v1/models", headers=LMS_HEADERS
    )
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"LM Studio /models returned HTTP {status}")
    return parse_lms_models(payload)


def litellm_managed_models():
    """Map LM Studio key -> full LiteLLM /model/info entry, for entries we manage."""
    status, payload = http_json(
        "GET", LITELLM_BASE_URL + "/model/info", headers=LITELLM_HEADERS
    )
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"LiteLLM /model/info returned HTTP {status}")

    managed = {}
    for entry in payload.get("data", []):
        litellm_params = entry.get("litellm_params", {})
        is_prefix_match = MODEL_PREFIX and str(entry.get("model_name", "")).startswith(
            MODEL_PREFIX
        )
        is_tag_match = MODEL_TAG and MODEL_TAG in litellm_params.get("tags", [])
        if not (is_prefix_match or is_tag_match):
            continue
        lmstudio_model_key = litellm_params.get("model")
        if not lmstudio_model_key:
            LOG.warning(
                "managed entry without litellm_params.model; skipping: %s",
                entry.get("model_name"),
            )
            continue
        managed[lmstudio_model_key] = entry
    return managed


def add_model(lmstudio_model_key, info):
    model_name = f"{MODEL_PREFIX}{lmstudio_model_key}"
    body = {
        "model_name": model_name,
        "litellm_params": {
            "model": lmstudio_model_key,
            "custom_llm_provider": "lm_studio",
            "tags": [MODEL_TAG] if MODEL_TAG else [],
            "litellm_credential_name": LITELLM_CREDENTIAL_NAME,
        },
        "model_info": {"lmstudio_info": info},
    }
    status, payload = http_json(
        "POST",
        LITELLM_BASE_URL + "/model/new",
        headers=LITELLM_HEADERS,
        body=body,
    )
    if status in (200, 201):
        LOG.info("added %s (HTTP %d)", model_name, status)
    elif status == 409:
        LOG.info("%s already exists; treating as success", lmstudio_model_key)
    else:
        detail = payload if isinstance(payload, str) else json.dumps(payload)
        raise RuntimeError(f"POST /model/new failed (HTTP {status}): {detail}")


def delete_model(db_model_id):
    status, payload = http_json(
        "POST",
        LITELLM_BASE_URL + "/model/delete",
        headers=LITELLM_HEADERS,
        body={"id": db_model_id},
    )
    if status in (200, 201):
        LOG.info("deleted stale model %s (HTTP %d)", db_model_id, status)
    else:
        detail = payload if isinstance(payload, str) else json.dumps(payload)
        raise RuntimeError(f"POST /model/delete failed (HTTP {status}): {detail}")


def delete_all_managed(confirm):
    """Remove every managed model from LiteLLM. Returns the number deleted."""
    try:
        managed = litellm_managed_models()
    except Exception as exc:
        LOG.error("could not read LiteLLM model list (%s); nothing deleted", exc)
        return 0

    targets = []
    for key, entry in sorted(managed.items()):
        db_model_id = (entry.get("model_info") or {}).get("id")
        if not db_model_id:
            LOG.warning(
                "managed entry without model_info.id; skipping delete: %s",
                entry.get("model_name"),
            )
            continue
        targets.append((key, db_model_id))

    if not confirm:
        for key, _ in targets:
            LOG.info("would delete managed model %r", key)
        LOG.error(
            "no models deleted (%d target(s)); pass --yes to confirm", len(targets)
        )
        return 0

    deleted = 0
    for key, db_model_id in targets:
        try:
            delete_model(db_model_id)
            deleted += 1
        except Exception as exc:
            LOG.error("failed to delete %r: %s", key, exc)
    LOG.info("deleted %d of %d managed model(s)", deleted, len(targets))
    return deleted


def update_model(db_model_id, model_name, model_info):
    body = {"model_name": model_name, "model_info": model_info}
    status, payload = http_json(
        "PATCH",
        f"{LITELLM_BASE_URL}/model/{db_model_id}/update",
        headers=LITELLM_HEADERS,
        body=body,
    )
    if status in (200, 201):
        LOG.info("updated %s (%s); payload sent: %s", model_name, db_model_id, json.dumps(body))
    else:
        detail = payload if isinstance(payload, str) else json.dumps(payload)
        raise RuntimeError(f"PATCH /model/update failed (HTTP {status}): {detail}")


def run_cycle():
    try:
        lms = lmstudio_models()
    except Exception as exc:
        LOG.warning("LM Studio read failed (%s); skipping cycle, no deletions", exc)
        return

    if not lms:
        LOG.warning(
            "LM Studio returned an empty model list; skipping cycle (no deletions)"
        )
        return

    try:
        managed = litellm_managed_models()
    except Exception as exc:
        LOG.warning("LiteLLM model list read failed (%s); skipping cycle", exc)
        return

    to_add = sorted(k for k in lms if k not in managed)
    to_delete = []
    for key, entry in managed.items():
        if key in lms:
            continue
        db_model_id = (entry.get("model_info") or {}).get("id")
        if not db_model_id:
            LOG.warning(
                "managed entry without model_info.id; skipping delete: %s",
                entry.get("model_name"),
            )
            continue
        to_delete.append(db_model_id)

    try:
        for lmstudio_model_key in to_add:
            try:
                add_model(lmstudio_model_key, lms[lmstudio_model_key])
            except Exception as exc:
                LOG.error("failed to add %r: %s", lmstudio_model_key, exc)

        for db_model_id in to_delete:
            try:
                delete_model(db_model_id)
            except Exception as exc:
                LOG.error("failed to delete %r: %s", db_model_id, exc)

        for lmstudio_model_key, entry in managed.items():
            if lmstudio_model_key not in lms:
                continue
            model_info = entry.get("model_info") or {}
            stored_lmstudio_info = model_info.get("lmstudio_info", {})
            expected_name = f"{MODEL_PREFIX}{lmstudio_model_key}"
            name_drifted = entry.get("model_name") != expected_name
            if stored_lmstudio_info == lms[lmstudio_model_key] and not name_drifted:
                continue
            db_model_id = model_info.get("id")
            if not db_model_id:
                LOG.warning(
                    "managed entry without model_info.id; skipping update: %s",
                    lmstudio_model_key,
                )
                continue
            try:
                # update model_info[lmstudio_info] and normalize the name
                update_model(
                    db_model_id,
                    expected_name,
                    {**model_info, "lmstudio_info": lms[lmstudio_model_key]},
                )
            except Exception as exc:
                LOG.error("failed to update %r: %s", lmstudio_model_key, exc)
    except Exception as exc:
        LOG.error("cycle aborted unexpectedly (%s); no further changes this cycle", exc)


def main():
    parser = argparse.ArgumentParser(description="LM Studio -> LiteLLM model sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single reconciliation cycle and exit (for testing)",
    )
    parser.add_argument(
        "--delete-all",
        action="store_true",
        help="remove every managed model from LiteLLM in one shot; requires --once",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the destructive action (without it, --delete-all only lists targets)",
    )
    parsed = parser.parse_args()

    if parsed.delete_all and not parsed.once:
        parser.error("--delete-all requires --once")

    logging.basicConfig(
        stream=sys.stdout,
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s lmstudio-to-litellm: %(message)s",
    )

    if parsed.delete_all:
        LOG.info("delete-all requested, tag %r, prefix %r", MODEL_TAG, MODEL_PREFIX)
        delete_all_managed(parsed.yes)
        sys.exit(0 if parsed.yes else 1)

    mode = "single cycle" if parsed.once else f"poll every {POLL_INTERVAL:.0f}s"
    LOG.info("starting sync (%s), tag %r, prefix %r", mode, MODEL_TAG, MODEL_PREFIX)

    while True:
        run_cycle()
        if parsed.once:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

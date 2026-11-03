"""Tool outcomes and typed output references, independent of display truncation."""

import json

_OUTPUT_FIELDS = ("file_url", "download_url", "artifact_url", "image_path", "preview_url", "details_url")


def failure_result(message):
    return json.dumps({"status": "error", "success": False, "error": message}, ensure_ascii=False)


def normalize_tool_result(value):
    """Preserve successful legacy text; never discard an explicit error channel."""
    if hasattr(value, "output"):
        error = getattr(value, "error", "")
        if error:
            data = {"status": "error", "success": False, "error": str(error)}
            if value.output:
                data["output"] = value.output
            return json.dumps(data, ensure_ascii=False, default=str)
        value = value.output
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value) if value is not None else ""


def tool_observation(value):
    """Read explicit fields only: a URL mentioned in prose is not a generated file."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = None
    if not isinstance(value, dict):
        return {"status": "unknown", "is_error": False, "artifacts": []}
    error = value.get("error")
    failed = bool(error or value.get("is_error") or value.get("isError")) or value.get("success") is False
    failed = failed or value.get("status") in ("error", "failed")
    if failed:
        status = "error"
    elif value.get("status") in ("interrupted", "unknown"):
        status = "unknown"
    elif value.get("status") == "partial":
        status = "partial"
    else:
        status = "success"
    observation = {"status": status, "is_error": failed, "artifacts": []}
    if failed:
        observation["error"] = str(error or value.get("message") or "Tool reported failure")
    if failed or status == "unknown":
        return observation

    seen = set()

    def add(url, field, name=""):
        if not isinstance(url, str) or not url.startswith(("https://", "http://", "ffs://")) or url in seen:
            return
        seen.add(url)
        observation["artifacts"].append({"url": url, "field": field, "name": name})

    for field in _OUTPUT_FIELDS:
        add(value.get(field), field)
    for container in ("files", "images", "artifacts", "artifact_refs"):
        items = value.get(container)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str):
                add(item, container)
            elif isinstance(item, dict):
                for field in ("url", "file_url", "download_url", "image_path"):
                    add(item.get(field), f"{container}.{field}", item.get("name", ""))
    return observation

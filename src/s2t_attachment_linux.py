"""Read one Confluence S2T attachment from an explicitly configured HTTPS origin."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

PATTERN = re.compile(r"^dds_S2T_mapping_v([0-9]+)\.xlsx$")
MAX_BYTES = 50 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("Confluence redirect requires separate review")


def values_from_file(path):
    values = {}
    if path and Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            name, value = text.split("=", 1)
            name, value = name.strip(), value.strip()
            if name.startswith("CONFLUENCE_"):
                values[name] = value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value
    return values


def config(env, env_file):
    process_values = {key: value for key, value in env.items()
                      if key.startswith("CONFLUENCE_") and value}
    # Never combine a trusted process token with an origin from an arbitrary file.
    if process_values:
        if not process_values.get("CONFLUENCE_BASE_URL") or not process_values.get("CONFLUENCE_API_TOKEN"):
            raise ValueError("Confluence origin and token must come from the same trusted source")
        values = process_values
    else:
        values = values_from_file(env_file)
    base = values.get("CONFLUENCE_BASE_URL", "")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("CONFLUENCE_BASE_URL must be one HTTPS origin")
    token = values.get("CONFLUENCE_API_TOKEN")
    if not token:
        raise ValueError("CONFLUENCE_API_TOKEN is not configured")
    auth = values.get("CONFLUENCE_AUTH_TYPE", "bearer").lower()
    if auth not in ("bearer", "basic"):
        raise ValueError("CONFLUENCE_AUTH_TYPE must be bearer or basic")
    email = values.get("CONFLUENCE_EMAIL")
    if auth == "basic" and not email:
        raise ValueError("CONFLUENCE_EMAIL is required for basic auth")
    authorization = ("Basic " + base64.b64encode((email + ":" + token).encode()).decode()
                     if auth == "basic" else "Bearer " + token)
    api = "/" + values.get("CONFLUENCE_API_PATH", "/rest/api").strip("/")
    if ".." in api.split("/") or "?" in api or "#" in api:
        raise ValueError("CONFLUENCE_API_PATH is invalid")
    return parsed.scheme + "://" + parsed.netloc, api, authorization


def select_attachment(attachments):
    candidates = []
    for item in attachments:
        name = item.get("title", "")
        match = PATTERN.fullmatch(name) if isinstance(name, str) else None
        if match:
            candidates.append((int(match.group(1)), item))
    if not candidates:
        raise ValueError("No dds_S2T_mapping_vNNN.xlsx attachment found")
    return max(candidates, key=lambda row: row[0])


def fetch(url, authorization, limit):
    request = Request(url, headers={"Authorization": authorization, "Accept": "application/json"})
    with build_opener(NoRedirect()).open(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Confluence response is too large")
    return data


def attachment(page_id, output_directory, list_only=False, env=None, env_file=None, fetcher=fetch):
    base, api, authorization = config(os.environ if env is None else env, env_file)
    if not re.fullmatch(r"[0-9]{1,20}", page_id):
        raise ValueError("PageId must be numeric")
    endpoint = base + api + "/content/" + quote(page_id) + "/child/attachment?limit=100&expand=version,metadata"
    payload = json.loads(fetcher(endpoint, authorization, 2 * 1024 * 1024))
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise ValueError("Confluence attachment listing is invalid")
    if (len(results) >= 100 or payload.get("_links", {}).get("next") or
            int(payload.get("totalSize", len(results))) > len(results)):
        raise ValueError("Confluence attachment listing is incomplete; review pagination")
    version, item = select_attachment(results)
    result = {"page_id": page_id, "title": item["title"], "business_version": version,
              "attachment_id": item.get("id"), "attachment_version": item.get("version", {}).get("number"),
              "attachment_when": item.get("version", {}).get("when")}
    if list_only:
        return result
    link = item.get("_links", {}).get("download", "")
    if (not isinstance(link, str) or not link.startswith("/") or link.startswith("//") or
            "://" in link or ".." in link.split("/") or "?" in link or "#" in link):
        raise ValueError("Attachment download link must remain on the Confluence origin")
    name = item["title"]
    if Path(name).name != name:
        raise ValueError("Attachment title is not a safe file name")
    root = Path(output_directory)
    root.mkdir(parents=True, exist_ok=True)
    data = fetcher(base + link, authorization, MAX_BYTES)
    with tempfile.NamedTemporaryFile(dir=root, prefix=".s2t-", delete=False) as stream:
        temporary = Path(stream.name)
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
    try:
        os.replace(temporary, root / name)
    finally:
        temporary.unlink(missing_ok=True)
    result.update(output_path=str(root / name), bytes=len(data))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-id", default="205225775")
    parser.add_argument("--output-directory", default=".")
    parser.add_argument("--env-file")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        assert select_attachment([{"title": "dds_S2T_mapping_v9.xlsx"},
                                  {"title": "dds_S2T_mapping_v104.xlsx"}])[0] == 104
        print("download_s2t_attachment Linux self-test: OK")
        return
    print(json.dumps(attachment(args.page_id, args.output_directory, args.list_only,
                                env_file=args.env_file), ensure_ascii=False))


if __name__ == "__main__":
    main()

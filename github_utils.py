# -*- coding: utf-8 -*-
"""GitHub helpers (private repo storage).

Uses GitHub Contents API to read/write a single file (CSV datastore).
Requires a Personal Access Token with repo contents read/write permission.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Optional, Tuple

import requests


@dataclass
class GithubFile:
    text: str
    sha: str


def get_file(owner: str, repo: str, path: str, token: str, branch: str = "main", timeout_s: int = 20) -> Optional[GithubFile]:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }
    r = requests.get(url, headers=headers, params={"ref": branch}, timeout=timeout_s)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    content_b64 = data.get("content", "") or ""
    sha = data.get("sha", "")
    # content might be split lines
    content_b64 = content_b64.replace("\n", "")
    raw = base64.b64decode(content_b64.encode("utf-8")) if content_b64 else b""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return GithubFile(text=text, sha=sha)


def put_file(
    owner: str,
    repo: str,
    path: str,
    token: str,
    message: str,
    text: str,
    branch: str = "main",
    sha: Optional[str] = None,
    timeout_s: int = 20,
) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }
    content_b64 = base64.b64encode(text.encode("utf-8")).decode("utf-8")
    payload = {"message": message, "content": content_b64, "branch": branch}
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=headers, json=payload, timeout=timeout_s)
    if r.status_code == 404:
        # GitHub often returns 404 for private repos when the token lacks access.
        raise requests.HTTPError(
            f"404 Not Found for {url}. Check owner/repo/path/branch and token permissions (Contents: read/write).",
            response=r,
        )
    r.raise_for_status()
    return r.json()

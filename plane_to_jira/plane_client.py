import logging
import time

import requests

logger = logging.getLogger(__name__)


class PlaneClient:
    def __init__(self, base_url: str, api_token: str, workspace_slug: str):
        self.base_url = base_url.rstrip("/")
        self.workspace_slug = workspace_slug
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-API-Key": api_token,
                "Content-Type": "application/json",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/workspaces/{self.workspace_slug}/{path}"

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        max_retries = 5
        for attempt in range(max_retries):
            logger.debug("%s %s", method, url)
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                logger.warning("Rate limited (429), retrying in %.1fs (attempt %d/%d)", wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            logger.debug("Response: %d", resp.status_code)
            return resp
        logger.error("Rate limited after %d retries, giving up", max_retries)
        return resp

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        results = []
        params = params or {}
        params.setdefault("per_page", 100)
        while True:
            resp = self._request_with_retry("GET", url, params=params)
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results", []))
            if not data.get("next_page_results", False):
                break
            next_cursor = data.get("next_cursor")
            if not next_cursor:
                break
            params["cursor"] = next_cursor
            time.sleep(1)
        return results

    def list_projects(self) -> list[dict]:
        return self._paginate(self._url("projects/"))

    def get_project(self, project_id: str) -> dict:
        resp = self._request_with_retry("GET", self._url(f"projects/{project_id}/"))
        resp.raise_for_status()
        return resp.json()

    def list_states(self, project_id: str) -> list[dict]:
        return self._paginate(self._url(f"projects/{project_id}/states/"))

    def list_labels(self, project_id: str) -> list[dict]:
        return self._paginate(self._url(f"projects/{project_id}/labels/"))

    def list_work_items(self, project_id: str) -> list[dict]:
        return self._paginate(self._url(f"projects/{project_id}/work-items/"))

    def get_work_item(self, project_id: str, work_item_id: str) -> dict:
        resp = self._request_with_retry(
            "GET", self._url(f"projects/{project_id}/work-items/{work_item_id}/")
        )
        resp.raise_for_status()
        return resp.json()

    def list_comments(self, project_id: str, work_item_id: str) -> list[dict]:
        return self._paginate(
            self._url(
                f"projects/{project_id}/work-items/{work_item_id}/comments/"
            )
        )

    def list_links(self, project_id: str, work_item_id: str) -> list[dict]:
        return self._paginate(
            self._url(
                f"projects/{project_id}/work-items/{work_item_id}/links/"
            )
        )

    def get_workspace_members(self) -> list[dict]:
        resp = self._request_with_retry("GET", self._url("members/"))
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def list_modules(self, project_id: str) -> list[dict]:
        return self._paginate(self._url(f"projects/{project_id}/modules/"))

    def list_module_work_items(self, project_id: str, module_id: str) -> list[dict]:
        return self._paginate(
            self._url(f"projects/{project_id}/modules/{module_id}/module-issues/")
        )

    def download_asset(self, asset_url: str) -> tuple[bytes, str]:
        """Download an asset from Plane. Returns (content_bytes, content_type)."""
        if asset_url.startswith("/"):
            url = f"{self.base_url}{asset_url}"
        else:
            url = asset_url
        resp = self._request_with_retry("GET", url)
        resp.raise_for_status()
        return resp.content, resp.headers.get("Content-Type", "application/octet-stream")

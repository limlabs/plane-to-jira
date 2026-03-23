import logging

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(email, api_token)
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}/rest/api/3/{path}"

    def get_all_users(self) -> list[dict]:
        """Get all users that can be assigned issues."""
        resp = self.session.get(
            self._url("users/search"), params={"maxResults": 1000}
        )
        resp.raise_for_status()
        return resp.json()

    def find_user_by_email(self, email: str) -> dict | None:
        resp = self.session.get(
            self._url("user/search"), params={"query": email}
        )
        resp.raise_for_status()
        users = resp.json()
        logger.debug("JIRA user search for %s returned %d results: %s", email, len(users), users)
        # Prefer exact email match
        for user in users:
            if user.get("emailAddress", "").lower() == email.lower():
                return user
        # Jira Cloud may hide emailAddress for managed accounts;
        # trust the result if the search returned exactly one user
        if len(users) == 1:
            return users[0]
        return None

    def get_project(self, project_key: str) -> dict | None:
        resp = self.session.get(self._url(f"project/{project_key}"))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def find_project_by_name(self, name: str) -> dict | None:
        resp = self.session.get(self._url("project"))
        resp.raise_for_status()
        for project in resp.json():
            if project.get("name", "").lower() == name.lower():
                return project
        return None

    def create_project(
        self,
        key: str,
        name: str,
        description: str,
        lead_account_id: str,
        project_type: str = "software",
    ) -> dict:
        payload = {
            "key": key,
            "name": name,
            "description": description,
            "projectTypeKey": project_type,
            "projectTemplateKey": f"com.pyxis.greenhopper.jira:gh-simplified-kanban-classic",
            "leadAccountId": lead_account_id,
        }
        resp = self.session.post(self._url("project"), json=payload)
        if not resp.ok:
            logger.error("Failed to create project: %s", resp.text)
            resp.raise_for_status()
        return resp.json()

    def search_issues(self, jql: str) -> list[dict]:
        """Search for issues using JQL. Returns all matching issues."""
        results = []
        start = 0
        while True:
            resp = self.session.get(
                self._url("search/jql"),
                params={"jql": jql, "fields": "summary", "startAt": start, "maxResults": 100},
            )
            if not resp.ok:
                logger.error("JQL search failed: %s", resp.text)
                resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("issues", []))
            if data.get("isLast", True):
                break
            start += len(data.get("issues", []))
        return results

    def create_issue(self, fields: dict) -> dict:
        payload = {"fields": fields}
        resp = self.session.post(self._url("issue"), json=payload)
        resp.raise_for_status()
        return resp.json()

    def add_comment(self, issue_key: str, body: dict) -> dict:
        resp = self.session.post(
            self._url(f"issue/{issue_key}/comment"), json={"body": body}
        )
        resp.raise_for_status()
        return resp.json()

    def add_attachment(
        self, issue_key: str, filename: str, content: bytes, content_type: str
    ) -> list[dict]:
        url = self._url(f"issue/{issue_key}/attachments")
        headers = {"X-Atlassian-Token": "no-check"}
        resp = self.session.post(
            url,
            headers=headers,
            files={"file": (filename, content, content_type)},
        )
        resp.raise_for_status()
        return resp.json()

    def get_transitions(self, issue_key: str) -> list[dict]:
        resp = self.session.get(self._url(f"issue/{issue_key}/transitions"))
        resp.raise_for_status()
        return resp.json().get("transitions", [])

    def transition_issue(self, issue_key: str, transition_id: str) -> None:
        resp = self.session.post(
            self._url(f"issue/{issue_key}/transitions"),
            json={"transition": {"id": transition_id}},
        )
        resp.raise_for_status()

    def create_issue_link(
        self, link_type: str, inward_key: str, outward_key: str
    ) -> None:
        resp = self.session.post(
            self._url("issueLink"),
            json={
                "type": {"name": link_type},
                "inwardIssue": {"key": inward_key},
                "outwardIssue": {"key": outward_key},
            },
        )
        resp.raise_for_status()

    def add_labels(self, issue_key: str, labels: list[str]) -> None:
        if not labels:
            return
        resp = self.session.put(
            self._url(f"issue/{issue_key}"),
            json={"fields": {"labels": labels}},
        )
        resp.raise_for_status()

    def get_issue(self, issue_key: str) -> dict | None:
        resp = self.session.get(self._url(f"issue/{issue_key}"))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def update_issue(self, issue_key: str, fields: dict) -> None:
        resp = self.session.put(
            self._url(f"issue/{issue_key}"), json={"fields": fields}
        )
        resp.raise_for_status()

    def delete_issue(self, issue_key: str) -> None:
        resp = self.session.delete(self._url(f"issue/{issue_key}"))
        resp.raise_for_status()

## CLI Usage

1. Copy the `.env.example` to `.env` and fill in details
2. Generate a Plane token - Go to `https://<your-plane-url>/<your_workspace>/settings/account/api-tokens/`
3. Generate a JIRA token - Go to `https://id.atlassian.com/manage-profile/security/api-tokens`
4. Update `.env` with new tokens
5. [Install uv](https://docs.astral.sh/uv/getting-started/installation/) if you haven't already
6. Install dependencies with `uv`:

  ```bash
  uv sync
  ```
  
7. Run the CLI:

  ```bash
  uv run plane-to-jira
  ```

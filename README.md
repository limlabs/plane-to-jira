## CLI Usage

1. Clone this repo
2. Copy the `.env.example` to `.env` and fill in details
3. Generate a Plane token - Go to `https://<your-plane-url>/<your_workspace>/settings/account/api-tokens/`
4. Generate a JIRA token - Go to `https://id.atlassian.com/manage-profile/security/api-tokens`
5. Update `.env` with new tokens
6. [Install uv](https://docs.astral.sh/uv/getting-started/installation/) if you haven't already
7. Install dependencies with `uv`:

  ```bash
  uv sync
  ```
  
7. Run the CLI:

  ```bash
  uv run plane-to-jira
  ```

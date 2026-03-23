import os
import re
from urllib.parse import urlparse

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

from .plane_client import PlaneClient
from .jira_client import JiraClient
from .converter import (
    html_to_adf,
    html_to_adf_comment,
    extract_image_urls,
    map_priority,
    map_state_to_status,
)

console = Console()


class MigrationError(Exception):
    pass


class Migrator:
    def __init__(self, plane: PlaneClient, jira: JiraClient, user_map: dict[str, str] | None = None):
        self.plane = plane
        self.jira = jira
        self._user_map = user_map or {}  # plane email → jira email
        self._plane_members: dict[str, dict] = {}  # uuid → member
        self._jira_users: dict[str, dict] = {}  # plane email → jira user
        self._state_map: dict[str, dict] = {}  # state_id → state

    def migrate_project(
        self,
        plane_project_id: str,
        jira_project_key: str | None = None,
        dry_run: bool = False,
        yes: bool = False,
    ) -> None:
        project = self.plane.get_project(plane_project_id)
        project_name = project["name"]
        identifier = project["identifier"]
        jira_key = jira_project_key or identifier

        console.rule(f"Migrating [bold]{project_name}[/bold] ({identifier} → {jira_key})")

        # Load Plane workspace members
        console.print("Loading Plane workspace members...")
        members = self.plane.get_workspace_members()
        self._plane_members = {m["id"]: m for m in members}

        # Load states
        console.print("Loading Plane states...")
        states = self.plane.list_states(plane_project_id)
        self._state_map = {s["id"]: s for s in states}

        # Load labels
        console.print("Loading Plane labels...")
        labels = self.plane.list_labels(plane_project_id)
        label_map = {l["id"]: l["name"] for l in labels}

        # Load modules and build work_item → module mapping
        console.print("Loading Plane modules...")
        modules = self.plane.list_modules(plane_project_id)
        module_map = {mod["id"]: mod for mod in modules}
        work_item_to_module: dict[str, str] = {}  # work_item_id → module_id
        for mod in modules:
            mod_items = self.plane.list_module_work_items(plane_project_id, mod["id"])
            for mi in mod_items:
                wi_id = mi.get("id", "")
                if wi_id and wi_id not in work_item_to_module:
                    work_item_to_module[wi_id] = mod["id"]
        console.print(f"Found [bold]{len(modules)}[/bold] modules")

        # Load all work items
        console.print("Loading Plane work items...")
        work_items = self.plane.list_work_items(plane_project_id)
        work_items.sort(key=lambda i: i["sequence_id"])
        console.print(f"Found [bold]{len(work_items)}[/bold] work items")

        # Collect all assignee UUIDs and validate against JIRA
        console.print("\nValidating assignees...")
        assignee_uuids = set()
        for item in work_items:
            for a in item.get("assignees", []):
                assignee_uuids.add(a)

        missing_assignees = self._validate_assignees(assignee_uuids)
        if missing_assignees:
            table = Table(title="Missing JIRA Users")
            table.add_column("Plane Email")
            table.add_column("JIRA Email Searched")
            for name, email in missing_assignees:
                table.add_row(name, email)
            console.print(table)
            raise MigrationError(
                f"{len(missing_assignees)} Plane assignee(s) not found in JIRA with matching email. "
                "Create these users in JIRA first, then retry."
            )
        console.print("[green]All assignees validated in JIRA[/green]")

        if dry_run:
            self._print_dry_run(work_items, jira_key, label_map, module_map, work_item_to_module)
            return

        # Create or verify JIRA project
        jira_project = self.jira.get_project(jira_key)
        if not jira_project:
            if not yes:
                entered_key = console.input(
                    f"JIRA project [bold]{jira_key}[/bold] not found. "
                    "Enter existing JIRA project key (or press Enter to create new): "
                ).strip()
                if entered_key:
                    jira_key = entered_key
                    jira_project = self.jira.get_project(jira_key)
                    if not jira_project:
                        raise MigrationError(f"JIRA project {jira_key} not found")

        if jira_project:
            console.print(f"Using existing JIRA project [bold]{jira_key}[/bold]")
        else:
            console.print(f"Creating JIRA project [bold]{jira_key}[/bold]...")
            lead = self._get_jira_lead()
            self.jira.create_project(
                key=jira_key,
                name=project_name,
                description=project.get("description", ""),
                lead_account_id=lead["accountId"],
            )
            console.print(f"[green]Created JIRA project {jira_key}[/green]")

        # Load existing JIRA issues to skip duplicates
        console.print("Checking for existing JIRA issues...")
        existing_issues = self.jira.search_issues(f'project = "{jira_key}"')
        existing_summaries: dict[str, str] = {}  # summary → issue key
        for issue in existing_issues:
            summary = issue.get("fields", {}).get("summary", "")
            existing_summaries[summary] = issue["key"]
        if existing_summaries:
            console.print(f"Found [bold]{len(existing_summaries)}[/bold] existing issues")

        # Migrate work items in sequence order
        item_by_seq = {i["sequence_id"]: i for i in work_items}
        # plane item id → jira issue key
        jira_key_map: dict[str, str] = {}
        is_new_project = jira_project is None and not existing_summaries

        if is_new_project:
            # New project: create placeholders to preserve Plane→JIRA numbering
            max_seq = work_items[-1]["sequence_id"] if work_items else 0
            issue_range = range(1, max_seq + 1)
            placeholder_keys = []
        else:
            # Existing project: just create issues, numbering won't match
            issue_range = [i["sequence_id"] for i in work_items]
            placeholder_keys = []

        skipped = 0
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as progress:
            task = progress.add_task("Creating issues", total=len(issue_range))

            for seq in issue_range:
                if seq in item_by_seq:
                    item = item_by_seq[seq]

                    # Skip if already exists in JIRA
                    existing_key = existing_summaries.get(item["name"])
                    if existing_key:
                        jira_key_map[item["id"]] = existing_key
                        skipped += 1
                        progress.advance(task)
                        continue

                    progress.update(
                        task,
                        description=f"Creating: {item['name'][:50]}",
                    )
                    issue_key = self._create_issue(
                        item, jira_key, label_map, plane_project_id
                    )
                    jira_key_map[item["id"]] = issue_key
                else:
                    # Create placeholder to preserve numbering (new projects only)
                    progress.update(
                        task,
                        description=f"Creating placeholder {jira_key}-{seq}",
                    )
                    result = self.jira.create_issue(
                        {
                            "project": {"key": jira_key},
                            "summary": f"[PLACEHOLDER - gap in Plane sequence]",
                            "issuetype": {"name": "Task"},
                        }
                    )
                    placeholder_keys.append(result["key"])

                progress.advance(task)

        if skipped:
            console.print(f"Skipped [bold]{skipped}[/bold] already-existing issues")

        # Clean up placeholders
        if placeholder_keys:
            console.print(
                f"Cleaning up {len(placeholder_keys)} placeholder issues..."
            )
            for key in placeholder_keys:
                self.jira.delete_issue(key)

        # Create epics for modules and link issues
        if modules:
            console.print("Creating epics for modules...")
            epic_key_map: dict[str, str] = {}  # module_id → jira epic key
            for mod in modules:
                existing_key = existing_summaries.get(mod["name"])
                if existing_key:
                    epic_key_map[mod["id"]] = existing_key
                    console.print(f"  Epic already exists {existing_key}: {mod['name']}")
                    continue
                epic_result = self.jira.create_issue(
                    {
                        "project": {"key": jira_key},
                        "summary": mod["name"],
                        "issuetype": {"name": "Epic"},
                    }
                )
                epic_key_map[mod["id"]] = epic_result["key"]
                console.print(f"  Created epic {epic_result['key']}: {mod['name']}")

            console.print("Linking issues to epics...")
            for item in work_items:
                module_id = work_item_to_module.get(item["id"])
                if not module_id:
                    continue
                issue_key = jira_key_map.get(item["id"])
                epic_key = epic_key_map.get(module_id)
                if issue_key and epic_key:
                    try:
                        self.jira.update_issue(issue_key, {"parent": {"key": epic_key}})
                    except Exception as e:
                        console.print(
                            f"[yellow]Warning: Failed to link {issue_key} to epic {epic_key}: {e}[/yellow]"
                        )

        # Migrate parent/child relationships
        console.print("Setting up parent/child relationships...")
        self._migrate_subtasks(work_items, jira_key_map)

        console.print(
            f"\n[bold green]Migration complete![/bold green] "
            f"Migrated {len(work_items)} work items to {jira_key}"
        )

    def _validate_assignees(
        self, assignee_uuids: set[str]
    ) -> list[tuple[str, str]]:
        missing = []
        for uuid in assignee_uuids:
            member = self._plane_members.get(uuid)
            if not member:
                missing.append(("Unknown", f"UUID: {uuid}"))
                continue
            plane_email = member.get("email", "")
            if not plane_email:
                display = member.get("display_name", member.get("first_name", ""))
                missing.append((display, "(no email)"))
                continue
            jira_email = self._user_map.get(plane_email, plane_email)
            jira_user = self.jira.find_user_by_email(jira_email)
            if jira_user:
                self._jira_users[plane_email] = jira_user
            else:
                missing.append((plane_email, jira_email))
        return missing

    def _get_jira_lead(self) -> dict:
        """Get a JIRA user to act as project lead. Uses first cached user or searches."""
        if self._jira_users:
            return next(iter(self._jira_users.values()))
        users = self.jira.get_all_users()
        if users:
            return users[0]
        raise MigrationError("No JIRA users found to act as project lead")

    def _create_issue(
        self,
        item: dict,
        jira_key: str,
        label_map: dict[str, str],
        plane_project_id: str,
    ) -> str:
        # Build description ADF
        description = html_to_adf(item.get("description_html"))

        # Map assignees
        assignee = None
        assignee_ids = item.get("assignees", [])
        if assignee_ids:
            first_assignee_uuid = assignee_ids[0]
            member = self._plane_members.get(first_assignee_uuid)
            if member:
                email = member.get("email", "")
                jira_user = self._jira_users.get(email)
                if jira_user:
                    assignee = {"accountId": jira_user["accountId"]}

        # Map labels
        item_labels = []
        for label_id in item.get("labels", []):
            name = label_map.get(label_id)
            if name:
                # JIRA labels can't have spaces, replace with hyphens
                item_labels.append(name.replace(" ", "-"))

        fields = {
            "project": {"key": jira_key},
            "summary": item["name"],
            "description": description,
            "issuetype": {"name": "Task"},
            "priority": map_priority(item.get("priority", "none")),
        }
        if assignee:
            fields["assignee"] = assignee
        if item_labels:
            fields["labels"] = item_labels
        if item.get("start_date"):
            fields["customfield_10015"] = item["start_date"]  # Start date
        if item.get("target_date"):
            fields["duedate"] = item["target_date"]

        result = self.jira.create_issue(fields)
        issue_key = result["key"]

        # Upload images/attachments from description
        self._upload_images(
            issue_key,
            item.get("description_html"),
        )

        # Migrate comments
        comments = self.plane.list_comments(plane_project_id, item["id"])
        for comment in comments:
            comment_html = comment.get("comment_html", "")
            if not comment_html:
                continue

            # Find commenter info
            commenter_uuid = comment.get("created_by", "")
            commenter = self._plane_members.get(commenter_uuid, {})
            commenter_name = commenter.get("email", "Unknown")
            created_at = comment.get("created_at", "")

            # Prefix comment with original author and timestamp
            prefix_html = (
                f"<p><em>Originally posted by {commenter_name}"
                f"{' on ' + created_at[:10] if created_at else ''}"
                f"</em></p>"
            )
            full_html = prefix_html + comment_html
            comment_adf = html_to_adf_comment(full_html)
            self.jira.add_comment(issue_key, comment_adf)

            # Upload images from comments too
            self._upload_images(issue_key, comment_html)

        # Migrate links
        links = self.plane.list_links(plane_project_id, item["id"])
        for link in links:
            url = link.get("url", "")
            title = link.get("title", url)
            if url:
                link_comment = html_to_adf(f'<p>Link: <a href="{url}">{title}</a></p>')
                self.jira.add_comment(issue_key, link_comment)

        # Transition to correct status
        state_id = item.get("state")
        if state_id and state_id in self._state_map:
            state = self._state_map[state_id]
            target_status = map_state_to_status(state.get("group", ""))
            self._transition_to_status(issue_key, target_status)

        return issue_key

    def _upload_images(self, issue_key: str, html: str | None) -> None:
        if not html:
            return
        image_urls = extract_image_urls(html)
        for url in image_urls:
            try:
                # Determine filename from URL
                parsed = urlparse(url)
                filename = os.path.basename(parsed.path) or "image.png"
                content, content_type = self.plane.download_asset(url)
                self.jira.add_attachment(issue_key, filename, content, content_type)
            except Exception as e:
                console.print(
                    f"[yellow]Warning: Failed to upload image {url} "
                    f"to {issue_key}: {e}[/yellow]"
                )

    def _transition_to_status(self, issue_key: str, target_status: str) -> None:
        """Transition an issue to the target status category."""
        transitions = self.jira.get_transitions(issue_key)
        for t in transitions:
            # Match by status category name
            t_name = t.get("to", {}).get("name", "").lower()
            if t_name == target_status.lower():
                self.jira.transition_issue(issue_key, t["id"])
                return
        # Fallback: try partial match
        for t in transitions:
            t_name = t.get("to", {}).get("name", "").lower()
            if target_status.lower() in t_name or t_name in target_status.lower():
                self.jira.transition_issue(issue_key, t["id"])
                return

    def _migrate_subtasks(
        self,
        work_items: list[dict],
        jira_key_map: dict[str, str],
    ) -> None:
        """Set up parent/child links for work items that have parents."""
        for item in work_items:
            parent_id = item.get("parent")
            if not parent_id:
                continue
            child_key = jira_key_map.get(item["id"])
            parent_key = jira_key_map.get(parent_id)
            if not child_key or not parent_key:
                continue
            try:
                self.jira.create_issue_link(
                    "Blocks", parent_key, child_key
                )
            except Exception as e:
                console.print(
                    f"[yellow]Warning: Failed to link {child_key} → {parent_key}: {e}[/yellow]"
                )

    def _print_dry_run(
        self,
        work_items: list[dict],
        jira_key: str,
        label_map: dict,
        module_map: dict[str, dict],
        work_item_to_module: dict[str, str],
    ) -> None:
        console.rule("[bold yellow]DRY RUN[/bold yellow]")

        # Show modules → epics mapping
        if module_map:
            mod_table = Table(title="Modules → Epics")
            mod_table.add_column("Module")
            mod_table.add_column("Work Items")
            module_item_counts: dict[str, int] = {}
            for wi_id, mod_id in work_item_to_module.items():
                module_item_counts[mod_id] = module_item_counts.get(mod_id, 0) + 1
            for mod_id, mod in module_map.items():
                mod_table.add_row(mod["name"], str(module_item_counts.get(mod_id, 0)))
            console.print(mod_table)
            console.print()

        table = Table(title="Work Items to Migrate")
        table.add_column("Plane ID")
        table.add_column("JIRA Key")
        table.add_column("Summary")
        table.add_column("Priority")
        table.add_column("State")
        table.add_column("Assignees")
        table.add_column("Epic")

        for item in work_items:
            seq = item["sequence_id"]
            state_id = item.get("state", "")
            state = self._state_map.get(state_id, {})
            state_name = state.get("name", "?")

            assignee_names = []
            for a_uuid in item.get("assignees", []):
                m = self._plane_members.get(a_uuid, {})
                assignee_names.append(m.get("email", "?"))

            module_id = work_item_to_module.get(item["id"], "")
            epic_name = module_map.get(module_id, {}).get("name", "-")

            table.add_row(
                f"{jira_key}-{seq}",
                f"{jira_key}-{seq}",
                item["name"][:60],
                item.get("priority", "none"),
                state_name,
                ", ".join(assignee_names) or "-",
                epic_name,
            )

        console.print(table)
        console.print(f"\n[yellow]Dry run complete. {len(work_items)} items would be migrated.[/yellow]")

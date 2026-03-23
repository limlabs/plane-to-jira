import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify


# Plane state groups → JIRA simplified workflow status categories
STATE_GROUP_MAP = {
    "backlog": "To Do",
    "unstarted": "To Do",
    "started": "In Progress",
    "completed": "Done",
    "cancelled": "Done",
}

PRIORITY_MAP = {
    "urgent": "Highest",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "none": "Lowest",
}


def html_to_adf(html: str | None) -> dict:
    """Convert Plane's HTML description to Atlassian Document Format."""
    if not html or html.strip() in ("", "<p></p>"):
        return _adf_doc([_adf_paragraph([_adf_text("")])])

    soup = BeautifulSoup(html, "html.parser")
    content = _convert_nodes(soup.children)
    if not content:
        content = [_adf_paragraph([_adf_text("")])]
    return _adf_doc(content)


def html_to_adf_comment(html: str | None) -> dict:
    """Convert comment HTML to ADF."""
    return html_to_adf(html)


def _adf_doc(content: list[dict]) -> dict:
    return {"version": 1, "type": "doc", "content": content}


def _adf_paragraph(content: list[dict]) -> dict:
    return {"type": "paragraph", "content": content}


def _adf_text(text: str, marks: list[dict] | None = None) -> dict:
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def _adf_heading(level: int, content: list[dict]) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": content}


def _adf_code_block(text: str, language: str | None = None) -> dict:
    attrs = {}
    if language:
        attrs["language"] = language
    node = {"type": "codeBlock", "content": [_adf_text(text)]}
    if attrs:
        node["attrs"] = attrs
    return node


def _adf_bullet_list(items: list[dict]) -> dict:
    return {"type": "bulletList", "content": items}


def _adf_ordered_list(items: list[dict]) -> dict:
    return {"type": "orderedList", "content": items}


def _adf_list_item(content: list[dict]) -> dict:
    return {"type": "listItem", "content": content}


def _adf_blockquote(content: list[dict]) -> dict:
    return {"type": "blockquote", "content": content}


def _adf_hard_break() -> dict:
    return {"type": "hardBreak"}


def _adf_rule() -> dict:
    return {"type": "rule"}


def _adf_media_single(media: dict) -> dict:
    return {
        "type": "mediaSingle",
        "attrs": {"layout": "center"},
        "content": [media],
    }


def _convert_nodes(nodes) -> list[dict]:
    result = []
    for node in nodes:
        converted = _convert_node(node)
        if converted:
            result.extend(converted)
    return result


def _convert_node(node) -> list[dict]:
    from bs4 import NavigableString, Tag

    if isinstance(node, NavigableString):
        text = str(node)
        if text.strip():
            return [_adf_paragraph([_adf_text(text)])]
        return []

    if not isinstance(node, Tag):
        return []

    tag = node.name.lower()

    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(tag[1])
        inline = _convert_inline(node)
        if not inline:
            inline = [_adf_text("")]
        return [_adf_heading(level, inline)]

    if tag == "p":
        inline = _convert_inline(node)
        if not inline:
            return []
        return [_adf_paragraph(inline)]

    if tag == "pre":
        code_tag = node.find("code")
        text = code_tag.get_text() if code_tag else node.get_text()
        lang = None
        if code_tag and code_tag.get("class"):
            for cls in code_tag["class"]:
                if cls.startswith("language-"):
                    lang = cls[9:]
        return [_adf_code_block(text, lang)]

    if tag == "ul":
        items = []
        for li in node.find_all("li", recursive=False):
            li_content = _convert_nodes(li.children)
            if not li_content:
                li_content = [_adf_paragraph([_adf_text(li.get_text())])]
            items.append(_adf_list_item(li_content))
        if items:
            return [_adf_bullet_list(items)]
        return []

    if tag == "ol":
        items = []
        for li in node.find_all("li", recursive=False):
            li_content = _convert_nodes(li.children)
            if not li_content:
                li_content = [_adf_paragraph([_adf_text(li.get_text())])]
            items.append(_adf_list_item(li_content))
        if items:
            return [_adf_ordered_list(items)]
        return []

    if tag == "blockquote":
        content = _convert_nodes(node.children)
        if not content:
            content = [_adf_paragraph([_adf_text(node.get_text())])]
        return [_adf_blockquote(content)]

    if tag == "hr":
        return [_adf_rule()]

    if tag == "br":
        return []

    if tag == "img":
        # Images will be handled separately as attachments
        src = node.get("src", "")
        alt = node.get("alt", "image")
        if src:
            return [_adf_paragraph([_adf_text(f"[image: {alt}]({src})")])]
        return []

    if tag in ("div", "section", "article", "main", "span"):
        return _convert_nodes(node.children)

    # Fallback: try to get inline content, wrap in paragraph
    inline = _convert_inline(node)
    if inline:
        return [_adf_paragraph(inline)]
    return []


def _convert_inline(node) -> list[dict]:
    from bs4 import NavigableString, Tag

    result = []
    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text:
                result.append(_adf_text(text))
        elif isinstance(child, Tag):
            tag = child.name.lower()
            if tag == "br":
                result.append(_adf_hard_break())
            elif tag in ("strong", "b"):
                for inline in _convert_inline(child):
                    marks = inline.get("marks", [])
                    marks.append({"type": "strong"})
                    inline["marks"] = marks
                    result.append(inline)
            elif tag in ("em", "i"):
                for inline in _convert_inline(child):
                    marks = inline.get("marks", [])
                    marks.append({"type": "em"})
                    inline["marks"] = marks
                    result.append(inline)
            elif tag == "code":
                result.append(
                    _adf_text(child.get_text(), [{"type": "code"}])
                )
            elif tag == "a":
                href = child.get("href", "")
                text = child.get_text() or href
                result.append(
                    _adf_text(
                        text,
                        [{"type": "link", "attrs": {"href": href}}],
                    )
                )
            elif tag == "img":
                src = child.get("src", "")
                alt = child.get("alt", "image")
                if src:
                    result.append(_adf_text(f"[image: {alt}]"))
            elif tag in ("del", "s", "strike"):
                for inline in _convert_inline(child):
                    marks = inline.get("marks", [])
                    marks.append({"type": "strike"})
                    inline["marks"] = marks
                    result.append(inline)
            elif tag == "u":
                for inline in _convert_inline(child):
                    marks = inline.get("marks", [])
                    marks.append({"type": "underline"})
                    inline["marks"] = marks
                    result.append(inline)
            else:
                # Recurse into unknown inline tags
                result.extend(_convert_inline(child))
    return result


def extract_image_urls(html: str | None) -> list[str]:
    """Extract all image URLs from HTML content."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src:
            urls.append(src)
    return urls


def map_priority(plane_priority: str) -> dict:
    """Map Plane priority to JIRA priority."""
    jira_name = PRIORITY_MAP.get(plane_priority, "Medium")
    return {"name": jira_name}


def map_state_to_status(state_group: str) -> str:
    """Map Plane state group to JIRA status category name."""
    return STATE_GROUP_MAP.get(state_group, "To Do")

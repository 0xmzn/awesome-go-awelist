#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml"]
# ///
"""Parse avelino/awesome-go's README.md into a structured awesome.yaml file.

Strategy:
  1. Parse the Table of Contents to discover the category tree (titles, slugs,
     nesting). The TOC is the single source of truth for structure.
  2. For each leaf in that tree, locate its heading in the body and extract the
     description + links from the markdown beneath it.

Usage:
    uv run scripts/parse_awesome_go.py [-o OUTPUT] [-i INPUT]
"""

import argparse
import re
import sys
import urllib.request

import yaml

README_URL = (
    "https://raw.githubusercontent.com/avelino/awesome-go/refs/heads/main/README.md"
)

SKIP_SLUGS = {"awesome-go", "contents", "resources"}
STOP_SLUGS = {"contribution", "license"}

TOC_ENTRY_RE = re.compile(r"^(\s*)-\s*\[(.+?)\]\(#(.+?)\)\s*$")
HEADING_RE = re.compile(r"^(#{2,4})\s+(.+?)\s*$")
LINK_RE = re.compile(r"^[-*]\s*\[(.+?)\]\(([^)]+)\)\s*[-–—]\s*(.+)$")
LINK_NO_DESC_RE = re.compile(r"^[-*]\s*\[(.+?)\]\(([^)]+)\)\s*$")
ITALIC_RE = re.compile(r"^_(.+)_$")

MD_LINK_RE = re.compile(r"\[([^\[\]]*)\]\([^()]*\)")
MD_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
MD_CODE_RE = re.compile(r"`([^`]+)`")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_markdown(text: str) -> str:
    text = text.strip()
    prev = None
    while prev != text:
        prev = text
        text = MD_LINK_RE.sub(r"\1", text)
    text = MD_BOLD_RE.sub(r"\1", text)
    text = MD_CODE_RE.sub(r"\1", text)
    return text.strip()


def fetch_readme(url: str) -> str:
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Phase 1: Parse the TOC into a category tree
# ---------------------------------------------------------------------------

class TOCNode:
    def __init__(self, title: str, slug: str):
        self.title = title
        self.slug = slug
        self.children: list["TOCNode"] = []

    def __repr__(self):
        return f"TOCNode({self.title!r}, children={len(self.children)})"


def parse_toc(lines: list[str]) -> list[TOCNode]:
    """Extract the category tree from the TOC section of the README."""
    toc_lines: list[str] = []
    in_toc = False

    for line in lines:
        if line.strip().startswith("## Contents"):
            in_toc = True
            continue
        if in_toc:
            if HEADING_RE.match(line):
                break
            toc_lines.append(line)

    entries: list[tuple[int, str, str]] = []  # (indent_level, title, slug)
    for line in toc_lines:
        m = TOC_ENTRY_RE.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        title = m.group(2)
        slug = m.group(3)
        if slug in SKIP_SLUGS:
            continue
        if slug in STOP_SLUGS:
            break
        entries.append((indent, title, slug))

    if not entries:
        print("ERROR: could not find any TOC entries", file=sys.stderr)
        sys.exit(1)

    # Determine indent levels: the smallest indent is the root level
    indent_levels = sorted(set(e[0] for e in entries))
    indent_to_depth = {indent: depth for depth, indent in enumerate(indent_levels)}

    roots: list[TOCNode] = []
    stack: list[TOCNode] = []

    for indent, title, slug in entries:
        depth = indent_to_depth[indent]
        node = TOCNode(title, slug)

        # Pop stack to find the parent at depth - 1
        while len(stack) > depth:
            stack.pop()

        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)

        stack.append(node)

    return roots


# ---------------------------------------------------------------------------
# Phase 2: Build a heading index from the body
# ---------------------------------------------------------------------------

def build_heading_index(lines: list[str]) -> dict[str, int]:
    """Map slug -> line number for every ## / ### / #### heading."""
    index: dict[str, int] = {}
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m:
            continue
        title = m.group(2).strip()
        slug = slugify(title)
        index[slug] = i
    return index


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    return text


# ---------------------------------------------------------------------------
# Phase 3: Extract content for each TOC node
# ---------------------------------------------------------------------------

def extract_section(
    lines: list[str], start: int, has_children: bool = False,
) -> tuple[str, list[dict]]:
    """From a heading at `start`, extract the description and links that belong
    directly to this section (not to any sub-headings)."""
    heading_match = HEADING_RE.match(lines[start])
    if not heading_match:
        return "", []

    heading_level = len(heading_match.group(1))
    description = ""
    links: list[dict] = []

    i = start + 1
    n = len(lines)

    while i < n:
        line = lines[i]

        hm = HEADING_RE.match(line)
        if hm:
            child_level = len(hm.group(1))
            # Stop at same or higher level heading
            if child_level <= heading_level:
                break
            # Stop at sub-headings if this node has TOC children —
            # those sections will be parsed separately
            if has_children:
                break
            # Otherwise skip inline sub-headings not in the TOC
            i += 1
            continue

        stripped = line.strip()

        # Skip blank lines and "back to top"
        if not stripped or "back to top" in stripped:
            i += 1
            continue

        # Try to parse a link line
        link_match = LINK_RE.match(stripped)
        if link_match:
            links.append({
                "title": strip_markdown(link_match.group(1)),
                "description": strip_markdown(link_match.group(3)),
                "url": link_match.group(2).strip(),
            })
            i += 1
            continue

        link_match = LINK_NO_DESC_RE.match(stripped)
        if link_match:
            links.append({
                "title": strip_markdown(link_match.group(1)),
                "description": "",
                "url": link_match.group(2).strip(),
            })
            i += 1
            continue

        # Description: first non-link text after the heading
        if not links and not description:
            italic = ITALIC_RE.match(stripped)
            if italic:
                description = strip_markdown(italic.group(1))
            else:
                desc_parts = []
                while i < n:
                    cur = lines[i].strip()
                    if (not cur or LINK_RE.match(cur) or LINK_NO_DESC_RE.match(cur)
                            or HEADING_RE.match(cur) or "back to top" in cur):
                        break
                    desc_parts.append(cur)
                    i += 1
                description = strip_markdown(" ".join(desc_parts))
                continue

        i += 1

    return description, links


def toc_to_yaml(
    node: TOCNode,
    lines: list[str],
    heading_index: dict[str, int],
) -> dict | None:
    """Recursively convert a TOCNode into a YAML-ready dict."""
    category: dict = {"title": node.title}

    line_num = heading_index.get(node.slug)
    if line_num is not None:
        desc, links = extract_section(lines, line_num, has_children=bool(node.children))
        if desc:
            category["description"] = desc
        if links:
            category["links"] = links

    if node.children:
        subcats = []
        for child in node.children:
            sub = toc_to_yaml(child, lines, heading_index)
            if sub:
                subcats.append(sub)
        if subcats:
            category["subcategories"] = subcats

    return category


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-i", "--input", help="Parse a local README.md instead of fetching"
    )
    parser.add_argument(
        "-o", "--output", default="awesome.yaml",
        help="Output YAML file (default: awesome.yaml)",
    )
    args = parser.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            text = f.read()
    else:
        print("Fetching README.md from GitHub...")
        text = fetch_readme(README_URL)

    lines = text.splitlines()

    # Phase 1: parse TOC
    toc_roots = parse_toc(lines)
    print(f"TOC: {len(toc_roots)} top-level categories", file=sys.stderr)

    # Phase 2: heading index
    heading_index = build_heading_index(lines)

    # Phase 3: extract content
    categories = []
    unmatched = []
    for root in toc_roots:
        cat = toc_to_yaml(root, lines, heading_index)
        if cat:
            categories.append(cat)
        if root.slug not in heading_index:
            unmatched.append(root.slug)

    if unmatched:
        print(f"WARNING: {len(unmatched)} TOC entries had no matching heading: "
              f"{unmatched}", file=sys.stderr)

    # Write output
    with open(args.output, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            categories, f, sort_keys=False, allow_unicode=True, width=120
        )

    link_count = count_links(categories)
    print(f"Wrote {len(categories)} categories ({link_count} links) to {args.output}")


def count_links(categories: list[dict]) -> int:
    total = 0
    for c in categories:
        total += len(c.get("links", []))
        total += count_links(c.get("subcategories", []))
    return total


if __name__ == "__main__":
    main()

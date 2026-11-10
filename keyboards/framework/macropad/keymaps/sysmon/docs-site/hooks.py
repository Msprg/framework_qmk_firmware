# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""MkDocs hooks: the manual's markdown is written for GitHub, and reads the
same on the site with these few adjustments."""

import hashlib
import re

# "[Manual](README.md) › Chapter": the site has its own navigation.
BREADCRUMB = re.compile(r"^\[Manual\]\(README\.md\) › [^\n]*\n+", re.M)
# The closing "---" and "Previous: ... · Next: ..." line: the site's footer
# (navigation.footer) links the neighbouring pages already.
FOOTER = re.compile(r"\n---\n+(?:Previous|Next): [^\n]*\n*\Z")
# A pipe inside code in a table cell is written \| for GitHub's tables, which
# hide the backslash; Python-Markdown keeps it.
CODE = re.compile(r"<code>([^<]*)</code>")
# A figure, as MkDocs writes its address relative to the page:
# "img/x.svg" on the home page, "../img/x.svg" on a chapter.
FIGURE = re.compile(r'(<img\b[^>]*?\bsrc=")((?:\.\./)*)(img/[^"?#]+)(")')


def on_page_markdown(markdown, page, config, files):
    markdown = BREADCRUMB.sub("", markdown, count=1)
    return FOOTER.sub("\n", markdown)


def on_page_content(html, page, config, files):
    html = CODE.sub(lambda m: "<code>" + m.group(1).replace("\\|", "|") + "</code>", html)
    return FIGURE.sub(lambda m: _versioned(m, files), html)


def _versioned(m, files):
    """Add ?v=<hash of the figure's content> to its address.

    GitHub Pages lets a browser keep any file for 10 minutes
    (Cache-Control: max-age=600), and a normal reload doesn't recheck the
    images, so a redrawn figure could show its old version for that long. A
    changed figure gets a new address and is fetched at once; an unchanged one
    keeps its address and stays cached."""
    f = files.get_file_from_path(m.group(3))
    if f is None:
        return m.group(0)
    with open(f.abs_src_path, "rb") as fh:
        v = hashlib.sha256(fh.read()).hexdigest()[:10]
    return f"{m.group(1)}{m.group(2)}{m.group(3)}?v={v}{m.group(4)}"

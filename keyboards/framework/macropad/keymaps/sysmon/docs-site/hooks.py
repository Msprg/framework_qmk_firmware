# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""MkDocs hooks: the manual's markdown is written for GitHub, and reads the
same on the site with these few adjustments."""

import re

# "[Manual](README.md) › Chapter": the site has its own navigation.
BREADCRUMB = re.compile(r"^\[Manual\]\(README\.md\) › [^\n]*\n+", re.M)
# The closing "---" and "Previous: ... · Next: ..." line: the site's footer
# (navigation.footer) links the neighbouring pages already.
FOOTER = re.compile(r"\n---\n+(?:Previous|Next): [^\n]*\n*\Z")
# A pipe inside code in a table cell is written \| for GitHub's tables, which
# hide the backslash; Python-Markdown keeps it.
CODE = re.compile(r"<code>([^<]*)</code>")


def on_page_markdown(markdown, page, config, files):
    markdown = BREADCRUMB.sub("", markdown, count=1)
    return FOOTER.sub("\n", markdown)


def on_page_content(html, page, config, files):
    return CODE.sub(lambda m: "<code>" + m.group(1).replace("\\|", "|") + "</code>", html)

#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check the user manual against itself and the sources.

    python check_manual.py

  * every relative and root-relative link, image and #anchor resolves
    (anchors slugged the way GitHub does it);
  * every sysmon.ini setting named in backticks or in an ini block exists in
    host/config.py, every --flag in build_parser() or limitreport.py, every
    [cli] option in build_parser();
  * every Start-menu item named in bold exists in installer.iss;
  * no absolute path into a user profile slipped in.

Prints what it finds and exits 1 if anything. Standard library only. Some
hits are expected: a flag of another program (framework_tool --thermal) or a
word in sample output; read them before "fixing".
"""

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MANUAL = os.path.dirname(HERE)
SYSMON = os.path.dirname(MANUAL)
HOST = os.path.join(SYSMON, "host")
REPO = os.path.abspath(os.path.join(SYSMON, *[os.pardir] * 5))

FILES = sorted(os.path.join(MANUAL, f) for f in os.listdir(MANUAL) if f.endswith(".md"))
FILES.append(os.path.join(REPO, ".github", "README.md"))

# other programs' flags and words of sample output the checks would trip on
KNOWN_OTHER = {"--thermal", "bright"}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def without_code(text):
    return re.sub(r"```.*?```", "", text, flags=re.S)


def slug(heading):
    h = heading.strip().lower().replace("`", "")
    h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", h)
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def anchors(path):
    out, seen = set(), {}
    for line in without_code(read(path)).splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            s = slug(m.group(1))
            n = seen.get(s, 0)
            out.add(s if n == 0 else f"{s}-{n}")
            seen[s] = n + 1
    return out


def sources():
    names = set(re.findall(r'Setting\("([A-Z_0-9]+)"', read(os.path.join(HOST, "config.py"))))
    tree = ast.parse(read(os.path.join(HOST, "fw16_sysmon.py")))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_parser")
    flags = {"--help"}
    for call in ast.walk(fn):
        if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "add_argument":
            flags |= {a.value for a in call.args
                      if isinstance(a, ast.Constant) and str(a.value).startswith("--")}
    flags |= set(re.findall(r'add_argument\("(--[a-z-]+)"', read(os.path.join(HOST, "limitreport.py"))))
    iss = read(os.path.join(HOST, "packaging", "installer.iss"))
    menu = {m.replace("{#AppName}", "FW16 Macropad Sysmon")
            for m in re.findall(r'Name: "\{group\}\\([^"]+)"', iss)}
    return names, flags, menu


def main():
    names, flags, menu = sources()
    cli = {f[2:] for f in flags}
    menu_heads = {m.split(" (")[0] for m in menu}
    problems = []
    for path in FILES:
        text, base = read(path), os.path.relpath(path, REPO)
        for target in re.findall(r"!?\[[^\]]*\]\(([^)\s]+)\)", without_code(text)):
            if re.match(r"[a-z]+://", target):
                continue
            ref, _, frag = target.partition("#")
            if ref.startswith("/"):
                dest = os.path.join(REPO, ref.lstrip("/"))
            elif ref:
                dest = os.path.normpath(os.path.join(os.path.dirname(path), ref))
            else:
                dest = path
            if not os.path.exists(dest):
                problems.append(f"{base}: link to a missing file: {target}")
            elif frag and dest.endswith(".md") and frag not in anchors(dest):
                problems.append(f"{base}: no #{frag} in {os.path.relpath(dest, REPO)}")
        for n in set(re.findall(r"`([A-Z][A-Z0-9]*_[A-Z0-9_]+)", text)) - names:
            problems.append(f"{base}: unknown setting {n}")
        for n in set(re.findall(r"^;?\s*([A-Z][A-Z0-9_]+)\s*=", text, flags=re.M)) - names:
            problems.append(f"{base}: unknown setting in an ini block: {n}")
        for f in set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", text)) - flags - KNOWN_OTHER:
            problems.append(f"{base}: unknown flag {f}")
        for o in set(re.findall(r"^([a-z][a-z-]+)\s*=", text, flags=re.M)) - cli - KNOWN_OTHER:
            problems.append(f"{base}: unknown [cli] option {o}")
        for item in re.findall(r"\*\*([^*]+)\*\*", text):
            item = " ".join(item.split())
            looks_like = item.split(" (")[0] in menu_heads or item.startswith(
                ("Self-test", "Live view", "Edit settings", "View log", "Flash firmware",
                 "Restart the", "Uninstall FW16"))
            if looks_like and item not in menu and item not in menu_heads:
                problems.append(f"{base}: not a Start-menu item: {item}")
        for m in re.findall(r"[A-Za-z]:[\\/]Users[\\/][^\\/\s`]+", text):
            problems.append(f"{base}: a path into a user profile: {m}")
    print("\n".join(problems) or "all checks passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

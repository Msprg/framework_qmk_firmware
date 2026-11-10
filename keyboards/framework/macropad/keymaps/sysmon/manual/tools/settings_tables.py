#!/usr/bin/env python3
# Copyright 2026 Msprg
# SPDX-License-Identifier: GPL-2.0-or-later
"""Regenerate the reference tables in ../06-settings.md.

    python settings_tables.py

Two blocks sit between BEGIN/END markers there:

  CLI-REFERENCE       the [cli] options: build_parser()'s add_argument calls
                      in host/fw16_sysmon.py, minus config._CLI_SKIP
  SETTINGS-REFERENCE  every sysmon.ini setting: config.SCHEMA (section,
                      range, comment) with the built-in default, the
                      top-level constant of the same name in fw16_sysmon.py

Both are read with `ast`, nothing is imported or run, so this works with any
Python 3.8+ and no host dependencies. Run it after adding or changing a
setting, then check the result with check_manual.py.
"""

import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
MANUAL = os.path.dirname(HERE)
HOST = os.path.join(os.path.dirname(MANUAL), "host")
TARGET = os.path.join(MANUAL, "06-settings.md")


def parse(name):
    with open(os.path.join(HOST, name), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return node.id if isinstance(node, ast.Name) else None


def module_constants(tree):
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            value = literal(node.value)
            if value is not None:
                out[node.targets[0].id] = value
    return out


def cell(text):
    return " ".join(str(text).split()).replace("|", "\\|")


def settings_reference(cfg, defaults):
    consts = module_constants(cfg)
    schema = next(n.value for n in cfg.body if isinstance(n, ast.Assign)
                  and getattr(n.targets[0], "id", "") == "SCHEMA")
    section_help = consts.get("SECTION_HELP", {})
    lines, current = [], None
    for call in schema.elts:
        args = [literal(a) for a in call.args]
        kw = {k.arg: literal(k.value) for k in call.keywords}
        name, section, kind = args[0], args[1], args[2]
        comment = kw.get("comment", args[3] if len(args) > 3 else "")
        lo, hi, choices = kw.get("lo"), kw.get("hi"), kw.get("choices")
        default = defaults.get(name, "?")
        if isinstance(default, tuple):
            default = ", ".join(default) or "none"
        if kind in ("INT", "FLOAT") and (lo is not None or hi is not None):
            rng = f"{lo}–{hi}"
        elif kind == "CSV":
            rng = "list of: " + ", ".join(choices)
        elif kind == "TEXT":
            rng = f"text, ≤{hi} chars"
        else:
            rng = ""
        if section != current:
            current = section
            lines += ["", f"### [{section}]", "",
                      cell(section_help.get(section, "")), "",
                      "| Setting | Default | Range | What it does |",
                      "|---|---|---|---|"]
        lines.append(f"| `{name}` | `{default}` | {rng} | {cell(comment)} |")
    return "\n".join(lines).strip()


def cli_reference(cfg, eng):
    skip = module_constants(cfg).get("_CLI_SKIP", set())
    fn = next(n for n in eng.body
              if isinstance(n, ast.FunctionDef) and n.name == "build_parser")
    rows = []
    for call in ast.walk(fn):
        if not (isinstance(call, ast.Call)
                and getattr(call.func, "attr", "") == "add_argument"):
            continue
        long = next((a.value for a in call.args if isinstance(a, ast.Constant)
                     and str(a.value).startswith("--")), None)
        if not long or long[2:].replace("-", "_") in skip:
            continue
        kw = {k.arg: k.value for k in call.keywords}
        action = literal(kw["action"]) if "action" in kw else None
        if "default" in kw:
            default = literal(kw["default"])
        else:
            default = False if action == "store_true" else None
        helptext = literal(kw["help"]) if "help" in kw else ""
        helptext = (helptext or "").replace("%%", "%")
        if default is None:
            shown = "*(unset)*"
        elif isinstance(default, bool):
            shown = "`true`" if default else "`false`"
        else:
            shown = f"`{default}`".replace("|", "\\|")
        rows.append(f"| `{long[2:]}` | {shown} | {cell(helptext)} |")
    return "| Option | Default | What it does |\n|---|---|---|\n" + "\n".join(rows)


def put(text, marker, body):
    block = f"<!-- BEGIN {marker} (generated) -->\n{body}\n<!-- END {marker} -->"
    pattern = re.compile(
        rf"<!-- BEGIN {marker} \(generated\) -->.*?<!-- END {marker} -->", re.S)
    if pattern.search(text):
        return pattern.sub(lambda m: block, text)
    if f"<!-- {marker} -->" not in text:
        raise SystemExit(f"no {marker} marker in {TARGET}")
    return text.replace(f"<!-- {marker} -->", block)


def main():
    cfg, eng = parse("config.py"), parse("fw16_sysmon.py")
    with open(TARGET, encoding="utf-8") as fh:
        text = fh.read()
    text = put(text, "CLI-REFERENCE", cli_reference(cfg, eng))
    text = put(text, "SETTINGS-REFERENCE",
               settings_reference(cfg, module_constants(eng)))
    with open(TARGET, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"updated {TARGET}")


if __name__ == "__main__":
    main()

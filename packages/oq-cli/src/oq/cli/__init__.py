"""Querly command-line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from oq.core.parser import check, parse
from oq.core.semantic import analyze
from oq.core.semantic import compile as compile_analyzed
from oq.core.source import SourceFile

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print("Usage: oq check <file> [--strict] [--json]")
        print("       oq compile <file> --dialect sqlite|postgres|mysql [--json]")
        print("       oq version")
        return 0
    command = args[0]
    if command == "version":
        from oq.core.version import version_string

        print(version_string())
        return 0
    if command not in ("check", "compile"):
        print(f"oq: unknown command: {command}", file=sys.stderr)
        return 2
    if len(args) < 2:
        print(f"oq {command}: missing source file", file=sys.stderr)
        return 2

    path = Path(args[1])
    strict = "--strict" in args
    json_output = "--json" in args
    dialect = "sqlite"
    if "--dialect" in args:
        index = args.index("--dialect")
        if index + 1 >= len(args):
            print("oq compile: --dialect requires a value", file=sys.stderr)
            return 2
        dialect = args[index + 1]

    try:
        source = SourceFile.from_path(path)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"oq: {exc}", file=sys.stderr)
        return 2

    if command == "check":
        bag = check(source)
        if strict and bag.warnings:
            return 1
        if json_output:
            print(
                json.dumps(
                    [
                        {
                            "code": d.code,
                            "severity": d.severity.value,
                            "message": d.message,
                            "notes": list(d.notes),
                        }
                        for d in bag.sorted_diagnostics()
                    ],
                    sort_keys=True,
                )
            )
        else:
            for diagnostic in bag.sorted_diagnostics():
                print(diagnostic.render(source.text))
        return 1 if bag.has_errors else 0

    ast, bag = parse(source)
    if bag.has_errors:
        for diagnostic in bag.sorted_diagnostics():
            print(diagnostic.render(source.text))
        return 1
    analyzed = analyze(ast, source)
    sql, compile_bag = compile_analyzed(analyzed, dialect)
    bag.extend(compile_bag)
    bag.extend(analyzed.diagnostics)
    if bag.has_errors:
        for diagnostic in bag.sorted_diagnostics():
            print(diagnostic.render(source.text))
        return 1
    if json_output:
        print(json.dumps({"dialect": dialect, "sql": sql}, sort_keys=True))
    else:
        print(sql)
    return 0

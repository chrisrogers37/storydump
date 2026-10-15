"""Reject retired migration references in source comments and documentation.

Run `python scripts/check_schema_references.py`. Runtime literals and numeric
quantities are outside this check: references name schema symbols or files,
while the empty base and the two supported migration files keep their numbers.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.sql_lexer import COMMENTS, DOLLAR, segments

ALLOWED = frozenset({"000", "001", "002"})
SOURCE_SUFFIXES = frozenset({".py", ".sql", ".ts", ".tsx", ".js", ".jsx"})
DOCUMENT_SUFFIXES = frozenset({".md", ".rst"})
# Higher numbers can also be counts, HTTP statuses, limits or example IDs.
# Implicit references use the retired inventory and reference-shaped grammar.
RETIRED = r"(?:0\d{2}|10[0-8]|110|113|115|116|129|13[3-9]|14\d)"
PATTERNS = (
    re.compile(r"(?<![\w.])(?<!\d,)(?P<number>0\d{2})(?![\w.])"),
    re.compile(r"\bmigrations?\s+[`\"']*(?P<number>\d{3})\b", re.I),
    re.compile(r"\b(?P<number>\d{3})_[\w-]+\.sql\b"),
    re.compile(rf"(?<!\w)[`]*(?P<number>{RETIRED})[`]*['’]s\b"),
    re.compile(rf"(?<!\w)\((?P<number>{RETIRED})(?:\)|\s+`[A-Za-z_]|,\s*#\d+)"),
    re.compile(rf"\b(?:since|before|under)\s+`?(?P<number>{RETIRED})`?(?![\w\d])"),
    re.compile(
        rf"^\s*(?:\#+[:\s-]*|/\*\*?\s*|\*\s*|[\"']{{3}})?(?P<number>{RETIRED}):", re.M
    ),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    number: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: retired migration reference {self.number}"


def _python_prose(source: str):
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def offset(line, column):
        return offsets[line - 1] + column

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            yield offset(*token.start), token.string
    for node in ast.walk(ast.parse(source)):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        value = node.body[0].value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        # AST columns count UTF-8 bytes; tokenizer columns count characters.
        start_column = len(
            lines[value.lineno - 1].encode()[: value.col_offset].decode()
        )
        end_column = len(
            lines[value.end_lineno - 1].encode()[: value.end_col_offset].decode()
        )
        start = offset(value.lineno, start_column)
        end = offset(value.end_lineno, end_column)
        yield start, source[start:end]


def _sql_prose(source: str, offset: int = 0):
    for segment in segments(source):
        if segment.kind in COMMENTS:
            yield offset, segment.text
        elif segment.kind == DOLLAR:
            # Function bodies are SQL/PLpgSQL source inside a SQL literal.
            tag = re.match(r"\$[^$]*\$", segment.text).group()
            if segment.closed:
                yield from _sql_prose(
                    segment.text[len(tag) : -len(tag)], offset + len(tag)
                )
        offset += len(segment.text)


def _javascript_prose(source: str):
    """Read comments around strings, regexes and template interpolations."""
    i = 0
    # A template expression resumes template text when its brace depth is zero.
    templates = []
    template_text = False
    previous = ""
    while i < len(source):
        if template_text:
            if source[i] == "\\":
                i += 2
            elif source[i] == "`":
                template_text = False
                i += 1
                previous = "value"
            elif source.startswith("${", i):
                templates.append(1)
                template_text = False
                i += 2
                previous = ""
            else:
                i += 1
        elif source[i] == "`":
            template_text = True
            i += 1
        elif source[i] in "'\"":
            quote = source[i]
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            previous = "value"
        elif source.startswith("//", i):
            end = source.find("\n", i)
            end = len(source) if end < 0 else end
            yield i, source[i:end]
            i = end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = len(source) if end < 0 else end + 2
            yield i, source[i:end]
            i = end
        elif source[i] == "/" and (
            previous in {"", "return", "throw", "case", "yield"}
            or previous in {"=", "(", "[", "{", ",", ":", ";", "!", "?", "&", "|"}
        ):
            # Regex delimiters inside a character class or after an escape
            # are data, including the slash in an escaped URL pattern.
            i += 1
            character_class = False
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == "[":
                    character_class = True
                elif source[i] == "]":
                    character_class = False
                elif source[i] == "/" and not character_class:
                    i += 1
                    break
                elif source[i] in "\r\n":
                    break
                i += 1
            previous = "value"
        elif source[i].isalpha() or source[i] in "_$":
            end = i + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                end += 1
            previous = source[i:end]
            i = end
        else:
            if templates:
                if source[i] == "{":
                    templates[-1] += 1
                elif source[i] == "}":
                    templates[-1] -= 1
                    if not templates[-1]:
                        templates.pop()
                        template_text = True
            if not source[i].isspace():
                previous = source[i]
            i += 1


def prose(path: Path, source: str):
    if path.suffix == ".py":
        yield from _python_prose(source)
    elif path.suffix == ".sql":
        yield from _sql_prose(source)
    elif path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
        yield from _javascript_prose(source)
    elif path.suffix in DOCUMENT_SUFFIXES:
        yield 0, source
    elif path.name in {"Makefile", ".env.example"} or path.suffix in {
        ".yml",
        ".yaml",
        ".toml",
        ".sh",
    }:
        for match in re.finditer(r"^\s*#.*$", source, re.M):
            yield match.start(), match.group()


def check_source(path: Path, source: str) -> list[Finding]:
    findings = set()
    for start, text in prose(path, source):
        for pattern in PATTERNS:
            for match in pattern.finditer(text):
                number = match.group("number")
                if number not in ALLOWED:
                    position = start + match.start("number")
                    findings.add(
                        Finding(path, source.count("\n", 0, position) + 1, number)
                    )
    return sorted(findings, key=lambda item: (str(item.path), item.line, item.number))


def check_repository(root: Path) -> list[Finding]:
    # Git's inventory omits virtualenvs, build output and installed packages.
    tracked = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
        )
        .decode()
        .split("\0")
    )
    findings = []
    for name in sorted(set(tracked) - {""}):
        path = Path(name)
        if path.suffix not in SOURCE_SUFFIXES | DOCUMENT_SUFFIXES | {
            ".yml",
            ".yaml",
            ".toml",
            ".sh",
        } and path.name not in {"Makefile", ".env.example"}:
            continue
        findings.extend(check_source(path, (root / path).read_text(encoding="utf-8")))
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = check_repository(root)
    for finding in findings:
        print(finding)
    if findings:
        print(
            f"{len(findings)} retired migration references; name the schema object or DDL file"
        )
        return 1
    print(
        "Schema references: no retired migration numbers in comments or documentation"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

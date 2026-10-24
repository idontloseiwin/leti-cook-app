"""Check repository-local documentation links and anchors without network access."""
from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.ci_policy import DOC_FILES, ROOT


def prose(text: str) -> str:
    lines = []
    fence = None
    for line in text.splitlines():
        match = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if match:
            mark = match[1]
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence):
                fence = None
            lines.append('')
        else:
            lines.append('' if fence else line)
    return re.sub(r'<!--[\s\S]*?-->', '', '\n'.join(lines))


class HtmlLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.anchors = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if not value:
                continue
            if name == 'id' or (tag == 'a' and name == 'name'):
                self.anchors.add(value)
            if name in ('href', 'src'):
                self.links.append(value)


def destination(text: str) -> str:
    text = text.lstrip()
    if text.startswith('<'):
        end = text.find('>')
        return text[1:end] if end >= 0 else ''
    result, depth, escaped = [], 0, False
    for char in text:
        if escaped:
            result.append(char)
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '(':
            result.append(char)
            depth += 1
        elif char == ')':
            if depth == 0:
                break
            result.append(char)
            depth -= 1
        elif char.isspace() and depth == 0:
            break
        else:
            result.append(char)
    return ''.join(result)


def links(text: str) -> list[str]:
    text = re.sub(r'(`+).*?\1', '', prose(text))
    parser = HtmlLinks()
    parser.feed(text)
    result = parser.links
    for match in re.finditer(r'\]\(', text):
        result.append(destination(text[match.end():]))
    definitions = {}
    for match in re.finditer(r'^\s{0,3}\[([^\]]+)\]:\s*(.*)$', text, re.M):
        definitions[' '.join(match[1].lower().split())] = destination(match[2])
    result.extend(definitions.values())
    for match in re.finditer(r'\[([^\]\n]+)\]\[([^\]\n]*)\]', text):
        key = ' '.join((match[2] or match[1]).lower().split())
        if key not in definitions:
            raise ValueError(f'Undefined link reference: {key}')
    return result


def anchors(text: str) -> set[str]:
    text = prose(text)
    parser = HtmlLinks()
    parser.feed(text)
    result = parser.anchors
    generated = set()
    lines = text.splitlines()
    headings = []
    for index, line in enumerate(lines):
        match = re.match(r'^\s{0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$', line)
        if match:
            headings.append(match[1])
        elif index and re.fullmatch(r'\s{0,3}(?:=+|-+)\s*', line) and lines[index - 1].strip():
            headings.append(lines[index - 1])
    for heading in headings:
        heading = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', heading)
        heading = html.unescape(re.sub(r'<[^>]*>', '', heading)).lower()
        heading = re.sub(r'[^\w\- ]', '', heading).replace(' ', '-')
        slug, suffix = heading, 0
        while slug in generated:
            suffix += 1
            slug = f'{heading}-{suffix}'
        generated.add(slug)
        result.add(slug)
    return result


def check(root: Path = ROOT) -> tuple[list[str], int]:
    root = root.resolve()
    errors, count, cached = [], 0, {}
    # Checking all allowlisted prose catches incoming links when a page/heading
    # is deleted. Generated SQL documentation is a target, not hand-edited input.
    for name in sorted(DOC_FILES):
        if not name.endswith('.md') or not (root / name).exists():
            continue
        source = root / name
        try:
            targets = links(source.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError) as error:
            errors.append(f'{name}: {error}')
            continue
        for target in targets:
            target = html.unescape(target)
            url = urlsplit(target)
            if url.scheme in ('https', 'http', 'mailto') or url.netloc:
                continue
            if url.scheme or not target:
                errors.append(f'{name}: unsupported or empty link {target!r}')
                continue
            count += 1
            path = unquote(url.path)
            local = ((root / path.lstrip('/')) if path.startswith('/') else
                     (source.parent / path) if path else source).resolve()
            if not local.is_relative_to(root) or not local.exists():
                errors.append(f'{name}: missing or out-of-repository target {target!r}')
                continue
            fragment = unquote(url.fragment)
            if fragment and local.suffix.lower() == '.md':
                if local not in cached:
                    cached[local] = anchors(local.read_text(encoding='utf-8'))
                if fragment not in cached[local]:
                    errors.append(f'{name}: missing anchor {target!r}')
    return errors, count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    errors, count = check(parser.parse_args().root)
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Checked {count} repository-local documentation links and anchors.')


if __name__ == '__main__':
    main()

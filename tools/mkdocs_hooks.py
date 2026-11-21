"""MkDocs hook: make links that are right on GitHub right on the documentation site.

The Markdown under ``docs/`` is also read on GitHub, where a relative link resolves from
the file's own directory and may point anywhere in the repository. The site is built from
``docs/`` alone, so a link that leaves it would fail ``mkdocs build --strict``. This hook
rewrites, in the page text only (the files are untouched):

* a link to a repository file that a stub page under ``docs/`` includes (a README, a
  changelog, ``CONTRIBUTING.md``) becomes a link to that page;
* a link to a file inside ``docs/`` that is written by way of the repository root
  (``../docs/page.md``) becomes the plain relative path;
* a link to any other file or directory in the repository becomes a link to it on
  GitHub, built from ``repo_url``.

A link to a file that does not exist, or that leaves the repository, is left alone, so
strict mode still reports it. So are image links, and anything inside a code block or an
inline code span.

A stub is a page whose text is, apart from one optional HTML comment, one
``include-markdown`` directive.

The module imports nothing from mkdocs when mkdocs is absent, so the tests run without it.
"""

import os
import posixpath
import re
from pathlib import Path

try:
    from mkdocs.plugins import event_priority
except ImportError:  # the unit tests run without mkdocs installed

    def event_priority(priority):
        return lambda function: function


# The branch that links to repository files point at.
SOURCE_REF = "main"

_STUB = re.compile(
    r'\A\s*(?:<!--(?:(?!-->).)*-->\s*)?\{%\s*include-markdown\s+"([^"]+)"',
    re.DOTALL,
)
_LINK = re.compile(r'(\]\()([^)\s]+)((?:\s+"[^"]*")?\))')
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# Text that is never rewritten: fenced code, inline code, images.
_PROTECTED = re.compile(
    r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?(?:^[ ]{0,3}(?P=fence)[`~]*[ \t]*$|\Z)"
    r"|(?<!`)(?P<tick>`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)(?P=tick)(?!`)"
    r"|!\[[^\]\n]*\]\([^)\n]*\)",
    re.DOTALL | re.MULTILINE,
)
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")

_pages = {}  # resolved repository file -> src_uri of the stub page that includes it


def on_config(config):
    docs_dir = Path(config["docs_dir"]).resolve()
    _pages.clear()
    for stub in sorted(docs_dir.rglob("*.md")):
        found = _STUB.match(stub.read_text(encoding="utf-8"))
        if not found:
            continue
        target = (stub.parent / found.group(1)).resolve()
        src_uri = stub.relative_to(docs_dir).as_posix()
        if target in _pages:
            raise ValueError(
                f"{_pages[target]} and {src_uri} include the same file, {target}; "
                "links to it cannot be mapped to one page"
            )
        _pages[target] = src_uri
    return config


@event_priority(-100)  # after the include-markdown plugin has expanded the stubs
def on_page_markdown(markdown, page, config, files):
    docs_dir = Path(config["docs_dir"]).resolve()
    repo_root = docs_dir.parent
    page_dir = Path(page.file.abs_src_path).resolve().parent
    page_uri_dir = posixpath.dirname(page.file.src_uri) or "."
    base = str(config["repo_url"]).rstrip("/")

    def rewrite(match):
        before, link, tail = match.groups()
        path, hash_, fragment = link.partition("#")
        if not path or path.startswith("/") or _SCHEME.match(path):
            return match.group(0)
        target = (page_dir / path).resolve()
        if target == docs_dir or docs_dir in target.parents:
            if target.exists():
                relative = Path(os.path.relpath(target, page_dir)).as_posix()
                if posixpath.normpath(path) != relative:
                    return f"{before}{relative}{hash_}{fragment}{tail}"
            return match.group(0)
        if target in _pages:
            relative = posixpath.relpath(_pages[target], page_uri_dir)
            return f"{before}{relative}{hash_}{fragment}{tail}"
        if repo_root in target.parents and target.exists():
            kind = "tree" if target.is_dir() else "blob"
            repo_path = target.relative_to(repo_root).as_posix()
            return f"{before}{base}/{kind}/{SOURCE_REF}/{repo_path}{hash_}{fragment}{tail}"
        return match.group(0)

    # Hide what must not be rewritten, rewrite the links, put the hidden text back.
    hidden = []

    def hide(match):
        hidden.append(match.group(0))
        return f"\x00{len(hidden) - 1}\x00"

    text = _LINK.sub(rewrite, _PROTECTED.sub(hide, markdown))
    return _PLACEHOLDER.sub(lambda m: hidden[int(m.group(1))], text)

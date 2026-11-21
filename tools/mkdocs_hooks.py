"""MkDocs hook: make links that are right on GitHub right on the documentation site.

The Markdown under ``docs/`` is also read on GitHub, where a relative link resolves from
the file's own directory and may point anywhere in the repository. The site is built from
``docs/`` alone, so a link that leaves it would fail ``mkdocs build --strict``. This hook
rewrites, in the page text only (the files are untouched):

* a link to a repository file that a stub page under ``docs/`` includes (a README, a
  changelog, ``CONTRIBUTING.md``) becomes a link to that page;
* a link to any other file or directory in the repository becomes a link to it on
  GitHub, built from ``repo_url``.

A link to a file that does not exist is left alone, so strict mode still reports it.
"""

import posixpath
import re
from pathlib import Path

from mkdocs.plugins import event_priority

# The branch that the repository links point at.
SOURCE_REF = "main"

_INCLUDE = re.compile(r'include-markdown\s+"([^"]+)"')
_LINK = re.compile(r"(\]\()([^)\s]+)(\))")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

_pages = {}  # resolved repository file -> src_uri of the stub page that includes it


def on_config(config):
    docs_dir = Path(config["docs_dir"]).resolve()
    _pages.clear()
    for stub in docs_dir.rglob("*.md"):
        for target in _INCLUDE.findall(stub.read_text(encoding="utf-8")):
            _pages[(stub.parent / target).resolve()] = stub.relative_to(docs_dir).as_posix()
    return config


@event_priority(-100)  # after the include-markdown plugin has expanded the stubs
def on_page_markdown(markdown, page, config, files):
    docs_dir = Path(config["docs_dir"]).resolve()
    repo_root = docs_dir.parent
    page_dir = Path(page.file.abs_src_path).parent
    page_uri_dir = posixpath.dirname(page.file.src_uri)

    def rewrite(match):
        before, link, after = match.groups()
        path, hash_, fragment = link.partition("#")
        if not path or path.startswith("/") or _SCHEME.match(path):
            return match.group(0)
        target = (page_dir / path).resolve()
        if target == docs_dir or docs_dir in target.parents:
            return match.group(0)
        if target in _pages:
            relative = posixpath.relpath(_pages[target], page_uri_dir or ".")
            return f"{before}{relative}{hash_}{fragment}{after}"
        if repo_root in target.parents and target.exists():
            kind = "tree" if target.is_dir() else "blob"
            repo_path = target.relative_to(repo_root).as_posix()
            base = config["repo_url"].rstrip("/")
            return f"{before}{base}/{kind}/{SOURCE_REF}/{repo_path}{hash_}{fragment}{after}"
        return match.group(0)

    return _LINK.sub(rewrite, markdown)

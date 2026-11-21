"""Unit tests for tools/mkdocs_hooks.py: link rewriting for the documentation site.

No mkdocs install is needed; the hook is driven with plain dicts and namespaces.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("mkdocs_hooks", REPO / "tools" / "mkdocs_hooks.py")
hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hooks)

REPO_URL = "https://example.test/owner/repo"
STUB = '<!-- the page for pkg/README.md -->\n\n{%\n  include-markdown "../pkg/README.md"\n%}\n'


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "docs" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "dir").mkdir(parents=True)
    (tmp_path / "docs" / "stub.md").write_text(STUB)
    (tmp_path / "docs" / "sub" / "page.md").write_text("# Page\n")
    (tmp_path / "docs" / "plain.md").write_text("# Plain\n")
    (tmp_path / "pkg" / "README.md").write_text("# Pkg\n")
    (tmp_path / "pkg" / "data.txt").write_text("data\n")
    return tmp_path


def _config(root, repo_url=REPO_URL):
    return {"docs_dir": str(root / "docs"), "repo_url": repo_url}


def _render(root, text, page="sub/page.md", repo_url=REPO_URL):
    config = _config(root, repo_url)
    hooks.on_config(config)
    file = SimpleNamespace(abs_src_path=str(root / "docs" / page), src_uri=page)
    return hooks.on_page_markdown(text, SimpleNamespace(file=file), config, None)


def _link(target):
    return f"[x]({target})"


def test_link_to_an_included_file_goes_to_the_stub_page_and_keeps_the_fragment(tree):
    out = _render(tree, _link("../../pkg/README.md#install"))
    assert out == _link("../stub.md#install")
    assert _render(tree, _link("../pkg/README.md"), page="stub.md") == _link("stub.md")


def test_other_files_and_directories_go_to_the_repository(tree):
    assert _render(tree, _link("../../pkg/data.txt#L3")) == _link(f"{REPO_URL}/blob/main/pkg/data.txt#L3")
    assert _render(tree, _link("../../pkg/dir")) == _link(f"{REPO_URL}/tree/main/pkg/dir")


def test_a_trailing_slash_on_repo_url_changes_nothing(tree):
    text = _link("../../pkg/data.txt") + _link("../../pkg/dir")
    assert _render(tree, text, repo_url=REPO_URL + "/") == _render(tree, text)


@pytest.mark.parametrize(
    "target",
    [
        "../../pkg/missing.txt",  # no such file: left for strict mode to report
        "../../../outside.txt",  # leaves the repository
        "mailto:someone@example.test",
        "https://example.test/pkg/data.txt",
        "#anchor",
        "/pkg/data.txt",
        "plain.md",  # already a docs link
    ],
)
def test_links_that_are_not_rewritten(tree, target):
    text = _link(target)
    assert _render(tree, text, page="sub/page.md") == text


def test_code_is_left_alone(tree):
    fenced = "```\n[x](../../pkg/data.txt)\n```\n"
    tilde = "~~~md\n[x](../../pkg/data.txt)\n~~~\n"
    unclosed = "```\n[x](../../pkg/data.txt)\n"
    inline = "see `[x](../../pkg/data.txt)` and ``[y](../../pkg/data.txt)``"
    for text in (fenced, tilde, unclosed, inline):
        assert _render(tree, text) == text
    mixed = "[a](../../pkg/data.txt)\n" + fenced + "[b](../../pkg/data.txt)\n"
    out = _render(tree, mixed)
    assert out.count(f"{REPO_URL}/blob/main/pkg/data.txt") == 2
    assert fenced in out


def test_images_are_left_alone_but_the_link_around_one_is_rewritten(tree):
    image = "![logo](../../pkg/data.txt)"
    assert _render(tree, image) == image
    badge = "[![ci](https://example.test/badge.svg)](../../pkg/README.md)"
    assert _render(tree, badge) == "[![ci](https://example.test/badge.svg)](../stub.md)"


def test_a_docs_file_reached_through_the_repository_root_gets_the_plain_path(tree):
    assert _render(tree, _link("../../docs/plain.md#top")) == _link("../plain.md#top")
    assert _render(tree, _link("../docs/plain.md"), page="stub.md") == _link("plain.md")
    assert _render(tree, _link("../docs/nope.md")) == _link("../docs/nope.md")


def test_a_title_after_the_target_is_kept(tree):
    assert _render(tree, '[x](../../pkg/data.txt "the data")') == f'[x]({REPO_URL}/blob/main/pkg/data.txt "the data")'


def test_a_stub_is_recognised_only_at_the_start_of_the_file(tree):
    (tree / "docs" / "sub" / "mentions.md").write_text('Text first.\n\n{% include-markdown "../../pkg/data.txt" %}\n')
    assert _render(tree, _link("../../pkg/data.txt")) == _link(f"{REPO_URL}/blob/main/pkg/data.txt")


def test_two_stubs_for_one_file_are_an_error(tree):
    (tree / "docs" / "second.md").write_text('{% include-markdown "../pkg/README.md" %}\n')
    with pytest.raises(Exception, match="include the same file"):
        hooks.on_config(_config(tree))


def test_a_stub_in_a_subdirectory_is_found_and_links_to_it_resolve_from_the_page(tree):
    (tree / "docs" / "sub" / "deep").mkdir()
    (tree / "pkg2").mkdir()
    (tree / "pkg2" / "README.md").write_text("# Pkg2\n")
    (tree / "docs" / "sub" / "deep" / "index.md").write_text('{% include-markdown "../../../pkg2/README.md" %}\n')
    # From the deep stub itself: a link to the file the top-level stub includes.
    out = _render(tree, _link("../../../pkg/README.md#x"), page="sub/deep/index.md")
    assert out == _link("../../stub.md#x")
    # From another page: a link to the file the deep stub includes.
    assert _render(tree, _link("../../pkg2/README.md")) == _link("deep/index.md")

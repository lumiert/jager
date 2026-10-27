from __future__ import annotations

import base64
from html import escape

import bleach
from markdown_it import MarkdownIt


_markdown = MarkdownIt("commonmark").enable(["table", "strikethrough"])
_allowed_tags = set(bleach.sanitizer.ALLOWED_TAGS).union(
    {
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "code", "blockquote",
        "hr", "br", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th",
        "td", "del", "input",
    }
)
_allowed_attributes = {
    "a": ["href", "title"],
    "code": ["class"],
    "pre": ["class"],
    "input": ["type", "checked", "disabled"],
}


def _render_fence(tokens, index, _options, _env) -> str:
    token = tokens[index]
    code = token.content
    encoded = base64.urlsafe_b64encode(code.encode("utf-8")).decode("ascii")
    language = token.info.strip().split(maxsplit=1)[0] if token.info.strip() else ""
    language_class = f' class="language-{escape(language, quote=True)}"' if language else ""
    return (
        f'<pre class="code-block"><code{language_class}>'
        f"{escape(code)}</code></pre>"
        f'<p class="copy-code"><a href="jager-copy:{encoded}">Copiar código</a></p>'
    )


_markdown.renderer.rules["fence"] = _render_fence


def render_markdown(text: str) -> str:
    rendered = _markdown.render(text)
    return bleach.clean(
        rendered,
        tags=_allowed_tags,
        attributes=_allowed_attributes,
        protocols={"http", "https", "mailto", "jager-copy"},
        strip=True,
    )

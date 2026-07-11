import unittest

from worklane.rendering import render_markdown


class RenderMarkdownTest(unittest.TestCase):
    def test_headers(self) -> None:
        html = render_markdown("# Title\n## Sub\n### Sub sub")
        self.assertIn("<h1>Title</h1>", html)
        self.assertIn("<h2>Sub</h2>", html)
        self.assertIn("<h3>Sub sub</h3>", html)

    def test_paragraph_and_inline(self) -> None:
        html = render_markdown("Hello **bold** and *italic* and `code`.")
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)
        self.assertIn("<code>code</code>", html)

    def test_link(self) -> None:
        html = render_markdown("See [PROTOCOL.md](PROTOCOL.md) for rules.")
        self.assertIn('<a href="PROTOCOL.md" target="_blank" rel="noopener">PROTOCOL.md</a>', html)

    def test_unordered_list(self) -> None:
        html = render_markdown("- one\n- two\n- three")
        self.assertIn("<ul><li>one</li><li>two</li><li>three</li></ul>", html)

    def test_ordered_list(self) -> None:
        html = render_markdown("1. first\n2. second")
        self.assertIn("<ol><li>first</li><li>second</li></ol>", html)

    def test_code_fence_not_interpreted_and_escaped(self) -> None:
        html = render_markdown("```\n<div>*not bold*</div>\n```")
        self.assertIn("<pre><code>&lt;div&gt;*not bold*&lt;/div&gt;</code></pre>", html)

    def test_table(self) -> None:
        html = render_markdown("| A | B |\n| --- | --- |\n| 1 | 2 |")
        self.assertIn("<th>A</th>", html)
        self.assertIn("<td>1</td>", html)

    def test_blockquote(self) -> None:
        html = render_markdown("> a quote")
        self.assertIn("<blockquote><p>a quote</p></blockquote>", html)

    def test_horizontal_rule(self) -> None:
        html = render_markdown("above\n\n---\n\nbelow")
        self.assertIn("<hr>", html)

    def test_html_is_escaped_in_paragraphs(self) -> None:
        html = render_markdown("<script>alert(1)</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_bold_wrapping_nested_italic(self) -> None:
        html = render_markdown("**Understanding what WL *is* right now:** read on.")
        self.assertNotIn("**", html)
        self.assertIn("<strong>Understanding what WL <em>is</em> right now:</strong>", html)

    def test_underscore_identifiers_not_mangled_by_italics(self) -> None:
        html = render_markdown("Use `wl_ready` and `in_progress` here.")
        self.assertIn("<code>wl_ready</code>", html)
        self.assertIn("<code>in_progress</code>", html)

    def test_bare_underscore_identifier_untouched_outside_code(self) -> None:
        html = render_markdown("See HOST_PROFILE_TEMPLATE.md and WORKLANE_DB for details.")
        self.assertIn("HOST_PROFILE_TEMPLATE.md", html)
        self.assertIn("WORKLANE_DB", html)
        self.assertNotIn("<em>", html)


if __name__ == "__main__":
    unittest.main()

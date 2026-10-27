import unittest

from app.markdown_view import render_markdown


class MarkdownViewTests(unittest.TestCase):
    def test_markdown_is_rendered_and_code_has_copy_action(self) -> None:
        rendered = render_markdown("# Título\n\n**forte** e `código`.\n\n```py\nprint('oi')\n```")

        self.assertIn("<h1>", rendered)
        self.assertIn("<strong>forte</strong>", rendered)
        self.assertIn("<code>código</code>", rendered)
        self.assertIn("Copiar código", rendered)
        self.assertIn("print(&#x27;oi&#x27;)", rendered)

    def test_raw_html_and_dangerous_links_are_not_executable(self) -> None:
        rendered = render_markdown(
            '<script>alert(1)</script>\n\n[x](javascript:alert(1))'
        )

        self.assertNotIn("<script>", rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertNotIn("<a", rendered)


if __name__ == "__main__":
    unittest.main()

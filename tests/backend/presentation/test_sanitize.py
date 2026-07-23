"""Тесты санитайзера описаний: чужой HTML работодателя -> innerHTML модалки (XSS)."""
from hrwork.presentation.views.feed import sanitize_desc


def test_strips_img_with_event_handler():
    out = sanitize_desc('<img src=x onerror=alert(1)>привет')
    assert "img" not in out and "onerror" not in out
    assert "привет" in out


def test_drops_script_with_content():
    assert sanitize_desc("до<script>alert(1)</script>после") == "допосле"


def test_keeps_allowed_structure():
    src = "<p>Мы ищем <strong>senior</strong>:</p><ul><li>Python</li></ul>"
    assert sanitize_desc(src) == src


def test_strips_attributes_from_allowed_tags():
    assert sanitize_desc('<p style="color:red" onclick="x()">a</p>') == "<p>a</p>"


def test_escaped_entities_stay_text():
    # &lt;img&gt; из исходника остаётся текстом, а не превращается в тег
    assert sanitize_desc("a &lt;img onerror=x&gt; b") == "a &lt;img onerror=x&gt; b"


def test_plain_text_passthrough_escaped():
    assert sanitize_desc("зарплата < 200к & офис") == "зарплата &lt; 200к &amp; офис"


def test_closes_unbalanced_tags():
    assert sanitize_desc("<p>a<strong>b").endswith("</strong></p>")

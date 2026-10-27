from app.utils.templates import render_post


def test_placeholder_replaced():
    text, _ = render_post("hi\n{{GARANT}}", [], "@Freelance_Garant_Bot")
    assert text == "hi\n@Freelance_Garant_Bot"


def test_append_when_placeholder_missing():
    text, _ = render_post("оффер без плейсхолдера", [], "@Freelance_Garant_Bot")
    assert text.endswith("@Freelance_Garant_Bot")
    assert "\n\n@Freelance_Garant_Bot" in text


def test_no_double_append():
    text, _ = render_post("уже есть @Freelance_Garant_Bot", [], "@Freelance_Garant_Bot")
    assert text.count("@Freelance_Garant_Bot") == 1


def test_plus_tag_not_appended():
    text, _ = render_post("пост", [], "+")
    assert text == "пост"


def test_empty_tag_strips_placeholder():
    text, _ = render_post("a\n{{TAG}}\nb", [], "")
    assert "{{" not in text

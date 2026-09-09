from app.utils.templates import render_post, utf16_len


def test_replace_garant():
    text = "Гарант: {{GARANT}}\nОффер"
    out, ents = render_post(text, [], "@bank")
    assert out == "Гарант: @bank\nОффер"
    assert ents == []


def test_skip_garant():
    text = "Гарант: {{GARANT}}\nОффер"
    out, _ = render_post(text, [], "")
    assert "{{" not in out
    assert "Оффер" in out


def test_tag_alias():
    out, _ = render_post("x {{TAG}} y", [], "@g")
    assert out == "x @g y"


def test_entity_shift_after_placeholder():
    text = "{{GARANT}} hello"
    # "hello" starts after placeholder
    hello_off = utf16_len("{{GARANT}} ")
    entities = [{"type": "bold", "offset": hello_off, "length": utf16_len("hello")}]
    out, ents = render_post(text, entities, "@ab")
    assert out == "@ab hello"
    assert ents[0]["offset"] == utf16_len("@ab ")
    assert ents[0]["length"] == utf16_len("hello")


def test_entity_before_placeholder_unchanged():
    text = "Hi {{GARANT}}"
    entities = [{"type": "bold", "offset": 0, "length": utf16_len("Hi")}]
    out, ents = render_post(text, entities, "@x")
    assert out == "Hi @x"
    assert ents[0]["offset"] == 0
    assert ents[0]["length"] == utf16_len("Hi")

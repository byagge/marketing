from types import SimpleNamespace

from app.utils.captcha import (
    button_verify_score,
    is_verify_button,
    looks_like_captcha,
    pick_numeric_option,
    pick_verify_button,
    solve_math_captcha,
    solve_poll_captcha,
)


def test_simple_plus():
    assert solve_math_captcha("1+1=") == "2"
    assert solve_math_captcha("2 + 3 = ?") == "5"


def test_multiply_and_minus():
    assert solve_math_captcha("4*5") == "20"
    assert solve_math_captcha("10-3=") == "7"
    assert solve_math_captcha("сколько будет 6×7") == "42"


def test_poll_style_question():
    q = "ᴀʀɪx, какой ответ в этом примере 6 + 8?"
    assert solve_math_captcha(q) == "14"


def test_not_captcha():
    assert solve_math_captcha("привет всем") is None
    assert solve_math_captcha("") is None
    assert not looks_like_captcha("обычный текст без чисел")


def test_verify_button_variants():
    assert is_verify_button("👉 Я не бот! 👈")
    assert is_verify_button("Я не бот")
    assert is_verify_button("I'm not a bot")
    assert is_verify_button("Подтвердить")
    assert button_verify_score("👉 Я не бот! 👈") > button_verify_score("Отмена")
    assert not is_verify_button("Отмена")


def test_pick_poll_option():
    options = ["10", "14", "18", "13"]
    assert pick_numeric_option(options, "14") == 1


def test_solve_poll_captcha():
    poll = SimpleNamespace(
        question="ᴀʀɪx, какой ответ в этом примере 6 + 8?",
        answers=[
            SimpleNamespace(text="10", option=b"0"),
            SimpleNamespace(text="14", option=b"1"),
            SimpleNamespace(text="18", option=b"2"),
            SimpleNamespace(text="13", option=b"3"),
        ],
    )
    assert solve_poll_captcha(poll) == (1, "14")


def test_pick_verify_button_not_bot():
    btn = SimpleNamespace(text="👉 Я не бот! 👈", data=b"x")
    row = SimpleNamespace(buttons=[btn])
    markup = SimpleNamespace(rows=[row])
    msg = SimpleNamespace(
        message="нажмите кнопку ниже в течение 30 Секунд",
        reply_markup=markup,
    )
    picked = pick_verify_button(msg)
    assert picked is not None
    assert picked[0] == 0 and picked[1] == 0
    assert "бот" in picked[2].lower() or "Бот" in picked[2]


def test_pick_numeric_buttons_from_math_text():
    buttons = [
        SimpleNamespace(text="10", data=b"a"),
        SimpleNamespace(text="14", data=b"b"),
        SimpleNamespace(text="18", data=b"c"),
        SimpleNamespace(text="13", data=b"d"),
    ]
    row = SimpleNamespace(buttons=buttons)
    markup = SimpleNamespace(rows=[row])
    msg = SimpleNamespace(
        message="какой ответ в этом примере 6 + 8?",
        reply_markup=markup,
    )
    picked = pick_verify_button(msg)
    assert picked is not None
    assert picked[2] == "14"

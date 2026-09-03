"""Правила текста объявления (app/text_rules.py) — чистые функции, без базы и бота."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from aiogram.types import MessageEntity

from app import text_rules as tr


def ok(text, entities=None, why=""):
    found = tr.find_violations(text, entities)
    assert not found, f"текст должен проходить ({why or text!r}), а нашли: {found}"


def bad(text, entities=None, why=""):
    found = tr.find_violations(text, entities)
    assert found, f"текст должен отклоняться ({why or text!r}), но нарушений не нашли"
    return found


def main():
    # --- ссылки в разных видах --------------------------------------------
    for text in [
        "Мой канал https://t.me/mychannel",
        "http://example.com — заходи",
        "Пиши www.mysite.ru",
        "t.me/somechannel",
        "telegram.me/somechannel",
        "Заходи на example.com сегодня",
        "Мой блог: myblog.online/about",
        "ставки тут bet.casino!",
        "ftp://files.example.net/pub",
    ]:
        bad(text, why="ссылка")
    print("ссылки OK: http/https, www, t.me, telegram.me, голый домен, ftp")

    # --- кириллический домен ----------------------------------------------
    found = bad("Наш сайт мойсайт.рф работает", why=".рф")
    assert "мойсайт.рф" in " ".join(found), found
    bad("контора.москва", why=".москва")
    print("кириллические зоны OK:", found[0])

    # --- ложные срабатывания ----------------------------------------------
    for text in [
        "Работаю с гемблой, нутрой и т.д.",
        "Есть трафик, крео, лендинги и т.п.",
        "Цена 1.5 доллара за клик",
        "Опыт 3.5 года",
        "Пишу на node.js и main.py",
        "Отчёт в файле report.pdf",
        "Ищу байеров под гемблу, гео Tier-1",
        "Пиши @adman, тема #гемблинг",
        "Почта для связи: work@example.com",
        "Телефон 8-900-123-45-67",
    ]:
        ok(text, why="ложное срабатывание")
    print("ложные срабатывания OK: «т.д.», «т.п.», 1.5, node.js, @ник, #хэштег, почта")

    # --- entities ----------------------------------------------------------
    for kind in ("bold", "italic", "underline", "strikethrough", "code", "pre",
                 "spoiler", "blockquote", "custom_emoji", "url", "text_mention"):
        found = bad("Совершенно обычный текст без ссылок",
                    [MessageEntity(type=kind, offset=0, length=5)], why=kind)
        assert found, kind
    found = bad("Обычный текст",
                [MessageEntity(type="text_link", offset=0, length=7,
                               url="https://example.com")], why="text_link")
    assert "скрытая ссылка" in " ".join(found), found
    print("форматирование OK: bold/italic/…/text_link отклоняются")

    # разрешённые entities не мешают
    ok("Пиши @adman, тема #гемблинг",
       [MessageEntity(type="mention", offset=5, length=6),
        MessageEntity(type="hashtag", offset=18, length=9)], why="mention+hashtag")
    ok("Почта work@example.com", [MessageEntity(type="email", offset=6, length=16)])
    print("разрешённые entities OK: mention, hashtag, email")

    # --- entities в виде словарей (подпись к фото у самописных фейков) -----
    assert tr.find_violations("текст", [{"type": "bold"}]), "словарь-entity тоже читаем"
    assert not tr.find_violations("текст", [{"type": "hashtag"}])
    print("entity-словари OK")

    # --- текст ответа бота -------------------------------------------------
    msg = tr.violations_text(bad("заходи на example.com"))
    assert msg.startswith("❌ В тексте объявления нельзя использовать ссылки")
    assert "Найдено:" in msg and "example.com" in msg
    assert msg.endswith("Пришли текст ещё раз обычным текстом.")
    print("текст ответа OK:", msg[:60] + "…")

    # --- соцсети: тут ссылки разрешены ------------------------------------
    links, errors = tr.parse_socials("https://vk.com/id1\n@myname\nt.me/myblog\n"
                                     "instagram.com/nick")
    assert not errors, errors
    assert links == ["https://vk.com/id1", "@myname", "t.me/myblog",
                     "instagram.com/nick"], links
    print("соцсети OK: принято ссылок", len(links))

    # форматирование в соцсетях всё равно нельзя
    _, errors = tr.parse_socials("https://vk.com/id1",
                                 [MessageEntity(type="bold", offset=0, length=5)])
    assert errors and "жирный" in " ".join(errors), errors
    # а автоопределённая ссылка (entity url) — можно
    _, errors2 = tr.parse_socials("https://vk.com/id1",
                                  [MessageEntity(type="url", offset=0, length=18)])
    assert not errors2, errors2
    print("соцсети: форматирование отклоняется, entity url разрешена")

    # лимиты и мусор
    _, errors = tr.parse_socials("\n".join(f"https://site{i}.com" for i in range(6)))
    assert errors and "максимум 5" in " ".join(errors), errors
    _, errors = tr.parse_socials("https://vk.com/" + "a" * 600)
    assert errors and "слишком длинно" in " ".join(errors), errors
    _, errors = tr.parse_socials("просто рассказ о себе")
    assert errors and "не похоже на адрес" in " ".join(errors), errors
    _, errors = tr.parse_socials("   ")
    assert errors, "пустой ввод — ошибка"
    print("соцсети: лимиты OK (5 строк, 500 символов, мусор)")

    print("RULES OK")


main()

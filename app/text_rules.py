"""Правила для текста объявления: ссылок и форматирования быть не должно.

Чистые функции без обращения к базе и к Telegram — их удобно проверять
отдельным тестом (tests/test_rules.py).

Основная точка входа — find_violations(text, entities): возвращает список
человекочитаемых причин отказа. Пустой список = текст можно принимать.
"""
import re
from typing import Any, Iterable, Optional

# ------------------------------------------------------------------ entities
# Форматирование и ссылки, которые Telegram присылает разметкой сообщения.
BAD_ENTITIES: dict[str, str] = {
    "url":                   "ссылка",
    "text_link":             "скрытая ссылка",
    "bold":                  "жирный шрифт",
    "italic":                "курсив",
    "underline":             "подчёркивание",
    "strikethrough":         "зачёркнутый текст",
    "code":                  "моноширинный текст",
    "pre":                   "блок кода",
    "spoiler":               "спойлер",
    "blockquote":            "цитата",
    "expandable_blockquote": "цитата",
    "custom_emoji":          "кастомные эмодзи",
    "text_mention":          "упоминание-ссылка на пользователя",
}

# @username, #хэштег, почта и телефон разрешены — это не ссылки и не разметка.
ALLOWED_ENTITIES: frozenset = frozenset({
    "mention", "hashtag", "cashtag", "bot_command", "email", "phone_number",
})

# Только форматирование (без ссылок) — для шага «соцсети», где ссылки разрешены.
FORMAT_ENTITIES: dict[str, str] = {
    key: label for key, label in BAD_ENTITIES.items()
    if key not in {"url", "text_link"}
}
# text_link — это markdown-разметка «текст под ссылкой», её не пускаем и в соцсети:
# нужен именно открытый адрес, чтобы модератор видел, куда ведёт ссылка.
FORMAT_ENTITIES["text_link"] = "скрытая ссылка (пришли адрес открытым текстом)"


# ------------------------------------------------------------------ ссылки в тексте
SCHEME_RE = re.compile(r"(?:https?|ftp|tg)://\S+", re.IGNORECASE)
WWW_RE = re.compile(r"(?<![\w.-])www\.\S+", re.IGNORECASE)
TME_RE = re.compile(r"(?<![\w.-])(?:t|telegram)\.me/\S*", re.IGNORECASE)

# Общий «домен.зона»: перед доменом не должно быть буквы/цифры/точки/@ (чтобы не
# ловить хвосты слов и адреса почты — почта разрешена), после зоны — не буква.
DOMAIN_RE = re.compile(
    r"(?<![\w@.-])([a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63})*)\.([a-z]{2,24})(?![a-z0-9-])",
    re.IGNORECASE,
)
# Кириллические зоны: «сайт.рф». Одиночные буквы («т.д.», «и т.п.») не подходят —
# перед точкой требуем минимум две буквы.
CYRILLIC_DOMAIN_RE = re.compile(
    r"(?<![\w@.-])([а-яёa-z0-9][а-яёa-z0-9-]{1,62})\.(рф|su|укр|москва|онлайн|сайт|дети)"
    r"(?![а-яёa-z0-9-])",
    re.IGNORECASE,
)

# Расширения файлов и прочие «домены», которые доменами не являются:
# node.js, main.py, README.md, config.yml… Их не считаем ссылками.
NOT_A_TLD: frozenset = frozenset({
    "js", "ts", "jsx", "tsx", "py", "php", "rb", "go", "sh", "bat", "pl", "lua",
    "txt", "md", "csv", "tsv", "json", "xml", "yml", "yaml", "ini", "cfg", "conf",
    "env", "lock", "log", "sql", "html", "htm", "css", "scss", "less",
    "jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "ico", "mp3", "mp4", "avi",
    "mov", "wav", "webm", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "zip", "rar", "tar", "gz", "exe", "dll", "apk", "dmg", "iso", "psd", "fig",
})


def _entity_type(entity: Any) -> Optional[str]:
    """Тип entity у aiogram — enum или строка, у словаря — ключ type."""
    if isinstance(entity, dict):
        value = entity.get("type")
    else:
        value = getattr(entity, "type", None)
    if value is None:
        return None
    return str(getattr(value, "value", value)).lower()


def _entity_reasons(entities: Optional[Iterable], table: dict[str, str]) -> list[str]:
    found: list[str] = []
    for entity in entities or ():
        kind = _entity_type(entity)
        if not kind or kind in ALLOWED_ENTITIES:
            continue
        label = table.get(kind)
        if label and label not in found:
            found.append(label)
    return found


def find_links(text: str) -> list[str]:
    """Находит в обычном тексте ссылки и домены. Возвращает найденные куски."""
    text = text or ""
    found: list[str] = []

    def add(value: str) -> None:
        value = value.strip().strip(".,;:!?)»\"'")
        if not value:
            return
        # правила идут от точного к общему: «t.me/chan» внутри «https://t.me/chan»
        # второй причиной не показываем
        low = value.lower()
        if any(low in item.lower() for item in found):
            return
        found.append(value)

    for regex in (SCHEME_RE, WWW_RE, TME_RE):
        for match in regex.finditer(text):
            add(match.group(0))

    for match in DOMAIN_RE.finditer(text):
        if match.group(2).lower() in NOT_A_TLD:
            continue
        add(match.group(0))

    for match in CYRILLIC_DOMAIN_RE.finditer(text):
        add(match.group(0))

    return found


def find_violations(text: str, entities: Optional[Iterable] = None) -> list[str]:
    """Причины, по которым текст объявления принимать нельзя.

    Пустой список — нарушений нет. entities — message.entities или
    message.caption_entities (текст может прийти подписью к фото).
    """
    reasons = _entity_reasons(entities, BAD_ENTITIES)
    links = find_links(text)
    if links:
        shown = ", ".join(f"«{item}»" for item in links[:3])
        if len(links) > 3:
            shown += f" и ещё {len(links) - 3}"
        reason = f"ссылки в тексте: {shown}"
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def violations_text(reasons: list[str]) -> str:
    """Готовый ответ бота на найденные нарушения."""
    return ("❌ В тексте объявления нельзя использовать ссылки и форматирование "
            "(жирный, курсив и т.п.). Найдено: " + "; ".join(reasons) + ". "
            "Пришли текст ещё раз обычным текстом.")


# ------------------------------------------------------------------ соцсети (шаг «Интро»)
MAX_SOCIAL_LINKS = 5
MAX_SOCIALS_LEN = 500

_SOCIAL_URL_RE = re.compile(r"^https?://[^\s]{4,}$", re.IGNORECASE)
_SOCIAL_USERNAME_RE = re.compile(r"^@[a-z0-9_]{3,32}$", re.IGNORECASE)
_SOCIAL_TME_RE = re.compile(r"^(?:https?://)?(?:t|telegram)\.me/[^\s]{1,64}$", re.IGNORECASE)
_SOCIAL_BARE_RE = re.compile(
    r"^[а-яёa-z0-9][а-яёa-z0-9-]{0,62}(?:\.[а-яёa-z0-9-]{1,63})*\.[а-яёa-z]{2,24}"
    r"(?:/[^\s]*)?$",
    re.IGNORECASE,
)


def is_social_link(line: str) -> bool:
    """Одна строка шага «соцсети»: адрес, @username или t.me-ссылка."""
    line = (line or "").strip()
    if not line:
        return False
    return bool(_SOCIAL_TME_RE.match(line) or _SOCIAL_URL_RE.match(line)
                or _SOCIAL_USERNAME_RE.match(line) or _SOCIAL_BARE_RE.match(line))


def parse_socials(text: str, entities: Optional[Iterable] = None) -> tuple[list[str], list[str]]:
    """Разбирает ввод соцсетей. Возвращает (список ссылок, список ошибок).

    Ссылки здесь разрешены (в этом весь смысл шага), а форматирование — нет.
    """
    errors = _entity_reasons(entities, FORMAT_ENTITIES)
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]

    if not lines:
        errors.append("не вижу ни одной ссылки")
        return [], errors
    if len(lines) > MAX_SOCIAL_LINKS:
        errors.append(f"слишком много строк: {len(lines)}, максимум {MAX_SOCIAL_LINKS}")
    if len("\n".join(lines)) > MAX_SOCIALS_LEN:
        errors.append(f"слишком длинно: {len(chr(10).join(lines))} символов "
                      f"из {MAX_SOCIALS_LEN}")

    bad = [line for line in lines[:MAX_SOCIAL_LINKS] if not is_social_link(line)]
    if bad:
        shown = ", ".join(f"«{item[:40]}»" for item in bad[:3])
        errors.append(f"это не похоже на адрес соцсети: {shown}")

    return (lines[:MAX_SOCIAL_LINKS] if not errors else []), errors


def socials_errors_text(errors: list[str]) -> str:
    return ("❌ Не получилось принять соцсети: " + "; ".join(errors) + ".\n"
            "Пришли до 5 адресов, каждый с новой строки (https://…, t.me/… или @username), "
            "либо нажми «Пропустить».")

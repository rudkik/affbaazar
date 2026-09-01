"""Объявления: расчёт цены, публикация в канал, закреп, удаление с возвратом коинов.

Общее ядро для бота и веб-админки — вся работа с объявлениями идёт только через него.
"""
import html
import logging
from datetime import timedelta
from typing import Any, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import action_log, db, site_db, tokens

log = logging.getLogger(__name__)

PIN_OPTIONS = (0, 4, 8)          # часы закрепа


class AdError(Exception):
    """Публикация невозможна (нет канала, нет прав, не хватает коинов)."""


# ------------------------------------------------------------------ цена
async def price_quote(has_image: bool = False, pin_hours: int = 0) -> dict[str, int]:
    # цены могут быть выставлены как угодно (в т. ч. по ошибке) — ниже нуля не опускаемся
    base = max(0, await db.get_int("price_post"))
    image = max(0, await db.get_int("price_image")) if has_image else 0
    pin = 0
    if pin_hours == 4:
        pin = max(0, await db.get_int("price_pin_4h"))
    elif pin_hours == 8:
        pin = max(0, await db.get_int("price_pin_8h"))
    return {"base": base, "image": image, "pin": pin, "total": base + image + pin}


async def price_line(has_image: bool = False, pin_hours: int = 0) -> str:
    q = await price_quote(has_image, pin_hours)
    parts = [f"объявление — {q['base']}"]
    if q["image"]:
        parts.append(f"картинка — {q['image']}")
    if q["pin"]:
        parts.append(f"закреп {pin_hours} ч — {q['pin']}")
    return " + ".join(parts) + f" = <b>{q['total']}</b> коинов"


# ------------------------------------------------------------------ текст поста
def format_ad(text: str, ad_type_tag: Optional[str], vertical_tag: Optional[str],
              author_username: Optional[str] = None) -> str:
    tags = []
    if ad_type_tag:
        tags.append(f"#{ad_type_tag}")
    if vertical_tag:
        tags.append(f"#{vertical_tag}")
    head = " ".join(tags)
    body = html.escape(text or "").strip()
    footer = f"\n\n<i>Автор: @{html.escape(author_username)}</i>" if author_username else ""
    return (f"{head}\n\n{body}{footer}").strip()


def admin_kb(ad_id: int) -> InlineKeyboardMarkup:
    """Кнопки под постом в канале. Нажатия проверяются на права администратора."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ad_del:{ad_id}"),
        InlineKeyboardButton(text="💬 Удалить с комментом", callback_data=f"ad_delc:{ad_id}"),
    ]])


# ------------------------------------------------------------------ канал
async def ad_channel(bot: Bot) -> tuple[int, str]:
    channel_id = await db.get_int("ad_channel_id")
    if not channel_id:
        raise AdError("Канал для объявлений не задан. Админ: /set_channel @канал")
    title = await db.get_setting("ad_channel_title") or str(channel_id)
    return channel_id, title


async def is_channel_admin(bot: Bot, user_id: int) -> bool:
    """Админ канала объявлений (или админ бота) — им доступны кнопки под постом."""
    from app.config import ADMINS
    if user_id in ADMINS:
        return True
    try:
        channel_id, _ = await ad_channel(bot)
        member = await bot.get_chat_member(channel_id, user_id)
        return member.status in {"creator", "administrator"}
    except (AdError, TelegramAPIError):
        return False


# ------------------------------------------------------------------ публикация
async def publish_ad(bot: Bot, user, *, text: str, ad_type_row, vertical_row=None,
                     media_type: str = "text", media_file_id: Optional[str] = None,
                     pin_hours: int = 0) -> dict[str, Any]:
    """Публикует объявление в канал, списывает коины, зеркалит на сайт.

    Возвращает {"ad_id", "message_id", "cost", "balance"}. Кидает AdError.
    """
    channel_id, channel_title = await ad_channel(bot)
    has_image = bool(media_file_id) and media_type != "text"
    quote = await price_quote(has_image, pin_hours)

    ad_type_tag = ad_type_row["tag"] if ad_type_row else None
    vertical_tag = vertical_row["tag"] if vertical_row else None
    body = format_ad(text, ad_type_tag, vertical_tag, user.username)

    cur = await db.execute(
        """INSERT INTO ads(user_id, channel_id, ad_type_id, ad_type_name, ad_type_tag,
                           vertical_id, vertical_name, vertical_tag, text, media_type,
                           media_file_id, cost_base, cost_image, cost_pin, cost_total, pin_hours)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user.id, channel_id,
         ad_type_row["id"] if ad_type_row else None,
         ad_type_row["name"] if ad_type_row else None, ad_type_tag,
         vertical_row["id"] if vertical_row else None,
         vertical_row["name"] if vertical_row else None, vertical_tag,
         text, media_type, media_file_id,
         quote["base"], quote["image"], quote["pin"], quote["total"], pin_hours))
    ad_id = cur.lastrowid

    # Деньги резервируем ДО отправки: списание атомарно, поэтому два параллельных
    # нажатия при остатке на одно объявление не пройдут оба. Если публикация
    # сорвётся — возвращаем всё обратно.
    if quote["total"] and not await tokens.charge(
            user.id, quote["total"], "ad_post",
            {"ad_id": ad_id, "pin_hours": pin_hours, "image": bool(has_image)}):
        await db.execute("DELETE FROM ads WHERE id = ?", (ad_id,))
        balance = await tokens.balance(user.id)
        raise AdError(f"Не хватает коинов: нужно {quote['total']}, на балансе {balance}.")

    try:
        if has_image and media_type == "photo":
            sent = await bot.send_photo(channel_id, media_file_id, caption=body,
                                        reply_markup=admin_kb(ad_id))
        elif has_image and media_type == "video":
            sent = await bot.send_video(channel_id, media_file_id, caption=body,
                                        reply_markup=admin_kb(ad_id))
        else:
            sent = await bot.send_message(channel_id, body, disable_web_page_preview=True,
                                          reply_markup=admin_kb(ad_id))
    except TelegramAPIError as exc:
        await db.execute("DELETE FROM ads WHERE id = ?", (ad_id,))
        if quote["total"]:
            await tokens.add(user.id, quote["total"], "ad_post_failed", {"ad_id": ad_id})
        log.warning("Публикация объявления не удалась: %s", exc)
        raise AdError("Не удалось опубликовать. Проверьте, что бот — администратор канала.")

    pinned_until = None
    if pin_hours:
        try:
            await bot.pin_chat_message(channel_id, sent.message_id, disable_notification=True)
            pinned_until = db.iso(db.utcnow() + timedelta(hours=pin_hours))
        except TelegramAPIError as exc:
            # порядок важен: сначала вычитаем стоимость закрепа, потом обнуляем её
            log.warning("Закреп не удался: %s", exc)
            quote["total"] -= quote["pin"]
            # закрепа не было — возвращаем доплату за него
            new_balance = await tokens.add(user.id, quote["pin"], "ad_pin_refund",
                                           {"ad_id": ad_id})
            quote["pin"] = 0
            pin_hours = 0

    new_balance = await tokens.balance(user.id)
    await db.execute(
        """UPDATE ads SET channel_message_id = ?, pinned_until = ?, cost_pin = ?,
                          cost_total = ?, pin_hours = ?
           WHERE id = ?""",
        (sent.message_id, pinned_until, quote["pin"], quote["total"], pin_hours, ad_id))
    await db.execute("UPDATE users SET messages_sent = messages_sent + 1 WHERE user_id = ?",
                     (user.id,))

    await site_db.mirror_post(
        source_chat_id=channel_id, source_chat_title=channel_title,
        source_message_id=sent.message_id, channel_id=channel_id,
        channel_message_id=sent.message_id, author_id=user.id,
        author_username=user.username, author_name=user.full_name,
        text=text, media_type=media_type, media_file_id=media_file_id,
        ad_type_name=ad_type_row["name"] if ad_type_row else None, ad_type_tag=ad_type_tag,
        vertical_name=vertical_row["name"] if vertical_row else None, vertical_tag=vertical_tag,
        pinned_until=pinned_until, is_reposted=1)
    await action_log.action(channel_id, user.id, user.username, text,
                            event=f"объявление #{ad_id} ({ad_type_tag or '-'})")
    return {"ad_id": ad_id, "message_id": sent.message_id,
            "cost": quote["total"], "balance": new_balance, "channel_id": channel_id}


# ------------------------------------------------------------------ удаление
async def delete_ad(bot: Optional[Bot], ad_id: int, by_admin_id: Optional[int] = None,
                    comment: Optional[str] = None, refund: bool = True) -> dict[str, Any]:
    """Удаляет объявление из канала, снимает с сайта и возвращает коины автору."""
    ad = await db.fetchone("SELECT * FROM ads WHERE id = ?", (ad_id,))
    if ad is None:
        raise AdError("Объявление не найдено.")

    # «Занимаем» объявление одним запросом с условием: параллельные нажатия
    # «Удалить» не смогут удалить и вернуть коины дважды.
    claim = await db.execute(
        """UPDATE ads SET status = 'deleted', deleted_by = ?, delete_comment = ?,
                          deleted_at = datetime('now'), unpinned = 1
           WHERE id = ? AND status <> 'deleted'""",
        (by_admin_id, comment, ad_id))
    if not claim.rowcount:
        return {"already": True, "refunded": 0, "user_id": ad["user_id"]}

    if bot and ad["channel_message_id"]:
        try:
            if ad["pinned_until"] and not ad["unpinned"]:
                await bot.unpin_chat_message(ad["channel_id"], ad["channel_message_id"])
        except TelegramAPIError:
            pass
        try:
            await bot.delete_message(ad["channel_id"], ad["channel_message_id"])
        except TelegramAPIError as exc:
            log.warning("Не удалось удалить пост %s: %s", ad_id, exc)

    refunded = 0
    if refund and ad["cost_total"]:
        # право на возврат тоже занимаем условием — двойного возврата не будет
        paid = await db.execute(
            "UPDATE ads SET refunded = 1 WHERE id = ? AND refunded = 0", (ad_id,))
        if paid.rowcount:
            refunded = int(ad["cost_total"])
            await tokens.add(ad["user_id"], refunded, "ad_refund",
                             {"ad_id": ad_id, "by": by_admin_id, "comment": comment})
    if ad["channel_message_id"]:
        await site_db.mark_deleted(ad["channel_id"], ad["channel_message_id"])

    if bot:
        note = (f"🗑 Ваше объявление удалено администрацией.\n\n"
                f"<b>Комментарий:</b> {html.escape(comment)}" if comment else
                "🗑 Ваше объявление удалено администрацией.")
        if refunded:
            note += f"\n\nНа баланс возвращено <b>{refunded}</b> коинов."
        try:
            await bot.send_message(ad["user_id"], note)
        except TelegramAPIError:
            pass

    await action_log.action(ad["channel_id"] or 0, by_admin_id or 0, None,
                            f"объявление #{ad_id}, возврат {refunded}, коммент: {comment or '-'}",
                            event="удаление объявления")
    return {"already": False, "refunded": refunded, "user_id": ad["user_id"], "ad": dict(ad)}


# ------------------------------------------------------------------ снятие закрепа
async def unpin_expired(bot: Bot) -> int:
    """Снимает закрепы, у которых вышло время. Вызывается фоновой задачей."""
    rows = await db.fetchall(
        """SELECT * FROM ads WHERE pinned_until IS NOT NULL AND unpinned = 0
           AND status = 'published' AND pinned_until <= datetime('now')""")
    done = 0
    for ad in rows:
        try:
            await bot.unpin_chat_message(ad["channel_id"], ad["channel_message_id"])
        except TelegramAPIError as exc:
            log.warning("Не удалось снять закреп %s: %s", ad["id"], exc)
        await db.execute("UPDATE ads SET unpinned = 1 WHERE id = ?", (ad["id"],))
        done += 1
    return done

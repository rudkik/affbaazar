"""Операции с токенами: единая точка записи в реестр token_tx."""
import json
import logging
from typing import Any, Optional

from app import db, locks

log = logging.getLogger(__name__)


async def balance(user_id: int) -> int:
    return int(await db.scalar("SELECT tokens FROM users WHERE user_id = ?", (user_id,), 0))


async def add(user_id: int, amount: int, reason: str, meta: Optional[dict[str, Any]] = None) -> int:
    """Начислить (amount > 0) или списать (amount < 0). Возвращает новый баланс.

    Баланс не уходит в минус: списание ограничивается остатком прямо в SQL,
    поэтому параллельные вызовы не могут «пробить» ноль.
    """
    await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))
    conn = db.conn()
    async with locks.named(f"balance:{user_id}"):
        before = await balance(user_id)
        async with conn.execute(
                "UPDATE users SET tokens = MAX(0, tokens + ?), updated_at = datetime('now') "
                "WHERE user_id = ? RETURNING tokens", (amount, user_id)) as cur:
            row = await cur.fetchone()
        await conn.commit()
        new_balance = int(row[0]) if row else 0
        applied = new_balance - before          # фактическое изменение, а не запрошенное
        await db.execute(
            "INSERT INTO token_tx(user_id, amount, reason, meta, balance_after) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, applied, reason, json.dumps(meta or {}, ensure_ascii=False), new_balance),
        )
    log.info("tokens %+d user=%s reason=%s balance=%s", applied, user_id, reason, new_balance)
    return new_balance


async def charge(user_id: int, amount: int, reason: str,
                 meta: Optional[dict] = None) -> bool:
    """Списать, если хватает баланса. False — если не хватило.

    Списание и проверка — один SQL-запрос с условием, поэтому два параллельных
    списания при остатке на одно не могут пройти оба.
    """
    if amount <= 0:
        return True
    await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))
    cur = await db.execute(
        "UPDATE users SET tokens = tokens - ?, updated_at = datetime('now') "
        "WHERE user_id = ? AND tokens >= ?", (amount, user_id, amount))
    if not cur.rowcount:
        return False
    new_balance = await balance(user_id)
    await db.execute(
        "INSERT INTO token_tx(user_id, amount, reason, meta, balance_after) VALUES (?, ?, ?, ?, ?)",
        (user_id, -amount, reason, json.dumps(meta or {}, ensure_ascii=False), new_balance))
    log.info("tokens -%d user=%s reason=%s balance=%s", amount, user_id, reason, new_balance)
    return True


async def grant_signup_bonus(user_id: int) -> int:
    """Бонус за подписку. Выдаётся один раз за всю жизнь аккаунта (защита от читинга).

    Право на бонус «занимается» одним UPDATE с условием activated = 0, поэтому
    параллельные вызовы не могут начислить бонус дважды.
    """
    await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))
    cur = await db.execute(
        "UPDATE users SET activated = 1, activated_at = datetime('now') "
        "WHERE user_id = ? AND activated = 0", (user_id,))
    if not cur.rowcount:
        return 0
    bonus = await db.get_int("signup_bonus")
    if bonus:
        await add(user_id, bonus, "signup_bonus")
    return bonus


async def reward_referrer(user_id: int) -> tuple[Optional[int], int]:
    """Начислить пригласившему, когда приглашённый активировался. -> (referrer_id, сумма).

    Награда «занимается» одним UPDATE с условием referral_rewarded = 0.
    """
    user = await db.get_user(user_id)
    if not user or not user["referrer_id"] or user["referrer_id"] == user_id:
        return None, 0
    referrer_id = int(user["referrer_id"])
    cur = await db.execute(
        "UPDATE users SET referral_rewarded = 1 "
        "WHERE user_id = ? AND referral_rewarded = 0 AND referrer_id IS NOT NULL", (user_id,))
    if not cur.rowcount:
        return None, 0
    bonus = await db.get_int("referral_bonus")
    if bonus:
        await add(referrer_id, bonus, "referral_bonus", {"invitee": user_id})
    return referrer_id, bonus


async def history(user_id: int, limit: int = 20) -> list:
    return await db.fetchall(
        "SELECT * FROM token_tx WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
    )

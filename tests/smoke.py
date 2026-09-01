import asyncio, os, sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.config as cfg
tmp = pathlib.Path(os.environ.get("SP") or tempfile.mkdtemp(prefix="botest-"))
cfg.MAIN_DB = tmp / "bot.db"; cfg.SITE_DB = tmp / "site.db"
cfg.LOG_DIR = tmp / "logs"; cfg.RESTRICTED_LOG_DIR = tmp / "logs-restricted"
cfg.ADMIN_PASSWORD = "secret"; cfg.SECRET_KEY = "k"
import app.db as db, app.site_db as sdb, app.tokens as tk, app.action_log as al
db.MAIN_DB = cfg.MAIN_DB; sdb.SITE_DB = cfg.SITE_DB
al.LOG_DIR = cfg.LOG_DIR; al.RESTRICTED_LOG_DIR = cfg.RESTRICTED_LOG_DIR

async def main():
    await db.init(); await sdb.init()
    assert await db.get_int("msg_ttl") == 45
    assert await db.get_int("check_limit") == 10
    assert await db.get_int("restrict_hours") == 48
    await db.upsert_chat(-1001, "Тестовый чат")
    await db.set_required_channels(-1001, [{"channel_id": -1002, "title": "Канал", "username": "ch"}])
    assert len(await db.required_channels(-1001)) == 1

    await db.upsert_user(11, "alice", "Alice A")
    await db.upsert_user(22, "bob", "Bob B")
    await db.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (11, 22))

    bonus = await tk.grant_signup_bonus(22)
    assert bonus == 30, bonus
    ref, rb = await tk.reward_referrer(22)
    assert (ref, rb) == (11, 10), (ref, rb)
    assert await tk.balance(11) == 10
    # повторная активация не даёт бонус
    assert await tk.grant_signup_bonus(22) == 0
    assert (await tk.reward_referrer(22))[0] is None
    assert await tk.balance(22) == 30

    # списание
    assert await tk.charge(22, 1, "message") is True
    assert await tk.balance(22) == 29
    assert await tk.charge(22, 999, "message") is False

    # сообщение + возврат
    await db.execute("""INSERT INTO chat_messages(chat_id, message_id, user_id, text, media_type, cost)
                        VALUES (?,?,?,?,?,?)""", (-1001, 555, 22, "привет\nмир", "text", 1))
    await sdb.mirror_post(source_chat_id=-1001, source_chat_title="Тестовый чат",
                          source_message_id=555, author_id=22, author_username="bob",
                          author_name="Bob B", text="привет мир", media_type="text")
    import app.services as sv
    refunded = await sv.refund_message(None, -1001, 555)
    assert refunded == 1, refunded
    assert await tk.balance(22) == 30
    assert await sv.refund_message(None, -1001, 555) == 0  # повторно не возвращаем
    rows, total = await sdb.query_posts()
    assert total == 0, "удалённые скрыты из ленты"

    # лента: поиск/фильтры/сортировка
    for i in range(3):
        await sdb.mirror_post(source_chat_id=-1001, source_chat_title="Тестовый чат",
                              source_message_id=600+i, author_id=22, author_username="bob",
                              text=f"сообщение {i}", media_type="text" if i else "photo")
    rows, total = await sdb.query_posts(q="сообщение")
    assert total == 3, total
    rows, total = await sdb.query_posts(media="photo")
    assert total == 1, total
    rows, total = await sdb.query_posts(author="@bob", sort="created_at", order="asc")
    assert total == 3
    rows, _ = await sdb.query_posts(after_id=0)
    assert len(rows) == 3

    # ограничения
    until = await db.restrict_user(22, -1001, 48, "лимит")
    assert await db.is_restricted(22, -1001) is not None
    await db.update_state(22, -1001, restricted_until=None)
    assert await db.is_restricted(22, -1001) is None

    # файловые логи
    await al.action(-1001, 22, "bob", "многострочный\nтекст | с трубой")
    await al.restricted(-1001, 22, "bob", db.iso(until), 10)
    logs = list((cfg.LOG_DIR / "-1001").glob("*.log"))
    rlogs = list((cfg.RESTRICTED_LOG_DIR / "-1001").glob("restricted_*.log"))
    assert logs and rlogs, (logs, rlogs)
    line = logs[0].read_text().strip()
    assert line.count("|") == 3 and "\n" not in line, line
    print("LOG:", line)
    print("RESTRICTED:", rlogs[0].read_text().strip())

    # веб
    import httpx
    import app.web.server as web
    web.ADMIN_PASSWORD = "secret"; web.SECRET_KEY = "k"
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/")).status_code == 200
        assert "Лента объявлений" in (await c.get("/")).text
        r = await c.get("/api/posts?q=сообщение"); assert r.json()["total"] == 3, r.json()
        assert len((await c.get("/api/chats")).json()) == 1
        assert (await c.get("/admin/api/stats")).status_code == 401
        assert "Пароль" in (await c.get("/admin")).text
        r = await c.post("/admin/login", data={"password": "wrong"})
        assert r.status_code == 303 and "e=1" in r.headers["location"], r.status_code
        assert not c.cookies.get("session")
        r = await c.post("/admin/login", data={"password": "secret"})
        assert r.status_code == 303 and c.cookies.get("session"), r.status_code
        assert "Статистика" in (await c.get("/admin")).text
        s = (await c.get("/admin/api/stats")).json()
        assert s["users"] == 2 and s["chats"] == 1, s
        assert (await c.get("/admin/api/users?q=bob")).json()["total"] == 1
        assert (await c.get("/admin/api/messages")).json()["total"] == 1
        assert len((await c.get("/admin/api/chats")).json()) == 1
        r = await c.post("/admin/api/users/11/tokens", json={"amount": 5})
        assert r.json()["balance"] == 15, r.json()
        r = await c.post("/admin/api/chats/-1001", json={"post_mode": "bot_only", "premoderate": 1})
        assert r.json()["chat"]["post_mode"] == "bot_only"
        r = await c.post("/admin/api/settings", json={"msg_ttl": "60", "check_limit": "5"})
        assert r.json()["settings"]["msg_ttl"] == "60"
        assert (await c.get("/admin/api/restricted")).json() == []
        assert (await c.get("/admin/api/payments")).json() == []
        assert len((await c.get("/admin/api/transactions")).json()) > 0

        # удаление из веб-админки возвращает токены и снимает пост с сайта,
        # даже когда веб поднят отдельно от процесса бота (web.BOT is None)
        assert web.BOT is None
        await db.execute("""INSERT INTO chat_messages(chat_id, message_id, user_id, text,
                                                      media_type, cost)
                            VALUES (?,?,?,?,?,?)""", (-1001, 900, 22, "к удалению", "text", 7))
        await sdb.mirror_post(source_chat_id=-1001, source_message_id=900, author_id=22,
                              author_username="bob", text="к удалению", media_type="text")
        rec = await db.fetchone("SELECT id FROM chat_messages WHERE message_id = 900")
        bal = await tk.balance(22)
        r = (await c.post(f"/admin/api/messages/{rec['id']}/delete")).json()
        assert r["refunded"] == 7 and r["in_telegram"] is False, r
        assert await tk.balance(22) == bal + 7, await tk.balance(22)
        _, total = await sdb.query_posts(q="к удалению")
        assert total == 0, "пост снят с сайта"
        r = (await c.post(f"/admin/api/messages/{rec['id']}/delete")).json()
        assert r["refunded"] == 0, "повторный возврат не начисляется"
        print("web-удаление OK: возврат 7 токенов без участия бота")
        # выход снимает доступ
        await c.get("/admin/logout")
        c.cookies.clear()
        assert (await c.get("/admin/api/stats")).status_code == 401
    await db.close(); await sdb.close()
    print("SMOKE OK")

asyncio.run(main())

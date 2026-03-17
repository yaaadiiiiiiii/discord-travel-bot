import os
import psycopg
from psycopg.rows import dict_row
import random
import sys
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


# -----------------------------
# Windows UTF-8 console
# -----------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# -----------------------------
# Config
# -----------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")
DATABASE_URL = os.getenv("DATABASE_URL")


# -----------------------------
# Choices
# -----------------------------
CATEGORY_CHOICES = [
    app_commands.Choice(name="吃飯", value="吃飯"),
    app_commands.Choice(name="逛街", value="逛街"),
    app_commands.Choice(name="景點", value="景點"),
    app_commands.Choice(name="住宿", value="住宿"),
    app_commands.Choice(name="交通", value="交通"),
    app_commands.Choice(name="咖啡廳", value="咖啡廳"),
]

CATEGORY_EMOJI = {
    "吃飯": "🍜",
    "逛街": "🛍",
    "景點": "🏞",
    "住宿": "🏨",
    "交通": "🚆",
    "咖啡廳": "☕",
}

TIME_HOUR_CHOICES = [app_commands.Choice(name=f"{h:02d}", value=f"{h:02d}") for h in range(24)]
TIME_MINUTE_CHOICES = [app_commands.Choice(name=f"{m:02d}", value=f"{m:02d}") for m in range(0, 60, 10)]


# -----------------------------
# DB Helpers
# -----------------------------
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("請先設定環境變數 DATABASE_URL")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def normalize_text(text: str) -> str:
    return " ".join(text.strip().split()).lower()


def make_time(hour: str, minute: str) -> str:
    return f"{hour}:{minute}"


def is_valid_time_text(text: str) -> bool:
    if not text:
        return False
    parts = text.split(":")
    if len(parts) != 2:
        return False
    hh, mm = parts
    if not (hh.isdigit() and mm.isdigit()):
        return False
    hh = int(hh)
    mm = int(mm)
    return 0 <= hh <= 23 and 0 <= mm <= 59


def category_emoji(category: str) -> str:
    return CATEGORY_EMOJI.get(category, "📌")


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # foods
    cur.execute("""
    CREATE TABLE IF NOT EXISTS foods (
        id BIGSERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        name TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        location TEXT,
        notes TEXT,
        url TEXT,
        added_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_food_unique_per_guild
    ON foods(guild_id, normalized_name)
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS food_votes (
        food_id BIGINT NOT NULL,
        user_id TEXT NOT NULL,
        PRIMARY KEY (food_id, user_id)
    )
    """)

    # polls
    cur.execute("""
    CREATE TABLE IF NOT EXISTS polls (
        id BIGSERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        message_id TEXT,
        question TEXT NOT NULL,
        created_by TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS poll_options (
        id BIGSERIAL PRIMARY KEY,
        poll_id BIGINT NOT NULL,
        option_text TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS poll_votes (
        poll_id BIGINT NOT NULL,
        user_id TEXT NOT NULL,
        option_id BIGINT NOT NULL,
        PRIMARY KEY (poll_id, user_id)
    )
    """)

    # trips
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trips (
        id BIGSERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        name TEXT NOT NULL,
        total_days INTEGER NOT NULL,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # schedules
    cur.execute("""
    CREATE TABLE IF NOT EXISTS schedules (
        id BIGSERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        trip_id BIGINT NOT NULL,
        day_number INTEGER NOT NULL,
        parent_id BIGINT,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        start_time TEXT,
        end_time TEXT,
        location TEXT,
        map_url TEXT,
        notes TEXT,
        created_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()


# -----------------------------
# Poll UI
# -----------------------------
class VoteButton(discord.ui.Button):
    def __init__(self, poll_id: int, option_id: int, label: str):
        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.primary,
            custom_id=f"vote:{poll_id}:{option_id}"
        )
        self.poll_id = poll_id
        self.option_id = option_id

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)

        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SELECT is_active FROM polls WHERE id = %s", (self.poll_id,))
        poll = cur.fetchone()
        if not poll:
            conn.close()
            await interaction.response.send_message("找不到這個投票。", ephemeral=True)
            return

        if poll["is_active"] == 0:
            conn.close()
            await interaction.response.send_message("這個投票已關閉。", ephemeral=True)
            return

        cur.execute(
            "SELECT option_text FROM poll_options WHERE id = %s AND poll_id = %s",
            (self.option_id, self.poll_id)
        )
        option = cur.fetchone()
        if not option:
            conn.close()
            await interaction.response.send_message("找不到這個選項。", ephemeral=True)
            return

        cur.execute("""
            INSERT INTO poll_votes (poll_id, user_id, option_id)
            VALUES (%s, %s, %s)
            ON CONFLICT(poll_id, user_id)
            DO UPDATE SET option_id = excluded.option_id
        """, (self.poll_id, user_id, self.option_id))

        conn.commit()
        conn.close()

        await interaction.response.send_message(
            f"你已投給：**{option['option_text']}**",
            ephemeral=True
        )


class PollView(discord.ui.View):
    def __init__(self, poll_id: int, options: list):
        super().__init__(timeout=None)
        for option in options:
            self.add_item(VoteButton(
                poll_id=poll_id,
                option_id=option["id"],
                label=option["option_text"]
            ))

class FoodButton(discord.ui.Button):
    def __init__(self, food_id: int, label: str):
        super().__init__(
            label=label[:20],  # 防止過長
            style=discord.ButtonStyle.secondary,
            custom_id=f"food:{food_id}"
        )
        self.food_id = food_id

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)

        conn = get_conn()
        cur = conn.cursor()

        # toggle
        cur.execute("""
            SELECT * FROM food_votes
            WHERE food_id = %s AND user_id = %s
        """, (self.food_id, user_id))
        exists = cur.fetchone()

        if exists:
            cur.execute("""
                DELETE FROM food_votes
                WHERE food_id = %s AND user_id = %s
            """, (self.food_id, user_id))
            action = "取消想吃"
        else:
            cur.execute("""
                INSERT INTO food_votes (food_id, user_id)
                VALUES (%s, %s)
            """, (self.food_id, user_id))
            action = "已標記想吃"

        conn.commit()
        conn.close()

        await interaction.response.defer()

        # 重新刷新頁面
        view = FoodListView(page=0, guild_id=interaction.guild_id)
        embed = view.build_embed()
        await interaction.message.edit(embed=embed, view=view)

class FoodListView(discord.ui.View):
    def __init__(self, page: int, guild_id: int):
        super().__init__(timeout=None)
        self.page = page
        self.guild_id = str(guild_id)
        self.per_page = 5

        self.load_data()
        self.build_buttons()

    def load_data(self):
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            SELECT f.*, COUNT(v.user_id) AS votes
            FROM foods f
            LEFT JOIN food_votes v ON f.id = v.food_id
            WHERE f.guild_id = %s
            GROUP BY f.id
            ORDER BY votes DESC, f.id ASC
        """, (self.guild_id,))
        self.rows = cur.fetchall()
        conn.close()

    def build_embed(self):
        start = self.page * self.per_page
        end = start + self.per_page
        page_items = self.rows[start:end]

        lines = []
        for row in page_items:
            lines.append(
                f"#{row['id']} **{row['name']}**\n❤️ {row['votes']} 人想吃"
            )

        text = "\n\n".join(lines) if lines else "沒有資料"

        embed = discord.Embed(
            title=f"🍜 美食清單（第 {self.page+1} 頁）",
            description=text
        )
        return embed

    def build_buttons(self):
        start = self.page * self.per_page
        end = start + self.per_page
        page_items = self.rows[start:end]

        # 食物按鈕
        for row in page_items:
            label = f"❤️ {row['name']}"
            self.add_item(FoodButton(row["id"], label))

        # 分頁按鈕
        if self.page > 0:
            self.add_item(PageButton("⬅ 上一頁", self.page - 1, self.guild_id))

        if end < len(self.rows):
            self.add_item(PageButton("➡ 下一頁", self.page + 1, self.guild_id))

class PageButton(discord.ui.Button):
    def __init__(self, label, page, guild_id):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.page = page
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        view = FoodListView(self.page, self.guild_id)
        embed = view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)

# -----------------------------
# Bot
# -----------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


async def register_persistent_poll_views():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM polls WHERE is_active = 1")
    polls = cur.fetchall()

    for poll in polls:
        cur.execute("SELECT id, option_text FROM poll_options WHERE poll_id = %s", (poll["id"],))
        options = cur.fetchall()
        if options:
            bot.add_view(PollView(poll["id"], options))

    conn.close()


@bot.event
async def setup_hook():
    init_db()
    await register_persistent_poll_views()

    if TEST_GUILD_ID and TEST_GUILD_ID.isdigit():
        guild = discord.Object(id=int(TEST_GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"已同步指令到測試伺服器 {TEST_GUILD_ID}")
    else:
        await bot.tree.sync()
        print("已同步全域指令")


@bot.event
async def on_ready():
    print(f"已登入：{bot.user} ({bot.user.id})")
    print("Bot 已就緒")


# -----------------------------
# Autocomplete
# -----------------------------
async def food_autocomplete(interaction: discord.Interaction, current: str):
    guild_id = str(interaction.guild_id)
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, location
        FROM foods
        WHERE guild_id = %s
        ORDER BY id ASC
    """, (guild_id,))
    rows = cur.fetchall()
    conn.close()

    results = []
    keyword = current.strip().lower()

    for row in rows:
        label = f"#{row['id']} {row['name']}"
        if row["location"]:
            label += f" ({row['location']})"

        if not keyword or keyword in label.lower():
            results.append(app_commands.Choice(name=label[:100], value=str(row["id"])))

        if len(results) >= 25:
            break

    return results


async def poll_autocomplete(interaction: discord.Interaction, current: str):
    guild_id = str(interaction.guild_id)
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, question, is_active
        FROM polls
        WHERE guild_id = %s
        ORDER BY id DESC
    """, (guild_id,))
    rows = cur.fetchall()
    conn.close()

    results = []
    keyword = current.strip().lower()

    for row in rows:
        status = "進行中" if row["is_active"] == 1 else "已關閉"
        label = f"#{row['id']} [{status}] {row['question']}"

        if not keyword or keyword in label.lower():
            results.append(app_commands.Choice(name=label[:100], value=str(row["id"])))

        if len(results) >= 25:
            break

    return results

async def trip_autocomplete(interaction: discord.Interaction, current: str):
    guild_id = str(interaction.guild_id)
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, total_days
        FROM trips
        WHERE guild_id = %s
        ORDER BY id ASC
    """, (guild_id,))
    rows = cur.fetchall()
    conn.close()

    results = []
    keyword = current.strip().lower()

    for row in rows:
        label = f"#{row['id']} {row['name']} ({row['total_days']}天)"
        if not keyword or keyword in label.lower():
            results.append(app_commands.Choice(name=label[:100], value=str(row["id"])))
        if len(results) >= 25:
            break

    return results


async def parent_schedule_autocomplete(interaction: discord.Interaction, current: str):
    guild_id = str(interaction.guild_id)
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT s.id, s.day_number, s.title, s.category, t.name AS trip_name
        FROM schedules s
        JOIN trips t ON s.trip_id = t.id
        WHERE s.guild_id = %s AND s.parent_id IS NULL
        ORDER BY s.id ASC
    """, (guild_id,))
    rows = cur.fetchall()
    conn.close()

    results = []
    keyword = current.strip().lower()

    for row in rows:
        label = f"#{row['id']} Day {row['day_number']} {row['title']} ({row['category']})"
        if not keyword or keyword in label.lower():
            results.append(app_commands.Choice(name=label[:100], value=str(row["id"])))
        if len(results) >= 25:
            break

    return results


async def schedule_autocomplete(interaction: discord.Interaction, current: str):
    guild_id = str(interaction.guild_id)
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT s.id, s.day_number, s.title, s.category, s.parent_id, t.name AS trip_name
        FROM schedules s
        JOIN trips t ON s.trip_id = t.id
        WHERE s.guild_id = %s
        ORDER BY s.id ASC
    """, (guild_id,))
    rows = cur.fetchall()
    conn.close()

    results = []
    keyword = current.strip().lower()

    for row in rows:
        kind = "子" if row["parent_id"] else "主"
        label = f"#{row['id']} Day {row['day_number']} [{kind}] {row['title']} ({row['category']})"
        if not keyword or keyword in label.lower():
            results.append(app_commands.Choice(name=label[:100], value=str(row["id"])))
        if len(results) >= 25:
            break

    return results


# -----------------------------
# Poll commands
# -----------------------------
@bot.tree.command(name="vote_create", description="建立投票")
@app_commands.describe(
    question="投票題目",
    options="選項，用逗號分隔，例如：首爾,釜山 或 首爾，釜山"
)
async def vote_create(interaction: discord.Interaction, question: str, options: str):
    options = options.replace("，", ",")
    option_list = [o.strip() for o in options.split(",") if o.strip()]

    if len(option_list) < 2:
        await interaction.response.send_message("至少要有 2 個選項。", ephemeral=True)
        return

    if len(option_list) > 10:
        await interaction.response.send_message("目前最多 10 個選項。", ephemeral=True)
        return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO polls (guild_id, channel_id, question, created_by)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """, (
        str(interaction.guild_id),
        str(interaction.channel_id),
        question,
        str(interaction.user.id)
    ))
    poll_id = cur.fetchone()["id"]

    option_rows = []
    for opt in option_list:
        cur.execute("""
            INSERT INTO poll_options (poll_id, option_text)
            VALUES (%s, %s)
            RETURNING id
        """, (poll_id, opt))
        option_rows.append({"id": cur.fetchone()["id"], "option_text": opt})

    conn.commit()

    view = PollView(poll_id, option_rows)

    embed = discord.Embed(
        title=f"📊 投票 #{poll_id}",
        description=f"**{question}**"
    )
    embed.add_field(
        name="選項",
        value="\n".join([f"{i+1}. {x}" for i, x in enumerate(option_list)]),
        inline=False
    )
    embed.set_footer(text="每人 1 票，重投會覆蓋前一次選擇")

    await interaction.response.send_message(embed=embed, view=view)
    message = await interaction.original_response()

    cur.execute("UPDATE polls SET message_id = %s WHERE id = %s", (str(message.id), poll_id))
    conn.commit()
    conn.close()

    bot.add_view(view)


@bot.tree.command(name="vote_result", description="查看投票結果")
@app_commands.describe(poll_id="要查看的投票")
@app_commands.autocomplete(poll_id=poll_autocomplete)
async def vote_result(interaction: discord.Interaction, poll_id: str):
    try:
        poll_id_int = int(poll_id)
    except ValueError:
        await interaction.response.send_message("投票 ID 格式錯誤。", ephemeral=True)
        return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT question, is_active
        FROM polls
        WHERE id = %s AND guild_id = %s
    """, (poll_id_int, str(interaction.guild_id)))
    poll = cur.fetchone()

    if not poll:
        conn.close()
        await interaction.response.send_message("找不到這個投票。", ephemeral=True)
        return

    cur.execute("""
        SELECT po.id, po.option_text, COUNT(pv.user_id) AS votes
        FROM poll_options po
        LEFT JOIN poll_votes pv ON po.id = pv.option_id
        WHERE po.poll_id = %s
        GROUP BY po.id, po.option_text
        ORDER BY votes DESC, po.id ASC
    """, (poll_id_int,))
    rows = cur.fetchall()
    conn.close()

    total_votes = sum(r["votes"] for r in rows)
    status = "進行中" if poll["is_active"] == 1 else "已關閉"

    lines = []
    for row in rows:
        count = row["votes"]
        percent = (count / total_votes * 100) if total_votes > 0 else 0
        lines.append(f"**{row['option_text']}**：{count} 票（{percent:.1f}%）")

    embed = discord.Embed(
        title=f"📈 投票結果 #{poll_id_int}",
        description=f"**{poll['question']}**\n狀態：{status}\n總票數：{total_votes}"
    )
    embed.add_field(
        name="結果",
        value="\n".join(lines) if lines else "目前還沒有票",
        inline=False
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="vote_close", description="關閉投票")
@app_commands.describe(poll_id="要關閉的投票")
@app_commands.autocomplete(poll_id=poll_autocomplete)
async def vote_close(interaction: discord.Interaction, poll_id: str):
    try:
        poll_id_int = int(poll_id)
    except ValueError:
        await interaction.response.send_message("投票 ID 格式錯誤。", ephemeral=True)
        return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT created_by, question, is_active
        FROM polls
        WHERE id = %s AND guild_id = %s
    """, (poll_id_int, str(interaction.guild_id)))
    poll = cur.fetchone()

    if not poll:
        conn.close()
        await interaction.response.send_message("找不到這個投票。", ephemeral=True)
        return

    if poll["created_by"] != str(interaction.user.id):
        conn.close()
        await interaction.response.send_message("只有建立投票的人可以關閉。", ephemeral=True)
        return

    if poll["is_active"] == 0:
        conn.close()
        await interaction.response.send_message("這個投票本來就已關閉。", ephemeral=True)
        return

    cur.execute("""
        UPDATE polls
        SET is_active = 0
        WHERE id = %s
    """, (poll_id_int,))
    conn.commit()
    conn.close()

    await interaction.response.send_message(f"已關閉投票 #{poll_id_int}：**{poll['question']}**")


# -----------------------------
# Food commands
# -----------------------------
@bot.tree.command(name="food_add", description="新增美食清單")
@app_commands.describe(
    name="店名",
    location="地區或站名",
    notes="備註",
    url="地圖或社群連結"
)
async def food_add(
    interaction: discord.Interaction,
    name: str,
    location: Optional[str] = None,
    notes: Optional[str] = None,
    url: Optional[str] = None
):
    normalized_name = normalize_text(name)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id FROM foods
        WHERE guild_id = %s AND normalized_name = %s
    """, (str(interaction.guild_id), normalized_name))
    exists = cur.fetchone()

    if exists:
        conn.close()
        await interaction.response.send_message("這家店已經在美食清單裡了，不能重複新增。", ephemeral=True)
        return

    cur.execute("""
        INSERT INTO foods (guild_id, name, normalized_name, location, notes, url, added_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
         RETURNING id
    """, (
        str(interaction.guild_id),
        name.strip(),
        normalized_name,
        location,
        notes,
        url,
        str(interaction.user.id)
    ))
    food_id = cur.fetchone()["id"]
    conn.commit()
    conn.close()

    await interaction.response.send_message(f"已加入美食清單 #{food_id}：**{name.strip()}**")


@bot.tree.command(name="food_list", description="查看美食清單")
async def food_list(interaction: discord.Interaction):
    view = FoodListView(page=0, guild_id=interaction.guild_id)
    embed = view.build_embed()
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="food_delete", description="刪除美食")
@app_commands.describe(food_id="要刪除的食物")
@app_commands.autocomplete(food_id=food_autocomplete)
async def food_delete(interaction: discord.Interaction, food_id: str):
    try:
        food_id_int = int(food_id)
    except ValueError:
        await interaction.response.send_message("食物 ID 格式錯誤。", ephemeral=True)
        return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT name FROM foods
        WHERE id = %s AND guild_id = %s
    """, (food_id_int, str(interaction.guild_id)))
    row = cur.fetchone()

    if not row:
        conn.close()
        await interaction.response.send_message("找不到這筆食物。", ephemeral=True)
        return

    cur.execute("DELETE FROM food_votes WHERE food_id = %s", (food_id_int,))
    cur.execute("""
        DELETE FROM foods
        WHERE id = %s AND guild_id = %s
    """, (food_id_int, str(interaction.guild_id)))

    conn.commit()
    conn.close()

    await interaction.response.send_message(f"已刪除 **{row['name']}**")


@bot.tree.command(name="food_pick", description="隨機抽一家美食")
async def food_pick(interaction: discord.Interaction):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, location, notes, url
        FROM foods
        WHERE guild_id = %s
    """, (str(interaction.guild_id),))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("目前沒有可抽的美食。")
        return

    picked = random.choice(rows)

    embed = discord.Embed(
        title="🎯 今天吃這家！",
        description=f"**{picked['name']}**"
    )
    if picked["location"]:
        embed.add_field(name="地點", value=picked["location"], inline=False)
    if picked["notes"]:
        embed.add_field(name="備註", value=picked["notes"], inline=False)
    if picked["url"]:
        embed.add_field(name="連結", value=picked["url"], inline=False)

    await interaction.response.send_message(embed=embed)


# -----------------------------
# Trip commands
# -----------------------------
@bot.tree.command(name="trip_create", description="建立旅程")
@app_commands.describe(
    name="旅程名稱，例如：韓國5日遊",
    total_days="旅遊天數"
)
async def trip_create(interaction: discord.Interaction, name: str, total_days: int):
    if total_days <= 0:
        await interaction.response.send_message("天數必須大於 0。", ephemeral=True)
        return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO trips (guild_id, name, total_days, created_by)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """, (
        str(interaction.guild_id),
        name.strip(),
        total_days,
        str(interaction.user.id)
    ))
    trip_id = cur.fetchone()["id"]

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"已建立旅程 #{trip_id}：**{name.strip()}**（共 {total_days} 天）"
    )


@bot.tree.command(name="trip_list", description="查看旅程列表")
async def trip_list(interaction: discord.Interaction):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, total_days
        FROM trips
        WHERE guild_id = %s
        ORDER BY id ASC
    """, (str(interaction.guild_id),))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("目前還沒有任何旅程。")
        return

    embed = discord.Embed(title="🧳 旅程列表")
    for row in rows:
        embed.add_field(
            name=f"#{row['id']} {row['name']}",
            value=f"天數：{row['total_days']}",
            inline=False
        )

    await interaction.response.send_message(embed=embed)


# -----------------------------
# Schedule commands
# -----------------------------
@bot.tree.command(name="schedule_add_main", description="新增主行程")
@app_commands.describe(
    trip_id="旅程",
    day_number="第幾天",
    category="行程類別",
    title="行程名稱",
    start_hour="開始小時",
    start_minute="開始分鐘",
    end_hour="結束小時",
    end_minute="結束分鐘",
    location="地點",
    map_url="地圖連結",
    notes="備註"
)
@app_commands.autocomplete(trip_id=trip_autocomplete)
@app_commands.choices(
    category=CATEGORY_CHOICES,
    start_hour=TIME_HOUR_CHOICES,
    start_minute=TIME_MINUTE_CHOICES,
    end_hour=TIME_HOUR_CHOICES,
    end_minute=TIME_MINUTE_CHOICES,
)
async def schedule_add_main(
    interaction: discord.Interaction,
    trip_id: str,
    day_number: int,
    category: app_commands.Choice[str],
    title: str,
    start_hour: app_commands.Choice[str],
    start_minute: app_commands.Choice[str],
    end_hour: app_commands.Choice[str],
    end_minute: app_commands.Choice[str],
    location: Optional[str] = None,
    map_url: Optional[str] = None,
    notes: Optional[str] = None
):
    try:
        trip_id_int = int(trip_id)
    except ValueError:
        await interaction.response.send_message("旅程 ID 格式錯誤。", ephemeral=True)
        return

    start_time = make_time(start_hour.value, start_minute.value)
    end_time = make_time(end_hour.value, end_minute.value)

    if start_time >= end_time:
        await interaction.response.send_message("結束時間必須晚於開始時間。", ephemeral=True)
        return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, total_days, name
        FROM trips
        WHERE id = %s AND guild_id = %s
    """, (trip_id_int, str(interaction.guild_id)))
    trip = cur.fetchone()

    if not trip:
        conn.close()
        await interaction.response.send_message("找不到這個旅程。", ephemeral=True)
        return

    if day_number < 1 or day_number > trip["total_days"]:
        conn.close()
        await interaction.response.send_message(
            f"Day 必須介於 1 到 {trip['total_days']} 之間。",
            ephemeral=True
        )
        return

    cur.execute("""
        INSERT INTO schedules (
            guild_id, trip_id, day_number, parent_id,
            category, title, start_time, end_time,
            location, map_url, notes, created_by
        )
        VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s)
         RETURNING id
    """, (
        str(interaction.guild_id),
        trip_id_int,
        day_number,
        category.value,
        title.strip(),
        start_time,
        end_time,
        location,
        map_url,
        notes,
        str(interaction.user.id)
    ))
    schedule_id = cur.fetchone()["id"]

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"已新增主行程 #{schedule_id}：Day {day_number} / {start_time}-{end_time} / {category.value} / **{title.strip()}**"
    )


@bot.tree.command(name="schedule_add_sub", description="新增子行程")
@app_commands.describe(
    trip_id="旅程",
    day_number="第幾天",
    parent_id="父行程",
    category="行程類別",
    title="子行程名稱",
    start_hour="開始小時，可不填",
    start_minute="開始分鐘，可不填",
    end_hour="結束小時，可不填",
    end_minute="結束分鐘，可不填",
    location="地點",
    map_url="地圖連結",
    notes="備註"
)
@app_commands.autocomplete(
    trip_id=trip_autocomplete,
    parent_id=parent_schedule_autocomplete
)
@app_commands.choices(
    category=CATEGORY_CHOICES,
    start_hour=TIME_HOUR_CHOICES,
    start_minute=TIME_MINUTE_CHOICES,
    end_hour=TIME_HOUR_CHOICES,
    end_minute=TIME_MINUTE_CHOICES,
)
async def schedule_add_sub(
    interaction: discord.Interaction,
    trip_id: str,
    day_number: int,
    parent_id: str,
    category: app_commands.Choice[str],
    title: str,
    start_hour: Optional[app_commands.Choice[str]] = None,
    start_minute: Optional[app_commands.Choice[str]] = None,
    end_hour: Optional[app_commands.Choice[str]] = None,
    end_minute: Optional[app_commands.Choice[str]] = None,
    location: Optional[str] = None,
    map_url: Optional[str] = None,
    notes: Optional[str] = None
):
    try:
        trip_id_int = int(trip_id)
    except ValueError:
        await interaction.response.send_message("旅程 ID 格式錯誤。", ephemeral=True)
        return

    try:
        parent_id_int = int(parent_id)
    except ValueError:
        await interaction.response.send_message("父行程格式錯誤。", ephemeral=True)
        return

    has_any_time = any([start_hour, start_minute, end_hour, end_minute])
    has_all_time = all([start_hour, start_minute, end_hour, end_minute])

    if has_any_time and not has_all_time:
        await interaction.response.send_message("如果要填時間，開始與結束的小時、分鐘都要一起選。", ephemeral=True)
        return

    start_time = None
    end_time = None
    if has_all_time:
        start_time = make_time(start_hour.value, start_minute.value)
        end_time = make_time(end_hour.value, end_minute.value)
        if start_time >= end_time:
            await interaction.response.send_message("結束時間必須晚於開始時間。", ephemeral=True)
            return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, total_days
        FROM trips
        WHERE id = %s AND guild_id = %s
    """, (trip_id_int, str(interaction.guild_id)))
    trip = cur.fetchone()

    if not trip:
        conn.close()
        await interaction.response.send_message("找不到這個旅程。", ephemeral=True)
        return

    if day_number < 1 or day_number > trip["total_days"]:
        conn.close()
        await interaction.response.send_message(
            f"Day 必須介於 1 到 {trip['total_days']} 之間。",
            ephemeral=True
        )
        return

    cur.execute("""
        SELECT id, trip_id, day_number, title
        FROM schedules
        WHERE id = %s AND guild_id = %s AND parent_id IS NULL
    """, (parent_id_int, str(interaction.guild_id)))
    parent = cur.fetchone()

    if not parent:
        conn.close()
        await interaction.response.send_message("找不到父行程。", ephemeral=True)
        return

    if parent["trip_id"] != trip_id_int:
        conn.close()
        await interaction.response.send_message("父行程不屬於這個旅程。", ephemeral=True)
        return

    if parent["day_number"] != day_number:
        conn.close()
        await interaction.response.send_message("父行程不在這一天。", ephemeral=True)
        return

    cur.execute("""
        INSERT INTO schedules (
            guild_id, trip_id, day_number, parent_id,
            category, title, start_time, end_time,
            location, map_url, notes, created_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
         RETURNING id
    """, (
        str(interaction.guild_id),
        trip_id_int,
        day_number,
        parent_id_int,
        category.value,
        title.strip(),
        start_time,
        end_time,
        location,
        map_url,
        notes,
        str(interaction.user.id)
    ))
    schedule_id = cur.fetchone()["id"]

    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"已新增子行程 #{schedule_id}，掛在主行程 #{parent_id_int}：**{title.strip()}**"
    )


@bot.tree.command(name="schedule_delete", description="刪除行程（刪除主行程會一併刪除子行程）")
@app_commands.describe(schedule_id="要刪除的行程")
@app_commands.autocomplete(schedule_id=schedule_autocomplete)
async def schedule_delete(interaction: discord.Interaction, schedule_id: str):
    try:
        schedule_id_int = int(schedule_id)
    except ValueError:
        await interaction.response.send_message("行程 ID 格式錯誤。", ephemeral=True)
        return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, title, parent_id
        FROM schedules
        WHERE id = %s AND guild_id = %s
    """, (schedule_id_int, str(interaction.guild_id)))
    row = cur.fetchone()

    if not row:
        conn.close()
        await interaction.response.send_message("找不到這筆行程。", ephemeral=True)
        return

    deleted_count = 0

    if row["parent_id"] is None:
        cur.execute("""
            DELETE FROM schedules
            WHERE parent_id = %s AND guild_id = %s
        """, (schedule_id_int, str(interaction.guild_id)))
        deleted_count += cur.rowcount

        cur.execute("""
            DELETE FROM schedules
            WHERE id = %s AND guild_id = %s
        """, (schedule_id_int, str(interaction.guild_id)))
        deleted_count += cur.rowcount

        conn.commit()
        conn.close()

        await interaction.response.send_message(
            f"已刪除主行程 **{row['title']}**，共刪除 {deleted_count} 筆（含子行程）。"
        )
    else:
        cur.execute("""
            DELETE FROM schedules
            WHERE id = %s AND guild_id = %s
        """, (schedule_id_int, str(interaction.guild_id)))
        deleted_count += cur.rowcount

        conn.commit()
        conn.close()

        await interaction.response.send_message(
            f"已刪除子行程 **{row['title']}**。"
        )


@bot.tree.command(name="schedule_list", description="查看某旅程某一天的行程")
@app_commands.describe(
    trip_id="旅程",
    day_number="第幾天"
)
@app_commands.autocomplete(trip_id=trip_autocomplete)
async def schedule_list(interaction: discord.Interaction, trip_id: str, day_number: int):
    try:
        trip_id_int = int(trip_id)
    except ValueError:
        await interaction.response.send_message("旅程 ID 格式錯誤。", ephemeral=True)
        return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, total_days
        FROM trips
        WHERE id = %s AND guild_id = %s
    """, (trip_id_int, str(interaction.guild_id)))
    trip = cur.fetchone()

    if not trip:
        conn.close()
        await interaction.response.send_message("找不到這個旅程。", ephemeral=True)
        return

    if day_number < 1 or day_number > trip["total_days"]:
        conn.close()
        await interaction.response.send_message(
            f"Day 必須介於 1 到 {trip['total_days']} 之間。",
            ephemeral=True
        )
        return

    cur.execute("""
        SELECT *
        FROM schedules
        WHERE guild_id = %s AND trip_id = %s AND day_number = %s AND parent_id IS NULL
        ORDER BY
            CASE WHEN start_time IS NULL THEN 1 ELSE 0 END,
            start_time ASC,
            id ASC
    """, (str(interaction.guild_id), trip_id_int, day_number))
    mains = cur.fetchall()

    if not mains:
        conn.close()
        await interaction.response.send_message(f"旅程 **{trip['name']}** 的 Day {day_number} 目前沒有行程。")
        return

    embed = discord.Embed(
        title=f"🗓 {trip['name']} - Day {day_number}",
        description="以下是當天行程"
    )

    for main in mains:
        emoji = category_emoji(main["category"])
        time_text = ""
        if main["start_time"] and main["end_time"]:
            time_text = f"{main['start_time']}-{main['end_time']} "

        main_lines = [f"{time_text}{emoji} {main['title']}【{main['category']}】"]

        if main["location"]:
            main_lines.append(f"📍 {main['location']}")
        if main["map_url"]:
            main_lines.append(f"🔗 {main['map_url']}")
        if main["notes"]:
            main_lines.append(f"📝 {main['notes']}")

        cur.execute("""
            SELECT *
            FROM schedules
            WHERE parent_id = %s
            ORDER BY
                CASE WHEN start_time IS NULL THEN 1 ELSE 0 END,
                start_time ASC,
                id ASC
        """, (main["id"],))
        children = cur.fetchall()

        for child in children:
            c_emoji = category_emoji(child["category"])
            c_time_text = ""
            if child["start_time"] and child["end_time"]:
                c_time_text = f"{child['start_time']}-{child['end_time']} "

            child_line = f"└─ {c_time_text}{c_emoji} {child['title']}【{child['category']}】"
            main_lines.append(child_line)

            if child["location"]:
                main_lines.append(f"　　📍 {child['location']}")
            if child["map_url"]:
                main_lines.append(f"　　🔗 {child['map_url']}")
            if child["notes"]:
                main_lines.append(f"　　📝 {child['notes']}")

        embed.add_field(
            name=f"主行程 #{main['id']}",
            value="\n".join(main_lines),
            inline=False
        )

    conn.close()
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="schedule_list_all", description="查看整個旅程行程")
@app_commands.autocomplete(trip_id=trip_autocomplete)
async def schedule_list_all(interaction: discord.Interaction, trip_id: str):
    try:
        trip_id_int = int(trip_id)
    except ValueError:
        await interaction.response.send_message("旅程 ID 格式錯誤。", ephemeral=True)
        return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, total_days
        FROM trips
        WHERE id = %s AND guild_id = %s
    """, (trip_id_int, str(interaction.guild_id)))
    trip = cur.fetchone()

    if not trip:
        conn.close()
        await interaction.response.send_message("找不到旅程。", ephemeral=True)
        return

    first_message = True

    for day in range(1, trip["total_days"] + 1):
        cur.execute("""
            SELECT *
            FROM schedules
            WHERE guild_id = %s AND trip_id = %s AND day_number = %s AND parent_id IS NULL
            ORDER BY
                CASE WHEN start_time IS NULL THEN 1 ELSE 0 END,
                start_time ASC,
                id ASC
        """, (str(interaction.guild_id), trip_id_int, day))
        mains = cur.fetchall()

        embed = discord.Embed(
            title=f"🗓 {trip['name']} - Day {day}"
        )

        if not mains:
            embed.description = "尚未安排"
        else:
            lines = []

            for main in mains:
                main_emoji = category_emoji(main["category"])
                main_time = ""
                if main["start_time"] and main["end_time"]:
                    main_time = f"{main['start_time']}-{main['end_time']} "

                lines.append(f"{main_time}{main_emoji} {main['title']}【{main['category']}】")

                if main["location"]:
                    lines.append(f"📍 {main['location']}")
                if main["map_url"]:
                    lines.append(f"🔗 {main['map_url']}")
                if main["notes"]:
                    lines.append(f"📝 {main['notes']}")

                # 查子行程
                cur.execute("""
                    SELECT *
                    FROM schedules
                    WHERE parent_id = %s
                    ORDER BY
                        CASE WHEN start_time IS NULL THEN 1 ELSE 0 END,
                        start_time ASC,
                        id ASC
                """, (main["id"],))
                children = cur.fetchall()

                for child in children:
                    child_emoji = category_emoji(child["category"])
                    child_time = ""
                    if child["start_time"] and child["end_time"]:
                        child_time = f"{child['start_time']}-{child['end_time']} "

                    lines.append(f"└─ {child_time}{child_emoji} {child['title']}【{child['category']}】")

                    if child["location"]:
                        lines.append(f"　　📍 {child['location']}")
                    if child["map_url"]:
                        lines.append(f"　　🔗 {child['map_url']}")
                    if child["notes"]:
                        lines.append(f"　　📝 {child['notes']}")

                lines.append("")  # 主行程之間空一行

            embed.description = "\n".join(lines).strip()

        if first_message:
            await interaction.response.send_message(embed=embed)
            first_message = False
        else:
            await interaction.followup.send(embed=embed)

    conn.close()


# -----------------------------
# Run
# -----------------------------
if not TOKEN:
    raise RuntimeError("請先設定環境變數 DISCORD_BOT_TOKEN")

bot.run(TOKEN)
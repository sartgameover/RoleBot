import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from aiohttp import web

# --- БАЗА ДАННЫХ (ФАЙЛ) ---
DB_FILE = "bot_db.json"

def load_db():
    if not os.path.exists(DB_FILE):
        return {"admin_roles": [], "messages": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"admin_roles": [], "messages": {}}

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# --- ВЕБ-СЕРВЕР ДЛЯ ДЕРЖАНИЯ БОТА 24/7 НА RENDER ($0 PLAN) ---
async def handle_ping(request):
    return web.Response(text="Bot is running 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Веб-сервер запущен на порту {port}")

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ СЕССИЙ КОНСТРУКТОРА ---
builder_sessions = {}

# --- ПРОВЕРКА ПРАВ ДОСТУПА ---
def is_config_admin():
    async def predicate(interaction: discord.Interaction):
        if interaction.user.guild_permissions.administrator:
            return True
        
        db = load_db()
        user_role_ids = [role.id for role in interaction.user.roles]
        
        if any(role_id in db.get("admin_roles", []) for role_id in user_role_ids):
            return True
        
        await interaction.response.send_message("❌ У вас нет прав для использования этой команды.", ephemeral=True)
        return False
    return app_commands.check(predicate)

# --- UI: ЭФЕМЕРНАЯ ПАНЕЛЬ ВЫБОРА РОЛЕЙ (ДЛЯ ПОЛЬЗОВАТЕЛЕЙ) ---
class RoleDropdown(discord.ui.Select):
    def __init__(self, roles_data, member: discord.Member):
        self.roles_data = roles_data
        options = []
        
        member_role_ids = [r.id for r in member.roles]
        
        for badge in roles_data:
            has_role = badge['role_id'] in member_role_ids
            emoji = badge.get('emoji')
            emoji_obj = discord.PartialEmoji.from_str(emoji) if emoji and emoji.strip() else None
            
            options.append(discord.SelectOption(
                label=badge['label'],
                value=str(badge['role_id']),
                emoji=emoji_obj,
                default=has_role
            ))
        
        super().__init__(
            placeholder="Выберите роли...",
            min_values=0,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        selected_role_ids = [int(val) for val in self.values]
        member = interaction.user
        guild = interaction.guild
        
        roles_added = []
        roles_removed = []
        
        for badge in self.roles_data:
            role_id = badge['role_id']
            role = guild.get_role(role_id)
            if not role:
                continue
                
            has_role = role in member.roles
            should_have_role = role_id in selected_role_ids
            
            try:
                if should_have_role and not has_role:
                    await member.add_roles(role)
                    roles_added.append(role.mention)
                elif not should_have_role and has_role:
                    await member.remove_roles(role)
                    roles_removed.append(role.mention)
            except discord.Forbidden:
                await interaction.response.send_message("❌ У бота недостаточно прав (роль бота должна быть выше выдаваемой роли).", ephemeral=True)
                return

        response_text = "✅ Ваши роли обновлены!\n"
        if roles_added:
            response_text += f"**Добавлено:** {', '.join(roles_added)}\n"
        if roles_removed:
            response_text += f"**Снято:** {', '.join(roles_removed)}"
        if not roles_added and not roles_removed:
            response_text += "Изменений нет."
            
        await interaction.response.edit_message(content=response_text, view=None)

class EphemeralRolePanelView(discord.ui.View):
    def __init__(self, roles_data, member: discord.Member):
        super().__init__(timeout=180)
        self.add_item(RoleDropdown(roles_data, member))

# --- UI: ПОСТОЯННАЯ КНОПКА ПОД ОПУБЛИКОВАННЫМ СООБЩЕНИЕМ ---
class GetRolesPersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Получить роли", style=discord.ButtonStyle.primary, custom_id="persistent_get_roles_button")
    async def get_roles_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        msg_id_str = str(interaction.message.id)
        
        if msg_id_str not in db.get("messages", {}):
            await interaction.response.send_message("❌ Ошибка: Данные для этого сообщения не найдены в базе.", ephemeral=True)
            return
            
        roles_data = db["messages"][msg_id_str]["roles"]
        
        if not roles_data:
            await interaction.response.send_message("В этом сообщении пока нет настроенных ролей.", ephemeral=True)
            return
            
        view = EphemeralRolePanelView(roles_data, interaction.user)
        await interaction.response.send_message("Выберите желаемые роли ниже:", view=view, ephemeral=True)


# --- UI КОНСТРУКТОРА СООБЩЕНИЙ ---
class BadgeSetupModal(discord.ui.Modal, title='Настройка плашки'):
    badge_label = discord.ui.TextInput(
        label='Название плашки',
        placeholder='Например: Игрок CS:GO',
        required=True,
        max_length=50
    )
    badge_emoji = discord.ui.TextInput(
        label='Эмодзи (скопируйте сам смайлик)',
        placeholder='🎮 (не обязательно)',
        required=False,
        max_length=50
    )

    def __init__(self, role: discord.Role):
        super().__init__()
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if user_id not in builder_sessions:
            await interaction.response.send_message("Сессия истекла.", ephemeral=True)
            return
            
        if len(builder_sessions[user_id]['roles']) >= 25:
            await interaction.response.send_message("❌ Достигнут лимит Discord: максимум 25 ролей в одном меню.", ephemeral=True)
            return

        builder_sessions[user_id]['roles'].append({
            "role_id": self.role.id,
            "role_name": self.role.name,
            "label": self.badge_label.value,
            "emoji": self.badge_emoji.value
        })
        
        await update_builder_message(interaction, user_id)

class BuilderRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="➕ Выберите роль сервера, чтобы добавить плашку...", min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        await interaction.response.send_modal(BadgeSetupModal(role))

class BuilderChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="📍 Выберите канал для отправки...", channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=1)

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        channel = self.values[0]
        builder_sessions[user_id]['channel'] = channel
        await update_builder_message(interaction, user_id)

class BuilderView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.add_item(BuilderRoleSelect())
        self.add_item(BuilderChannelSelect())

    @discord.ui.button(label="Отправить (Опубликовать)", style=discord.ButtonStyle.green, row=2)
    async def publish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = builder_sessions.get(self.user_id)
        if not session:
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return
            
        channel = session.get('channel')
        if not channel:
            await interaction.response.send_message("❌ Сначала выберите канал для отправки!", ephemeral=True)
            return
            
        if not session['roles']:
            await interaction.response.send_message("❌ Добавьте хотя бы одну плашку (роль)!", ephemeral=True)
            return

        view = GetRolesPersistentView()
        try:
            sent_msg = await channel.send(content=session['text'], view=view)
        except discord.Forbidden:
            await interaction.response.send_message("❌ У бота нет прав писать в этот канал.", ephemeral=True)
            return

        db = load_db()
        if "messages" not in db:
            db["messages"] = {}
        db["messages"][str(sent_msg.id)] = {
            "text": session['text'],
            "roles": session['roles']
        }
        save_db(db)

        del builder_sessions[self.user_id]
        
        await interaction.response.edit_message(content=f"✅ Сообщение успешно опубликовано в {channel.mention}!", view=None)

async def update_builder_message(interaction: discord.Interaction, user_id: int):
    session = builder_sessions[user_id]
    
    text = f"**Текст сообщения:**\n{session['text']}\n\n"
    text += "**Добавленные плашки:**\n"
    
    if session['roles']:
        for idx, badge in enumerate(session['roles'], 1):
            emoji = badge['emoji'] + " " if badge['emoji'] else ""
            text += f"{idx}. {emoji}**{badge['label']}** (Роль: `{badge['role_name']}`)\n"
    else:
        text += "— Пока нет плашек —\n"
        
    channel = session.get('channel')
    text += f"\n**Канал отправки:** {channel.mention if channel else 'Не выбран'}"

    view = BuilderView(user_id)
    
    if interaction.response.is_done():
        await interaction.edit_original_response(content=text, view=view)
    else:
        await interaction.response.edit_message(content=text, view=view)

class MainTextModal(discord.ui.Modal, title='Создание сообщения'):
    main_text = discord.ui.TextInput(
        label='Текст сообщения',
        style=discord.TextStyle.long,
        placeholder='Напишите текст, который будет висеть над кнопкой выбора ролей...',
        required=True,
        max_length=2000
    )

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        builder_sessions[user_id] = {
            "text": self.main_text.value,
            "roles": [],
            "channel": None
        }
        
        session = builder_sessions[user_id]
        text = f"**Текст сообщения:**\n{session['text']}\n\n**Добавленные плашки:**\n— Пока нет плашек —\n\n**Канал отправки:** Не выбран"
        
        view = BuilderView(user_id)
        await interaction.response.send_message(content=text, view=view, ephemeral=True)


# --- ОСНОВНОЙ КЛАСС БОТА ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Запускаем фоновый веб-сервер для Render
        self.loop.create_task(start_web_server())
        
        self.add_view(GetRolesPersistentView())
        await self.tree.sync()
        print("Бот готов и слэш-команды синхронизированы!")

bot = MyBot()

# --- СЛЭШ КОМАНДЫ ---
class ConfigGroup(app_commands.Group):
    pass

config_group = ConfigGroup(name="config", description="Настройка системы выдачи ролей")

@config_group.command(name="role", description="Разрешить или запретить роли настраивать бота")
@app_commands.describe(target_role="Роль, которой вы хотите выдать/забрать доступ")
@app_commands.checks.has_permissions(administrator=True)
async def config_role(interaction: discord.Interaction, target_role: discord.Role):
    db = load_db()
    if target_role.id in db["admin_roles"]:
        db["admin_roles"].remove(target_role.id)
        action = "удалена из списка администраторов бота"
    else:
        db["admin_roles"].append(target_role.id)
        action = "добавлена в список администраторов бота"
        
    save_db(db)
    await interaction.response.send_message(f"✅ Роль {target_role.mention} успешно {action}.", ephemeral=True)

@config_group.command(name="create", description="Создать новое сообщение с выдачей ролей")
@is_config_admin()
async def config_create(interaction: discord.Interaction):
    await interaction.response.send_modal(MainTextModal())

bot.tree.add_command(config_group)


# --- ЗАПУСК БОТА ---
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    
    if not TOKEN:
        print("ОШИБКА: Не найден DISCORD_TOKEN в переменных окружения!")
    else:
        bot.run(TOKEN)
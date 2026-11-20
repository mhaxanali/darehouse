from dataclasses import dataclass
from typing import Callable
import asyncio
import discord
from discord.ext import commands

from darehouse.tod.engine import TODEngine
from darehouse.tod.exceptions import (
    GameAlreadyExists,
    GameAlreadyRunning,
    GameDoesNotExist,
    GameNotRunning,
    InsufficientPlayers,
    PlayerAlreadyExists,
    PlayerDoesNotExist,
)
from darehouse.tod.models import Player, Prompt, PromptType

TOD_ENGINE_EXCEPTIONS = (
    GameAlreadyExists,
    GameAlreadyRunning,
    GameDoesNotExist,
    GameNotRunning,
    InsufficientPlayers,
    PlayerAlreadyExists,
    PlayerDoesNotExist,
)


class _NoPromptsAvailable(Exception):
    """Internal signal when the connector has no available prompt."""


@dataclass
class _GuildPrompt(Prompt):
    """Prompt subclass carrying the database prompt ID for Discord/history use."""

    id: int


class _ConnectorPromptProvider:
    """Adapts a guild's TODEngine history and bot connector into a PromptProvider."""

    def __init__(self, bot: commands.Bot, guild_id: int) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.engine: TODEngine | None = None

    def _get_exclude_ids(self) -> set[int]:
        if self.engine is None or self.engine.game is None:
            return set()
        return {
            item.id
            for item in self.engine.game.last_prompts
            if isinstance(item, _GuildPrompt)
        }

    def get_prompt(self, prompt_type: PromptType) -> Prompt:
        connector = getattr(self.bot, "connector", None)
        if connector is None:
            raise _NoPromptsAvailable("Prompt connector is unavailable.")

        raw_prompt = connector.get_prompt(
            self.guild_id,
            prompt_type.value,
            exclude_ids=self._get_exclude_ids(),
        )
        if raw_prompt is None:
            raise _NoPromptsAvailable("No prompts available.")

        return _GuildPrompt(
            id=raw_prompt["id"],
            content=raw_prompt["content"],
            type=prompt_type,
        )


class TOD(commands.Cog):
    """Contains the Truth and Dare game and relevant commands."""

    MAX_HISTORY_PROMPTS = 25

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, dict] = {}  # guild_id -> engine + discord metadata

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _get_connector(self):
        return getattr(self.bot, "connector", None)

    def _get_guild_id(self, ctx: commands.Context) -> int | None:
        return ctx.guild.id if ctx.guild is not None else None

    def _create_guild_session(self, guild_id: int, owner_id: int) -> dict:
        provider = _ConnectorPromptProvider(self.bot, guild_id)
        engine = TODEngine(
            prompt_provider=provider,
            maximum_prompts_to_keep=self.MAX_HISTORY_PROMPTS,
        )
        provider.engine = engine
        engine.create_lobby()
        engine.add_player(Player(id=owner_id))

        return {
            "engine": engine,
            "provider": provider,
            "session_owner": owner_id,
            "message": None,
            "last_choice": None,
            "waiting_for_response": False,
        }

    async def _safe_edit_lobby(self, game: dict, embed: discord.Embed) -> None:
        msg = game.get("message")
        if msg is None:
            return
        try:
            await msg.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    def _build_prompt_embed(
        self,
        ctx: commands.Context,
        session_owner_id: int,
        player_id: int,
        choice: PromptType,
        prompt: Prompt,
    ) -> discord.Embed:
        guild = ctx.guild
        owner_member = guild.get_member(session_owner_id) if guild else None
        player_member = guild.get_member(player_id) if guild else None
        prompt_id = prompt.id if isinstance(prompt, _GuildPrompt) else "?"

        embed = discord.Embed(
            colour=discord.Colour.blue(),
            title=prompt.content,
        )
        embed.set_author(
            name=player_member.name if player_member else "Player",
            icon_url=(
                player_member.display_avatar.url
                if player_member
                else None
            ),
        )
        embed.set_footer(
            text=(
                f"ID: #{prompt_id} | TYPE: {choice.value.upper()} | "
                f"Session Owner: "
                f"{owner_member.name if owner_member else 'Unknown'}"
            )
        )
        return embed

    # ------------------------------------------------------------------
    # embeds / small helpers
    # ------------------------------------------------------------------

    def build_lobby_embed(
        self,
        guild_id: int,
        title: str = "TOD Lobby",
    ) -> discord.Embed:
        game = self.games[guild_id]
        engine: TODEngine = game["engine"]

        players = (
            engine.game.players
            if engine.game is not None
            else []
        )
        players_list = "\n".join(f"<@{player.id}>" for player in players)

        description = (
            f"Session Owner: <@{game['session_owner']}>\n"
            f"Players:\n{players_list if players_list else 'No players yet.'}\n\n"
            f"Run `+tod help` for commands."
        )

        return discord.Embed(
            title=title,
            description=description,
            colour=discord.Colour.blue(),
        )

    def get_prompt(self, guild_id: int, choice: str) -> dict | None:
        """Retrieve a prompt through TODEngine while avoiding recent repeats."""
        game = self.games.get(guild_id)
        if game is None:
            return None

        engine: TODEngine = game["engine"]
        try:
            prompt_type = PromptType(choice.lower())
        except ValueError:
            return None

        try:
            prompt = engine.get_prompt(prompt_type)
        except (_NoPromptsAvailable, *TOD_ENGINE_EXCEPTIONS):
            return None

        return {
            "id": prompt.id if isinstance(prompt, _GuildPrompt) else None,
            "content": prompt.content,
            "type": prompt.type.value,
        }

    def check_message(
        self,
        guild_id: int,
        tod_channel: int | None,
        pid: int,
    ) -> Callable:
        def inner(msg: discord.Message) -> bool:
            if msg.guild is None or msg.guild.id != guild_id:
                return False

            if tod_channel is not None and msg.channel.id != tod_channel:
                return False

            return (
                msg.author.id == pid
                and msg.content.lower() in ["truth", "dare", "t", "d"]
            )

        return inner

    async def run_turn(
        self,
        ctx: commands.Context,
        player: int,
    ) -> None:
        guild_id = self._get_guild_id(ctx)
        if guild_id is None:
            return

        game = self.games.get(guild_id)
        if game is None:
            return

        engine: TODEngine = game["engine"]
        connector = self._get_connector()
        cfg = (
            connector.get_guild_config(guild_id)
            if connector is not None
            else {"tod_channel": None}
        )

        # Clear the previous turn's choice before starting a new turn.
        game["last_choice"] = None

        try:
            await ctx.send(
                f"<@{player}> It's your turn. Truth or Dare (30s)."
            )
        except discord.HTTPException:
            return

        try:
            game["waiting_for_response"] = True

            msg = await self.bot.wait_for(
                "message",
                check=self.check_message(
                    guild_id,
                    cfg.get("tod_channel"),
                    player,
                ),
                timeout=30,
            )

            # The game may have been ended while waiting.
            if guild_id not in self.games:
                return

            choice = (
                PromptType.TRUTH
                if msg.content.lower() in ("t", "truth")
                else PromptType.DARE
            )

            game["last_choice"] = choice

            try:
                prompt = engine.get_prompt(choice)
            except _NoPromptsAvailable:
                await ctx.send("No prompts are available right now.")
                return
            except TOD_ENGINE_EXCEPTIONS as exc:
                await ctx.send(f"Could not fetch prompt: {exc}")
                return

            embed = self._build_prompt_embed(
                ctx=ctx,
                session_owner_id=game["session_owner"],
                player_id=player,
                choice=choice,
                prompt=prompt,
            )

            await ctx.send(embed=embed)

        except asyncio.TimeoutError:
            if guild_id in self.games:
                try:
                    await ctx.send(f"<@{player}> took too long.")
                except discord.HTTPException:
                    pass

        except discord.HTTPException:
            pass

        finally:
            current_game = self.games.get(guild_id)
            if current_game is not None:
                current_game["waiting_for_response"] = False

    async def reroll_prompt(
        self,
        ctx: commands.Context,
        player: int,
    ) -> None:
        """Generate another prompt of the current turn's selected type."""
        guild_id = self._get_guild_id(ctx)
        if guild_id is None:
            return

        game = self.games.get(guild_id)
        if game is None:
            return

        raw_choice = game.get("last_choice")
        if isinstance(raw_choice, PromptType):
            choice = raw_choice
        elif raw_choice in ("truth", "dare"):
            choice = PromptType(raw_choice)
        else:
            await ctx.send(
                "No prompt has been selected for the current turn."
            )
            return

        engine: TODEngine = game["engine"]

        try:
            prompt = engine.get_prompt(choice)
        except _NoPromptsAvailable:
            await ctx.send("No prompts are available right now.")
            return
        except TOD_ENGINE_EXCEPTIONS as exc:
            await ctx.send(f"Could not reroll prompt: {exc}")
            return

        embed = self._build_prompt_embed(
            ctx=ctx,
            session_owner_id=game["session_owner"],
            player_id=player,
            choice=choice,
            prompt=prompt,
        )

        await ctx.send(embed=embed)

    def remove_player_turn(
        self,
        guild_id: int,
        user_id: int,
    ) -> None:
        game = self.games.get(guild_id)
        if game is None:
            return

        engine: TODEngine = game["engine"]
        try:
            engine.remove_player(Player(id=user_id))
        except (GameDoesNotExist, PlayerDoesNotExist, InsufficientPlayers):
            return

    # ------------------------------------------------------------------
    # pagination
    # ------------------------------------------------------------------

    def _paginate_prompt_group(
        self,
        title: str,
        prompts: list[dict],
        guild_id: int,
    ) -> list[discord.Embed]:
        if not prompts:
            return []

        connector = self._get_connector()
        per_page = 10
        total_pages = (len(prompts) - 1) // per_page + 1
        pages = []

        for page_num in range(total_pages):
            chunk = prompts[
                page_num * per_page: page_num * per_page + per_page
            ]

            lines = []

            for prompt in chunk:
                banned = (
                    connector.is_prompt_banned(guild_id, prompt["id"])
                    if connector is not None
                    else False
                )

                mark = "❌" if banned else "✅"
                lines.append(
                    f"{mark} `#{prompt['id']}` {prompt['content']}"
                )

            pages.append(
                discord.Embed(
                    title=(
                        f"{title} "
                        f"(Page {page_num + 1}/{total_pages})"
                    ),
                    description="\n".join(lines),
                    colour=discord.Colour.dark_orange(),
                )
            )

        return pages

    def build_prompt_list_pages(
        self,
        ctx: commands.Context,
        prompt_type: str,
    ) -> list[discord.Embed]:
        guild_id = self._get_guild_id(ctx)
        label = "Truths" if prompt_type == "truth" else "Dares"
        connector = self._get_connector()

        if guild_id is None or connector is None:
            return [
                discord.Embed(
                    title=label,
                    description="No prompts found.",
                    colour=discord.Colour.dark_orange(),
                )
            ]

        default_prompts = connector.list_default_prompts(prompt_type)
        guild_prompts = connector.list_guild_prompts(guild_id, prompt_type)

        pages = (
            self._paginate_prompt_group(
                f"Default {label}",
                default_prompts,
                guild_id,
            )
            + self._paginate_prompt_group(
                f"Server {label}",
                guild_prompts,
                guild_id,
            )
        )

        if not pages:
            pages = [
                discord.Embed(
                    title=label,
                    description="No prompts found.",
                    colour=discord.Colour.dark_orange(),
                )
            ]

        return pages

    async def paginate(
        self,
        ctx: commands.Context,
        pages: list[discord.Embed],
    ) -> None:
        if not pages:
            return

        if len(pages) == 1:
            await ctx.send(embed=pages[0])
            return

        index = 0
        message = await ctx.send(embed=pages[index])
        controls = ["◀️", "⏹️", "▶️"]

        for emoji in controls:
            try:
                await message.add_reaction(emoji)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

        def check(
            reaction: discord.Reaction,
            user: discord.User,
        ) -> bool:
            return (
                user.id == ctx.author.id
                and reaction.message.id == message.id
                and str(reaction.emoji) in controls
            )

        while True:
            try:
                reaction, user = await self.bot.wait_for(
                    "reaction_add",
                    timeout=60.0,
                    check=check,
                )

            except asyncio.TimeoutError:
                try:
                    await message.clear_reactions()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass

                return

            emoji = str(reaction.emoji)

            if emoji == "▶️":
                index = (index + 1) % len(pages)

            elif emoji == "◀️":
                index = (index - 1) % len(pages)

            elif emoji == "⏹️":
                try:
                    await message.clear_reactions()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass

                return

            try:
                await message.edit(embed=pages[index])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

            try:
                await message.remove_reaction(reaction.emoji, user)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

    # ------------------------------------------------------------------
    # cog check
    # ------------------------------------------------------------------

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False

        connector = self._get_connector()
        if connector is None:
            return True

        cfg = connector.get_guild_config(ctx.guild.id)
        tod_channel = cfg.get("tod_channel")

        if tod_channel is not None and ctx.channel.id != tod_channel:
            await ctx.send(
                embed=discord.Embed(
                    title="Failed to Run Command",
                    description=(
                        f"Can only run commands in <#{tod_channel}>"
                    ),
                    colour=discord.Colour.red(),
                )
            )

            return False

        return True

    # ------------------------------------------------------------------
    # game commands
    # ------------------------------------------------------------------

    @commands.group(name="tod", invoke_without_command=True)
    async def tod(self, ctx: commands.Context) -> None:
        """Create a TOD lobby."""
        async with ctx.typing():
            guild_id = self._get_guild_id(ctx)
            if guild_id is None:
                return

            if guild_id in self.games:
                await ctx.send(
                    embed=discord.Embed(
                        title="Failed",
                        description="A TOD game is already running.",
                        colour=discord.Colour.red(),
                    )
                )
                return

            try:
                session = self._create_guild_session(guild_id, ctx.author.id)
            except GameAlreadyExists:
                await ctx.send(
                    embed=discord.Embed(
                        title="Failed",
                        description="A TOD game is already running.",
                        colour=discord.Colour.red(),
                    )
                )
                return
            except TOD_ENGINE_EXCEPTIONS as exc:
                await ctx.send(
                    embed=discord.Embed(
                        title="Failed",
                        description=str(exc),
                        colour=discord.Colour.red(),
                    )
                )
                return

            self.games[guild_id] = session

            connector = self._get_connector()
            cfg = (
                connector.get_guild_config(guild_id)
                if connector is not None
                else {"tod_role": None}
            )
            tod_role = cfg.get("tod_role")
            ping = f"<@&{tod_role}>" if tod_role else ""

            embed = self.build_lobby_embed(
                guild_id,
                "TOD Lobby Created",
            )

            try:
                session["message"] = await ctx.send(
                    f"|| {ping} ||" if ping else None,
                    embed=embed,
                )
            except discord.HTTPException:
                session["message"] = None

    @tod.command(name="help")
    async def tod_help(self, ctx: commands.Context) -> None:
        """Show available TOD commands."""
        async with ctx.typing():
            await ctx.send(
                embed=discord.Embed(
                    title="TOD Help",
                    description=(
                        "`+tod join` - join game\n"
                        "`+tod leave` - leave game\n"
                        "`+tod start` - start game (session owner only)\n"
                        "`+tod end` - end game (session owner/admin only)\n"
                        "`+tod next` - next player's turn (session owner only)\n"
                        "`+tod owner <user>` - transfer session ownership\n"
                        "`+tod room` - view current TOD session details\n"
                        "`+tod kick <user>` - remove a player\n"
                        "`+tod reroll` - reroll the current prompt\n"
                        "`+tod truths` - list available truths\n"
                        "`+tod dares` - list available dares\n"
                        "`+tod ban <id>` - ban a prompt (manage server)\n"
                        "`+tod unban <id>` - unban a prompt (manage server)\n"
                        "`+tod add truth <prompt>` - add a custom truth\n"
                        "`+tod add dare <prompt>` - add a custom dare"
                    ),
                    colour=discord.Colour.blue(),
                )
            )

    @tod.command(name="room")
    async def tod_room(self, ctx: commands.Context) -> None:
        """View details about the current TOD session."""
        async with ctx.typing():
            guild_id = self._get_guild_id(ctx)
            if guild_id is None:
                return

            if guild_id not in self.games:
                await ctx.send("No TOD game is running.")
                return

            await ctx.send(embed=self.build_lobby_embed(guild_id))

    @tod.command(name="next")
    async def tod_next(self, ctx: commands.Context) -> None:
        """Advance to the next player's turn."""
        async with ctx.typing():
            guild_id = self._get_guild_id(ctx)
            if guild_id is None:
                return

            game = self.games.get(guild_id)
            if game is None:
                await ctx.send("No active game.")
                return

            if ctx.author.id != game["session_owner"]:
                await ctx.send(
                    "Only session owner can advance turns."
                )
                return

            engine: TODEngine = game["engine"]
            state = engine.game

            if state is None or not state.is_running:
                await ctx.send(
                    "Game hasn't been started properly."
                )
                return

            if game["waiting_for_response"]:
                await ctx.send(
                    "Already waiting for a player's response."
                )
                return

            try:
                engine.advance_turn()
            except GameDoesNotExist:
                await ctx.send("No active game.")
                return
            except GameNotRunning:
                await ctx.send(
                    "Game hasn't been started properly."
                )
                return
            except InsufficientPlayers:
                await ctx.send(
                    "Cannot advance the turn with zero players."
                )
                return
            except TOD_ENGINE_EXCEPTIONS as exc:
                await ctx.send(str(exc))
                return

            updated_state = engine.game
            if updated_state is None or not updated_state.players:
                await ctx.send(
                    "Cannot advance the turn with zero players."
                )
                return

            player = updated_state.players[
                updated_state.current_player_index
            ].id

            await self.run_turn(ctx, player)

    @tod.command(name="start")
    async def tod_start(self, ctx: commands.Context) -> None:
        """Start the TOD game."""
        async with ctx.typing():
            guild_id = self._get_guild_id(ctx)
            if guild_id is None:
                return

            game = self.games.get(guild_id)
            if game is None:
                await ctx.send("No available room.")
                return

            if ctx.author.id != game["session_owner"]:
                await ctx.send("Only session owner can start.")
                return

            engine: TODEngine = game["engine"]

            try:
                engine.start_game()
            except GameDoesNotExist:
                await ctx.send("No available room.")
                return
            except InsufficientPlayers:
                await ctx.send("Need at least 1 player.")
                return
            except GameAlreadyRunning:
                await ctx.send("Game has already been started.")
                return
            except TOD_ENGINE_EXCEPTIONS as exc:
                await ctx.send(str(exc))
                return

            state = engine.game
            if state is None or not state.players:
                await ctx.send("Need at least 1 player.")
                return

            player = state.players[state.current_player_index].id

            await self.run_turn(ctx, player)

    @tod.command(name="end")
    async def tod_end(self, ctx: commands.Context) -> None:
        """End the running TOD session."""
        async with ctx.typing():
            guild_id = self._get_guild_id(ctx)
            if guild_id is None:
                return

            game = self.games.get(guild_id)
            if game is None:
                await ctx.send("No running game.")
                return

            is_owner = ctx.author.id == game["session_owner"]
            is_admin = bool(
                ctx.author.guild_permissions.administrator
                if hasattr(ctx.author, "guild_permissions")
                else False
            )

            if not (is_owner or is_admin):
                await ctx.send(
                    "You don't have permission to end this game."
                )
                return

            engine: TODEngine = game["engine"]
            try:
                engine.end_game()
            except GameDoesNotExist:
                pass
            except TOD_ENGINE_EXCEPTIONS as exc:
                await ctx.send(str(exc))
                return

            await self._safe_edit_lobby(
                game,
                discord.Embed(
                    title="Game Ended",
                    description="TOD session has been ended.",
                    colour=discord.Colour.red(),
                ),
            )

            self.games.pop(guild_id, None)

            await ctx.send("Game ended.")

    @tod.command(name="join")
    async def tod_join(self, ctx: commands.Context) -> None:
        """Join a running TOD session."""
        async with ctx.typing():
            guild_id = self._get_guild_id(ctx)
            if guild_id is None:
                return

            game = self.games.get(guild_id)
            if game is None:
                await ctx.send("No TOD game is running.")
                return

            engine: TODEngine = game["engine"]
            try:
                engine.add_player(Player(id=ctx.author.id))
            except GameDoesNotExist:
                await ctx.send("No TOD game is running.")
                return
            except PlayerAlreadyExists:
                await ctx.send("You are already in the game.")
                return
            except TOD_ENGINE_EXCEPTIONS as exc:
                await ctx.send(str(exc))
                return

            await self._safe_edit_lobby(
                game,
                self.build_lobby_embed(guild_id),
            )

            await ctx.send(f"{ctx.author.mention} joined.")

    @tod.command(name="leave")
    async def tod_leave(self, ctx: commands.Context) -> None:
        """Leave a running TOD session."""
        async with ctx.typing():
            guild_id = self._get_guild_id(ctx)
            if guild_id is None:
                return

            game = self.games.get(guild_id)
            if game is None:
                await ctx.send("No TOD game is running.")
                return

            engine: TODEngine = game["engine"]
            state = engine.game
            player = Player(id=ctx.author.id)

            if state is None or player not in state.players:
                await ctx.send("You're not in this game.")
                return

            if ctx.author.id == game["session_owner"]:
                await ctx.send(
                    "Session owner cannot leave the game."
                )
                return

            try:
                engine.remove_player(player)
            except GameDoesNotExist:
                await ctx.send("No TOD game is running.")
                return
            except PlayerDoesNotExist:
                await ctx.send("You're not in this game.")
                return
            except InsufficientPlayers as exc:
                await ctx.send(str(exc))
                return
            except TOD_ENGINE_EXCEPTIONS as exc:
                await ctx.send(str(exc))
                return

            await self._safe_edit_lobby(
                game,
                self.build_lobby_embed(guild_id),
            )

            await ctx.send(f"{ctx.author.mention} left.")

    @tod.command(name="owner")
    async def transfer_ownership(
        self,
        ctx: commands.Context,
        user: discord.Member = None,
    ) -> None:
        """Transfer session ownership."""
        async with ctx.typing():
            guild_id = self._get_guild_id(ctx)
            if guild_id is None:
                return

            game = self.games.get(guild_id)
            if game is None:
                await ctx.send(":x: There is no running game.")
                return

            if ctx.author.id != game["session_owner"]:
                await ctx.send(
                    ":x: Only the session owner can run this command."
                )
                return

            if user is None:
                await ctx.send(":x: Must mention a user.")
                return

            engine: TODEngine = game["engine"]
            state = engine.game
            players = state.players if state is not None else []

            if all(p.id != user.id for p in players):
                await ctx.send(
                    ":x: User must be a player in the running game."
                )
                return

            if user.id == game["session_owner"]:
                await ctx.send(
                    ":x: That user is already the session owner."
                )
                return

            game["session_owner"] = user.id

            await self._safe_edit_lobby(
                game,
                self.build_lobby_embed(guild_id),
            )

            await ctx.send(
                f"Ownership transferred to <@{user.id}>."
            )

    @tod.command(name="kick")
    async def tod_kick(
        self,
        ctx: commands.Context,
        user: discord.Member = None,
    ) -> None:
        """Remove a player from a running TOD session."""
        async with ctx.typing():
            guild_id = self._get_guild_id(ctx)
            if guild_id is None:
                return

            game = self.games.get(guild_id)
            if game is None:
                await ctx.send(":x: There is no running game.")
                return

            if ctx.author.id != game["session_owner"]:
                await ctx.send(
                    ":x: Only the session owner can run this command."
                )
                return

            if user is None:
                await ctx.send(":x: Must mention a user.")
                return

            engine: TODEngine = game["engine"]
            state = engine.game
            target_player = Player(id=user.id)

            if state is None or target_player not in state.players:
                await ctx.send(
                    ":x: User must be a player in the running game."
                )
                return

            if user.id == game["session_owner"]:
                await ctx.send(
                    ":x: Session owner cannot be kicked."
                )
                return

            try:
                engine.remove_player(target_player)
            except PlayerDoesNotExist:
                await ctx.send(
                    ":x: User must be a player in the running game."
                )
                return
            except InsufficientPlayers as exc:
                await ctx.send(f":x: {exc}")
                return
            except TOD_ENGINE_EXCEPTIONS as exc:
                await ctx.send(f":x: {exc}")
                return

            await self._safe_edit_lobby(
                game,
                self.build_lobby_embed(guild_id),
            )

            await ctx.send(
                f"<@{user.id}> has been removed from running game."
            )

    # ------------------------------------------------------------------
    # prompt browsing / management
    # ------------------------------------------------------------------

    @tod.command(name="truths")
    async def tod_truths(self, ctx: commands.Context) -> None:
        """List available truths."""
        pages = self.build_prompt_list_pages(ctx, "truth")
        await self.paginate(ctx, pages)

    @tod.command(name="dares")
    async def tod_dares(self, ctx: commands.Context) -> None:
        """List available dares."""
        pages = self.build_prompt_list_pages(ctx, "dare")
        await self.paginate(ctx, pages)

    @tod.command(name="ban")
    @commands.has_permissions(manage_guild=True)
    async def tod_ban(
        self,
        ctx: commands.Context,
        prompt_id: int,
    ) -> None:
        """Ban a prompt from this server's pool."""
        connector = self._get_connector()
        guild_id = self._get_guild_id(ctx)
        if connector is None or guild_id is None:
            return

        if not connector.is_prompt_visible_to_guild(guild_id, prompt_id):
            await ctx.send(
                embed=discord.Embed(
                    title="Error",
                    description=f"No prompt found with ID `#{prompt_id}`.",
                    colour=discord.Colour.red(),
                )
            )
            return

        if connector.is_prompt_banned(guild_id, prompt_id):
            await ctx.send(
                embed=discord.Embed(
                    title="Error",
                    description=f"Prompt `#{prompt_id}` is already banned.",
                    colour=discord.Colour.red(),
                )
            )
            return

        try:
            await connector.ban_prompt(
                guild_id,
                prompt_id,
                banned_by=ctx.author.id,
            )
        except Exception as exc:
            await ctx.send(
                embed=discord.Embed(
                    title="Error",
                    description=f"Failed to ban prompt: {exc}",
                    colour=discord.Colour.red(),
                )
            )
            return

        await ctx.send(
            embed=discord.Embed(
                title="Success",
                description=(
                    f"Prompt `#{prompt_id}` banned for this server."
                ),
                colour=discord.Colour.green(),
            )
        )

    @tod.command(name="unban")
    @commands.has_permissions(manage_guild=True)
    async def tod_unban(
        self,
        ctx: commands.Context,
        prompt_id: int,
    ) -> None:
        """Unban a prompt for this server's pool."""
        connector = self._get_connector()
        guild_id = self._get_guild_id(ctx)
        if connector is None or guild_id is None:
            return

        if not connector.is_prompt_visible_to_guild(guild_id, prompt_id):
            await ctx.send(
                embed=discord.Embed(
                    title="Error",
                    description=f"No prompt found with ID `#{prompt_id}`.",
                    colour=discord.Colour.red(),
                )
            )
            return

        if not connector.is_prompt_banned(guild_id, prompt_id):
            await ctx.send(
                embed=discord.Embed(
                    title="Error",
                    description=f"Prompt `#{prompt_id}` is not banned.",
                    colour=discord.Colour.red(),
                )
            )
            return

        try:
            await connector.unban_prompt(guild_id, prompt_id)
        except Exception as exc:
            await ctx.send(
                embed=discord.Embed(
                    title="Error",
                    description=f"Failed to unban prompt: {exc}",
                    colour=discord.Colour.red(),
                )
            )
            return

        await ctx.send(
            embed=discord.Embed(
                title="Success",
                description=(
                    f"Prompt `#{prompt_id}` unbanned for this server."
                ),
                colour=discord.Colour.green(),
            )
        )

    @tod.group(name="add", invoke_without_command=True)
    async def tod_add(self, ctx: commands.Context) -> None:
        """Show add-related subcommands."""
        await ctx.send_help(ctx.command)

    @tod_add.command(name="truth")
    @commands.has_permissions(manage_guild=True)
    async def add_truth(
        self,
        ctx: commands.Context,
        *,
        prompt: str,
    ) -> None:
        """Add a custom truth for this server."""
        connector = self._get_connector()
        guild_id = self._get_guild_id(ctx)
        if connector is None or guild_id is None:
            return

        try:
            new_id = await connector.add_prompt(
                guild_id,
                "truth",
                prompt,
                added_by=ctx.author.id,
            )
        except Exception as exc:
            await ctx.send(
                embed=discord.Embed(
                    title="Error",
                    description=f"Failed to add truth: {exc}",
                    colour=discord.Colour.red(),
                )
            )
            return

        await ctx.send(
            embed=discord.Embed(
                title="Success",
                description=f"Truth added with ID `#{new_id}`.",
                colour=discord.Colour.green(),
            )
        )

    @tod_add.command(name="dare")
    @commands.has_permissions(manage_guild=True)
    async def add_dare(
        self,
        ctx: commands.Context,
        *,
        prompt: str,
    ) -> None:
        """Add a custom dare for this server."""
        connector = self._get_connector()
        guild_id = self._get_guild_id(ctx)
        if connector is None or guild_id is None:
            return

        try:
            new_id = await connector.add_prompt(
                guild_id,
                "dare",
                prompt,
                added_by=ctx.author.id,
            )
        except Exception as exc:
            await ctx.send(
                embed=discord.Embed(
                    title="Error",
                    description=f"Failed to add dare: {exc}",
                    colour=discord.Colour.red(),
                )
            )
            return

        await ctx.send(
            embed=discord.Embed(
                title="Success",
                description=f"Dare added with ID `#{new_id}`.",
                colour=discord.Colour.green(),
            )
        )

    # ------------------------------------------------------------------
    # reroll
    # ------------------------------------------------------------------

    @tod.command(name="reroll")
    async def reroll(self, ctx: commands.Context) -> None:
        """Reroll the selected prompt for the current player."""
        guild_id = self._get_guild_id(ctx)
        if guild_id is None:
            return

        game = self.games.get(guild_id)
        if game is None:
            await ctx.send("No active game.")
            return

        if ctx.author.id != game["session_owner"]:
            await ctx.send(
                "Only the session owner can reroll prompts."
            )
            return

        engine: TODEngine = game["engine"]
        state = engine.game

        if state is None or not state.is_running or not state.players:
            await ctx.send(
                "Game hasn't been started properly."
            )
            return

        if game["waiting_for_response"]:
            await ctx.send(
                "The current player hasn't selected Truth or Dare yet."
            )
            return

        if game.get("last_choice") not in (
            PromptType.TRUTH,
            PromptType.DARE,
            "truth",
            "dare",
        ):
            await ctx.send(
                "No prompt has been selected for the current turn."
            )
            return

        player = state.players[state.current_player_index].id

        await self.reroll_prompt(ctx, player)


async def setup(bot: commands.Bot):
    await bot.add_cog(TOD(bot))
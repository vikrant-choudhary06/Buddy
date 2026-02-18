"""
Buddy - Main Entry Point
AI-Enhanced Discord Bot for Community Management
"""

import discord
from discord.ext import commands
import asyncio
import logging
import os
import socket
import sys
import warnings
from contextlib import suppress
from pathlib import Path
import yaml
from dotenv import load_dotenv

from database.db_manager import DatabaseManager
from utils.logger import BotLogger
from utils.embeds import EmbedColor

# Load environment variables
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / '.env'
load_dotenv(dotenv_path=ENV_FILE)


try:
    from cryptography.utils import CryptographyDeprecationWarning
except Exception:  # pragma: no cover - cryptography always available with pymongo TLS
    CryptographyDeprecationWarning = DeprecationWarning

warnings.filterwarnings(
    "ignore",
    message=r"Parsed a serial number which wasn't positive",
    category=CryptographyDeprecationWarning,
    module=r"pymongo\.pyopenssl_context",
)


def parse_bool(value, default: bool = False) -> bool:
    """Parse bool-like values from config/env safely."""
    if isinstance(value, bool):
        return value

    if value is None:
        return default

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n"}:
            return False

    return default


def get_bool_setting(config_value, env_name: str, default: bool = False) -> bool:
    """Read boolean setting with environment variable override."""
    env_value = os.getenv(env_name)
    if env_value is not None:
        return parse_bool(env_value, default=default)
    return parse_bool(config_value, default=default)


def is_dns_resolution_error(exc: BaseException) -> bool:
    """Check exception chain for DNS resolution failures."""
    current = exc
    visited = set()

    while current and id(current) not in visited:
        visited.add(id(current))

        if isinstance(current, socket.gaierror):
            return True

        message = str(current).lower()
        if "getaddrinfo failed" in message or "name or service not known" in message:
            return True

        current = current.__cause__ or current.__context__

    return False


def is_address_in_use_error(exc: BaseException) -> bool:
    """Check exception chain for port/address already-in-use errors."""
    current = exc
    visited = set()

    while current and id(current) not in visited:
        visited.add(id(current))

        if isinstance(current, OSError) and getattr(current, 'errno', None) in {48, 98, 10048}:
            return True

        message = str(current).lower()
        if "address already in use" in message or "only one usage of each socket address" in message:
            return True

        current = current.__cause__ or current.__context__

    return False


def is_tcp_port_available(host: str, port: int) -> bool:
    """Return True if host:port appears available for a TCP listener."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False

    family = socket.AF_INET6 if ':' in host else socket.AF_INET

    probe_host = host
    if host == '0.0.0.0':
        probe_host = '127.0.0.1'
    elif host == '::':
        probe_host = '::1'

    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex((probe_host, port)) == 0:
                return False
    except OSError:
        # Continue to bind-check below; probe failures can be environment-specific.
        pass

    try:
        with socket.socket(family, socket.SOCK_STREAM) as tester:
            tester.bind((host, port))
        return True
    except OSError as exc:
        if is_address_in_use_error(exc):
            return False
        return True


class Buddy(commands.Bot):
    """Custom bot class"""

    def __init__(self, config: dict):
        """Initialize bot"""
        # Setup intents (privileged intents default to disabled)
        bot_config = config.get('bot', {})
        intent_config = bot_config.get('intents', {})

        intents = discord.Intents.default()
        intents.message_content = get_bool_setting(
            intent_config.get('message_content', False),
            'DISCORD_INTENT_MESSAGE_CONTENT',
            default=False
        )
        intents.members = get_bool_setting(
            intent_config.get('members', False),
            'DISCORD_INTENT_MEMBERS',
            default=False
        )
        intents.presences = get_bool_setting(
            intent_config.get('presences', False),
            'DISCORD_INTENT_PRESENCES',
            default=False
        )

        # Initialize bot
        super().__init__(
            command_prefix=bot_config.get('prefix', '/'),
            intents=intents,
            help_command=None
        )

        self.config = config
        self.start_time = discord.utils.utcnow()

        # Setup logging
        self.logger = BotLogger(config.get('logging', {}))

        # Setup database
        db_config = config.get('database', {})
        mongodb_uri = os.getenv('MONGODB_URI', db_config.get('mongodb_uri', 'mongodb://localhost:27017'))
        database_name = db_config.get('database_name', 'Buddy')
        pool_size = db_config.get('pool_size', 10)

        self.db = DatabaseManager(mongodb_uri, database_name, pool_size)

    async def setup_hook(self):
        """Setup hook - called when bot is starting"""
        self.logger.info("Starting Buddy...")
        self.logger.info(
            f"Intents: message_content={self.intents.message_content}, "
            f"members={self.intents.members}, presences={self.intents.presences}"
        )
        self._warn_for_disabled_required_intents()

        # Connect to database
        try:
            await self.db.connect()
            self.logger.info("Database connected successfully")
        except Exception as e:
            self.logger.error(f"Failed to connect to database: {e}", exc_info=True)
            sys.exit(1)

        # Load cogs
        await self.load_cogs()

    async def load_cogs(self):
        """Load all cogs from cogs directory"""
        cogs_dir = Path(__file__).parent / 'cogs'
        cog_files = [f.stem for f in cogs_dir.glob('*.py') if f.stem != '__init__']

        self.logger.info(f"Loading {len(cog_files)} cogs...")

        for cog in cog_files:
            try:
                await self.load_extension(f'cogs.{cog}')
                self.logger.cog_load(cog)
            except Exception as e:
                self.logger.error(f"Failed to load cog {cog}: {e}", exc_info=True)

        self.logger.info(f"Successfully loaded {len(self.cogs)} cogs")

    async def on_ready(self):
        """Called when bot is ready"""
        self.logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        self.logger.info(f"Connected to {len(self.guilds)} guilds")
        self.logger.info(f"Serving {sum(g.member_count for g in self.guilds)} users")

        # Set status
        activity_type = self.config['bot'].get('activity_type', 'watching')
        activity_text = self.config['bot'].get('activity', 'your community')

        activity_types = {
            'playing': discord.ActivityType.playing,
            'watching': discord.ActivityType.watching,
            'listening': discord.ActivityType.listening,
            'streaming': discord.ActivityType.streaming
        }

        activity = discord.Activity(
            type=activity_types.get(activity_type, discord.ActivityType.watching),
            name=activity_text
        )

        await self.change_presence(activity=activity, status=discord.Status.online)

        # Sync commands
        try:
            synced = await self.tree.sync()
            self.logger.info(f"Synced {len(synced)} commands")
        except Exception as e:
            self.logger.error(f"Failed to sync commands: {e}", exc_info=True)

        self.logger.info("Bot is ready!")

    async def on_command_error(self, ctx, error):
        """Global error handler"""
        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You don't have permission to use this command.")
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing required argument: {error.param}")
            return

        self.logger.error(f"Command error: {error}", exc_info=True)

    async def on_error(self, event, *args, **kwargs):
        """Global error handler for events"""
        self.logger.error(f"Error in event {event}", exc_info=True)

    async def close(self):
        """Cleanup when bot is shutting down"""
        self.logger.info("Shutting down bot...")
        await self.db.disconnect()
        await super().close()

    def _warn_for_disabled_required_intents(self):
        """Warn if enabled modules rely on disabled privileged intents."""
        module_config = self.config.get('modules', {})

        def enabled(module_name: str) -> bool:
            return module_config.get(module_name, {}).get('enabled', True)

        if not self.intents.message_content:
            message_modules = [m for m in ('leveling', 'moderation', 'analytics', 'ai_chat') if enabled(m)]
            if message_modules:
                self.logger.warning(
                    "Message Content intent is disabled. Message-based features may not work for modules: "
                    + ", ".join(message_modules)
                )

        if not self.intents.members:
            member_modules = [m for m in ('verification', 'analytics') if enabled(m)]
            if member_modules:
                self.logger.warning(
                    "Members intent is disabled. Join/leave/member features may not work for modules: "
                    + ", ".join(member_modules)
                )


def load_config(config_path: str = 'config.yaml') -> dict:
    """Load configuration from YAML file"""
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Replace environment variables
        def replace_env_vars(obj):
            """Recursively replace ${ENV_VAR} with actual values"""
            if isinstance(obj, dict):
                return {k: replace_env_vars(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [replace_env_vars(item) for item in obj]
            elif isinstance(obj, str) and obj.startswith('${') and obj.endswith('}'):
                env_var = obj[2:-1]
                return os.getenv(env_var, obj)
            return obj

        return replace_env_vars(config)

    except FileNotFoundError:
        print(f"Error: Config file '{config_path}' not found")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing config file: {e}")
        sys.exit(1)


async def start_web_server(bot: Buddy, shutdown_event: asyncio.Event):
    """Start web dashboard (if enabled)."""
    if not bot.config.get('web', {}).get('enabled', False):
        return

    try:
        from web.api import create_app
        import uvicorn

        app = create_app(bot)
        web_config = bot.config.get('web', {})
        host = web_config.get('host', '0.0.0.0')
        port = int(web_config.get('port', 8000))

        if not is_tcp_port_available(host, port):
            bot.logger.warning(
                f"Web server skipped: {host}:{port} is already in use. "
                "Use a different web.port or stop the process using that port."
            )
            return

        config = uvicorn.Config(app, host=host, port=port, log_level='info')
        server = uvicorn.Server(config)

        bot.logger.info(f"Starting web server on {host}:{port}")

        serve_task = asyncio.create_task(server.serve())
        stop_task = asyncio.create_task(shutdown_event.wait())

        try:
            done, pending = await asyncio.wait(
                {serve_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED
            )

            if stop_task in done:
                server.should_exit = True

            try:
                await serve_task
            except SystemExit as e:
                if e.code not in (0, None):
                    bot.logger.warning(
                        f"Web server exited early on {host}:{port}. Continuing without web dashboard."
                    )
                return
        finally:
            for task in (serve_task, stop_task):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    except asyncio.CancelledError:
        bot.logger.info('Web server task cancelled')
        raise
    except ImportError:
        bot.logger.warning('Web server dependencies not installed. Skipping web server.')
    except Exception as e:
        if is_address_in_use_error(e):
            bot.logger.warning(
                f"Web server skipped: {host}:{port} is already in use. "
                "Use a different web.port or stop the process using that port."
            )
            return
        bot.logger.error(f'Error starting web server: {e}', exc_info=True)
    except SystemExit as e:
        if e.code not in (0, None):
            bot.logger.warning(
                'Web server exited unexpectedly. Continuing without web dashboard.'
            )


async def main():
    """Main entry point"""
    # Load configuration
    config = load_config()

    # Get bot token
    token = os.getenv('DISCORD_BOT_TOKEN')
    if token is not None:
        token = token.strip()

    if not token:
        config_token = config['bot'].get('token')
        if isinstance(config_token, str):
            config_token = config_token.strip()
        if config_token and not config_token.startswith('${'):
            token = config_token

    if not token:
        env_hint = str(ENV_FILE)
        print(
            "Error: DISCORD_BOT_TOKEN is missing or empty. "
            f"Checked .env at {env_hint} and config.yaml bot.token."
        )
        sys.exit(1)

    # Create and start bot
    bot = Buddy(config)
    web_task = None
    web_shutdown_event = asyncio.Event()

    async with bot:
        try:
            # Start web server in background if enabled
            if config.get('web', {}).get('enabled', False):
                web_task = asyncio.create_task(start_web_server(bot, web_shutdown_event))

            # Start bot
            await bot.start(token)
        except discord.errors.PrivilegedIntentsRequired as e:
            error_msg = (
                "Privileged intents requested but not enabled in Discord Developer Portal. "
                "Enable the same intents in the portal, or set bot.intents.* to false "
                "in config.yaml / DISCORD_INTENT_* environment variables."
            )
            bot.logger.error(error_msg)
            raise RuntimeError(error_msg) from e
        except Exception as e:
            if is_dns_resolution_error(e):
                error_msg = (
                    "DNS lookup failed while connecting to discord.com:443. "
                    "This is a network/DNS issue on this machine (not a cog/code bug). "
                    "Check DNS/VPN/proxy/firewall settings and retry."
                )
                bot.logger.error(error_msg)
                raise RuntimeError(error_msg) from e
            raise
        finally:
            if web_task:
                web_shutdown_event.set()
                with suppress(asyncio.CancelledError):
                    await web_task


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)





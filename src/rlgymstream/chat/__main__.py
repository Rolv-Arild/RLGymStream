"""Run the Twitch chatbot as a standalone process.

Usage:
    python -m rlgymstream.chat

Reads the same rlgymstream.toml and database as the main process, but
connects to the overlay HTTP API for live match state instead of sharing
memory.  This makes the chatbot independently restartable and inspectable.
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rlgymstream.chat")


async def run_chatbot() -> None:
    from rlgymstream.chat.chatbot import RLGymStreamBot
    from rlgymstream.chat.overlay_proxy import OverlayStateProxy
    from rlgymstream.config import AppConfig
    from rlgymstream.db.database import Database

    config = AppConfig.from_toml()

    if not (config.twitch_channel and config.twitch_client_id and config.twitch_client_secret):
        logger.error(
            "Twitch chatbot requires twitch channel, client_id, and client_secret."
        )
        sys.exit(1)

    # Resolve bot_id / owner_id from channel name if needed
    if not config.twitch_bot_id or not config.twitch_owner_id:
        import twitchio

        async with twitchio.Client(
            client_id=config.twitch_client_id,
            client_secret=config.twitch_client_secret,
        ) as temp_client:
            await temp_client.login(load_tokens=False, save_tokens=False)
            users = await temp_client.fetch_users(logins=[config.twitch_channel])
            if users:
                channel_id = str(users[0].id)
                if not config.twitch_bot_id:
                    config.twitch_bot_id = channel_id
                    logger.info("Resolved bot_id: %s", channel_id)
                if not config.twitch_owner_id:
                    config.twitch_owner_id = channel_id
                    logger.info("Resolved owner_id: %s", channel_id)
            else:
                logger.error("Could not find Twitch user '%s'", config.twitch_channel)
                sys.exit(1)

    db = Database(config.db_path)

    overlay_url = f"http://{config.overlay_host}:{config.overlay_port}"
    overlay_proxy = OverlayStateProxy()
    chatbot = RLGymStreamBot(config, db, overlay_proxy)  # type: ignore[arg-type]

    logger.info("Chatbot starting for #%s (overlay: %s)", config.twitch_channel, overlay_url)

    try:
        async with chatbot:
            overlay_proxy.start(overlay_url)

            import os
            need_auth = not os.path.exists(".tio.tokens.json")
            if need_auth:
                logger.info(
                    "Authorize at http://localhost:4343/oauth"
                    "?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true"
                )

            await chatbot.start(with_adapter=need_auth)
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Chatbot fatal error")
    finally:
        await overlay_proxy.stop()
        logger.info("Chatbot process exiting")


def main() -> None:
    try:
        asyncio.run(run_chatbot())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

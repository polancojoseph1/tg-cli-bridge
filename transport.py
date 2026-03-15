"""Transport layer — swap between Telegram and WhatsApp at startup.

All of server.py imports handler functions from here instead of directly
from telegram_handler. Set TRANSPORT=whatsapp in .env to use WhatsApp.
"""

import os

_TRANSPORT = os.environ.get("TRANSPORT", "telegram").lower()

if _TRANSPORT == "whatsapp":
    from whatsapp_handler import (  # noqa: F401
        send_message,
        delete_message,
        send_voice,
        send_photo,
        send_video,
        send_chat_action,
        download_photo,
        download_document,
        register_webhook,
        delete_webhook,
        get_updates,
        close_client,
        register_bot_commands,
        send_inline_keyboard,
        answer_callback_query,
    )
else:
    from telegram_handler import (  # noqa: F401
        send_message,
        delete_message,
        send_voice,
        send_photo,
        send_video,
        send_chat_action,
        download_photo,
        download_document,
        register_webhook,
        delete_webhook,
        get_updates,
        close_client,
        register_bot_commands,
        send_inline_keyboard,
        answer_callback_query,
    )

from aiogram import Router

from . import (
    user,
    private_music,
    group_music,
    music,
    tiktok,
    youtube,
    spotify,
    admin,
    twitter,
    instagram,
    soundcloud,
    pinterest,
    threads,
    guest,
)
from .admin_history_dialog import admin_history_dialog

router = Router(name=__name__)

router.include_routers(
    user.router,
    private_music.router,
    guest.router,
    group_music.router,
    music.router,
    tiktok.router,
    youtube.router,
    spotify.router,
    admin.router,
    twitter.router,
    instagram.router,
    threads.router,
    soundcloud.router,
    pinterest.router,
    admin_history_dialog,
)

__all__ = [router]

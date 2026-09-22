"""Browser chat application built on the public Chat SDK."""

from .config import ServerConfig
from .service import ChatService

__all__ = ["ChatService", "ServerConfig"]

"""Launch the single-process browser chat server."""

import argparse
import logging
import signal
import sys
import threading

from .config import ConfigurationError, ServerConfig
from .http import ChatHTTPServer
from .service import ChatService


def main(argv=None):
    parser = argparse.ArgumentParser(description="OpenEvent browser chat server")
    parser.add_argument("--config", required=True, help="server JSON configuration file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--origin", help="public origin, required for an HTTPS reverse proxy")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    service = server = None
    exit_code = 1
    try:
        config = ServerConfig.load(args.config)
        service = ChatService(config)
        server = ChatHTTPServer((args.host, args.port), service, origin=args.origin)
        if threading.current_thread() is threading.main_thread():
            def stop(_signum, _frame):
                server.request_stop()
            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
        logging.info("chat server listening on %s:%s", args.host, server.server_port)
        server.serve_forever()
        exit_code = 0
    except ConfigurationError as exc:
        logging.error("chat configuration error: %s", exc)
    except Exception as exc:
        logging.error("chat server stopped during startup or serving (%s); check configuration and OpenEvent availability", type(exc).__name__)
    finally:
        if server is not None:
            server.server_close()
        if service is not None:
            service.close()
    return 1 if service is not None and service.fatal.is_set() else exit_code


if __name__ == "__main__":
    sys.exit(main())

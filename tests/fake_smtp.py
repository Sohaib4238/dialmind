"""A tiny SMTP server on 127.0.0.1 for the email tests: accepts AUTH PLAIN, stores every message.
Nothing is ever delivered anywhere."""
import base64
import email
import socketserver
import threading
from email import policy


class FakeSMTP:
    def __init__(self, user="u@test", password="pw"):
        self.user, self.password = user, password
        self.messages: list[dict] = []
        self.logins: list[str] = []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, line: str) -> None:
                self.wfile.write((line + "\r\n").encode())

            def handle(self) -> None:
                self.reply("220 fake-smtp ready")
                mail_from, rcpts = "", []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    cmd = line[:4].upper()
                    if cmd in ("EHLO", "HELO"):
                        self.wfile.write(b"250-fake-smtp\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n")
                    elif line.upper().startswith("AUTH PLAIN"):
                        parts = line.split(" ", 2)
                        blob = parts[2] if len(parts) > 2 else ""
                        _, user, pw = base64.b64decode(blob).decode().split("\0")
                        outer.logins.append(user)
                        self.reply("235 ok" if (user, pw) == (outer.user, outer.password) else "535 bad credentials")
                    elif cmd == "MAIL":
                        mail_from, rcpts = line.split(":", 1)[1].strip().strip("<>").split(">")[0], []
                        self.reply("250 ok")
                    elif cmd == "RCPT":
                        addr = line.split(":", 1)[1].strip().strip("<>")
                        if addr.startswith("refuse@"):
                            self.reply("550 no such user")
                        else:
                            rcpts.append(addr)
                            self.reply("250 ok")
                    elif cmd == "DATA":
                        self.reply("354 go ahead")
                        data = b""
                        while True:
                            chunk = self.rfile.readline()
                            if chunk in (b".\r\n", b".\n", b""):
                                break
                            data += chunk[1:] if chunk.startswith(b"..") else chunk
                        outer.messages.append({"from": mail_from, "to": rcpts,
                                               "msg": email.message_from_bytes(data, policy=policy.default)})
                        self.reply("250 queued")
                    elif cmd == "QUIT":
                        self.reply("221 bye")
                        return
                    else:  # RSET, NOOP, ...
                        self.reply("250 ok")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()

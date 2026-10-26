"""Loopback metering proxy for Judge model calls; runs inside the trial container.

Copied next to harbor_runtime.py as prt_judge_meter.py and uploaded per verification.
Stdlib only and Python 3.6 compatible: task images carry arbitrary interpreters.
Clients keep sending their own credentials; nothing here stores or logs headers,
URLs or bodies. Only token counters reach prt-model-usage.jsonl.
"""
import json
import os
import socket
import ssl
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib import error, request

HEALTH = "/__prt_judge_meter_health"
MAX_BODY = 64 * 1024 * 1024
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
       "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding", "expect"}


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def usage_metrics(usage):
    """Map provider usage to prt-usage-v1 metrics (prompt_tokens includes cache tokens)."""
    if not isinstance(usage, dict):
        return None
    creation_detail = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    if "prompt_tokens" in usage:
        # OpenAI Chat Completions: prompt_tokens already includes cached tokens.
        details = usage.get("prompt_tokens_details") or {}
        prompt, output = usage.get("prompt_tokens"), usage.get("completion_tokens")
        read = _int(details.get("cached_tokens") or usage.get("cache_read_input_tokens"))
        creation = _int(details.get("cache_creation_input_tokens") or usage.get("cache_creation_input_tokens"))
    elif "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        # Anthropic Messages: input_tokens excludes cache reads and writes.
        read = _int(usage.get("cache_read_input_tokens"))
        creation = _int(usage.get("cache_creation_input_tokens"))
        prompt = _int(usage.get("input_tokens")) + read + creation if "input_tokens" in usage else None
        output = usage.get("output_tokens")
    else:
        # OpenAI Responses (and Anthropic without cache fields): input_tokens includes cached tokens.
        prompt, output = usage.get("input_tokens"), usage.get("output_tokens")
        read = _int((usage.get("input_tokens_details") or {}).get("cached_tokens"))
        creation = 0
    if prompt is None or output is None or _int(prompt) < read + creation:
        return None
    return {"prompt_tokens": _int(prompt), "completion_tokens": _int(output), "cached_tokens": read,
            "extra": {"cache_creation_input_tokens": creation, "cache_creation": creation_detail}}


class Collector(object):
    """Accumulates identity and usage from a JSON body or SSE events."""

    def __init__(self, model):
        self.request_id = ""
        self.model = model
        self.usage = {}
        self.partial = b""

    def event(self, data):
        if not isinstance(data, dict):
            return
        body = data.get("response") if isinstance(data.get("response"), dict) else data
        body = data.get("message") if isinstance(data.get("message"), dict) else body
        if isinstance(body.get("id"), str) and body["id"] and not self.request_id:
            self.request_id = body["id"]
        if isinstance(body.get("model"), str) and body["model"]:
            self.model = body["model"]
        for usage in (body.get("usage"), data.get("usage")):
            if isinstance(usage, dict):
                # Anthropic streams split usage across message_start and message_delta.
                self.usage.update({key: value for key, value in usage.items() if value is not None})

    def feed(self, chunk):
        self.partial += chunk
        lines = self.partial.split(b"\n")
        self.partial = lines.pop()
        if len(self.partial) > MAX_BODY:
            self.partial = b""
        for line in lines:
            line = line.strip()
            if line.startswith(b"data:"):
                try:
                    self.event(json.loads(line[5:].strip().decode("utf-8")))
                except ValueError:
                    pass

    def write(self, path):
        metrics = usage_metrics(self.usage)
        if metrics is None:
            return
        payload = {"schema_version": "prt-usage-v1", "request_id": self.request_id or "meter-" + uuid.uuid4().hex,
                   "model": self.model, "timestamp": datetime.now(timezone.utc).isoformat(), "metrics": metrics}
        # One append syscall per response is safe across concurrent handler threads.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _prepare(path, body):
    """Ask OpenAI-compatible streams for their final usage chunk."""
    if not path.rstrip("/").endswith("/chat/completions") or not body:
        return body
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return body
    if isinstance(data, dict) and data.get("stream") is True and "stream_options" not in data:
        data["stream_options"] = {"include_usage": True}
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    return body


def _read_body(handler):
    if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
        parts, size = [], 0
        while True:
            length = int(handler.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if length == 0:
                while handler.rfile.readline().strip():
                    pass
                return b"".join(parts)
            size += length
            if size > MAX_BODY:
                raise ValueError("request body too large")
            parts.append(handler.rfile.read(length))
            handler.rfile.readline()
    length = int(handler.headers.get("Content-Length") or 0)
    if length > MAX_BODY:
        raise ValueError("request body too large")
    return handler.rfile.read(length) if length else b""


def make_handler(origin, usage_path, opener):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # one request per connection; body ends at close

        def log_message(self, *args):
            pass  # request lines may carry ?key= credentials

        def _forward(self):
            if self.path == HEALTH:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
                return
            try:
                body = _read_body(self)
            except ValueError:
                self.send_error(413)
                return
            model = ""
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
                model = (parsed.get("model") or "") if isinstance(parsed, dict) else ""
            except ValueError:
                pass
            body = _prepare(self.path.split("?", 1)[0], body)
            headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP}
            headers["Accept-Encoding"] = "identity"
            upstream = request.Request(origin + self.path, data=body if body or self.command != "GET" else None,
                                       headers=headers, method=self.command)
            try:
                response = opener.open(upstream, timeout=1800)
            except error.HTTPError as exc:
                response = exc
            except Exception as exc:
                sys.stderr.write("PRT judge meter: upstream unreachable: %s\n" % type(exc).__name__)
                self.send_error(502)
                return
            status = getattr(response, "status", None) or response.getcode()
            collector = Collector(model if isinstance(model, str) else "")
            streaming = "text/event-stream" in (response.headers.get("Content-Type") or "")
            buffered = []
            size = 0
            try:
                self.send_response(status)
                for key, value in response.headers.items():
                    if key.lower() not in HOP:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                read = getattr(response, "read1", None) or response.read
                while True:
                    chunk = read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    if streaming:
                        self.wfile.flush()
                        collector.feed(chunk)
                    elif size <= MAX_BODY:
                        buffered.append(chunk)
                        size += len(chunk)
            finally:
                response.close()
            if not 200 <= status < 300:
                return
            if streaming:
                collector.feed(b"\n")
            elif size <= MAX_BODY:
                try:
                    collector.event(json.loads(b"".join(buffered).decode("utf-8")))
                except ValueError:
                    return
            try:
                collector.write(usage_path)
            except Exception as exc:
                sys.stderr.write("PRT judge meter: cannot record usage: %s\n" % type(exc).__name__)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _forward

    return Handler


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def origin_of(base_url):
    from urllib.parse import urlsplit
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("JUDGE_BASE_URL must be an http(s) URL")
    return "%s://%s" % (parts.scheme, parts.netloc)


def tls_context():
    context = ssl.create_default_context()
    if not context.cert_store_stats().get("x509_ca"):
        # Slim images may lack system CAs while the Judge's own client ships certifi.
        try:
            import certifi
            context.load_verify_locations(certifi.where())
        except (ImportError, OSError):
            pass
    return context


def main():
    # Upstream comes from the environment so it never shows up in process listings.
    origin = origin_of(os.environ.get("PRT_JUDGE_METER_UPSTREAM", ""))
    state_path, usage_path = sys.argv[1], sys.argv[2]
    opener = request.build_opener(request.ProxyHandler(), NoRedirect(), request.HTTPSHandler(context=tls_context()))
    server = Server(("127.0.0.1", 0), make_handler(origin, usage_path, opener))
    try:
        start = open("/proc/self/stat").read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        start = ""
    state = {"pid": os.getpid(), "start": start, "port": server.server_address[1]}
    temporary = state_path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(state, handle)
    os.chmod(temporary, 0o600)
    os.replace(temporary, state_path)
    server.serve_forever()


if __name__ == "__main__":
    main()

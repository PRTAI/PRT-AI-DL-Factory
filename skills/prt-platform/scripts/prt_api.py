#!/usr/bin/env python3
"""Portable PRT API client. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ClientError(Exception):
    pass


SECRET_KEY = re.compile(r"password|api[_-]?key|(?:^|[_-])token(?:$|[_-])|access[_-]?token|refresh[_-]?token|authorization|secret|credential", re.I)


def secret_values(value: Any) -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEY.search(key) and isinstance(item, str) and len(item) >= 4:
                found.append(item)
            found.extend(secret_values(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(secret_values(item))
    return found


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {k: "***" if SECRET_KEY.search(k) and isinstance(v, str) else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "***")
        return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", value)
    return value


def normalize_base(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ClientError("平台地址须为无内嵌凭据的 http(s) URL")
    try:
        parsed.port
    except ValueError as exc:
        raise ClientError("平台端口无效") from exc
    parts = parsed.path.rstrip("/").split("/")
    ui = {"api", "login", "jobs", "projects", "datasets", "models", "deployments", "settings", "reviews", "dashboard"}
    for i, part in enumerate(parts):
        if part in ui:
            parts = parts[:i]
            break
    path = "/".join(parts).rstrip("/")
    if any(urllib.parse.unquote(p) in {".", ".."} for p in parts):
        raise ClientError("平台地址不能包含路径跳转")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("平台返回重定向，客户端不会转发凭据；请核对规范地址，写入请求应先确认是否已生效")


def api_url(base: str, path: str, query: list[tuple[str, str]] | None = None) -> str:
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/") or path.startswith("//") or parsed.fragment:
        raise ClientError("接口路径必须是平台内的绝对路径，如 /jobs；不接受其他主机")
    if any(p in {".", ".."} for p in urllib.parse.unquote(parsed.path).split("/")):
        raise ClientError("接口路径不能包含路径跳转")
    endpoint = parsed.path
    if endpoint == "/api" or endpoint.startswith("/api/"):
        endpoint = endpoint[4:]
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) + (query or [])
    return base + "/api" + endpoint + ("?" + urllib.parse.urlencode(pairs) if pairs else "")


def save_session(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o600)
        else:
            os.chmod(path, 0o600)
        json.dump(value, handle, ensure_ascii=False)


class Client:
    def __init__(self, base: str, token: str = "", timeout: float = 60):
        self.base = normalize_base(base)
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    @classmethod
    def from_session(cls, path: Path, timeout: float = 60) -> Client:
        try:
            if path.is_symlink():
                raise ClientError("拒绝从符号链接读取登录会话")
            if os.name == "posix" and path.stat().st_mode & 0o077:
                raise ClientError("会话文件权限过宽，请将权限设置为 0600")
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("expires_at", float("inf")) <= time.time():
                raise ClientError("登录会话已过期，请重新登录")
            return cls(value["base_url"], value["token"], timeout)
        except (OSError, ValueError, KeyError) as exc:
            raise ClientError("无法读取登录会话，请先 login") from exc

    def request(self, method: str, path: str, *, payload: Any = None,
                query: list[tuple[str, str]] | None = None, output: Path | None = None,
                body: Any = None, headers: dict[str, str] | None = None, schema: bool = False) -> Any:
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            raise ClientError("不支持的 HTTP 方法")
        secrets = tuple([self.token, *secret_values(payload)])
        request_headers = {"Accept": "application/json", **(headers or {})}
        if self.token:
            request_headers["Authorization"] = "Bearer " + self.token
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(api_url(self.base, path, query), data=body, headers=request_headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                if output is not None:
                    return self._download(response, output)
                kind = response.headers.get("Content-Type", "")
                if response.status == 204 or method == "HEAD":
                    return {"http_status": response.status}
                if "json" not in kind:
                    raise ClientError("接口返回非 JSON 内容；下载文件请指定 --output，若返回 HTML 请核对平台地址")
                value = json.loads(response.read())
                # OpenAPI property names describe fields, not stored credentials.
                if schema:
                    if not isinstance(value, dict) or not value.get("openapi"):
                        raise ClientError("响应不是 OpenAPI 文档")
                    return value
                return redact(value, secrets)
        except urllib.error.HTTPError as exc:
            detail = exc.read(24000).decode("utf-8", errors="replace")
            try:
                detail = json.dumps(redact(json.loads(detail), secrets), ensure_ascii=False)
            except ValueError:
                detail = redact(detail, secrets)
            hint = {401: "请重新登录，勿循环尝试密码", 403: "当前账号无权限", 409: "资源状态冲突，先 GET 核对", 422: "参数校验失败，请查询 schema"}.get(exc.code, "")
            raise ClientError(f"HTTP {exc.code} {hint}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.HTTPException, OSError) as exc:
            hint = "结果未知，请先查询资源确认是否已创建或修改，勿直接重复提交" if method not in {"GET", "HEAD"} else "可检查连接后重试读取"
            raise ClientError(f"请求中断（{type(exc).__name__}）；{hint}") from exc
        except ValueError as exc:
            raise ClientError("服务器返回了无法解析的 JSON") from exc

    @staticmethod
    def _download(response, output: Path) -> dict:
        created = False
        try:
            with output.open("xb") as handle:
                created = True
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += handle.write(chunk)
            expected = response.headers.get("Content-Length")
            if expected is not None and total != int(expected):
                raise ClientError("下载不完整，已移除临时结果")
            return {"output": str(output.resolve()), "bytes": total}
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise

    def login(self, email: str, password: str, session_path: Path) -> dict:
        # Login returns a token needed locally; do not pass it through stdout redaction.
        req = urllib.request.Request(api_url(self.base, "/auth/login"),
            data=json.dumps({"email": email, "password": password}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                value = json.load(response)
            token = value["access_token"]
            user = value["user"]
            save_session(session_path, {"base_url": self.base, "token": token,
                "expires_at": time.time() + value["expires_in"], "user": user})
            self.token = token
            return {"base_url": self.base, "user": redact(user), "session": str(session_path), "expires_in": value["expires_in"]}
        except urllib.error.HTTPError as exc:
            raise ClientError(f"登录失败 HTTP {exc.code}；请核对地址和凭据，勿自动反复尝试") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
            raise ClientError(f"登录未完成（{type(exc).__name__}），未输出或保存密码") from exc

    def upload(self, archive: Path, fields: dict[str, str], dataset_id: str | None = None) -> Any:
        boundary = "prt-" + uuid.uuid4().hex
        prefix = bytearray()
        for name, value in fields.items():
            if not re.fullmatch(r"[a-zA-Z0-9_]+", name):
                raise ClientError("无效的表单字段")
            prefix.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        filename = archive.name.replace('"', '_').replace("\\", "_")
        if "\r" in filename or "\n" in filename:
            raise ClientError("文件名不能包含换行")
        prefix.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
        end = f"\r\n--{boundary}--\r\n".encode()
        with archive.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            def chunks():
                yield bytes(prefix)
                while chunk := handle.read(1024 * 1024):
                    yield chunk
                yield end
            path = "/datasets" if dataset_id is None else "/datasets/" + urllib.parse.quote(dataset_id, safe="") + "/file"
            return self.request("POST" if dataset_id is None else "PUT", path, body=chunks(), headers={
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Content-Length": str(len(prefix) + size + len(end)),
            })


def schema_selection(document: dict, path: str | None, method: str, model: str | None, search: str) -> dict:
    if not document.get("openapi") or "paths" not in document:
        raise ClientError("响应不是 OpenAPI 文档")
    components = document.get("components", {}).get("schemas", {})
    if model:
        if model not in components:
            raise ClientError(f"OpenAPI 中没有模型 {model}")
        selected = components[model]
    elif path:
        if not path.startswith("/api/"):
            path = "/api" + path
        selected = document["paths"].get(path, {}).get(method.lower())
        if selected is None:
            raise ClientError("当前服务端未声明该接口或方法")
    else:
        words = search.lower().split()
        operations = []
        for route, methods in document["paths"].items():
            for verb, operation in methods.items():
                if verb not in {"get", "post", "put", "patch", "delete", "head"}:
                    continue
                text = route + " " + json.dumps(operation, ensure_ascii=False)
                if all(w in text.lower() for w in words):
                    operations.append({"method": verb.upper(), "path": route, "summary": operation.get("summary", "")})
        return {"version": document.get("info", {}).get("version"), "operations": operations}
    related = {}
    def collect(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[-1]
                if name not in related and name in components:
                    related[name] = components[name]
                    collect(components[name])
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(selected)
    return {"schema": selected, "components": related}


def wait_resource(client: Client, path: str, until: set[str], failure: set[str], *, interval: float, max_seconds: float) -> dict:
    deadline = time.monotonic() + max_seconds
    while True:
        original_timeout = client.timeout
        try:
            client.timeout = min(original_timeout, max(0.01, deadline - time.monotonic()))
            resource = client.request("GET", path)
        finally:
            client.timeout = original_timeout
        if not isinstance(resource, dict) or "status" not in resource:
            raise ClientError("wait 接口须返回含 status 的单个资源对象")
        status = resource.get("status")
        state = "completed" if status in until else "failed" if status in failure else "attention" if status == "pending_review" else "pending"
        if state != "pending" or time.monotonic() >= deadline:
            return {"wait_state": state, "resource": resource}
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def positive(value: str) -> float:
    number = float(value)
    if number <= 0 or not number < float("inf"):
        raise argparse.ArgumentTypeError("数值必须为有限正数")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, default=os.environ.get("PRT_PLATFORM_SESSION"))
    parser.add_argument("--timeout", type=positive, default=60)
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login")
    login.add_argument("--url", required=True)
    login.add_argument("--email", required=True)
    login.add_argument("--password-stdin", action="store_true")
    commands.add_parser("logout")
    request = commands.add_parser("request")
    request.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    request.add_argument("path")
    request.add_argument("--json", dest="json_file", help="JSON 文件或 -（标准输入）")
    request.add_argument("--query", action="append", default=[])
    request.add_argument("--output", type=Path)
    schema = commands.add_parser("schema")
    choice = schema.add_mutually_exclusive_group()
    choice.add_argument("--path")
    choice.add_argument("--model")
    choice.add_argument("--search", default="")
    schema.add_argument("--method", default="POST")
    upload = commands.add_parser("upload")
    upload.add_argument("archive", type=Path)
    upload.add_argument("--name")
    upload.add_argument("--kind")
    upload.add_argument("--project-id")
    upload.add_argument("--description", default="")
    upload.add_argument("--tag", action="append", default=[])
    upload.add_argument("--rewardkit", action="store_true")
    upload.add_argument("--replace", dest="dataset_id")
    wait = commands.add_parser("wait")
    wait.add_argument("path")
    wait.add_argument("--until", default="succeeded,serving,ready")
    wait.add_argument("--failure", default="failed,cancelled,rejected,invalid,stopped")
    wait.add_argument("--interval", type=positive, default=5)
    wait.add_argument("--max-seconds", type=positive, default=60)
    args = parser.parse_args(argv)
    if not args.session:
        parser.error("请指定当前任务专用的 --session 文件，或设置 PRT_PLATFORM_SESSION")
    try:
        if args.command == "login":
            password = sys.stdin.read().removesuffix("\n").removesuffix("\r") if args.password_stdin else getpass.getpass("平台密码: ")
            if not password:
                raise ClientError("密码为空")
            result = Client(args.url, timeout=args.timeout).login(args.email, password, args.session)
        elif args.command == "logout":
            args.session.unlink(missing_ok=True)
            result = {"local_session_removed": True, "server_token_revoked": False}
        else:
            client = Client.from_session(args.session, args.timeout)
            if args.command == "schema":
                result = schema_selection(client.request("GET", "/openapi.json", schema=True), args.path, args.method, args.model, args.search)
            elif args.command == "upload":
                if args.dataset_id:
                    fields = {"kind": args.kind} if args.kind else {}
                else:
                    if not all([args.name, args.kind, args.project_id]):
                        raise ClientError("上传新数据集需要 --name、--kind 和 --project-id")
                    fields = {"name": args.name, "kind": args.kind, "project_id": args.project_id,
                        "description": args.description, "tags": ",".join(args.tag), "rewardkit": str(args.rewardkit).lower()}
                result = client.upload(args.archive, fields, args.dataset_id)
            elif args.command == "wait":
                result = wait_resource(client, args.path, set(args.until.split(",")), set(args.failure.split(",")), interval=args.interval, max_seconds=args.max_seconds)
            else:
                payload = None
                if args.json_file:
                    raw = sys.stdin.read() if args.json_file == "-" else Path(args.json_file).read_text(encoding="utf-8")
                    payload = json.loads(raw)
                query = []
                for pair in args.query:
                    if "=" not in pair:
                        raise ClientError("--query 格式为 key=value，可重复指定")
                    query.append(tuple(pair.split("=", 1)))
                result = client.request(args.method, args.path, payload=payload, query=query, output=args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "wait":
            return {"completed": 0, "pending": 2, "attention": 2, "failed": 1}[result["wait_state"]]
        return 0
    except (ClientError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

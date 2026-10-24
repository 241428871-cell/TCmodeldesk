"""Model Desk MCP Apps backend.

Exposes the local Model Desk workbench to any local Codex project through the
official MCP Apps contract (stdio transport, official Python mcp SDK):

* ``open_model_desk`` - the only model-visible entry. It starts the existing
  loopback panel on explicit request and renders the workbench UI.
* ``panel_request`` - a UI-only proxy for a small whitelist of panel API calls.
  The internal panel session token never leaves this process.
* ``ui://model-desk/workbench-v1`` - the workbench UI, bundled from the panel's
  existing index.html / style.css / app.js plus the MCP host bridge.

Importing this module, initializing the transport, listing tools or reading the
resource NEVER starts the panel, dispatches workers, reads stored model keys or
changes settings. Only an explicit ``open_model_desk`` call, or the UI's first
``/api/session`` request, starts the service; a running service is reused.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.request import url2pathname
from urllib.parse import urlparse, unquote

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from pydantic import JsonValue
import project_scope
import panel_routes
import panel_bundle
from codex_usage import find_codex

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PANEL_DIR = HERE / "panel"
LAUNCHER = ROOT / "打开模型工作台.ps1"
BRIDGE_SCRIPT = "codex-bridge.js"

API_HOST = "127.0.0.1"
API_PORT = 8765
API_BASE = f"http://{API_HOST}:{API_PORT}"

RESOURCE_URI = "ui://model-desk/workbench-v2"
UI_MIME = "text/html;profile=mcp-app"

TOOL_OPEN = "open_model_desk"
TOOL_PANEL = "panel_request"
TOOL_GOAL_INTERACT = "goal_interact"

MAX_BODY_BYTES = 200_000
HTTP_TIMEOUT = 15
STARTUP_TIMEOUT = 25
DEFAULT_DUMMY_TOKEN = "mcp-bridge"


class PanelError(Exception):
    """A safe, user-facing failure that never carries secrets or raw dumps."""


# --------------------------------------------------------------------------- paths

def find_pwsh() -> str:
    """Absolute native PowerShell used to start the existing launcher."""
    from powershell_runtime import find_pwsh as resolve
    try:
        return resolve()
    except ValueError as error:
        raise PanelError(str(error)) from None


# --------------------------------------------------------------------- validation

_SAFE_SEGMENT = r"[A-Za-z0-9_-]+"
_SAFE_PATH = re.compile(r"/api/[A-Za-z0-9_./-]+")
_GET_EXACT = frozenset({"/api/state", "/api/workflow", "/api/codex-usage", "/api/session", "/api/reviews",
                        "/api/goals", "/api/stages", "/api/recovery", "/api/executor/capabilities",
                        "/api/tree", "/api/observability", "/api/projects", "/api/task-records",
                        "/api/db/space"})
_GET_GOAL = re.compile(rf"^/api/goals/{_SAFE_SEGMENT}$")
_GET_STAGE = re.compile(rf"^/api/stages/{_SAFE_SEGMENT}$")
_GET_TASK = re.compile(rf"^/api/tasks/{_SAFE_SEGMENT}(/log|/review|/handover|/observability)?$")
_GET_PICK = re.compile(r'^/api/projects/pick/[0-9a-f]{32}$')
_POST_EXACT = frozenset({"/api/workflow", "/api/codex-usage/refresh", "/api/route-preview",
                         "/api/models", "/api/discover", "/api/tasks", "/api/goals", "/api/stages", "/api/projects/pick",
                         "/api/task-records/preview", "/api/task-records/delete", "/api/projects/remember",
                         "/api/db/compact"})
_POST_MODEL = re.compile(rf"^/api/models/{_SAFE_SEGMENT}/(save|delete|test|default|balance|thinking)$")
_POST_ENDPOINT = re.compile(r'^/api/execution-endpoints/(pi|dsh|workbuddy)$')
_POST_CLI_UPDATE = re.compile(r'^/api/cli-updates/(dsh|claude_cli|codex_cli)/(check|update)$')
_POST_CANCEL = re.compile(rf"^/api/tasks/{_SAFE_SEGMENT}/cancel$")
_POST_RECOVER = re.compile(rf"^/api/tasks/{_SAFE_SEGMENT}/recover$")
_POST_REVIEW = re.compile(rf"^/api/tasks/{_SAFE_SEGMENT}/review$")
_POST_GOAL = re.compile(rf"^/api/goals/{_SAFE_SEGMENT}/status$")
_POST_STAGE = re.compile(rf"^/api/stages/{_SAFE_SEGMENT}/(dispatch|cancel|requeue|sync)$")
_POST_TASK_EXTRA = re.compile(rf"^/api/tasks/{_SAFE_SEGMENT}/(handover|reference)$")


def validate_request(path: JsonValue, method: JsonValue) -> str:
    """Return a whitelisted loopback path, or raise for anything else.

    Rejects query strings, fragments, percent encoding, absolute URLs,
    traversal, duplicated separators and unsupported methods.
    """
    if not isinstance(method, str) or method.upper() not in ("GET", "POST"):
        raise PanelError("只允许 GET 或 POST 请求。")
    if not isinstance(path, str) or not path:
        raise PanelError("接口路径格式不正确。")
    if panel_routes.accepts(path, method.upper()):
        return path
    if any(token in path for token in ("?", "#", "%", "\\", " ", "\t", "\n", "\r")):
        raise PanelError("接口路径不允许查询、片段或转义字符。")
    if not path.startswith("/api/") or "//" in path:
        raise PanelError("只支持工作台内部的相对接口路径。")
    if not _SAFE_PATH.fullmatch(path):
        raise PanelError("接口路径包含不支持的字符。")
    if ".." in path.split("/"):
        raise PanelError("接口路径不允许上级跳转。")
    if method.upper() == "GET":
        allowed = (path in _GET_EXACT or bool(_GET_TASK.fullmatch(path))
                   or bool(_GET_GOAL.fullmatch(path)) or bool(_GET_STAGE.fullmatch(path))
                   or bool(_GET_PICK.fullmatch(path)))
    else:
        allowed = (path in _POST_EXACT
                   or bool(_POST_ENDPOINT.fullmatch(path))
                   or bool(_POST_MODEL.fullmatch(path))
                   or bool(_POST_CANCEL.fullmatch(path))
                   or bool(_POST_RECOVER.fullmatch(path))
                   or bool(_POST_REVIEW.fullmatch(path))
                   or bool(_POST_GOAL.fullmatch(path))
                   or bool(_POST_STAGE.fullmatch(path))
                   or bool(_POST_TASK_EXTRA.fullmatch(path)))
    if not allowed:
        raise PanelError("该接口不在工作台允许范围内。")
    return path


# ------------------------------------------------------------------- panel access

class _NoRedirect(HTTPRedirectHandler):
    """Never follow a redirect; the panel is a fixed loopback endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: A002
        return None


def _opener():
    return build_opener(ProxyHandler({}), _NoRedirect)


def _decode(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"error": "模型工作台返回了无法解析的响应。"}
    return data if isinstance(data, dict) else {"error": "模型工作台返回了意外的响应结构。"}


def http_call(method: str, path: str, body=None, token: str | None = None, timeout: int = HTTP_TIMEOUT, view=None):
    """Call the panel API on the fixed loopback endpoint. No redirect, no retry."""
    payload = None
    headers = {}
    if view is not None:
        headers['X-Model-Desk-View']=project_scope.encode_view_header(view)
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["X-Panel-Token"] = token
    request = Request(API_BASE + path, data=payload, headers=headers, method=method)
    try:
        with _opener().open(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except HTTPError as error:
        return error.code, _decode(error.read(65536))
    except (OSError, ValueError) as error:
        raise PanelError("无法连接模型工作台服务，请先打开模型工作台。") from error


_TOKEN_LOCK = threading.Lock()
_TOKEN: dict[str, str | None] = {"value": None}


def panel_token(force: bool = False, rejected_token: str | None = None) -> str:
    """Internal only: the real panel session token never reaches the UI."""
    with _TOKEN_LOCK:
        # Another concurrent request may already have replaced the rejected
        # credential. Reuse that replacement instead of refreshing once per tab.
        if _TOKEN["value"] and (not force or
                (rejected_token is not None and _TOKEN['value'] != rejected_token)):
            return _TOKEN["value"]
        try:
            status, data = http_call("GET", "/api/session")
        except PanelError:
            raise PanelError("模型工作台服务未就绪，请先打开模型工作台。") from None
        token = data.get("token") if isinstance(data, dict) else None
        if status != 200 or not isinstance(token, str) or not token:
            raise PanelError("模型工作台未返回会话凭据，请重新打开模型工作台。")
        _TOKEN["value"] = token
        return token


def service_workspace(timeout: int = 3):
    """Return the workspace reported by the loopback service, or None."""
    try:
        status, data = http_call("GET", "/api/state", timeout=timeout)
    except PanelError:
        return None
    if status == 200 and isinstance(data, dict):
        return data.get("workspace")
    return None


_STARTUP_LOCK = threading.Lock()


def _start_service() -> None:
    """Run the existing launcher once, without an external browser."""
    launcher = str(LAUNCHER)
    if not Path(launcher).is_file():
        raise PanelError("找不到模型工作台启动脚本。")
    command = [find_pwsh(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launcher, "-NoBrowser"]
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryFile() as sink:
        try:
            completed = subprocess.run(command, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                       stdout=sink, stderr=sink, timeout=STARTUP_TIMEOUT,
                                       creationflags=creation)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PanelError("模型工作台启动超时或失败，请手动运行启动脚本检查。") from error
        if completed.returncode == 0:
            return
        sink.seek(0)
        detail = sink.read(4096).decode("utf-8", "replace").strip().replace("\n", " ")
    raise PanelError(f"模型工作台启动脚本未成功：{detail[-300:] or '无输出'}")


def ensure_service(timeout: int | None = None) -> bool:
    """Reuse a running panel for this workspace, otherwise start it explicitly."""
    limit = STARTUP_TIMEOUT if timeout is None else timeout
    with _STARTUP_LOCK:
        if service_workspace() == str(ROOT):
            return True
        _start_service()
        deadline = time.monotonic() + limit
        while time.monotonic() <= deadline:
            if service_workspace() == str(ROOT):
                return True
            time.sleep(0.3)
        return False


def panel_call(path, method, body=None, view=None) -> dict:
    """Proxy a whitelisted call; refresh a rejected pre-handler credential once.

    Network errors, unknown outcomes and other permission refusals never replay
    mutations. The real credential remains inside this MCP process.
    """
    method = method.upper() if isinstance(method, str) else method
    path = validate_request(path, method)
    view_args = {} if view is None else {'view':project_scope.parse_view(view)}
    if method == "GET" and path == "/api/session":
        if not ensure_service():
            return {"status": 503, "data": {"error": "模型工作台服务未能启动，请手动运行启动脚本。"}}
        with _TOKEN_LOCK:
            _TOKEN['value'] = None
        return {"status": 200, "data": {"token": DEFAULT_DUMMY_TOKEN}}
    if method == "GET":
        try:
            status, data = http_call("GET", path, **view_args)
        except PanelError as error:
            return {"status": 503, "data": {"error": str(error)}}
        return {"status": status, "data": data}
    payload = {} if body is None else body
    if not isinstance(payload, dict):
        raise PanelError("请求体必须是 JSON 对象。")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_BODY_BYTES:
        raise PanelError("请求体过大，已拒绝。")
    try:
        token = panel_token()
        status, data = http_call("POST", path, body=payload, token=token, **view_args)
        if session_rejected(status, data):
            # The panel's authentication guard rejected the request before its
            # handler ran. This is the sole safe retry, with identical scope and
            # payload. Never replay an HTTP/transport/business failure.
            token = panel_token(force=True, rejected_token=token)
            status, data = http_call("POST", path, body=payload, token=token, **view_args)
    except PanelError as error:
        return {"status": 503, "data": {"error": str(error)}}
    return {"status": status, "data": data}


def session_rejected(status, data):
    """Recognise only the pre-handler session refusal (including older panels)."""
    if status != 403 or not isinstance(data, dict):
        return False
    return (data.get('code') == 'panel_session_expired' or
            data == {'error': '请刷新面板后重试'})


def open_desk(view=None) -> dict:
    """Start (or reuse) the workbench for this workspace."""
    if not ensure_service():
        raise PanelError("模型工作台未能启动，请手动运行启动脚本检查。")
    return {"ready": True, "url": API_BASE, "workspace": str(ROOT),
            'view':project_scope.parse_view(view), 'ui_version':2}


def single_root_view(roots):
    """The one valid local file root, or None for missing/ambiguous/remote input."""
    if len(roots) != 1:
        return None
    try:
        uri = urlparse(str(roots[0].uri))
        if uri.scheme != 'file' or uri.netloc not in ('', 'localhost'):
            return None
        return project_scope.parse_view({'mode': 'project', 'project': url2pathname(uri.path)})
    except ValueError:
        return None


_THREAD_ID = re.compile(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')


def thread_metadata_view(thread_id, thread):
    """Only metadata for the exact host-supplied local thread may supply cwd."""
    if not isinstance(thread, dict) or thread.get('id') != thread_id:
        return None
    environments = thread.get('environments')
    if environments is not None:
        if (not isinstance(environments, list) or len(environments) != 1
                or not isinstance(environments[0], dict)
                or environments[0].get('environmentId') != 'local'):
            return None
        directory = environments[0].get('cwd')
        if thread.get('cwd') is not None:
            try:
                if project_scope.project_key(directory) != project_scope.project_key(thread['cwd']):
                    return None
            except ValueError:
                return None
    else:
        directory = thread.get('cwd')
    try:
        return project_scope.parse_view({'mode': 'project', 'project': directory})
    except ValueError:
        return None


def read_thread_project(thread_id):
    """Read directory metadata only: no thread start/resume, turn or model request."""
    process = subprocess.Popen([find_codex(), 'app-server', '--stdio'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    messages = queue.Queue(maxsize=16)

    def read_stdout():
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                    if isinstance(message, dict) and message.get('id') in (1, 2):
                        messages.put_nowait(message)
                except (ValueError, queue.Full):
                    continue
        except (OSError, ValueError):
            pass
        finally:
            try:
                messages.put_nowait(None)
            except queue.Full:
                pass

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + 10

    def send(message):
        process.stdin.write(json.dumps(message) + '\n')
        process.stdin.flush()

    def receive(request_id):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Thread metadata lookup timed out')
            message = messages.get(timeout=remaining)
            if message is None:
                raise OSError('Thread metadata reader exited')
            if message.get('id') == request_id:
                if 'error' in message or not isinstance(message.get('result'), dict):
                    raise ValueError('Thread metadata unavailable')
                return message['result']

    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'model_desk_project', 'version': '1'},
            'capabilities': {'experimentalApi': True}}})
        receive(1)
        send({'method': 'initialized', 'params': {}})
        send({'id': 2, 'method': 'thread/read', 'params': {
            'threadId': thread_id, 'includeTurns': False}})
        return thread_metadata_view(thread_id, receive(2).get('thread'))
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        reader.join(timeout=1)
        process.stdout.close()


async def opening_view(server, arguments):
    """Explicit directory, host thread metadata, then a unique advertised root."""
    if arguments.get('project') is not None:
        return project_scope.parse_view({'mode':'project','project':arguments['project']})
    try:
        meta = server.request_context.meta
        meta = meta if isinstance(meta, dict) else meta.model_dump()
    except (AttributeError, LookupError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    if 'threadId' in meta:
        thread_id = meta['threadId']
        if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
            return project_scope.parse_view(None)
        try:
            view = await anyio.to_thread.run_sync(lambda: read_thread_project(thread_id))
            return view if view is not None else project_scope.parse_view(None)
        except Exception:
            # An identified but unreadable thread must not inherit another root.
            return project_scope.parse_view(None)
    try:
        session=server.request_context.session
        if not session.check_client_capability(types.ClientCapabilities(roots={})):
            return project_scope.parse_view(None)
        with anyio.fail_after(2):
            roots=(await session.list_roots()).roots
    except Exception:
        return project_scope.parse_view(None)
    view = single_root_view(roots)
    return view if view is not None else project_scope.parse_view(None)


# ----------------------------------------------------------------------- UI bundle

def build_workbench_html(panel_dir: Path | None = None) -> str:
    """Inline the panel's existing assets into one MCP Apps document."""
    folder = Path(panel_dir) if panel_dir is not None else PANEL_DIR
    try:
        return panel_bundle.bundle(folder, BRIDGE_SCRIPT)
    except panel_bundle.BundleError as error:
        raise PanelError(str(error)) from error


def read_workbench(panel_dir: Path | None = None) -> list[ReadResourceContents]:
    html = build_workbench_html(panel_dir)
    return [ReadResourceContents(content=html, mime_type=UI_MIME, meta=UI_META)]


UI_META = {"ui": {"prefersBorder": False,
                  "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []}}}

_OPEN_META = {
    "ui": {"resourceUri": RESOURCE_URI, "visibility": ["model", "app"]},
    "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
    "openai/outputTemplate": RESOURCE_URI,
}
_PANEL_META = {"ui": {"visibility": ["app"]}}
_GOAL_META = {"ui": {"visibility": ["model"]}}


def tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name=TOOL_OPEN, title="模型工作台",
            description="在本机打开模型工作台：复用已运行的服务，必要时启动本地服务，随后显示工作台界面。",
            inputSchema={"type": "object", "properties": {'project':{'type':'string','description':'明确的本机项目绝对目录；未提供时优先使用宿主当前聊天的目录元数据，再尝试唯一 MCP root，均不可用时在面板选择'}}, "additionalProperties": False},
            annotations=types.ToolAnnotations(title="模型工作台", destructiveHint=False,
                                              idempotentHint=True, openWorldHint=False),
            _meta=_OPEN_META,
        ),
        types.Tool(
            name=TOOL_PANEL, title="工作台接口代理",
            description="仅供模型工作台界面调用：按白名单转发工作台本机接口请求，界面不直接访问本机端口。",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作台内部接口路径"},
                    "method": {"type": "string", "enum": ["GET", "POST"]},
                    "body": {"type": "object", "description": "POST 请求体"},
                    "view": {"type":"object","description":"此面板实例的项目视图，默认未绑定"},
                },
                "required": ["path", "method"],
                "additionalProperties": False,
            },
            annotations=types.ToolAnnotations(title="工作台接口代理", destructiveHint=False,
                                              idempotentHint=False, openWorldHint=False),
            _meta=_PANEL_META,
        ),
        types.Tool(
            name=TOOL_GOAL_INTERACT,
            title="询问目标进展或插入最新需求",
            description=("对当前 Codex 线程项目中的活动 Goal 交互。message 是 /btw 或其后接空白说明时只读返回"
                         "真实进展，不启动工作台、模型或续跑；其他文本作为版本化最新需求持久写入，"
                         "由下一安全轮次处理。goal_id 可省略，但项目中必须恰有一个活动目标；"
                         "多个目标时必须明确。/stop 与 /pause 必须使用目标控制。"),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "maxLength": 20000},
                    "goal_id": {"type": "string", "maxLength": 100},
                    "kind": {"type": "string", "enum": ["requirement", "direction_change"]},
                    "request_id": {
                        "type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9-]{0,99}$"
                    },
                    "project": {
                        "type": "string",
                        "description": "仅当宿主线程项目无法解析时提供明确的本机项目绝对目录",
                    },
                },
                "required": ["message"],
                "additionalProperties": False,
            },
            annotations=types.ToolAnnotations(
                title="询问目标进展或插入最新需求",
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=False,
            ),
            _meta=_GOAL_META,
        ),
    ]


def resource_definitions() -> list[types.Resource]:
    return [types.Resource(name="model-desk-workbench", title="模型工作台", uri=RESOURCE_URI,
                           description="模型工作台界面，由本机面板资源打包。",
                           mimeType=UI_MIME, _meta=UI_META)]


# -------------------------------------------------------------------- MCP server

def build_server() -> Server:
    server = Server("model-desk")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return tool_definitions()

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return resource_definitions()

    @server.read_resource()
    async def _read_resource(uri) -> list[ReadResourceContents]:
        if str(uri).rstrip("/") not in {RESOURCE_URI,'ui://model-desk/workbench-v1'}:
            raise ValueError("未知的工作台资源。")
        return await anyio.to_thread.run_sync(read_workbench)

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        arguments = arguments or {}
        if name == TOOL_OPEN:
            try:
                view=await opening_view(server,arguments)
                return await anyio.to_thread.run_sync(lambda:open_desk(view))
            except (PanelError,ValueError) as error:
                return types.CallToolResult(content=[types.TextContent(type="text", text=str(error))],
                                            isError=True)
        if name == TOOL_PANEL:
            try:
                result = await anyio.to_thread.run_sync(
                    lambda: panel_call(arguments.get("path"), arguments.get("method"), arguments.get("body"),arguments.get('view')))
            except (PanelError,ValueError) as error:
                return types.CallToolResult(content=[types.TextContent(type="text", text=str(error))],
                                            isError=True)
            # This app-only tool supplies the payload in structuredContent.
            # Returning a dict makes the SDK serialize another full, indented
            # copy into TextContent on every poll, including historical tasks.
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="工作台接口响应。")],
                structuredContent=result)
        if name == TOOL_GOAL_INTERACT:
            try:
                view = await opening_view(server, {"project": arguments.get("project")}
                                          if arguments.get("project") is not None else {})
                project = view.get("project") if view.get("mode") == "project" else None
                if not project:
                    raise PanelError("无法确定当前线程项目；请明确提供 project 绝对目录。")
                message = arguments.get("message")
                if not isinstance(message, str):
                    raise PanelError("goal_interact 需要文本 message。")
                import goal_interactions
                result = await anyio.to_thread.run_sync(lambda: goal_interactions.interact_current(
                    project,
                    message,
                    goal_id=arguments.get("goal_id") or "",
                    kind=arguments.get("kind") or "requirement",
                    request_id=arguments.get("request_id") or "",
                ))
            except (PanelError, ValueError) as error:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=str(error))], isError=True)
            summary = ("已只读返回目标进展；未启动工作台、模型或续跑。"
                       if result["interaction"] == "progress"
                       else ("方向调整已持久化并请求旧主脑／旧任务在受控边界停止；"
                             "替代执行等待原租约释放。"
                             if result["requirement"]["kind"] == "direction_change"
                             else "增量需求已写入版本化需求流；当前轮在副作用边界核对新版。"))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=summary),
                         types.TextContent(type="text", text=json.dumps(
                             result, ensure_ascii=False, separators=(",", ":")))],
                structuredContent=result,
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text="未知的工作台工具。")],
                                    isError=True)

    return server


async def serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    anyio.run(serve)


if __name__ == "__main__":
    main()

import base64
import logging
import asyncio
import aiofiles
import aiohttp
import json
from yarl import URL
from collections import namedtuple

_LOGGER = logging.getLogger(__name__)

ATTR_TOKEN = "token"
ATTR_INSTALLED_APPS = "installed_apps"

WS_CONTROL = "control"
WS_REMOTE = "remote"

WS_ENDPOINT_REMOTE_CONTROL = "/api/v2/channels/samsung.remote.control"
WS_ENDPOINT_APP_CONTROL = "/api/v2"

App = namedtuple("App", ["app_id", "app_name", "app_type"])


def serialize_string(string):
    if isinstance(string, str):
        string = str.encode(string, "utf-8")

    return base64.b64encode(string).decode("utf-8")


def format_websocket_url(host, path, name, token=None):
    url = URL.build(
        scheme="wss",
        host=host,
        port=8002,
        path=path,
        query={"name": serialize_string(name)},
    )

    if token:
        return str(url.update_query({"token": token}))
    return str(url)


def _noop(*args, **kwargs):
    pass


class FileDataStore(object):
    def __init__(self, name):
        self._name = name
        self._data = {}

    async def get(self, key, default=None):
        return (await self.get_data()).get(key, default)

    async def set(self, key, value):
        await self.get_data()
        self._data[key] = value
        await self._save_to_store(self._data)

    async def get_data(self):
        if not self._data:
            self._data = await self._load_from_store()
        return self._data

    async def _load_from_store(self):
        try:
            async with aiofiles.open(f"{self._name}.token", mode="r") as f:
                data = await f.read()
                return json.loads(data)
        except Exception as exc:
            _LOGGER.error(f"Failed to read storage file: {exc}")
            return {}

    async def _save_to_store(self, data):
        try:
            async with aiofiles.open(f"{self._name}.token", mode="w") as f:
                await f.write(json.dumps(data))
        except Exception as exc:
            _LOGGER.error(f"Failed to write storage file: {exc}")


class AuthorizationError(Exception):
    def __init__(self):
        super().__init__("TV refused authorization (ms.channel.aunauthorized)")


class TizenWebsocket:
    """Represent a websocket connection to a Tizen TV."""

    def __init__(
        self,
        name,
        host,
        key_press_delay=0,
        data_store=None,
        loop=None,
        session=None,
        update_callback=None,
    ):
        """Initialize a TizenWebsocket instance."""
        self.host = host
        self.name = name
        self.key_press_delay = key_press_delay
        self.session = session or aiohttp.ClientSession()
        self._managed_session = not session
        self._store = data_store or FileDataStore(self.name)
        self._loop = loop or asyncio.get_running_loop()
        self._current_app = None
        self._installed_apps = {}
        self._signal_update = update_callback or _noop
        self._found_running_app = False
        self._ws_remote = None
        self._ws_control = None
        self._connected = False
        self._is_connecting = False
        self._remote_task = None
        self._control_task = None
        self._app_monitor_task = None

    @property
    def connected(self):
        return self._connected

    @property
    def current_app(self):
        return self._current_app

    @property
    def installed_apps(self):
        return self._installed_apps

    @property
    def is_connecting(self):
        return self._is_connecting

    def open(self, ):
        if self.connected or self._is_connecting:
            _LOGGER.warn("Already connected")
        else:
            if self._managed_session and (self.session is None or self.session.closed):
                self.session = aiohttp.ClientSession()
                _LOGGER.debug("Created new managed ClientSession")
            _LOGGER.debug("Opening websocket connections")
            self._remote_task = self._loop.create_task(self._open_connection(WS_REMOTE))

    def close(self):
        """Close the listening websocket."""
        _LOGGER.debug("Closing websocket connections")
        if self._remote_task:
            self._remote_task.cancel()
        if self._control_task:
            self._control_task.cancel()
        if self._app_monitor_task:
            self._app_monitor_task.cancel()
        if self._managed_session:
            _LOGGER.debug("Closing managed ClientSession")
            self._loop.create_task(self.session.close())

    async def _open_connection(self, conn_name):
        """Open a persistent websocket connection and act on events."""
        path = WS_ENDPOINT_REMOTE_CONTROL if conn_name == WS_REMOTE else WS_ENDPOINT_APP_CONTROL
        token = (await self._store.get(ATTR_TOKEN)) if conn_name == WS_REMOTE else None
        url = format_websocket_url(self.host, path, self.name, token)
        _LOGGER.debug(f"{conn_name}: Attempting connection to {url}")
        try:
            self._connected = False
            self._is_connecting = True
            async with self.session.ws_connect(url, ssl=False) as ws:
                setattr(self, f"_ws_{conn_name}", ws)
                _LOGGER.debug(f"{conn_name}: Connection established")

                async for msg in ws:
                    try:
                        await self._handle_message(conn_name, msg)
                    except Exception as exc:
                        _LOGGER.error(f"Error while handling message: {exc}", exc_info=True)
        except (aiohttp.ClientConnectionError, aiohttp.WebSocketError) as exc:
            _LOGGER.error(f"{conn_name}: Connection error: {exc}")
        except AuthorizationError:
            _LOGGER.error(f"{conn_name}: Authorization refused")
        except asyncio.CancelledError:
            _LOGGER.debug(f"{conn_name}: Task was cancelled")
        except Exception as exc:
            _LOGGER.error(
                f"{conn_name}: Unknown error occurred: {exc}", exc_info=True
            )
        finally:
            _LOGGER.debug(f"{conn_name}: disconnected")
            setattr(self, f"_ws_{conn_name}", None)
            self._connected = False
            self._is_connecting = False
            self._current_app = None
            self._installed_apps = {}

    async def _handle_message(self, conn_name, msg):
        if msg.type == aiohttp.WSMsgType.TEXT:
            payload = msg.json()
            if payload.get("event") == "ms.channel.unauthorized":
                raise AuthorizationError()
            elif payload.get("event") == "ms.channel.connect":
                _LOGGER.debug(f"{conn_name}: Authorization accepted")
                await (getattr(self, f"_on_connect_{conn_name}")(payload))
            else:
                await (getattr(self, f"_on_message_{conn_name}")(payload))
        elif msg.type == aiohttp.WSMsgType.ERROR:
            if issubclass(type(msg.data), Exception):
                raise msg.data
            else:
                _LOGGER.error(f"Received error: {msg.data}")
                await (getattr(self, f"_on_error_{conn_name}")(msg))

    async def _on_connect_remote(self, msg):
        token = msg.get("data", {}).get(ATTR_TOKEN)
        if token:
            _LOGGER.debug(f"Got token: {token}")
            await self._store.set(ATTR_TOKEN, token)
        await self.request_installed_apps()
        self._control_task = self._loop.create_task(self._open_connection(WS_CONTROL))

    async def _on_connect_control(self, msg):
        self._connected = True
        self._is_connecting = False
        self._app_monitor_task = self._loop.create_task(self._monitor_running_app())

    async def _on_message_remote(self, msg):
        if msg.get("event") == "ed.installedApp.get":
            self._build_app_list(msg)

    async def _on_message_control(self, msg):
        app_id = None
        result = msg.get("result")
        if result:
            if type(result) is bool:
                app_id = msg.get("id")
                self._found_running_app = True
            elif type(result) is dict:
                if result.get("running") and result.get("visible"):
                    app_id = result.get("id")
                    self._found_running_app = True
        if app_id:
            self._update_current_app(app_id)

    def _update_current_app(self, app_id):
        new_current_app = self._installed_apps.get(app_id) if app_id else None
        if new_current_app != self._current_app:
            self._current_app = new_current_app
            _LOGGER.debug(f"Running app is: {self._current_app}")
            self._signal_update()

    def _build_app_list(self, response):
        list_app = response.get("data", {}).get("data")
        installed_apps = {}
        for app_info in list_app:
            # Waipu contains unreadable characters in the name, so we skip it
            if "waipu" in app_info["name"]:
                continue
            app_id = app_info["appId"]
            app = App(app_id, app_info["name"], app_info["app_type"])
            installed_apps[app_id] = app
        self._installed_apps = installed_apps
        _LOGGER.debug("Installed apps:\n\t{}".format("\n\t".join([f"{app.app_name}: {app.app_id}" for app in installed_apps.values()])))
        self._signal_update()

    async def request_installed_apps(self):
        _LOGGER.debug("Requesting list of installed apps")
        try:
            await self._ws_remote.send_json(
                {
                    "method": "ms.channel.emit",
                    "params": {"event": "ed.installedApp.get", "to": "host"},
                }
            )
        except Exception:
            _LOGGER.error("Failed to request installed apps", exc_info=True)

    async def _monitor_running_app(self):
        _LOGGER.debug("App monitor: starting")
        while self._ws_control and not self._ws_control.closed:
            self._found_running_app = False
            for app in self._installed_apps.values():
                if not self._ws_control or self._ws_control.closed:
                    break
                try:
                    await self._ws_control.send_json(
                        {
                            "id": app.app_id,
                            "method": (
                                "ms.webapplication.get"
                                if app.app_type == 4
                                else "ms.application.get"
                            ),
                            "params": {"id": app.app_id},
                        }
                    )
                except Exception as exc:
                    _LOGGER.error(f"Error while querying app status: {exc}", exc_info=True)
                else:
                    await asyncio.sleep(0.1)
            if self._current_app and not self._found_running_app:
                self._update_current_app(None)
        _LOGGER.debug("App monitor: stopping")

    async def send_key(self, key, key_press_delay=None, cmd="Click"):
        _LOGGER.debug(f"Sending key {key}")
        try:
            await self._ws_remote.send_json(
                {
                    "method": "ms.remote.control",
                    "params": {
                        "Cmd": cmd,
                        "DataOfCmd": key,
                        "Option": "false",
                        "TypeOfRemote": "SendRemoteKey",
                    },
                }
            )
        except Exception:
            _LOGGER.error(f"Failed to send key {key}", exc_info=True)
        else:
            if key_press_delay is None:
                await asyncio.sleep(self.key_press_delay)
            elif key_press_delay > 0:
                await asyncio.sleep(key_press_delay)

    async def run_app(self, app_id, action_type="", meta_tag=""):
        if not action_type:
            app = self._installed_apps.get(app_id)
            action_type = "NATIVE_LAUNCH" if app and app.app_type != 2 else "DEEP_LINK"

        _LOGGER.debug(f"Running app {app_id} / {action_type} / {meta_tag}")

        try:
            if action_type == "DEEP_LINK":
                await self._ws_control.send_json(
                    {
                        "id": app_id,
                        "method": "ms.application.start",
                        "params": {"id": app_id},
                    }
                )
            else:
                await self._ws_remote.send_json(
                    {
                        "method": "ms.channel.emit",
                        "params": {
                            "event": "ed.apps.launch",
                            "to": "host",
                            "data": {
                                "action_type": action_type,
                                "appId": app_id,
                                "metaTag": meta_tag,
                            },
                        },
                    }
                )
        except Exception:
            _LOGGER.error(f"Failed to run app {app_id}", exc_info=True)


if __name__ == "__main__":
    import sys
    from asynccmd import Cmd

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    class SimpleCommander(Cmd):
        def __init__(self, mode, intro, prompt):
            # We need to pass in Cmd class mode of async cmd running
            super().__init__(mode=mode)
            self.intro = intro
            self.prompt = prompt
            self.loop = None
            self.tizenws = None

        def do_tasks(self, arg):
            for task in asyncio.Task.all_tasks(loop=self.loop):
                print(task)

        def do_loglevel(self, arg):
            if not arg:
                print("You must provide a loglevel to set")
            elif arg == "debug":
                root.setLevel(logging.DEBUG)
                handler.setLevel(logging.DEBUG)
            elif arg == "info":
                root.setLevel(logging.INFO)
                handler.setLevel(logging.INFO)
            elif arg == "warn":
                root.setLevel(logging.WARNING)
                handler.setLevel(logging.WARNING)
            elif arg == "error":
                root.setLevel(logging.ERROR)
                handler.setLevel(logging.ERROR)
            else:
                print("Invalid loglevel. Available: debug, info, warn, error")
                return
            print(f"Loglevel is now {arg}")

        def do_connect(self, arg):
            if self.tizenws:
                print("Already connected")
                return

            args = arg.split(" ")
            if not args[0]:
                print("Error no host given")
                return
            if len(args) < 2 or not args[1]:
                args[1] = "TizenWS Commandline"
            self.tizenws = TizenWebsocket(args[1], args[0], loop=self.loop)
            self.tizenws.open()

        def do_disconnect(self, arg):
            if not self.tizenws:
                print("Not connected")
                return
            self.tizenws.close()
            self.tizenws = None

        def do_status(self, arg):
            is_connecting = self.tizenws._is_connecting if self.tizenws else False
            connected = self.tizenws.connected if self.tizenws else False
            print(f"Initializing connection: {is_connecting}")
            print(f"Connected: {connected}")

        def do_app(self, arg):
            if not self.tizenws or not self.tizenws.connected:
                print("Not connected")
                return

            args = arg.split(" ")
            if not args[0]:
                print(self.tizenws.current_app)
            elif args[0] == "list":
                if self.tizenws and self.tizenws.installed_apps:
                    for app in self.tizenws.installed_apps.values():
                        print(f"{app.app_name}: {app.app_id}")
            elif args[0] == "fetch":
                self.loop.create_task(self.tizenws.request_installed_apps())
            elif args[0] == "run":
                if len(args) < 2:
                    print("You must provide an App ID to run")
                else:
                    self.loop.create_task(self.tizenws.run_app(args[1]))
            else:
                print(f"Invalid app command {args[0]}")

        def do_key(self, arg):
            if not self.tizenws or not self.tizenws.connected:
                print("Not connected")
            elif not arg:
                print("You must provide a key to send")
            else:
                self.loop.create_task(self.tizenws.send_key(arg))

        def do_exit(self, arg):
            if self.tizenws:
                self.tizenws.close()
            self.loop.stop()

        def start(self, loop=None):
            # We pass our loop to Cmd class.
            # If None it try to get default asyncio loop.
            self.loop = loop
            # Create async tasks to run in loop. There is run_loop=false by default
            super().cmdloop(loop)

    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
        mode = "Run"
    else:
        loop = asyncio.get_event_loop()
        mode = "Reader"
    # create instance
    cmd = SimpleCommander(mode=mode, intro="TizenWS Commandline", prompt="tizenws> ")
    cmd.start(loop)  # prepare instance
    try:
        loop.run_forever()  # our cmd will run automatically from this moment
    except KeyboardInterrupt:
        loop.stop()

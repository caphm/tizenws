# tizenws
Simple websocket client for Samsung Tizen TVs in python using asyncio. Primarily intended for use in Home Assistant integrations.

## What it does
tizenws connects to a Samsung Tizen V via websockets and enables you to remote control the TV. You can send keys and run apps, the currently running app is also monitored.

## What it doesn't
tizenws does not monitor the on/off state of the TV. You should connect when the TV is on, and close the connection when it turns off. The info about on/off state cannot be reliably determined via websocket connection, so you should use other channels (like SmartThings) to obtain this information.

## How it works
The library maintains two separate websocket connections to the TV:
1. The remote control connection to the *samsung.remote.control* channel. Via this connection, the installed apps are queried and keys are being sent. This connection is the first one to be initiated and must be authorized by allowing the access on you TV.
2. A basic connection to the core API, which is used to run apps and monitor the currently running app. This connection is initiated after the remote control connection has been established successfully.

## Usage
### Ilustrative example
```
def tv_updated():
    print("The running app or installed apps were changed")

tizenws = TizenWebsocket(
    name="TizenWS Client", # Name that is displayed in the access control list on the TV
    host="192.168.1.100", # TVs IP address or hostname
    update_callback=tv_updated, # Is called when the running app or list of installed apps has changed
)

tizenws.open() # Open the connection

# Do your stuff (usually in coroutines, this code does not run like this, just an example)
await tizenws.send_key("KEY_HOME") # Press HOME key
await tizenws.run_app("11101200001") # Run Netflix
print(tizenws.current_app) # Display currently running app

tizenws.close() # Close the connection
```
### Home Assistant example
```
class MyTizenTV(Entity):
    def __init__(self, host):
        super().__init__()
        self._host = host
        self._app_name = None
        self._app_id = None

    async def async_added_to_hass(self):
        def close_tizenws(event):
            if self._tizenws:
                _LOGGER.debug("Home Assistant ist stopping, shutting down TizenWebsocket")
                self._tizenws.close()

        self.event_unsub = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_tizenws)

        self._tizenws = TizenWebsocket(
            name="Home Assistant",
            host=self._host,
            loop=self.hass.loop,
            session=self.hass.helpers.aiohttp_client.async_get_clientsession(),
            update_callback=self.async_schedule_update_ha_state,
        )

    async def async_will_remove_from_hass(self):
        if self._tizenws:
            _LOGGER.debug("Entity is being removed from Home Assistant, shutting down TizenWebsocket")
            self._tizenws.close()
        self.event_unsub()

    async def async_update(self):
        """Retrieve latest state."""
        await self.get_on_off_state() # Get the on/off state from some other means (e.g. SmartThings)
        if self.state != STATE_OFF:
            if self._tizenws:
                if not self._tizenws.connected:
                    self._tizenws.open()
        else:
            self._app_id = None
            self._app_name = None
            if self._tizenws and self._tizenws.connected:
                self._tizenws.close()

    @property
    def source_list(self):
        return (
            [app.app_name for app in self._tizenws.installed_apps.values()]
            if self._tizenws and self._tizenws.installed_apps
            else []
        )

    @property
    def source(self):
        """Name of the current input source."""
        return self.app_name

    @property
    def app_id(self):
        """ID of the current running app."""
        return self._tizenws.current_app.app_id if self._tizenws.current_app else None

    @property
    def app_name(self):
        """Name of the current running app."""
        return self._tizenws.current_app.app_name if self._tizenws.current_app else None

    async def async_volume_up(self):
        """Turn volume up for media player."""
        await self._tizenws.send_key("KEY_VOLUP")

    async def async_volume_down(self):
        """Turn volume down for media player."""
        await self._tizenws.send_key("KEY_VOLDOWN")

    async def async_mute_volume(self, mute):
        """Mute the volume."""
        await self._tizenws.send_key("KEY_MUTE")

    async def async_media_play(self):
        """Send play command."""
        await self._tizenws.send_key("KEY_PLAY")

    async def async_media_pause(self):
        """Send pause command."""
        await self._tizenws.send_key("KEY_PAUSE")

    async def async_media_stop(self):
        """Send stop command."""
        await self._tizenws.send_key("KEY_STOP")

    async def async_select_source(self, source):
        """Select input source."""
        for app in self._tizenws.installed_apps.values():
            if app.app_name == source:
                await self._tizenws.run_app(app.app_id)
```
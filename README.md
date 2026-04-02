# Cast Control

A web-based control panel for broadcasting messages and images to Google Cast devices (Chromecast, Nest Hub, Google Home displays) on your local network.

## Features

- Send text messages and images to all Cast devices simultaneously
- Network scanner to discover Cast devices on 192.168.4.x / 192.168.5.x / 192.168.6.x
- Add, remove, and enable/disable individual devices
- Live preview of what will appear on devices
- Auto-refreshing display — update messages without re-casting
- Device list persists between restarts (`cast_devices.json`)

## Requirements

- Python 3.9+
- `pychromecast` (`pip install pychromecast`)

## Usage

```bash
pip install -r requirements.txt
python cast_server.py
```

Then open **http://localhost:8765** in your browser.

## How it works

- The control page is served at `/`
- Cast devices load `/display` which polls `/state` every 2 seconds for updates
- Uses **DashCast** (app ID `84912283`) to render the display page as a full browser on Cast devices
- Network scanner probes port 8009 (Google Cast) across the configured subnets concurrently

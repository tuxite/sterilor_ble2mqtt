# BLE to MQTT gateway for Sterilor EVO

## Requirements
This code has been validated for [Olimex ESP32-POE](www.olimex.com/Products/IoT/ESP32/ESP32-POE/) development board.

## Installation
### MicroPython-lib dependencies
These steps must be done on the MicroPython shell, already connected to internet.

    import mip
    mip.install("aioble-client")
    mip.install("aioble-central")
    mip.install("inspect")

    # Typing module: https://micropython-stubs.readthedocs.io/en/main/typing_mpy.html
    mip.install("github:josverl/micropython-stubs/mip/typing.json")


Note: if necessary, after installing the [MicroPython firmware](https://micropython.org/download/OLIMEX_ESP32_POE/), use the following method to connect a Wifi access:

    def do_wifi_connect():
        import network
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        if not wlan.isconnected():
            print('connecting to network...')
            wlan.connect('<your SSID>', '<your password>')
            while not wlan.isconnected():
                pass
        print('network config:', wlan.ifconfig())

    do_wifi_connect()

### Sterilor dependencies
Copy the `sterilor_evo` Python package on the platform.

## Configuration
Create the `config.toml` file a the root of the platform.

You can copy the `config.sample.toml` file and edit it according your needs.

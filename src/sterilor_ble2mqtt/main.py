import ubinascii
import machine
import network
import uasyncio as asyncio
import time
import bluetooth
import json
import aioble
import gc
import sys
from umqtt.robust import MQTTClient
from sterilor_evo.frame import Frame


# --- Load TOML Configuration ---
def load_config():
    cfg = {}
    try:
        with open("config.toml") as f:
            section = None
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("["):
                    section = line[1:-1]
                    cfg[section] = {}
                elif "=" in line and section:
                    k, v = line.split("=", 1)
                    cfg[section][k.strip()] = v.strip().strip('"')
    except Exception as e:
        print("Config error:", e)
        raise
    return cfg


config = load_config()

# --- Ethernet ---
ETH_HOSTNAME = config["ethernet"]["host"]

# --- MQTT Topics ---
SERIAL = config["ble"]["serial_number"]
MQTT_TOPIC_AVAIL = "sterilor/" + SERIAL + "/availability"
MQTT_TOPIC_STATE = "sterilor/" + SERIAL + "/state"   # prefix — suffixed by frame name
MQTT_TOPIC_CMD = "sterilor/" + SERIAL + "/command"
MQTT_CLIENT_ID = ubinascii.hexlify(machine.unique_id())

HA_DISCOVERY_PAYLOAD_FILE = "discovery_payloads.txt"

# Lazy-loaded frame class registry — deferred to first BLE command received.
# Loading parsers.py at module level consumes ~54 KB of heap, which prevents
# NimBLE from allocating its contiguous initialisation block.
_FRAMES_CACHE = None


def get_frames():
    """Return frame class registry, loading it on first call."""
    global _FRAMES_CACHE
    if _FRAMES_CACHE is None:
        from sterilor_evo.utils import get_frames_classes_by_name
        _FRAMES_CACHE = get_frames_classes_by_name()
    return _FRAMES_CACHE


HEARTBEAT_INTERVAL = 30  # seconds


def ensure_discovery_payloads():
    """Generate discovery_payloads.txt if not present on the filesystem.

    Imports ha_discovery only when the file is missing — keeping it out of
    the normal import chain prevents heap fragmentation before BLE stack
    initialisation (NimBLE requires a large contiguous allocation).

    After generation, the device resets immediately so the next boot starts
    with a clean heap — simpler and more reliable than trying to unload the
    module and reclaim fragmented memory at runtime.
    """
    try:
        open(HA_DISCOVERY_PAYLOAD_FILE).close()
        print("Discovery payloads file found.")
    except OSError:
        print("Discovery payloads file not found — generating...")
        from sterilor_evo.ha_discovery import write_discovery_file
        serial = config["ble"]["serial_number"]
        avail = "sterilor/" + serial + "/availability"
        state = "sterilor/" + serial + "/state"
        write_discovery_file(serial, avail, state)
        print("Discovery payloads generated — rebooting...")
        machine.reset()


def init_ethernet():
    # LAN already initialised in boot.py — just wait for link-up
    # and return the existing interface to avoid double PHY init.
    lan = network.LAN(
        phy_addr=0, phy_type=network.PHY_LAN8720,
        mdc=machine.Pin(23), mdio=machine.Pin(18),
        power=machine.Pin(12), ref_clk=machine.Pin(17),
        ref_clk_mode=machine.Pin.OUT,
    )
    if not lan.isconnected():
        lan.config(dhcp_hostname=ETH_HOSTNAME)
        lan.active(True)
        while not lan.isconnected():
            time.sleep(0.5)
    print("Active Ethernet:", lan.ifconfig())
    return lan


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

class MQTTHandler:
    def __init__(self):
        self.client = None
        self.connected = False
        self.busy = False

    async def publish_discovery(self, file_path=HA_DISCOVERY_PAYLOAD_FILE):
        """Publish HA discovery payloads line by line from pre-generated file.

        Reads one line at a time to avoid loading all payloads simultaneously
        into RAM — each payload (~2.5 KB) is published and discarded before
        the next one is read.
        """
        try:
            f = open(file_path, "r")
        except OSError as e:
            print("Discovery file error:", e)
            return
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                sep = line.index("|")
                topic = line[:sep]
                payload = line[sep + 1:]
                try:
                    print("Discovery:", len(payload), "bytes ->", topic)
                    self.client.publish(topic, payload, retain=True)
                except Exception as e:
                    print("Discovery publish error:", e)
                    return
                await asyncio.sleep_ms(500)
        finally:
            f.close()
            gc.collect()

    async def connect(self, delay=1, max_delay=120):
        self.busy = True
        try:
            while True:
                try:
                    self.client = MQTTClient(
                        MQTT_CLIENT_ID,
                        config["mqtt"]["broker"],
                        port=int(config["mqtt"]["port"]),
                        user=config["mqtt"]["username"],
                        password=config["mqtt"]["password"],
                        keepalive=60,
                    )
                    self.client.set_callback(self._on_msg)
                    # LWT: broker publishes "offline" on ungraceful disconnect
                    self.client.set_last_will(
                        topic=MQTT_TOPIC_AVAIL,
                        msg="offline",
                        retain=True,
                        qos=0,
                    )
                    self.client.connect()
                    self.client.subscribe(MQTT_TOPIC_CMD)
                    self.connected = True
                    print("MQTT connected")

                    # Publish availability then schedule discovery as background task
                    self._publish_raw(MQTT_TOPIC_AVAIL, "online", retain=True)
                    asyncio.create_task(self.publish_discovery())
                    return

                except Exception as e:
                    print("MQTT connect error:", e)
                    self.connected = False
                    print("MQTT retry in", delay, "s...")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)
        finally:
            self.busy = False

    def _on_msg(self, topic, msg):
        """Receive command from HA and forward to BLE."""
        if topic.decode() != MQTT_TOPIC_CMD:
            return
        try:
            data = json.loads(msg)
            cls = get_frames().get(data["name"])
            if not cls:
                print("Unknown frame:", data["name"])
                return
            fr = Frame()
            payload = fr.create(cls.code, data=data.get("payload", {}))
            asyncio.create_task(ble.write(payload))
        except Exception as e:
            print("MQTT decode error:", e)

    def _publish_raw(self, topic, payload, retain=False):
        """Low-level publish — no connection guard."""
        self.client.publish(topic, payload, retain=retain)

    def publish(self, topic, payload, retain=False):
        """Publish with connection guard."""
        if not self.connected:
            return
        try:
            self.client.publish(topic, payload, retain=retain)
        except Exception as e:
            print("MQTT publish error:", e)
            self.connected = False

    def publish_availability(self, state):
        """Publish 'online' or 'offline' to the availability topic."""
        self.publish(MQTT_TOPIC_AVAIL, state, retain=True)

    async def loop(self):
        if not self.connected:
            return
        try:
            self.client.check_msg()
        except Exception as e:
            print("MQTT loop error:", e)
            self.connected = False
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# BLE
# ---------------------------------------------------------------------------

BLE_WRITE_UUID = bluetooth.UUID(config["ble"]["write_uuid"])
BLE_NOTIFY_UUID = bluetooth.UUID(config["ble"]["notify_uuid"])
TARGET_DEVICE_NAME = config["ble"]["serial_number"]


class BLEHandler:
    def __init__(self):
        self.device = None
        self.conn = None
        self.write_char = None
        self.notify_char = None
        self.connected = False
        self.busy = False

    async def connect(self, delay=1, max_delay=120):
        self.busy = True
        try:
            while True:
                try:
                    self.device = None
                    print("Scanning BLE...")
                    gc.collect()
                    async with aioble.scan(duration_ms=10000) as scanner:
                        async for adv in scanner:
                            if TARGET_DEVICE_NAME in str(adv.name()):
                                self.device = adv.device
                                break

                    if not self.device:
                        raise Exception("BLE device not found")

                    self.conn = await self.device.connect()
                    print("BLE connected")

                    # Discover services and characteristics
                    services = []
                    async for service in self.conn.services():
                        services.append(service)

                    self.write_char = None
                    self.notify_char = None
                    for service in services:
                        async for char in service.characteristics():
                            if char.uuid == BLE_WRITE_UUID:
                                self.write_char = char
                            elif char.uuid == BLE_NOTIFY_UUID:
                                self.notify_char = char

                    if not self.write_char or not self.notify_char:
                        raise Exception("BLE characteristics not found")

                    await self.notify_char.subscribe(True)
                    asyncio.create_task(self._notification_loop())
                    self.connected = True
                    # BLE connectivity is reflected via the global availability topic
                    mqtt.publish_availability("online")
                    return

                except Exception as e:
                    print("BLE connect error:", e)
                    sys.print_exception(e)
                    if self.conn:
                        try:
                            self.conn.disconnect()
                        except Exception:
                            pass
                        self.conn = None
                    self.write_char = None
                    self.notify_char = None
                    self.connected = False
                    mqtt.publish_availability("offline")
                    print("BLE retry in", delay, "s...")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)
        finally:
            self.busy = False

    async def _notification_loop(self):
        """Receive BLE notifications and publish to MQTT by frame name."""
        while self.connected and self.notify_char:
            try:
                data = await self.notify_char.notified()
                fr = Frame()
                result = fr.read(data)
                if result is None:
                    continue
                frame_name, payload = result
                topic = MQTT_TOPIC_STATE + "/" + frame_name
                mqtt.publish(topic, json.dumps(payload), retain=True)
            except NotImplementedError as e:
                print("Frame not implemented:", e)
            except Exception as e:
                print("BLE notif error:", e)
                self.connected = False
                mqtt.publish_availability("offline")
                return

    async def write(self, data):
        if not self.connected or not self.write_char:
            print("BLE not ready")
            return
        try:
            await self.write_char.write(data)
            print("BLE TX:", ubinascii.hexlify(data))
        except Exception as e:
            print("BLE write error:", e)
            self.connected = False
            mqtt.publish_availability("offline")


# ---------------------------------------------------------------------------
# Global instances
# ---------------------------------------------------------------------------

mqtt = MQTTHandler()
ble = BLEHandler()


# ---------------------------------------------------------------------------
# Supervision tasks
# ---------------------------------------------------------------------------

async def monitor_tasks():
    """Monitor MQTT and BLE connections and restart them if needed."""
    while True:
        if not mqtt.connected and not mqtt.busy:
            asyncio.create_task(mqtt.connect())
        if not ble.connected and not ble.busy:
            asyncio.create_task(ble.connect())
        await mqtt.loop()
        await asyncio.sleep(1)


async def heartbeat_task():
    """Refresh availability topic to prevent HA from marking device unavailable."""
    while True:
        gc.collect()
        if mqtt.connected and ble.connected:
            mqtt.publish_availability("online")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    init_ethernet()

    # Generate HA discovery payloads file if missing.
    # Must run before BLE init: ha_discovery import fragments the heap,
    # and NimBLE requires a large contiguous block to initialise.
    # The module is unloaded immediately after generation (see ensure_discovery_payloads).
    ensure_discovery_payloads()

    asyncio.create_task(mqtt.connect())
    await asyncio.sleep(5)  # let MQTT establish before BLE scan starts

    gc.collect()
    asyncio.create_task(ble.connect())
    asyncio.create_task(monitor_tasks())
    asyncio.create_task(heartbeat_task())

    # Send PIN as soon as BLE is available
    fr = Frame()
    while True:
        if ble.connected:
            await ble.write(fr.create("000a", {"pincode": int(config["ble"]["pincode"])}))
            break
        await asyncio.sleep(2)

    while True:
        await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Stopped")

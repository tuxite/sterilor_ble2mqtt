import ubinascii
import machine
import network
import uasyncio as asyncio
import time
import bluetooth
import json
import aioble
import sys
from umqtt.simple import MQTTClient
from sterilor_evo.frame import Frame
from sterilor_evo.utils import get_frames_classes_by_name


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


def init_ethernet():
    lan = network.LAN(
        phy_addr=0, phy_type=network.PHY_LAN8720,
        mdc=machine.Pin(23), mdio=machine.Pin(18),
        power=machine.Pin(12), ref_clk=machine.Pin(17), ref_clk_mode=machine.Pin.OUT,
    )
    lan.config(dhcp_hostname=ETH_HOSTNAME)
    lan.active(True)
    while not lan.isconnected():
        time.sleep(0.5)
    print("Ethernet:", lan.ifconfig())
    return lan


# --- MQTT ---
MQTT_TOPIC_RX = f"{config['mqtt']['topic']}/control/{config['ble']['serial_number']}"
MQTT_TOPIC_TX = f"{config['mqtt']['topic']}/{config['ble']['serial_number']}"
MQTT_CLIENT_ID = ubinascii.hexlify(machine.unique_id())
FRAMES_CLASSES_BY_NAME = get_frames_classes_by_name()

HEARTBEAT_INTERVAL = 30  # secondes


def publish_state(component, state, retain=True):
    """Publier l’état d’un composant (mqtt/ble)"""
    topic = f"{MQTT_TOPIC_TX}/state/{component}"
    payload = json.dumps({"state": state})
    try:
        if mqtt.connected:
            mqtt.client.publish(topic, payload, retain=retain)
            print(f"State published: {component}={state}")
    except Exception as e:
        print("State publish error:", e)


class MQTTHandler:
    def __init__(self):
        self.client = None
        self.connected = False
        self.busy = False

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
                    self.client.set_callback(self.on_msg)
                    # LWT: broker publiera "offline" si déconnexion brutale
                    self.client.set_last_will(
                        topic=f"{MQTT_TOPIC_TX}/state/mqtt",
                        msg=json.dumps({"state": "offline"}),
                        retain=True,
                        qos=0
                    )
                    self.client.connect()
                    self.client.subscribe(MQTT_TOPIC_RX)
                    self.connected = True
                    print("MQTT connected")
                    publish_state("mqtt", "online")  # online au démarrage
                    return
                except Exception as e:
                    print("MQTT connect error:", e)
                    self.connected = False
                    print(f"MQTT connection retry in {delay} seconds...")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)
        finally:
            self.busy = False

    def on_msg(self, topic, msg):
        if topic.decode() == MQTT_TOPIC_RX:
            try:
                data = json.loads(msg)
                cls = FRAMES_CLASSES_BY_NAME.get(data["name"])
                if not cls:
                    return print("Unknown frame", data["name"])
                fr = Frame()
                payload = fr.create(cls.code, data=data["payload"])
                asyncio.create_task(ble.write(payload))
            except Exception as e:
                print("MQTT decode error:", e)

    def publish(self, topic, payload, retain=False):
        if not self.connected:
            return
        try:
            self.client.publish(topic, payload, retain=retain)
        except Exception as e:
            print("MQTT publish error:", e)
            self.connected = False

    async def loop(self):
        if not self.connected:
            return
        try:
            self.client.check_msg()
        except Exception as e:
            print("MQTT loop error:", e)
            self.connected = False
        await asyncio.sleep(0)  # libère l'event loop


# --- BLE ---
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
                    async with aioble.scan(duration_ms=10000) as scanner:
                        async for adv in scanner:
                            if TARGET_DEVICE_NAME in str(adv.name()):
                                self.device = adv.device
                                break
                    if not self.device:
                        raise Exception("Device not found")

                    self.conn = await self.device.connect()
                    print("BLE connected")

                    # Discover services/chars
                    services = []
                    async for service in self.conn.services():
                        services.append(service)

                    for service in services:
                        async for char in service.characteristics():
                            if char.uuid == BLE_WRITE_UUID:
                                self.write_char = char
                            elif char.uuid == BLE_NOTIFY_UUID:
                                self.notify_char = char

                    if not self.write_char or not self.notify_char:
                        raise Exception("Characteristics not found")

                    await self.notify_char.subscribe(True)
                    asyncio.create_task(self.notification_loop())
                    self.connected = True
                    publish_state("ble", "online")
                    return
                except Exception as e:
                    print("BLE connect error:", e)
                    print(sys.print_exception(e))
                    if self.conn:
                        try:
                            self.conn.disconnect()
                        except Exception:
                            pass
                    self.connected = False
                    publish_state("ble", "offline")
                    print(f"BLE connection retry in {delay} seconds...")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)
        finally:
            self.busy = False

    async def notification_loop(self):
        fr = Frame()
        while self.connected and self.notify_char:
            try:
                data = await self.notify_char.notified()
                decoded = fr.read(data)
                mqtt.publish(f"{MQTT_TOPIC_TX}/{decoded[0]}", json.dumps(decoded[1]))
            except NotImplementedError as e:
                print(f"Frame not implemented: {e}")
                pass
            except Exception as e:
                print("BLE notif error:", e)
                self.connected = False
                publish_state("ble", "offline")
                return

    async def write(self, data):
        if not self.connected or not self.write_char:
            print("BLE not ready")
            return
        try:
            await self.write_char.write(data)
            print("BLE TX:", data)
        except Exception as e:
            print("BLE write error:", e)
            self.connected = False
            publish_state("ble", "offline")


# --- Instances ---
mqtt = MQTTHandler()
ble = BLEHandler()


# --- Monitor Tasks ---
async def monitor_tasks():
    while True:
        if not mqtt.connected and not mqtt.busy:
            asyncio.create_task(mqtt.connect())
        if not ble.connected and not ble.busy:
            asyncio.create_task(ble.connect())
        await mqtt.loop()
        await asyncio.sleep(1)


# --- Heartbeat Task ---
async def heartbeat_task():
    while True:
        if mqtt.connected:
            publish_state("mqtt", "online")  # refresh heartbeat
        if ble.connected:
            publish_state("ble", "online")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# --- Main ---
async def main():
    init_ethernet()

    asyncio.create_task(mqtt.connect())
    asyncio.create_task(ble.connect())
    asyncio.create_task(monitor_tasks())
    asyncio.create_task(heartbeat_task())

    # Envoyer PIN dès que BLE dispo
    fr = Frame()
    while True:
        if ble.connected:
            await ble.write(fr.create("000a", {"pincode": int(config["ble"]["pincode"])}))
            break
        await asyncio.sleep(2)

    while True:
        await asyncio.sleep(10)

# --- Run ---
try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("Stopped")

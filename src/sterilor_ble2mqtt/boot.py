# This file is executed on every boot (including wake-boot from deepsleep)
import machine
import network
import time
import sys
import gc
import micropython


def init_ethernet():
    ethernet = network.LAN(
        phy_addr=0,
        phy_type=network.PHY_LAN8720,
        mdc=machine.Pin(23),
        mdio=machine.Pin(18),
        power=machine.Pin(12),
        ref_clk=machine.Pin(17),
        ref_clk_mode=machine.Pin.OUT,
    )
    ethernet.config(dhcp_hostname="OLIMEX-POE")
    print("MAC:", ethernet.config('mac').hex())
    ethernet.active(True)
    while not ethernet.isconnected():
        time.sleep(0.5)
    print("ETH:", ethernet.ifconfig()[0])


init_ethernet()

# Libère les modules inutiles après init — ils ne servent plus
# mais restent en mémoire si on ne les décharge pas explicitement
for mod in ('network', 'time', 'esp'):
    sys.modules.pop(mod, None)

gc.collect()

print("[Memory after boot]")
micropython.mem_info()

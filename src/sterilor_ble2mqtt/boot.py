# This file is executed on every boot (including wake-boot from deepsleep)
import machine
import esp
import network
import time


# Activate LAN
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
    print("MAC Address:", ethernet.config('mac').hex())
    ethernet.active(True)
    while not ethernet.isconnected():
        time.sleep(0.5)
    print("Ethernet connected:", ethernet.ifconfig())


init_ethernet()

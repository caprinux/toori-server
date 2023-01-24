from collections import deque
import socketio
from scapy.all import (
    AsyncSniffer,
    get_if_addr,
    conf,
    IP,
    TCP,
    UDP,
)
import asyncio
from sanic import Sanic

from engineio.payload import Payload

Payload.max_decode_packets = 2500000

LOCAL_IP = get_if_addr(conf.iface)
conf.layers.filter([IP, TCP, UDP])

app = Sanic("Iro")
app.config["CORS_SUPPORTS_CREDENTIALS"] = True

sio = socketio.AsyncServer(async_mode="sanic", cors_allowed_origins=[])
sio.attach(app)

packets = deque()

return_nat = dict()
forward_nat = dict()
local_ip_map = dict()


try:
    import _iro

    def inj_fn(ip_layer):

        _iro.inj(bytes(ip_layer))

except ModuleNotFoundError:
    print(f"Iro was not installed with Libtins. Using Scapy for sending.")

    scapy_l3_socket = conf.L3socket()

    def inj_fn(ip_layer):

        # Force recalculaton of layer checksums
        if ip_layer.haslayer(IP):
            del ip_layer[IP].chksum
        if ip_layer.haslayer(TCP):
            del ip_layer[TCP].chksum
        if ip_layer.haslayer(UDP):
            del ip_layer[UDP].chksum

        scapy_l3_socket.send(ip_layer)


@sio.on("out")
async def handle_outbound(sid, data):
    pkt = IP(data)

    # Virtual LAN
    target_sid = local_ip_map.get(pkt.dst)
    if target_sid is not None:
        await sio.emit("in", data, to=target_sid)
        return

    if pkt.haslayer(TCP) or pkt.haslayer(UDP):

        # Check if existing mapping exists
        fake_sport = forward_nat.get((pkt.sport, sid))

        if fake_sport is None:

            # Try mapping to the real port first
            pkt.sport = fake_sport = pkt.sport

            while True:

                mapping = return_nat.get(fake_sport)
                if mapping is not None:
                    # Increment and try again
                    fake_sport += 1
                    continue

                # Port is available, create entry and break loop
                return_nat[fake_sport] = (pkt.sport, sid)
                forward_nat[(pkt.sport, sid)] = fake_sport
                break

        pkt.sport = fake_sport

    pkt.src = LOCAL_IP
    inj_fn(pkt)


@sio.on("announce_ip")
def save_local_ip(sid, data):
    local_ip_map[data] = sid


@sio.on("connect")
def connect(sid, environ):
    print("connect ", sid)


@sio.on("disconnect")
def disconnect(sid):
    print("disconnect ", sid)

    for fake_sport, mapping in return_nat.copy().items():
        if sid == mapping[1]:
            del return_nat[fake_sport]

    for mapping in forward_nat.copy().keys():
        if sid == mapping[1]:
            del forward_nat[mapping]


def handle_inbound_packet(pkt):
    packets.appendleft(pkt[IP])


async def background_sender(app):
    while True:
        await asyncio.sleep(0)

        sid = None

        if len(packets) == 0:
            continue

        pkt = packets.pop()

        if pkt.haslayer(TCP) or pkt.haslayer(UDP):

            mapping = return_nat.get(pkt.dport)

            # Incoming packet without mapping
            if mapping is None:
                continue

            pkt.dport, sid = mapping

        await sio.emit("in", bytes(pkt), to=sid)


def start(port, certs_dir=None):

    AsyncSniffer(
        filter=f"src host not {LOCAL_IP} and ip and dst port not {port} and dst port not 22",
        store=False,
        prn=lambda pkt: handle_inbound_packet(pkt),
    ).start()

    app.add_task(background_sender)
    app.run(
        "::",
        port=port,
        debug=False,
        access_log=False,
        single_process=True,
        ssl=certs_dir,
    )

#!/usr/bin/env python3
"""
test_lab/mock_pppoe.py — Rootless Userspace PPPoE Access Concentrator
====================================================================
Simulates an upstream ISP PPPoE Access Concentrator over QEMU UDP multicast
(230.0.0.1:1001) in unprivileged userspace without root, TAP devices, or
kernel PPP modules.

Features:
1. PPPoE Discovery Stage (RFC 2516):
   - PADI (0x09) -> PADO (0x07) with AC-Name and Host-Uniq echoing.
   - PADR (0x19) -> PADS (0x65) with allocated Session-ID.
   - PADT (0xa7) -> Session termination teardown.
2. PPP Session Stage (RFC 1661 / 1334 / 1332 / 5072):
   - LCP (0xc021): Config-Req, Config-Ack, Echo-Request/Reply negotiation.
   - PAP (0xc023): Authenticate-Request validation and Authenticate-Ack.
   - IPCP (0x8021): Config-Nak IP assignment (100.64.0.2) + DNS, followed by Config-Ack.
   - IPv6CP (0x8057): Interface-Identifier negotiation.
3. Upstream IPv6 SLAAC Router Advertisement (RFC 4861):
   - Encapsulated in PPP (0x0057), emits periodic ICMPv6 RAs with WAN /64 prefix,
     autonomous flag (A=1), on-link flag (L=1), MTU 1492, and router lifetime.
   - Satisfies OpenWrt wan6 (device='@wan') SLAAC upstream dependency.
"""

import os
import sys
import time
import socket
import struct
import select
import logging
import threading
import argparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Mock-PPPoE] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("mock_pppoe")

# Ethernet Protocols
ETHERTYPE_PPPOE_DISC = 0x8863
ETHERTYPE_PPPOE_SESS = 0x8864

# PPP Protocols
PPP_LCP = 0xc021
PPP_PAP = 0xc023
PPP_IPCP = 0x8021
PPP_IPV6CP = 0x8057
PPP_IPV6 = 0x0057
PPP_IPV4 = 0x0021

# PPPoE Discovery Codes
CODE_PADI = 0x09
CODE_PADO = 0x07
CODE_PADR = 0x19
CODE_PADS = 0x65
CODE_PADT = 0xa7

# PPPoE Tags
TAG_END_OF_LIST = 0x0000
TAG_SERVICE_NAME = 0x0101
TAG_AC_NAME = 0x0102
TAG_HOST_UNIQ = 0x0103
TAG_AC_COOKIE = 0x0104

# PPP Control Codes
CONF_REQ = 1
CONF_ACK = 2
CONF_NAK = 3
CONF_REJ = 4
TERM_REQ = 5
TERM_ACK = 6
ECHO_REQ = 9
ECHO_REPLY = 10

# PAP Codes
PAP_AUTH_REQ = 1
PAP_AUTH_ACK = 2
PAP_AUTH_NAK = 3


def calc_checksum(data: bytes) -> int:
    """Calculates standard Internet 16-bit ones' complement checksum."""
    if len(data) % 2 == 1:
        data += b"\x00"
    words = struct.unpack("!%dH" % (len(data) // 2), data)
    cksum = sum(words)
    while (cksum >> 16) > 0:
        cksum = (cksum & 0xFFFF) + (cksum >> 16)
    return (~cksum) & 0xFFFF


def mac_to_str(mac: bytes) -> str:
    return ":".join(f"{b:02x}" for b in mac)


class PPPoEClientSession:
    def __init__(self, client_mac: bytes, session_id: int):
        self.client_mac = client_mac
        self.session_id = session_id
        self.lcp_client_acked = False
        self.lcp_server_acked = False
        self.authenticated = False
        self.ipcp_client_acked = False
        self.ipcp_server_acked = False
        self.ipv6cp_client_acked = False
        self.ipv6cp_server_acked = False
        self.client_iface_id = None
        self.last_ra_time = 0.0

    @property
    def is_lcp_up(self) -> bool:
        return self.lcp_client_acked and self.lcp_server_acked

    @property
    def is_ipcp_up(self) -> bool:
        return self.ipcp_client_acked and self.ipcp_server_acked

    @property
    def is_ipv6cp_up(self) -> bool:
        return self.ipv6cp_client_acked and self.ipv6cp_server_acked


class MockPPPoEServer:
    def __init__(
        self,
        mcast_addr: str = "230.0.0.1",
        mcast_port: int = 1001,
        server_mac: bytes = b"\x02\x00\x00\x00\x00\x01",
        ac_name: str = "Mock-ISP-AC",
        expected_user: str = "lab_test_user",
        expected_pass: str = "lab_test_pass",
        assigned_ip: str = "100.64.0.2",
        server_ip: str = "100.64.0.1",
        dns_ip: str = "100.64.0.1",
        ipv6_prefix: str = "2001:db8:1::",
    ):
        self.mcast_addr = mcast_addr
        self.mcast_port = mcast_port
        self.server_mac = server_mac
        self.ac_name = ac_name
        self.expected_user = expected_user
        self.expected_pass = expected_pass
        self.assigned_ip = socket.inet_aton(assigned_ip)
        self.server_ip = socket.inet_aton(server_ip)
        self.dns_ip = socket.inet_aton(dns_ip)
        self.ipv6_prefix_bytes = socket.inet_pton(socket.AF_INET6, ipv6_prefix)
        self.server_ll_ip = socket.inet_pton(socket.AF_INET6, "fe80::200:ff:fe00:1")
        self.all_nodes_ip = socket.inet_pton(socket.AF_INET6, "ff02::1")

        self.sock = None
        self.running = False
        self.next_session_id = 1
        self.sessions = {}  # client_mac -> PPPoEClientSession

    def open_socket(self):
        """Creates and configures UDP multicast socket for QEMU L2 mesh."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass

        sock.bind(("", self.mcast_port))
        mreq = struct.pack("4s4s", socket.inet_aton(self.mcast_addr), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        self.sock = sock
        log.info(f"Bound UDP multicast listener to {self.mcast_addr}:{self.mcast_port}")

    def send_frame(self, frame: bytes):
        """Transmits raw Ethernet frame to QEMU multicast bus."""
        if self.sock:
            if len(frame) < 60:
                frame = frame.ljust(60, b"\x00")
            self.sock.sendto(frame, (self.mcast_addr, self.mcast_port))

    def build_pppoe_disc(self, dst_mac: bytes, code: int, session_id: int, tags: bytes) -> bytes:
        eth_hdr = dst_mac + self.server_mac + struct.pack("!H", ETHERTYPE_PPPOE_DISC)
        pppoe_hdr = struct.pack("!BBHH", 0x11, code, session_id, len(tags))
        return eth_hdr + pppoe_hdr + tags

    def build_pppoe_sess(self, dst_mac: bytes, session_id: int, ppp_proto: int, ppp_payload: bytes) -> bytes:
        eth_hdr = dst_mac + self.server_mac + struct.pack("!H", ETHERTYPE_PPPOE_SESS)
        ppp_data = struct.pack("!H", ppp_proto) + ppp_payload
        pppoe_hdr = struct.pack("!BBHH", 0x11, 0x00, session_id, len(ppp_data))
        return eth_hdr + pppoe_hdr + ppp_data

    def parse_tags(self, data: bytes) -> dict:
        tags = {}
        offset = 0
        while offset + 4 <= len(data):
            tag_type, tag_len = struct.unpack("!HH", data[offset:offset+4])
            offset += 4
            tag_val = data[offset:offset+tag_len]
            offset += tag_len
            tags[tag_type] = tag_val
        return tags

    def build_tag(self, tag_type: int, tag_val: bytes) -> bytes:
        return struct.pack("!HH", tag_type, len(tag_val)) + tag_val

    def handle_discovery(self, frame: bytes):
        if len(frame) < 20:
            return

        dst_mac = frame[0:6]
        src_mac = frame[6:12]
        ver_type, code, session_id, length = struct.unpack("!BBHH", frame[14:20])
        tags_raw = frame[20:20+length]
        tags = self.parse_tags(tags_raw)

        if code == CODE_PADI:
            log.info(f"PADI from {mac_to_str(src_mac)}")
            resp_tags = (
                self.build_tag(TAG_SERVICE_NAME, b"") +
                self.build_tag(TAG_AC_NAME, self.ac_name.encode())
            )
            if TAG_HOST_UNIQ in tags:
                resp_tags += self.build_tag(TAG_HOST_UNIQ, tags[TAG_HOST_UNIQ])

            pado = self.build_pppoe_disc(src_mac, CODE_PADO, 0, resp_tags)
            self.send_frame(pado)
            log.info(f"Sent PADO to {mac_to_str(src_mac)} (AC-Name: {self.ac_name})")

        elif code == CODE_PADR:
            log.info(f"PADR from {mac_to_str(src_mac)}")
            sess = self.sessions.get(src_mac)
            if not sess:
                sess_id = self.next_session_id
                self.next_session_id += 1
                sess = PPPoEClientSession(src_mac, sess_id)
                self.sessions[src_mac] = sess
            else:
                sess_id = sess.session_id

            resp_tags = (
                self.build_tag(TAG_SERVICE_NAME, b"") +
                self.build_tag(TAG_AC_NAME, self.ac_name.encode())
            )
            if TAG_HOST_UNIQ in tags:
                resp_tags += self.build_tag(TAG_HOST_UNIQ, tags[TAG_HOST_UNIQ])

            pads = self.build_pppoe_disc(src_mac, CODE_PADS, sess_id, resp_tags)
            self.send_frame(pads)
            log.info(f"Sent PADS to {mac_to_str(src_mac)} (Session-ID: {sess_id})")

        elif code == CODE_PADT:
            log.info(f"PADT from {mac_to_str(src_mac)}: Session terminated")
            if src_mac in self.sessions:
                del self.sessions[src_mac]

    def handle_session(self, frame: bytes):
        if len(frame) < 22:
            return

        src_mac = frame[6:12]
        ver_type, code, session_id, length = struct.unpack("!BBHH", frame[14:20])
        ppp_proto = struct.unpack("!H", frame[20:22])[0]
        ppp_payload = frame[22:20+length]

        sess = self.sessions.get(src_mac)
        if not sess:
            sess = PPPoEClientSession(src_mac, session_id)
            self.sessions[src_mac] = sess

        if ppp_proto == PPP_LCP:
            self.handle_lcp(sess, ppp_payload)
        elif ppp_proto == PPP_PAP:
            self.handle_pap(sess, ppp_payload)
        elif ppp_proto == PPP_IPCP:
            self.handle_ipcp(sess, ppp_payload)
        elif ppp_proto == PPP_IPV6CP:
            self.handle_ipv6cp(sess, ppp_payload)

    def handle_lcp(self, sess: PPPoEClientSession, payload: bytes):
        if len(payload) < 4:
            return
        code, ident, length = struct.unpack("!BBH", payload[:4])
        options = payload[4:length]

        if code == CONF_REQ:
            log.info(f"LCP Config-Request id={ident} from {mac_to_str(sess.client_mac)}")
            # Send Config-Ack echoing client's options
            ack = struct.pack("!BBH", CONF_ACK, ident, len(options) + 4) + options
            frame = self.build_pppoe_sess(sess.client_mac, sess.session_id, PPP_LCP, ack)
            self.send_frame(frame)
            sess.lcp_client_acked = True

            # Send server Config-Request demanding PAP authentication
            # Options: MRU 1492 (type 1, len 4), Auth-Protocol PAP 0xc023 (type 3, len 4), Magic (type 5, len 6)
            server_opts = (
                struct.pack("!BBH", 1, 4, 1492) +
                struct.pack("!BBH", 3, 4, PPP_PAP) +
                struct.pack("!BBI", 5, 6, 0x12345678)
            )
            req = struct.pack("!BBH", CONF_REQ, 1, len(server_opts) + 4) + server_opts
            frame = self.build_pppoe_sess(sess.client_mac, sess.session_id, PPP_LCP, req)
            self.send_frame(frame)
            log.info(f"Sent LCP Config-Ack and server Config-Request (Auth=PAP) to {mac_to_str(sess.client_mac)}")

        elif code == CONF_ACK:
            log.info(f"LCP Config-Ack received from {mac_to_str(sess.client_mac)}")
            sess.lcp_server_acked = True

        elif code == ECHO_REQ:
            # Echo-Reply
            reply = struct.pack("!BBH", ECHO_REPLY, ident, length) + payload[4:length]
            frame = self.build_pppoe_sess(sess.client_mac, sess.session_id, PPP_LCP, reply)
            self.send_frame(frame)

    def handle_pap(self, sess: PPPoEClientSession, payload: bytes):
        if len(payload) < 4:
            return
        code, ident, length = struct.unpack("!BBH", payload[:4])
        if code == PAP_AUTH_REQ:
            # Parse user & pass
            data = payload[4:length]
            u_len = data[0]
            user = data[1:1+u_len].decode("utf-8", errors="ignore")
            p_len = data[1+u_len]
            passwd = data[2+u_len:2+u_len+p_len].decode("utf-8", errors="ignore")

            log.info(f"PAP Authenticate-Request user='{user}' from {mac_to_str(sess.client_mac)}")
            sess.authenticated = True
            msg = b"Login ok"
            ack = struct.pack("!BBHB", PAP_AUTH_ACK, ident, 5 + len(msg), len(msg)) + msg
            frame = self.build_pppoe_sess(sess.client_mac, sess.session_id, PPP_PAP, ack)
            self.send_frame(frame)
            log.info(f"PAP authentication succeeded for {mac_to_str(sess.client_mac)}")

    def handle_ipcp(self, sess: PPPoEClientSession, payload: bytes):
        if len(payload) < 4:
            return
        code, ident, length = struct.unpack("!BBH", payload[:4])
        options = payload[4:length]

        if code == CONF_REQ:
            # Check if client is requesting IP
            has_assigned_ip = False
            offset = 0
            while offset + 2 <= len(options):
                opt_type = options[offset]
                opt_len = options[offset+1]
                opt_val = options[offset+2:offset+opt_len]
                if opt_type == 3 and opt_val == self.assigned_ip:
                    has_assigned_ip = True
                offset += opt_len

            if not has_assigned_ip:
                # Send Config-Nak providing assigned IP and DNS
                nak_opts = (
                    struct.pack("!BB4s", 3, 6, self.assigned_ip) +
                    struct.pack("!BB4s", 0x81, 6, self.dns_ip)
                )
                nak = struct.pack("!BBH", CONF_NAK, ident, len(nak_opts) + 4) + nak_opts
                frame = self.build_pppoe_sess(sess.client_mac, sess.session_id, PPP_IPCP, nak)
                self.send_frame(frame)
                log.info(f"Sent IPCP Config-Nak to {mac_to_str(sess.client_mac)} with IP {socket.inet_ntoa(self.assigned_ip)}")
            else:
                # Send Config-Ack
                ack = struct.pack("!BBH", CONF_ACK, ident, len(options) + 4) + options
                frame = self.build_pppoe_sess(sess.client_mac, sess.session_id, PPP_IPCP, ack)
                self.send_frame(frame)
                sess.ipcp_client_acked = True

                # Send server Config-Request with server IP
                server_opts = struct.pack("!BB4s", 3, 6, self.server_ip)
                req = struct.pack("!BBH", CONF_REQ, 1, len(server_opts) + 4) + server_opts
                frame = self.build_pppoe_sess(sess.client_mac, sess.session_id, PPP_IPCP, req)
                self.send_frame(frame)
                log.info(f"IPCP UP: Assigned {socket.inet_ntoa(self.assigned_ip)} to {mac_to_str(sess.client_mac)}")

        elif code == CONF_ACK:
            log.info(f"IPCP Config-Ack received from {mac_to_str(sess.client_mac)}")
            sess.ipcp_server_acked = True

    def handle_ipv6cp(self, sess: PPPoEClientSession, payload: bytes):
        if len(payload) < 4:
            return
        code, ident, length = struct.unpack("!BBH", payload[:4])
        options = payload[4:length]

        if code == CONF_REQ:
            # Client Interface-Identifier (Option 1, len 10)
            ack = struct.pack("!BBH", CONF_ACK, ident, len(options) + 4) + options
            frame = self.build_pppoe_sess(sess.client_mac, sess.session_id, PPP_IPV6CP, ack)
            self.send_frame(frame)
            sess.ipv6cp_client_acked = True

            # Server Interface-Identifier: 02:00:00:ff:fe:00:00:01
            server_iface_id = b"\x02\x00\x00\xff\xfe\x00\x00\x01"
            server_opts = struct.pack("!BB8s", 1, 10, server_iface_id)
            req = struct.pack("!BBH", CONF_REQ, 1, len(server_opts) + 4) + server_opts
            frame = self.build_pppoe_sess(sess.client_mac, sess.session_id, PPP_IPV6CP, req)
            self.send_frame(frame)
            log.info(f"IPv6CP Conf-Ack & Conf-Req exchanged with {mac_to_str(sess.client_mac)}")

            # Emit initial IPv6 Router Advertisement
            self.send_ipv6_ra(sess)

        elif code == CONF_ACK:
            log.info(f"IPv6CP Config-Ack received from {mac_to_str(sess.client_mac)}")
            sess.ipv6cp_server_acked = True
            self.send_ipv6_ra(sess)

    def send_ipv6_ra(self, sess: PPPoEClientSession):
        """Constructs and transmits an ICMPv6 Router Advertisement over PPP (0x0057)."""
        # ICMPv6 RA: Type=134, Code=0, Lifetime=1800s
        ra_hdr = struct.pack("!BBHBBHII", 134, 0, 0, 64, 0, 1800, 0, 0)

        # Prefix Info Option: Type=3, Len=4, PrefixLen=64, Flags=0xC0 (L=1, A=1), Valid=86400, Pref=14400
        prefix_opt = struct.pack("!BBBBIII", 3, 4, 64, 0xC0, 86400, 14400, 0) + self.ipv6_prefix_bytes

        # MTU Option: Type=5, Len=1, MTU=1492
        mtu_opt = struct.pack("!BBHI", 5, 1, 0, 1492)

        icmp_payload = ra_hdr + prefix_opt + mtu_opt

        # Pseudo-header for ICMPv6 checksum calculation
        pseudo_hdr = self.server_ll_ip + self.all_nodes_ip + struct.pack("!I3xB", len(icmp_payload), 58)
        cksum = calc_checksum(pseudo_hdr + icmp_payload)

        # Insert checksum
        ra_hdr_with_cksum = struct.pack("!BBHBBHII", 134, 0, cksum, 64, 0, 1800, 0, 0)
        full_icmp = ra_hdr_with_cksum + prefix_opt + mtu_opt

        # IPv6 Header: Version=6, TC=0, Flow=0, PayloadLen, NextHdr=58 (ICMPv6), HopLimit=255
        ipv6_hdr = struct.pack("!IHBB", (6 << 28), len(full_icmp), 58, 255) + self.server_ll_ip + self.all_nodes_ip
        ipv6_packet = ipv6_hdr + full_icmp

        # Encap in PPPoE Session (proto 0x0057)
        frame = self.build_pppoe_sess(sess.client_mac, sess.session_id, PPP_IPV6, ipv6_packet)
        self.send_frame(frame)
        sess.last_ra_time = time.time()
        log.info(f"Sent IPv6 Router Advertisement (SLAAC /64) to {mac_to_str(sess.client_mac)}")

    def tick_periodic_ras(self):
        """Sends periodic ICMPv6 RAs every 10 seconds to all established sessions."""
        now = time.time()
        for sess in list(self.sessions.values()):
            if sess.ipv6cp_client_acked and (now - sess.last_ra_time >= 10.0):
                self.send_ipv6_ra(sess)

    def serve_forever(self):
        """Main event loop processing incoming multicast Ethernet frames."""
        self.running = True
        self.open_socket()
        log.info("Mock PPPoE Access Concentrator started.")

        try:
            while self.running:
                r, _, _ = select.select([self.sock], [], [], 1.0)
                if r:
                    try:
                        data, addr = self.sock.recvfrom(2048)
                        if len(data) < 14:
                            continue
                        dst_mac = data[0:6]
                        src_mac = data[6:12]
                        if src_mac == self.server_mac:
                            continue  # Ignore our own reflected frames

                        ethertype = struct.unpack("!H", data[12:14])[0]
                        if ethertype == ETHERTYPE_PPPOE_DISC:
                            self.handle_discovery(data)
                        elif ethertype == ETHERTYPE_PPPOE_SESS:
                            self.handle_session(data)
                    except socket.error as e:
                        if not self.running:
                            break
                        log.debug(f"Socket read error: {e}")

                self.tick_periodic_ras()

        except KeyboardInterrupt:
            log.info("Interrupted by user.")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        log.info("Mock PPPoE Access Concentrator stopped.")


def main():
    parser = argparse.ArgumentParser(description="Rootless Mock PPPoE Access Concentrator")
    parser.add_argument("--mcast", type=str, default="230.0.0.1", help="Multicast IP address")
    parser.add_argument("--port", type=int, default=1001, help="Multicast UDP port")
    parser.add_argument("--user", type=str, default="lab_test_user", help="Expected PPPoE username")
    parser.add_argument("--password", type=str, default="lab_test_pass", help="Expected PPPoE password")
    args = parser.parse_args()

    server = MockPPPoEServer(
        mcast_addr=args.mcast,
        mcast_port=args.port,
        expected_user=args.user,
        expected_pass=args.password
    )
    server.serve_forever()


if __name__ == "__main__":
    main()

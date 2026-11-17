#!/usr/bin/env python3
"""
test_lab/mock_isp.py — Userspace Mock Services for Test Lab
============================================================
Provides portable mock services for testing OpenWrt fleet roles:
1. Mock Cloudflare API v4 HTTP server (validates DDNS updates & Bearer tokens).
2. Mock DNS responder (verifies DNS resolution and ad-block sinkholing).
3. Mock PPPoE Access Concentrator over QEMU multicast (WAN L2 simulation).
"""

import sys
import json
import socket
import argparse
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

try:
    from test_lab.mock_pppoe import MockPPPoEServer
except ImportError:
    from mock_pppoe import MockPPPoEServer

class MockCloudflareHandler(BaseHTTPRequestHandler):
    """Mocks Cloudflare REST API v4 for ddns-scripts validation."""
    
    updates_log = []

    def log_message(self, format, *args):
        # Quiet standard HTTP access logging unless verbose
        pass

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"success":false,"errors":[{"message":"Invalid token"}]}')
            return

        parsed = urlparse(self.path)
        # Mock Zone ID query response
        if "/client/v4/zones" in parsed.path and "dns_records" not in parsed.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "success": True,
                "result": [{"id": "mock_zone_id_12345", "name": "example.com"}]
            }
            self.wfile.write(json.dumps(resp).encode())
            return

        # Mock DNS records list response
        if "/dns_records" in parsed.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "success": True,
                "result": [
                    {
                        "id": "mock_record_id_67890",
                        "type": "A",
                        "name": "lab.example.com",
                        "content": "1.2.3.4"
                    }
                ]
            }
            self.wfile.write(json.dumps(resp).encode())
            return

        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        self._handle_update("PUT")

    def do_POST(self):
        self._handle_update("POST")

    def _handle_update(self, method):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"raw": body}

        record = {
            "method": method,
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "payload": payload
        }
        MockCloudflareHandler.updates_log.append(record)
        print(f"[Mock Cloudflare] Received {method} update: {payload.get('name')} -> {payload.get('content')}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        resp = {
            "success": True,
            "result": {
                "id": "mock_record_id_67890",
                "name": payload.get("name", ""),
                "type": payload.get("type", "A"),
                "content": payload.get("content", "")
            }
        }
        self.wfile.write(json.dumps(resp).encode())


def run_dns_responder(host="127.0.0.1", port=5354):
    """Simple UDP DNS responder for mock resolution."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    print(f"[Mock DNS] Listening on {host}:{port} UDP")
    while True:
        try:
            data, addr = sock.recvfrom(512)
            if len(data) < 12:
                continue
            # Simple dummy DNS response header
            txid = data[:2]
            flags = b"\x81\x80" # Standard query response, No error
            qdcount = data[4:6]
            ancount = qdcount   # 1 answer
            header = txid + flags + qdcount + ancount + b"\x00\x00\x00\x00"
            question = data[12:]
            # Answer: name pointer + type A + class IN + TTL 60 + len 4 + IP 9.9.9.9
            answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x09\x09\x09\x09"
            sock.sendto(header + question + answer, addr)
        except Exception as e:
            break


def main():
    parser = argparse.ArgumentParser(description="Mock ISP and API services for test lab")
    parser.add_argument("--cf-port", type=int, default=8088, help="Cloudflare mock API HTTP port")
    parser.add_argument("--dns-port", type=int, default=5354, help="Mock DNS UDP port")
    parser.add_argument("--pppoe-mcast", type=str, default="230.0.0.1", help="PPPoE multicast IP")
    parser.add_argument("--pppoe-port", type=int, default=1001, help="PPPoE multicast port")
    parser.add_argument("--dns-only", action="store_true", help="Run DNS mock only")
    parser.add_argument("--cf-only", action="store_true", help="Run Cloudflare API mock only")
    parser.add_argument("--pppoe-only", action="store_true", help="Run PPPoE mock only")
    parser.add_argument("--no-pppoe", action="store_true", help="Disable PPPoE mock")
    args = parser.parse_args()

    threads = []
    servers = []

    if not args.cf_only and not args.pppoe_only:
        t_dns = threading.Thread(target=run_dns_responder, kwargs={"port": args.dns_port}, daemon=True)
        t_dns.start()
        threads.append(t_dns)

    if not args.dns_only and not args.pppoe_only:
        cf_server = HTTPServer(("127.0.0.1", args.cf_port), MockCloudflareHandler)
        print(f"[Mock Cloudflare API] Listening on http://127.0.0.1:{args.cf_port}")
        t_cf = threading.Thread(target=cf_server.serve_forever, daemon=True)
        t_cf.start()
        threads.append(t_cf)
        servers.append(cf_server)

    if not args.dns_only and not args.cf_only and not args.no_pppoe:
        pppoe_server = MockPPPoEServer(
            mcast_addr=args.pppoe_mcast,
            mcast_port=args.pppoe_port
        )
        t_pppoe = threading.Thread(target=pppoe_server.serve_forever, daemon=True)
        t_pppoe.start()
        threads.append(t_pppoe)
        servers.append(pppoe_server)

    print("Mock ISP services are running. Press Ctrl+C to exit.")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nStopping mock services...")
        for s in servers:
            if hasattr(s, "shutdown"):
                s.shutdown()
            elif hasattr(s, "stop"):
                s.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()

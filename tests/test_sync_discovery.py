"""Tests for P2P LAN device discovery (discovery.py)."""

import json
import socket
import time

import pytest

from doctoragent.sync.discovery import (
    BROADCAST_PORT,
    HEARTBEAT_INTERVAL,
    PEER_TIMEOUT,
    DeviceDiscovery,
    PeerInfo,
    UdpDiscovery,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def discovery() -> DeviceDiscovery:
    """A DeviceDiscovery instance for testing."""
    disc = DeviceDiscovery(device_name="Test Device", port=19527, device_id="test-dev-1")
    yield disc
    disc.stop()


# ── PeerInfo ─────────────────────────────────────────────────────────────────


def test_peer_info_creation() -> None:
    """PeerInfo should hold discovery data."""
    peer = PeerInfo(
        device_id="abc",
        device_name="Laptop",
        ip="192.168.1.5",
        port=9527,
        last_seen=time.time(),
    )
    assert peer.device_id == "abc"
    assert peer.device_name == "Laptop"
    assert peer.ip == "192.168.1.5"
    assert peer.port == 9527


# ── DeviceDiscovery construction ─────────────────────────────────────────────


def test_discovery_defaults() -> None:
    """Constructing DeviceDiscovery with minimal args generates a device_id."""
    disc = DeviceDiscovery(device_name="Test")
    assert disc.device_id
    assert len(disc.device_id) > 0
    assert disc.device_name == "Test"
    assert disc.port == 9527


def test_discovery_custom_device_id() -> None:
    """Custom device_id should be preserved."""
    disc = DeviceDiscovery(device_name="Test", device_id="custom-123")
    assert disc.device_id == "custom-123"


def test_discovery_get_peers_empty(discovery: DeviceDiscovery) -> None:
    """Initially, no peers should be discovered."""
    peers = discovery.get_peers()
    assert peers == []


# ── UdpDiscovery subclass ────────────────────────────────────────────────────


def test_udp_discovery_never_tries_mdns() -> None:
    """UdpDiscovery._try_start_mdns must always return False."""
    disc = UdpDiscovery(device_name="UDP Only")
    assert disc._try_start_mdns() is False
    disc.stop()


# ── UDP broadcast discovery ──────────────────────────────────────────────────


class TestUdpDiscovery:
    """Integration-style tests for UDP broadcast discovery."""

    def test_udp_announce_and_discover(self, discovery: DeviceDiscovery) -> None:
        """A peer broadcasting via UDP should be discovered."""
        discovery.start()
        time.sleep(0.5)

        # Simulate another device broadcasting
        peer_msg = json.dumps(
            {
                "device_id": "peer-1",
                "device_name": "Laptop",
                "ip": "192.168.1.100",
                "port": 9527,
            }
        ).encode("utf-8")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.sendto(peer_msg, ("255.255.255.255", BROADCAST_PORT))
            time.sleep(0.5)
        finally:
            sock.close()

        peers = discovery.get_peers()
        peer_ids = {p["device_id"] for p in peers}
        assert "peer-1" in peer_ids

    def test_udp_skip_self(self, discovery: DeviceDiscovery) -> None:
        """A broadcast from our own device_id should be ignored."""
        discovery.start()
        time.sleep(0.3)

        self_msg = json.dumps(
            {
                "device_id": discovery.device_id,
                "device_name": discovery.device_name,
                "ip": "127.0.0.1",
                "port": discovery.port,
            }
        ).encode("utf-8")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.sendto(self_msg, ("255.255.255.255", BROADCAST_PORT))
            time.sleep(0.5)
        finally:
            sock.close()

        peers = discovery.get_peers()
        peer_ids = {p["device_id"] for p in peers}
        assert discovery.device_id not in peer_ids

    def test_udp_malformed_message(self, discovery: DeviceDiscovery) -> None:
        """Malformed UDP messages should be ignored gracefully."""
        discovery.start()
        time.sleep(0.3)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            # Send garbage
            sock.sendto(b"not-json", ("255.255.255.255", BROADCAST_PORT))
            # Send valid JSON but missing required fields
            sock.sendto(b'{"foo": "bar"}', ("255.255.255.255", BROADCAST_PORT))
            time.sleep(0.5)
        finally:
            sock.close()

        # Should not crash
        peers = discovery.get_peers()
        assert isinstance(peers, list)

    def test_multiple_peers(self, discovery: DeviceDiscovery) -> None:
        """Multiple different peers should all be discovered."""
        discovery.start()
        time.sleep(0.3)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            for i in range(3):
                msg = json.dumps(
                    {
                        "device_id": f"peer-{i}",
                        "device_name": f"Device {i}",
                        "ip": f"192.168.1.1{i}",
                        "port": 9527,
                    }
                ).encode("utf-8")
                sock.sendto(msg, ("255.255.255.255", BROADCAST_PORT))
            time.sleep(0.5)
        finally:
            sock.close()

        peers = discovery.get_peers()
        assert len(peers) == 3


# ── Peer expiry ──────────────────────────────────────────────────────────────


class TestPeerExpiry:
    """Tests for peer timeout and expiry."""

    def test_peer_not_expired_within_window(self, discovery: DeviceDiscovery) -> None:
        """A recently seen peer should not be expired."""
        discovery.start()
        time.sleep(0.3)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            msg = json.dumps(
                {
                    "device_id": "peer-expire",
                    "device_name": "Temp Device",
                    "ip": "192.168.1.50",
                    "port": 9527,
                }
            ).encode("utf-8")
            sock.sendto(msg, ("255.255.255.255", BROADCAST_PORT))
            time.sleep(1.0)
        finally:
            sock.close()

        peers = discovery.get_peers()
        assert any(p["device_id"] == "peer-expire" for p in peers)


# ── Stop semantics ───────────────────────────────────────────────────────────


def test_stop_then_get_peers(discovery: DeviceDiscovery) -> None:
    """After stop(), get_peers should still work (return current state)."""
    discovery.start()
    time.sleep(0.3)
    discovery.stop()
    peers = discovery.get_peers()
    assert isinstance(peers, list)


def test_double_start(discovery: DeviceDiscovery) -> None:
    """Starting twice should be a no-op."""
    discovery.start()
    discovery.start()
    assert discovery._running
    discovery.stop()


def test_double_stop(discovery: DeviceDiscovery) -> None:
    """Stopping twice should be safe."""
    discovery.start()
    discovery.stop()
    discovery.stop()
    assert not discovery._running


# ── _get_local_ip ────────────────────────────────────────────────────────────


def test_get_local_ip() -> None:
    """_get_local_ip should return a valid IP or loopback."""
    ip = DeviceDiscovery._get_local_ip()
    assert ip
    assert isinstance(ip, str)
    # Should be a valid IPv4 address
    parts = ip.split(".")
    assert len(parts) == 4


# ── Constants ────────────────────────────────────────────────────────────────


def test_constants() -> None:
    """Verify key protocol constants."""
    assert HEARTBEAT_INTERVAL == 30
    assert PEER_TIMEOUT == 120
    assert BROADCAST_PORT == 9528

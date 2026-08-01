"""P2P LAN device discovery for DoctorAgent multi-device sync.

Provides two discovery backends:
- **mDNS/DNS-SD** via ``zeroconf`` (preferred when available).
- **UDP broadcast** as a fallback for environments where mDNS is
  unavailable (e.g. Docker containers, restrictive networks).

Both backends advertise the same service type ``_doctoragent._tcp.local.``
and share a common peer data format.
"""

import ipaddress
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

# ── Peer representation ──────────────────────────────────────────────────────


@dataclass
class PeerInfo:
    """Information about a discovered peer device."""

    device_id: str
    device_name: str
    ip: str
    port: int
    last_seen: float = 0.0


# ── Constants ────────────────────────────────────────────────────────────────

SERVICE_TYPE = "_doctoragent._tcp.local."
BROADCAST_PORT = 9528
HEARTBEAT_INTERVAL = 30  # seconds
PEER_TIMEOUT = 120  # seconds – mark peer as offline after no heartbeat


def _is_private_ip(ip_str: str) -> bool:
    """Return ``True`` if *ip_str* is a private/loopback IPv4 or IPv6 address.

    Used to reject discovery announcements arriving from public (e.g.
    Internet-routable) source addresses so a remote attacker cannot inject
    peers into the local mesh.
    """
    try:
        return ipaddress.ip_address(ip_str).is_private
    except ValueError:
        return False


# ── Base discovery interface ─────────────────────────────────────────────────


class DeviceDiscovery:
    """P2P LAN device discovery with mDNS and UDP-broadcast fallback.

    Parameters
    ----------
    device_name: Human-readable name for this device.
    port: TCP port on which the sync service runs.
    device_id: Unique identifier for this device.  If empty, a random
               UUID is generated.
    """

    SERVICE_TYPE: ClassVar[str] = SERVICE_TYPE
    BROADCAST_PORT: ClassVar[int] = BROADCAST_PORT

    def __init__(
        self,
        device_name: str,
        port: int = 9527,
        device_id: str = "",
        enabled: bool = True,
    ) -> None:
        import uuid

        self.device_name = device_name
        self.port = port
        self.device_id = device_id or str(uuid.uuid4())
        # Controlled by config.discovery_enabled: when disabled, start() neither
        # broadcasts nor listens. Defaults to True for backward compatibility
        # with explicit construction (e.g. tests); the application layer should
        # pass enabled=config.discovery_enabled from config.
        self._enabled = enabled
        self._peers: dict[str, PeerInfo] = {}
        self._lock = threading.Lock()
        self._running = False
        # Signalled by ``stop()`` to wake every loop currently parked in a
        # ``time.sleep`` / socket timeout so shutdown is prompt and the worker
        # threads can be joined cleanly.
        self._stop_event = threading.Event()

        # Threads
        self._heartbeat_thread: threading.Thread | None = None
        self._mdns_thread: threading.Thread | None = None
        self._udp_listen_thread: threading.Thread | None = None
        self._broadcast_thread: threading.Thread | None = None

        # Backend state
        self._mdns_available = False
        self._zeroconf_service_info: Any = None  # zeroconf.ServiceInfo
        self._zeroconf_obj: Any = None  # zeroconf.Zeroconf

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Register the service and begin discovering peers."""
        if self._running:
            return
        # When disabled, do not broadcast or listen (controlled by
        # config.discovery_enabled)
        if not self._enabled:
            logger.info("DeviceDiscovery 已禁用（discovery_enabled=False），跳过广播与监听")
            return
        self._running = True
        # Clear any leftover signal from a previous run so the new worker
        # threads actually loop.
        self._stop_event.clear()

        # Try mDNS first
        self._mdns_available = self._try_start_mdns()

        # Always start UDP fallback (works even when mDNS is active)
        self._start_udp()

        # Heartbeat for peer expiry
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="discv-heartbeat"
        )
        self._heartbeat_thread.start()

        logger.info(
            "DeviceDiscovery started (id=%s, mdns=%s, udp=%s)",
            self.device_id,
            self._mdns_available,
            True,
        )

    def stop(self) -> None:
        """Stop advertising and browsing.

        Signals all worker threads via the stop ``Event`` (waking any that are
        parked in a heartbeat sleep) and joins them so the caller can be
        confident the sockets are closed and no background work is in flight.
        """
        self._running = False
        self._stop_event.set()
        self._stop_mdns()

        # Join worker threads with a short timeout.  Daemon threads, but
        # joining guarantees the sockets are released before we return.
        for thread in (self._broadcast_thread, self._udp_listen_thread, self._heartbeat_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        # UDP listeners are daemon threads – they exit on `_running == False`

    def get_peers(self) -> list[dict[str, Any]]:
        """Return a list of currently visible peers.

        Each peer dict contains: ``device_id``, ``device_name``, ``ip``,
        ``port``, ``last_seen``.
        """
        with self._lock:
            return [
                {
                    "device_id": p.device_id,
                    "device_name": p.device_name,
                    "ip": p.ip,
                    "port": p.port,
                    "last_seen": p.last_seen,
                }
                for p in self._peers.values()
            ]

    # ------------------------------------------------------------------
    # mDNS backend (zeroconf)
    # ------------------------------------------------------------------

    def _try_start_mdns(self) -> bool:
        """Attempt to start mDNS/DNS-SD via zeroconf. Returns True on success."""
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            logger.debug("zeroconf not available – using UDP fallback only")
            return False

        try:
            local_ip = self._get_local_ip()

            self._zeroconf_obj = Zeroconf()

            properties: dict[str, str] = {
                "device_id": self.device_id.encode("utf-8").hex(),
                "device_name": self.device_name.encode("utf-8").hex(),
                "protocol": "doctoragent-sync-v1",
            }

            self._zeroconf_service_info = ServiceInfo(
                type_=SERVICE_TYPE,
                name=f"{self.device_name}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(local_ip)],
                port=self.port,
                properties=properties,
            )

            zc = self._zeroconf_obj
            info = self._zeroconf_service_info
            zc.register_service(info)

            # Start browser in a thread.
            def _browse() -> None:
                from zeroconf import ServiceBrowser

                class _Listener:
                    @staticmethod
                    def add_service(zc_obj: Any, svc_type: str, name: str) -> None:  # noqa: ARG004
                        pass

                    @staticmethod
                    def remove_service(zc_obj: Any, svc_type: str, name: str) -> None:  # noqa: ARG004
                        info_obj = zc_obj.get_service_info(svc_type, name)
                        if info_obj is None:
                            return
                        self._remove_peer_from_mdns(info_obj)

                    @staticmethod
                    def update_service(zc_obj: Any, svc_type: str, name: str) -> None:  # noqa: ARG004
                        info_obj = zc_obj.get_service_info(svc_type, name)
                        if info_obj is None:
                            return
                        self._add_peer_from_mdns(info_obj)

                self._zc_listener = _Listener()
                self._zc_browser = ServiceBrowser(zc, SERVICE_TYPE, self._zc_listener)

            self._mdns_thread = threading.Thread(target=_browse, daemon=True, name="discv-mdns")
            self._mdns_thread.start()

            logger.info("mDNS discovery started on %s:%d", local_ip, self.port)
            return True

        except Exception:
            logger.debug("Failed to start mDNS discovery", exc_info=True)
            self._stop_mdns()
            return False

    def _stop_mdns(self) -> None:
        """Unregister and close zeroconf resources."""
        try:
            if self._zeroconf_obj is not None:
                self._zeroconf_obj.unregister_service(self._zeroconf_service_info)
                self._zeroconf_obj.close()
        except Exception:
            logger.debug("Error during mDNS cleanup", exc_info=True)
        finally:
            self._zeroconf_obj = None
            self._zeroconf_service_info = None

    def _add_peer_from_mdns(self, info: Any) -> None:
        """Add or update a peer discovered via mDNS."""
        try:
            props = info.properties
            device_id_hex = props.get(b"device_id", b"").decode("utf-8")
            device_id = bytes.fromhex(device_id_hex).decode("utf-8")
            device_name_hex = props.get(b"device_name", b"unknown").decode("utf-8")
            device_name = bytes.fromhex(device_name_hex).decode("utf-8")

            addr = socket.inet_ntoa(info.addresses[0])
            port = info.port

            if device_id == self.device_id:
                return  # skip self

            with self._lock:
                self._peers[device_id] = PeerInfo(
                    device_id=device_id,
                    device_name=device_name,
                    ip=addr,
                    port=port,
                    last_seen=time.time(),
                )
        except Exception:
            logger.debug("Failed to parse mDNS peer info", exc_info=True)

    def _remove_peer_from_mdns(self, info: Any) -> None:
        """Remove a peer that left mDNS."""
        try:
            props = info.properties
            device_id_hex = props.get(b"device_id", b"").decode("utf-8")
            device_id = bytes.fromhex(device_id_hex).decode("utf-8")
            with self._lock:
                self._peers.pop(device_id, None)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UDP broadcast fallback
    # ------------------------------------------------------------------

    def _start_udp(self) -> None:
        """Start UDP broadcast advertiser and listener threads."""
        self._broadcast_thread = threading.Thread(
            target=self._broadcast_presence,
            daemon=True,
            name="discv-udp-bcast",
        )
        self._broadcast_thread.start()

        self._udp_listen_thread = threading.Thread(
            target=self._listen_for_peers,
            daemon=True,
            name="discv-udp-listen",
        )
        self._udp_listen_thread.start()

    def _broadcast_presence(self) -> None:
        """Periodically broadcast our presence via UDP."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        local_ip = self._get_local_ip()
        msg = json.dumps(
            {
                "device_id": self.device_id,
                "device_name": self.device_name,
                "ip": local_ip,
                "port": self.port,
            }
        ).encode("utf-8")

        try:
            while not self._stop_event.is_set():
                try:
                    sock.sendto(msg, ("255.255.255.255", BROADCAST_PORT))
                except OSError:
                    logger.debug("UDP broadcast send failed", exc_info=True)
                # ``Event.wait`` returns True if the event is set during the
                # sleep, letting ``stop()`` interrupt a 30s heartbeat at once.
                if self._stop_event.wait(HEARTBEAT_INTERVAL):
                    break
        finally:
            sock.close()

    def _listen_for_peers(self) -> None:
        """Listen for UDP broadcast announcements from other devices."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", BROADCAST_PORT))
        except OSError:
            logger.warning("Could not bind UDP listen port %d", BROADCAST_PORT)
            sock.close()
            return

        sock.settimeout(1.0)

        try:
            while not self._stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(4096)
                    self._handle_udp_message(data, addr[0])
                except TimeoutError:
                    continue
                except OSError:
                    if not self._stop_event.is_set():
                        logger.debug("UDP listen error", exc_info=True)
                    break
        finally:
            sock.close()

    def _handle_udp_message(self, data: bytes, source_ip: str) -> None:
        """Parse and store a UDP peer announcement.

        Only the *actual* datagram source address is trusted for the peer's
        IP; any ``ip`` field in the JSON body is ignored so a malicious peer
        cannot impersonate another device or redirect sync traffic to a
        victim host.  Announcements from non-private (public) source
        addresses are rejected outright.
        """
        try:
            obj = json.loads(data.decode("utf-8"))
            device_id = obj["device_id"]
            if device_id == self.device_id:
                return  # skip self
            # Reject announcements from public/Internet-routable sources so
            # the local mesh cannot be polluted by a remote attacker.
            if not _is_private_ip(source_ip):
                logger.debug("Ignoring discovery message from non-private source %s", source_ip)
                return
            port = obj["port"]
            with self._lock:
                self._peers[device_id] = PeerInfo(
                    device_id=device_id,
                    device_name=obj["device_name"],
                    ip=source_ip,
                    port=port,
                    last_seen=time.time(),
                )
        except (json.JSONDecodeError, KeyError):
            logger.debug("Malformed UDP discovery message from %s", source_ip)

    # ------------------------------------------------------------------
    # Heartbeat / peer expiry
    # ------------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        """Periodically prune peers that haven't been seen recently."""
        # ``Event.wait`` doubles as an interruptible sleep: it returns True as
        # soon as ``stop()`` sets the event, exiting the loop promptly.
        while not self._stop_event.wait(HEARTBEAT_INTERVAL):
            now = time.time()
            with self._lock:
                expired = [
                    did for did, p in self._peers.items() if now - p.last_seen > PEER_TIMEOUT
                ]
                for did in expired:
                    logger.info("Peer %s expired (no heartbeat for %.0fs)", did, PEER_TIMEOUT)
                    del self._peers[did]

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _get_local_ip() -> str:
        """Best-effort determination of the local network IP."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"


# ── UDP-only discovery (for explicit fallback) ───────────────────────────────


class UdpDiscovery(DeviceDiscovery):
    """UDP broadcast discovery only – skips mDNS entirely.

    Useful in environments where mDNS is known to be unavailable and
    you want to avoid the import attempt overhead.
    """

    def _try_start_mdns(self) -> bool:
        """UDP-only: never try mDNS."""
        return False

"""Tiny one-way UDP probe for measuring FORWARD-direction outage.

Unlike ping (round-trip), this streams UDP src->dst only, so it isolates the
forward fast-failover path -- the bidirectional ping masks it behind the reverse
path's controller reroute.  The receiver records arrival timestamps; the largest
inter-arrival gap is the real one-way outage (accurate regardless of send jitter).

  receiver:  python3 udp_probe.py recv <port> <count> <timeout_s>
             -> prints one arrival epoch time per line
  sender:    python3 udp_probe.py send <dst_ip> <port> <count> <interval_s>
"""
import socket
import sys
import time


def recv(port, count, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", port))
    s.settimeout(timeout)
    times = []
    while len(times) < count:
        try:
            s.recvfrom(64)
        except socket.timeout:
            break
        times.append(time.time())
    sys.stdout.write("\n".join("%.6f" % t for t in times))


def send(ip, port, count, interval):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for seq in range(count):
        s.sendto(seq.to_bytes(8, "big"), (ip, port))
        time.sleep(interval)


def main():
    mode = sys.argv[1]
    if mode == "recv":
        recv(int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))
    elif mode == "send":
        send(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5]))


if __name__ == "__main__":
    main()

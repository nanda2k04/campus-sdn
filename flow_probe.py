"""Timestamped one-way UDP probe for the multi-flow failover benchmark.

Each datagram carries an 8-byte sequence number and an 8-byte send timestamp.
Because Mininet host namespaces share the machine's clock, the receiver can
compute a true ONE-WAY latency (arrival - send), not just a round-trip time, and
can tell exactly which sequence numbers were lost and how large the outage gap
was.

  receiver:  python3 flow_probe.py recv <port> <count> <timeout_s>
             -> prints "<seq> <recv_epoch> <send_epoch>" per received packet
  sender:    python3 flow_probe.py send <dst_ip> <port> <count> <interval_s>
"""
import socket
import struct
import sys
import time

HDR = struct.Struct("!Qd")   # seq (uint64), send timestamp (double)


def recv(port, count, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", port))
    s.settimeout(timeout)
    rows = []
    while len(rows) < count:
        try:
            data, _ = s.recvfrom(64)
        except socket.timeout:
            break
        rt = time.time()
        if len(data) >= HDR.size:
            seq, st = HDR.unpack(data[:HDR.size])
            rows.append("%d %.6f %.6f" % (seq, rt, st))
    sys.stdout.write("\n".join(rows))
    sys.stdout.flush()


def send(ip, port, count, interval):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for seq in range(count):
        s.sendto(HDR.pack(seq, time.time()), (ip, port))
        time.sleep(interval)


def main():
    mode = sys.argv[1]
    if mode == "recv":
        recv(int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))
    elif mode == "send":
        send(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5]))


if __name__ == "__main__":
    main()

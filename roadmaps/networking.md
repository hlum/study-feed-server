# Networking roadmap

Each `##` is a series and each numbered line is one card. Ingest takes them in order: `topics.next_item` counts across all series in this file.

## tcp-basics: How TCP actually works
1. What a packet is, and why the internet splits data into them
2. IP addresses and ports: the two halves of "where"
3. Why TCP exists when IP already delivers packets
4. The three-way handshake: SYN, SYN-ACK, ACK
5. What happens when the final ACK is lost (half-open connections)
6. Sequence numbers and why they start random
7. Retransmission and timeouts
8. The sliding window and flow control
9. Congestion control: slow start in one picture
10. Closing a connection: FIN, and why TIME_WAIT exists
11. listen() backlog and the accept queue
12. Reading `ss -tan` output like a pro

## dns: Names to numbers
1. What happens when you type a URL, up to the first DNS query
2. Recursive resolvers vs. authoritative servers
3. Record types that matter: A, AAAA, CNAME, MX, TXT
4. TTLs and why DNS changes "take time"
5. `dig +trace`: following a lookup by hand
6. DNS over HTTPS and what it hides

## tls: The lock icon
1. What TLS guarantees: confidentiality, integrity, authenticity
2. Certificates and chains of trust
3. The TLS 1.3 handshake in one round trip
4. SNI and why the hostname leaks
5. `openssl s_client`: inspect any site's certificate
6. Let's Encrypt and the ACME challenge

## tailscale-and-vpns: How your phone reaches your server
1. NAT and why home servers are hard to reach
2. WireGuard in one card
3. How Tailscale punches through NAT (DERP relays)
4. MagicDNS and the 100.x.y.z addresses

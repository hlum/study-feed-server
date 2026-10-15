# Security roadmap

Each `##` is a series and each numbered line is one card. Keep every card defensive and educational: explain how attacks work so the reader can recognize and prevent them.

## web-basics: How web apps get broken
1. The same-origin policy in one card
2. XSS: when user input becomes code
3. CSRF and why SameSite cookies stopped most of it
4. SQL injection and parameterized queries
5. Path traversal: ../ and how to resolve paths safely
6. SSRF: making the server fetch for you
7. Insecure deserialization, explained with pickle
8. The OWASP Top 10 as a mental checklist

## auth: Who are you?
1. Hashing vs. encryption for passwords
2. Why bcrypt and argon2 are slow on purpose
3. Sessions vs. JWTs: tradeoffs
4. OAuth 2.0: the authorization code flow
5. Passkeys and WebAuthn
6. MFA fatigue attacks and number matching

## linux-hardening: Locking down a box
1. Users, groups and permission bits
2. sudo and least privilege
3. SSH keys, and disabling password login
4. Reading auth logs for brute-force attempts
5. Firewalls: ufw and nftables basics
6. Keeping secrets out of shell history and env dumps

## crypto-intuition: The math you need, minus the math
1. Symmetric vs. asymmetric encryption
2. Diffie-Hellman with paint colors
3. Digital signatures
4. Why you never roll your own crypto

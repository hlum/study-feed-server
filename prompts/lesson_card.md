You write the next {{count}} cards in a lesson series for a personal IT-learning feed on iPhone. The reader is a developer who wants to learn something real in 30-60 seconds instead of doomscrolling. Lessons are written in English.

HARD LIMITS. A card over any limit is rejected. Aim for about 80% of each limit, because it is easy to overshoot.
1. title: 80 characters or fewer (aim for about 64)
2. body: 280 characters or fewer (aim for about 220)
3. cliffhanger: 70 characters or fewer (aim for about 55)
4. slides: same title and body limits. Slides stay empty unless card_type is "poster".

Topic: {{topic}}
Series: {{series_id}}, of {{parts_total}} parts
Parts to write now, in order:
{{parts_list}}
The cliffhanger of the part before the first one, which the first card should answer: {{previous_cliffhanger}}

Write all the parts in one reply. Each part's cliffhanger must set up the next part you write, and each card after the first should open by answering the cliffhanger of the card before it. The last card's cliffhanger sets up the part after this batch.

Return only a JSON array with exactly one card per part, in order. No markdown fence, no text before or after it. Everything below describes one card.

Never invent anything: no quote, link, number, version or API you are not sure is real. If you are unsure of a detail, leave it out rather than guess. "source" is always null for lessons.

Pick the card_type:
- "poster" when this is part 1 of a series, or when the idea is big. Use 3-5 slides, each short. The last slide ends on the cliffhanger.
- "terminal" when the reader can see the idea in real command output or code. Use real, accurate output, e.g. tcpdump, ss, strace, curl -v, git, python.
- "post" otherwise.

Use a visual when it teaches faster than words:
- "steps" for any exchange between two parties (handshakes, auth flows, request/response)
- "code" with lettered notes for code
- "terminal" for commands
- "mermaid" only for structure that the others can't show

Voice:
- Plain and specific, like a sharp colleague.
- No hype words, no emoji, no em-dashes.
- The title states the insight. The body adds what the title doesn't say; never repeat the title in the body.
- Be technically exact.

The cliffhanger is one short question that the next part of the series will answer. The quiz tests understanding with 4 different, plausible options and exactly one correct answer; "answer" is the index (0-3) of the correct option. The flashcard is one atomic fact.

Example card. Copy this shape, not this content. Set "part" to each card's own part number.
{"card_type":"post","topic":"{{topic}}","series_id":"{{series_id}}","part":2,"parts_total":{{parts_total}},"title":"A port picks the program; the IP picks the machine","body":"The OS hands each incoming segment to the socket bound to its destination port, so one address can serve SSH on 22 and HTTPS on 443 at once.","source":null,"slides":[],"visual":null,"cliffhanger":"Then why does TCP need a handshake at all?","quiz":{"question":"Two programs on one host both receive TCP traffic. What tells them apart?","options":["The MAC address","The destination port","The TTL","The IP checksum"],"answer":1,"explanation":"The kernel delivers each segment to the socket bound to its destination port."},"flashcard":{"front":"What does a port number identify?","back":"A program (socket) on a host."}}

Before you answer, check every card against this list:
1. title 80 or fewer, body 280 or fewer, cliffhanger 70 or fewer characters (aim for 80% of each).
2. Slides are empty unless card_type is "poster"; a poster has 3-5.
3. Nothing is invented, and source is null.
4. The body says something the title doesn't.
5. The quiz has 4 different options, and "answer" is the index of the correct one.
6. The reply is only the JSON array, one card per part, in order.

---

DEEPER MODE (used by /cards/{id}/deeper; the server swaps in this section instead of the above):

Here is a card the reader wants to go deeper on:
<card>{{card_json}}</card>

Write at most 250 words of markdown that go one level deeper than the card. Structure:
- One short heading that states the deeper insight.
- 1-2 tight paragraphs.
- Optionally one code block of 4-8 lines. If you include it, follow it with notes labeled a, b, c that point at specific lines.

If the card's "lang" is "ja", write the whole thing in Japanese: plain, polite Japanese (です・ます調) with no hype, keeping technical terms people actually use in English as they are (API, async/await, SwiftUI). The limit is then about 600 Japanese characters instead of 250 words. Code stays as code.

No images, no tables, no emoji, no em-dashes. Return only the markdown.

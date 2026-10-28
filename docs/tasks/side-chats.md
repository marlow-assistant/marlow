# Side chats — throwaway threads that stay out of the main conversation

**Status: PARKED 2026-10-05.** Nothing built. Raised by Mike alongside
`ideas-panel.md`.

## The symptom

Mike alt-tabs to another chat app for throwaway questions ("tell me about
Montreal") because in Marlow they would pile onto the one main conversation.
Each one is a reason to leave Marlow. His wishlist on the box already records
the same thing from the other end: a day of tourist chat in Quebec City
(2026-09-30) needed almost no continuity, but it grew the main conversation
through several auto-compactions. That entry asks for "compact now". Side
chats answer it better: the throwaway chat never lands in the main thread.

**Why it beats the other app, not just matches it:** a side chat still has
Marlow's identity and memory. It knows he is in the van, which trip is next and
what he likes. And Marlow can read it afterwards. Asking about Montreal is
something consolidation should notice.

## Shape

- **The main conversation is unchanged.** Cron jobs, schedules and the morning
  brief always go to main (`one-conversation.md`). A side chat is a separate
  CLI session with no history from main. Identity is injected on every call
  already, so a fresh session is still Marlow. No fork is needed.
- **Server-minted thread ids.** The phone names a side chat with an opaque
  thread id that the server maps to a session. It never sends a session id:
  That hardening deliberately took the only client-supplied string off `--resume`, and
  this must not put it back.
- **The one work queue still serializes everything.** A side-chat turn waits
  behind the morning brief like any other turn. That is acceptable, and it
  is a hard constraint anyway.
- **Same permission path.** Approval cards appear in the side chat that
  raised them. Nothing is relaxed for being "throwaway".
- **Transcripts in the server transcript store** (`server-transcript.md`), with
  the same 30-day retention. Side chats age out with it, or Mike deletes one.
- **Consolidation reads the day's side chats** alongside main, and files
  anything durable ("planning a Montreal stop"). This is where the value
  compounds. It is also the reason "throwaway" means *out of the way*, not
  *private*. If a "don't remember this one" switch is ever wanted, that is a
  felt symptom to wait for, not something to build up front.
- **Phone UI:** main stays pinned as the default. Side chats are a list
  reached from a speech-bubble icon on the chat screen (Mike, 2026-10-05),
  with "new side chat" one tap away. The slot first planned for it, the
  model/effort chip's, went to the ideas lightbulb (2026-10-12), so it needs
  another home. Each is
  titled by its first message, trimmed on the phone.
- **Also the session for dreaming and other background jobs** that should not
  grow main (`ideas-panel.md`).
- **Ideas link:** "Do it" on an idea (`ideas-panel.md`) could open a side chat
  instead of landing in main.

## Open before building

- **Session growth.** Idle compaction keys off the main conversation only. A
  side chat is short by nature, so leave it uncompacted until one shows a
  problem.
- **Cache interaction.** A side-chat turn between two main turns does not
  share main's history prefix. The identity block at the front is shared, so
  the cost is probably small. Measure it with the usage panel rather than
  argue it.
- **Usage visibility.** Should side chats show as their own line under
  "Marlow's turns"? A turn-source value (`chat` vs `side`) would make that
  free.
- **Web client parity** (`portable-web-client.md`).
- When built, update `identity/CLAUDE.md` and the live box copy so Marlow
  knows side chats exist and that consolidation reads them.
